"""
Signal classifier — regex fast-path first, Claude AI fallback.
Reads any Telegram message and returns structured trading intent.
"""

import json
import re
import anthropic
import config
from logger import get_logger

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Symbol map — expands aliases to standard pair names
# ---------------------------------------------------------------------------
_SYMBOL_MAP = {
    "gold": "XAUUSD", "xauusd": "XAUUSD",
    "btc": "BTCUSD",  "bitcoin": "BTCUSD", "btcusd": "BTCUSD",
    "eth": "ETHUSD",  "ethereum": "ETHUSD", "ethusd": "ETHUSD",
    "eurusd": "EURUSD", "gbpusd": "GBPUSD",
    "usdjpy": "USDJPY", "us30": "US30", "nas100": "NAS100",
}

def _extract_symbol(text: str):
    t = text.lower()
    for alias, sym in _SYMBOL_MAP.items():
        if re.search(r'\b' + alias + r'\b', t):
            return sym
    return None

# ---------------------------------------------------------------------------
# Regex fast-path classifier
# Returns a result dict, or None if the message is too complex for regex.
# ---------------------------------------------------------------------------
_RE_CLOSE = re.compile(
    r'\b(close|cancel|exit|cancelled|canceled)\b',
    re.IGNORECASE
)
_RE_BE = re.compile(
    r'(?i)(\bBE\b|\bb/e\b|\bbreakeven\b|\bbreak\s+even\b'
    r'|risk\s*free|go\s*risk\s*free|make\s+it\s+risk\s+free)',
)
_RE_DIRECTION = re.compile(r'\b(buy|sell)\b', re.IGNORECASE)
_RE_SL = re.compile(
    r'(?:🛑|🔴|sl|stop\s*loss)\s*:?\s*(\d+(?:\.\d+)?)',
    re.IGNORECASE
)
_RE_TP = re.compile(
    r'(?:✅|tp\d*|take\s*profit)\s*:?\s*(\d+(?:\.\d+)?|open)',
    re.IGNORECASE
)
_RE_TP_STEP = re.compile(
    r'(?:tp\s+every|take\s+(?:profit|tp)\s+every)\s+(\d+)\s*pips?',
    re.IGNORECASE
)
_RE_LOOKING = re.compile(
    r'\b(looking|watching|wait|waiting|be\s+active)\b',
    re.IGNORECASE
)
_RE_TP_HIT = re.compile(
    r'\btp\s*(hit|done|reached|successfully)\b',
    re.IGNORECASE
)


def _regex_classify(text: str):
    """
    Fast regex classifier. Returns result dict or None to fall back to Claude.
    """
    t_clean = text.strip()
    t_lower = t_clean.lower()

    # --- Ignore: TP hit notifications ---
    if _RE_TP_HIT.search(t_clean):
        return {"type": "ignore", "direction": None, "symbol": None,
                "sl": None, "tps": None, "tp_step_pips": None, "is_complete": False}

    # --- Ignore: looking/watching messages ---
    if _RE_LOOKING.search(t_clean) and not _RE_DIRECTION.search(t_clean):
        return {"type": "ignore", "direction": None, "symbol": None,
                "sl": None, "tps": None, "tp_step_pips": None, "is_complete": False}

    # --- Close: must check before BE (close with BE = close) ---
    if _RE_CLOSE.search(t_clean):
        return {"type": "close", "direction": None, "symbol": None,
                "sl": None, "tps": None, "tp_step_pips": None, "is_complete": False}

    # --- Breakeven ---
    if _RE_BE.search(t_clean):
        return {"type": "breakeven", "direction": None, "symbol": None,
                "sl": None, "tps": None, "tp_step_pips": None, "is_complete": False}

    # --- New signal: needs direction ---
    dir_match = _RE_DIRECTION.search(t_clean)
    if not dir_match:
        return None  # can't determine — fall back to Claude

    direction = dir_match.group(1).lower()
    symbol = _extract_symbol(t_clean)

    # Parse SL
    sl_match = _RE_SL.search(t_clean)
    sl = float(sl_match.group(1)) if sl_match else None

    # Parse TPs
    tp_step_match = _RE_TP_STEP.search(t_clean)
    tp_step_pips = int(tp_step_match.group(1)) if tp_step_match else None

    tp_matches = _RE_TP.findall(t_clean)
    tps = None
    if tp_matches:
        tps = []
        for v in tp_matches:
            if v.lower() == "open":
                tps.append(None)
            else:
                tps.append(float(v))

    is_complete = bool(sl is not None and tps and any(t is not None for t in tps))

    return {
        "type": "new_signal",
        "direction": direction,
        "symbol": symbol,
        "sl": sl,
        "tps": tps,
        "tp_step_pips": tp_step_pips,
        "is_complete": is_complete,
    }

_client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

SYSTEM_PROMPT = """You are a trading signal parser for a Telegram copy-trading channel.
Analyze each message and return ONLY a JSON object — no explanation, no markdown.

Message types:
- "new_signal"    : A confirmed trade to open RIGHT NOW. Includes:
    * Full signals with SL/TP lines — any header like "RiskY traDE ☠️", "HIGH risk traDE ☠️", "RISKY TRADE ☠️" etc.
    * Direction indicators: 👉🏾 👉🏼 👉 🫰 before BUY/SELL, or just "BUY"/"SELL" anywhere in the line.
    * Short pre-entry messages with no SL/TPs: "Sell gold", "Buy gold", "Sell again", "Buy now again", "Sell again gold", "Buy now", "Sell now", "Buy BTC", "Sell BTC" etc.
- "signal_update" : Follow-up providing actual SL/TP numbers for a previous incomplete signal.
- "breakeven"     : Move stop loss to entry price. e.g. "Move SL to breakeven", "move stop loss to break even", "BE", "B/E", "be", "b/e", "BE now", "move to BE", "SL to BE", "breakeven now", "Go risk free", "Go risk-free", "Risk free", "risk free", "risk free now", "make it risk free". ANY message containing "BE" or "B/E" in any case/combination (be, Be, BE, b/e, B/E, bE) that means move SL to entry — treat as breakeven. This includes single-word messages like just "Be" or "be" or "BE". ONLY when the instruction is to MOVE the stop loss — NOT when it says to close.
- "close"         : Close/cancel all open trades. e.g. "Cancel the trade", "Close all", "Close trade", "Cancel signal", "Exit now", "Close positions", "I don't like it close this trade", "close with breakeven", "close at breakeven", "Not good anymore close with breakeven", "Close the rest". IMPORTANT: any message that says to close/cancel — even if it mentions breakeven — is a close, NOT a breakeven. "Close with breakeven" = close immediately.
- "ignore"        : Everything else — including:
    * "Looking buys/sells on X" — watching, not yet entering (ALWAYS ignore)
    * "Wait for my instructions", "Looking for entry", "Be active and wait" — ALWAYS ignore
    * "TP hit", "Our first TP hit", "TP successfully hit" — status notifications
    * General announcements, copy trader check messages, disclaimers
    * Voice/media messages, emojis-only, unrelated chat

JSON format:
{
  "type": "new_signal" | "signal_update" | "breakeven" | "close" | "ignore",
  "direction": "buy" | "sell" | null,
  "symbol": "XAUUSD" | null,
  "sl": 1234.5 | null,
  "tps": [1240.0, 1245.0, null] | null,
  "tp_step_pips": 100 | null,
  "is_complete": true | false
}

Rules:
- is_complete = true ONLY when type=new_signal AND sl is a real number AND tps has at least one real number.
- "TP open" in the message means null in the tps array (trade runs freely with no TP).
- SL/TP lines with labels but NO numbers (e.g. "🔴 SL" or "🛑 SL" with no value) → sl = null, is_complete = false.
- SL indicators: 🔴 🛑 or the word "SL" — all mean stop loss.
- symbol: extract the base trading pair (e.g. XAUUSD, BTCUSD, EURUSD). Strip broker suffixes like m, micro. "gold" or "GOLD" always maps to XAUUSD.
- Numbers without decimals are valid (e.g. "SL 5340" → sl: 5340.0).
- Entry price in signal (e.g. "BUY XAUUSD at 2917-2918", "SELL XAUUSD @ 4420", "BUY GOLD entrY 2917-2918") — IGNORE the entry price, it is not used. Use market price.
- Annotations like "(swing)", "( swing )" after a TP number — ignore them, extract only the number.
- "TP every N pips" or "Take TP every N pips": set tp_step_pips = N (integer). Only include the explicitly listed TP numbers in tps (not the generated ones).
- If all TPs say "every N pips" with no explicit number, set tps = null and tp_step_pips = N.
- CRITICAL: "Looking buys on gold" or "Looking sells on X" = ALWAYS type "ignore". These are NOT trade signals.
"""


def classify(text: str) -> dict:
    """
    Classify a Telegram message and extract trading data.
    Tries regex fast-path first; falls back to Claude AI for complex messages.
    """
    # Fast path — no API call needed
    fast = _regex_classify(text)
    if fast is not None:
        log.debug("Regex classified → %s", fast)
        return fast

    # Slow path — Claude API
    log.debug("Falling back to Claude for: %s", text[:80])
    try:
        resp = _client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": text}],
        )
        raw = resp.content[0].text.strip()
        # Strip markdown code block if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
        result = json.loads(raw)
        log.debug("Classified message → %s", result)
        return result
    except json.JSONDecodeError as e:
        log.error("Classifier returned invalid JSON: %s | raw=%s", e, raw)
        return {"type": "ignore"}
    except Exception as e:
        log.error("Classifier error: %s", e)
        return {"type": "ignore"}
