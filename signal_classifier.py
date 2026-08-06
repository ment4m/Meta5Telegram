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
    r'(?:🛑|🔴|sl|stop\s*loss)\s*:?\s*-?(\d+(?:\.\d+)?)',
    re.IGNORECASE
)
_RE_TP = re.compile(
    r'(?:✅|tp\d*|take\s*profit)\s*:?\s*-?(\d+(?:\.\d+)?|open)',
    re.IGNORECASE
)
_RE_TP_STEP = re.compile(
    r'(?:tp\s+every|take\s+(?:profit|tp)\s+every)\s+(\d+)\s*pips?',
    re.IGNORECASE
)
_RE_SL_MOVE = re.compile(
    r'(?:move|set|update|change)\s+(?:the\s+)?(?:sl|stop\s*loss)\s+(?:to\s+)?(\d+(?:\.\d+)?)',
    re.IGNORECASE
)
_RE_LOOKING = re.compile(
    r'\b(looking|watching|wait|waiting|be\s+active)\b',
    re.IGNORECASE
)
_RE_TP_HIT = re.compile(
    r'(?:'
    r'\btp\s*\d*\s*(?:hit|done|reached|smashed|bang+)\b'  # TP smashed, TP1 hit, TP BANGGG
    r'|\btp\s*\d+\s*hit\b'                                 # TP 1 hit (number between TP and hit)
    r'|\d+\s*pips?\s*,?\s*tp\s*\d'                        # 27 pips, TP1
    r'|\btp\d+\s*smashed\b'                                # TP2 smashed
    r')',
    re.IGNORECASE
)
# Slash-separated TP block: "TP :4268/4278/4288/4298/4308"
_RE_TP_BLOCK = re.compile(
    r'\btp\d*\s*:?\s*((?:\d+(?:\.\d+)?/)+\d+(?:\.\d+)?)',
    re.IGNORECASE
)
# Zone analysis messages (not trade signals)
_RE_ZONE = re.compile(r'\b(?:buy|sell)\s+zone\b', re.IGNORECASE)
# "BUY NOW." / "SELL NOW." — opens trades immediately with fallback SL; full signal follows shortly
_RE_NOW_ONLY = re.compile(r'^(?:buy|sell)\s+now[.!]?\s*$', re.IGNORECASE)
# Take partials / partial close notifications
_RE_PARTIAL = re.compile(r'\bpartials?\b', re.IGNORECASE)


def _regex_classify(text: str):
    """
    Fast regex classifier. Returns result dict or None to fall back to Claude.
    """
    t_clean = text.strip()

    # --- Ignore: TP hit notifications ---
    if _RE_TP_HIT.search(t_clean):
        return {"type": "ignore", "direction": None, "symbol": None,
                "sl": None, "tps": None, "tp_step_pips": None, "is_complete": False}

    # --- Ignore: zone analysis messages (BUY ZONE / SELL ZONE) ---
    if _RE_ZONE.search(t_clean):
        return {"type": "ignore", "direction": None, "symbol": None,
                "sl": None, "tps": None, "tp_step_pips": None, "is_complete": False}

    # --- Ignore: looking/watching messages (even when direction is present) ---
    if _RE_LOOKING.search(t_clean) and not _RE_SL.search(t_clean):
        return {"type": "ignore", "direction": None, "symbol": None,
                "sl": None, "tps": None, "tp_step_pips": None, "is_complete": False}

    # --- "BUY NOW." / "SELL NOW." — open immediately with fallback SL, full signal follows ---
    if _RE_NOW_ONLY.match(t_clean):
        dir_m = _RE_DIRECTION.search(t_clean)
        return {"type": "new_signal", "direction": dir_m.group(1).lower(),
                "symbol": None, "sl": None, "tps": None,
                "tp_step_pips": None, "is_complete": False}

    # --- Close: must check before BE (close with BE = close) ---
    if _RE_CLOSE.search(t_clean):
        return {"type": "close", "direction": None, "symbol": None,
                "sl": None, "tps": None, "tp_step_pips": None, "is_complete": False}

    # --- Breakeven ---
    if _RE_BE.search(t_clean):
        return {"type": "breakeven", "direction": None, "symbol": None,
                "sl": None, "tps": None, "tp_step_pips": None, "is_complete": False}

    # --- Ignore: take partials notifications (no SL = not a real signal) ---
    if _RE_PARTIAL.search(t_clean) and not _RE_SL.search(t_clean):
        return {"type": "ignore", "direction": None, "symbol": None,
                "sl": None, "tps": None, "tp_step_pips": None, "is_complete": False}

    # --- SL move to a specific price (e.g. "Move SL 4200", "Move SL to 4200") ---
    sl_move_match = _RE_SL_MOVE.search(t_clean)
    if sl_move_match:
        new_sl = float(sl_move_match.group(1))
        symbol = _extract_symbol(t_clean)
        dir_m = _RE_DIRECTION.search(t_clean)
        direction = dir_m.group(1).lower() if dir_m else None
        return {"type": "sl_move", "direction": direction, "symbol": symbol,
                "sl": new_sl, "tps": None, "tp_step_pips": None, "is_complete": False}

    # --- New signal: needs direction ---
    dir_match = _RE_DIRECTION.search(t_clean)
    if not dir_match:
        return None  # can't determine — fall back to Claude

    direction = dir_match.group(1).lower()
    symbol = _extract_symbol(t_clean)

    # Parse SL
    sl_match = _RE_SL.search(t_clean)
    sl = float(sl_match.group(1)) if sl_match else None

    # Parse TPs — slash block first (e.g. "TP :4268/4278/4288/4298/4308"),
    # then fall back to individual TP matches
    tp_step_pips = None
    tp_block = _RE_TP_BLOCK.search(t_clean)
    if tp_block:
        tps = [float(x) for x in tp_block.group(1).split('/') if x.strip()]
    else:
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
    * CallistoFx format: "🟢XAUUSD🟢\nBUY RANGE: 4249-4255.5\nSL 4245\nTP :4268/4278/4288/4298/4308"
      → direction=buy, symbol=XAUUSD, sl=4245, tps=[4268,4278,4288,4298,4308], is_complete=true
    * Direction indicators: 👉🏾 👉🏼 👉 🫰 before BUY/SELL, or just "BUY"/"SELL" anywhere in the line.
    * Short pre-entry messages with no SL/TPs: "Sell gold", "Buy gold", "Sell again", "Buy now again", "Buy BTC" etc.
- "signal_update" : Follow-up providing actual SL/TP numbers for a previous incomplete signal.
- "sl_move"       : Explicit instruction to move the SL to a specific price. e.g. "Move SL to 4200", "Move SL 4200", "Move stop loss to 3980", "Set SL to 4150". Must have a price number. If it says "Move SL to BE" that is breakeven, not sl_move.
- "breakeven"     : Move stop loss to entry price. e.g. "Move SL to breakeven", "move stop loss to break even", "BE", "B/E", "be", "b/e", "BE now", "move to BE", "SL to BE", "breakeven now", "Go risk free", "Go risk-free", "Risk free", "risk free", "risk free now", "make it risk free", "Set SL TO BE". ANY message containing "BE" or "B/E" in any case/combination that means move SL to entry — treat as breakeven. ONLY when the instruction is to MOVE the stop loss — NOT when it says to close.
- "close"         : Close/cancel all open trades. e.g. "Cancel the trade", "Close all", "Close trade", "Cancel signal", "Exit now", "Close positions", "CLOSE ALL OPEN POSITIONS NOW", "POSITION CLOSED", "close with breakeven", "close at breakeven". IMPORTANT: any message that says to close/cancel — even if it mentions breakeven — is a close, NOT a breakeven.
- "ignore"        : Everything else — including:
    * "BUY NOW." or "SELL NOW." alone (standalone trigger, actual signal comes in the next message)
    * "BUY ZONE: ...", "SELL ZONE: ..." — analysis zones, NOT trade signals
    * "BUY ZONE TAPPED!", "SELL ZONE TAPPED!", "SELL ZONE UPDATE" — zone update, NOT a trade
    * "Looking buys/sells on X", "Looking for BUYS", "Looking for CONFIRMATIONS" — watching, not entering
    * "Wait for my instructions", "Looking for entry", "Be active and wait", "Wait for the BUY NOW TEXT"
    * "TP hit", "TP 1 hit", "TP2 SMASHED", "27 pips TP 1", "107 Pips TP 4 BANGGG", "TP successfully hit" — TP status notifications
    * "Take partials", "Take some partials", "Take 10% partial" — partial close instructions
    * "N pips" standalone result updates — e.g. "🟢231 Pips", "🟢277 Pips"
    * General announcements, copy trader check messages, disclaimers, livestream links
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
- CallistoFx slash TPs: "TP :4268/4278/4288/4298/4308" → tps=[4268.0, 4278.0, 4288.0, 4298.0, 4308.0]
- "BUY RANGE: 4249-4255.5" or "SELL RANGE: ..." — ignore the range, use market entry.
- "TP open" in the message means null in the tps array (trade runs freely with no TP).
- SL/TP lines with labels but NO numbers (e.g. "🔴 SL" or "🛑 SL" with no value) → sl = null, is_complete = false.
- SL indicators: 🔴 🛑 or the word "SL" — all mean stop loss. "SL 4245" with no colon is valid.
- symbol: extract the base trading pair (e.g. XAUUSD, BTCUSD, EURUSD). Strip broker suffixes like m, micro. "gold" or "GOLD" always maps to XAUUSD.
- Numbers without decimals are valid (e.g. "SL 5340" → sl: 5340.0).
- Entry price in signal (e.g. "BUY XAUUSD at 2917-2918", "SELL XAUUSD @ 4420", "BUY RANGE: 4249-4255.5") — IGNORE the entry price, it is not used. Use market price.
- Annotations like "(swing)", "( swing )" after a TP number — ignore them, extract only the number.
- "TP every N pips" or "Take TP every N pips": set tp_step_pips = N (integer). Only include the explicitly listed TP numbers in tps (not the generated ones).
- If all TPs say "every N pips" with no explicit number, set tps = null and tp_step_pips = N.
- CRITICAL: "Looking buys on gold", "Looking for BUYS", "Looking for CONFIRMATIONS" = ALWAYS type "ignore". These are NOT trade signals.
- CRITICAL: "BUY ZONE", "SELL ZONE", "BUY ZONE TAPPED", "SELL ZONE UPDATE" = ALWAYS type "ignore".
- CRITICAL: "BUY NOW." or "SELL NOW." as the entire message = ALWAYS type "ignore".
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
