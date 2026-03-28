"""
Signal classifier using Claude AI.
Reads any Telegram message and returns structured trading intent.
"""

import json
import anthropic
import config
from logger import get_logger

log = get_logger(__name__)

_client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

SYSTEM_PROMPT = """You are a trading signal parser for a Telegram copy-trading channel.
Analyze each message and return ONLY a JSON object — no explanation, no markdown.

Message types:
- "new_signal"    : A confirmed trade to open RIGHT NOW. Must explicitly say "buy X now" or "sell X now" with a symbol.
- "signal_update" : Follow-up providing actual SL/TP numbers for a previous incomplete signal.
- "breakeven"     : Move stop loss to entry price. e.g. "Move SL to breakeven", "move stop loss to break even".
- "ignore"        : Everything else — including:
    * "Looking buys/sells on X" — market analysis or preview, NOT a trade (ALWAYS ignore)
    * "TP hit", "Our first TP hit", "TP successfully hit" — status notifications
    * General announcements, copy trader check messages, disclaimers
    * Voice/media messages, emojis-only, unrelated chat

JSON format:
{
  "type": "new_signal" | "signal_update" | "breakeven" | "ignore",
  "direction": "buy" | "sell" | null,
  "symbol": "XAUUSD" | null,
  "sl": 1234.5 | null,
  "tps": [1240.0, 1245.0, null] | null,
  "is_complete": true | false
}

Rules:
- is_complete = true ONLY when type=new_signal AND sl is a real number AND tps has at least one real number.
- "TP open" in the message means null in the tps array (trade runs freely with no TP).
- SL/TP lines with labels but NO numbers (e.g. "🔴 SL" with no value) → sl = null, is_complete = false.
- symbol: extract the base trading pair (e.g. XAUUSD, BTCUSD, EURUSD). Strip broker suffixes like m, micro.
- Numbers without decimals are valid (e.g. "SL 5340" → sl: 5340.0).
- CRITICAL: "Looking buys on gold" or "Looking sells on X" = ALWAYS type "ignore". These are NOT trade signals.
"""


def classify(text: str) -> dict:
    """
    Classify a Telegram message and extract trading data.

    Returns dict with keys:
        type        : "new_signal" | "signal_update" | "breakeven" | "ignore"
        direction   : "buy" | "sell" | None
        symbol      : e.g. "XAUUSD" | None
        sl          : float | None
        tps         : list[float|None] | None
        is_complete : bool
    """
    try:
        resp = _client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
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
