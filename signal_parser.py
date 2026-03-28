import re


def parse_signal(text: str) -> dict | None:
    """
    Parse a Telegram trading signal message.

    Expected format:
        👉 sell/buy SYMBOL now
        🔴 SL XXXX
        ✅ TP XXXX
        ✅ TP XXXX
        ...
        ✅ TP open

    Returns a dict with keys:
        direction  : 'buy' or 'sell'
        symbol     : e.g. 'XAUUSD'
        sl         : float stop loss price
        entry      : float entry price or None (None = market order)
        tps        : list of floats/None  (None = no TP, let it run)
        num_trades : int = len(tps)
    Returns None if message is not a recognisable signal.
    """
    raw = text

    # ── 1. Direction + symbol ────────────────────────────────────────────────
    dir_sym = re.search(
        r"(buy|sell)\s+([a-zA-Z]{3,10})",
        raw,
        re.IGNORECASE,
    )
    if not dir_sym:
        return None

    direction = dir_sym.group(1).lower()
    symbol = dir_sym.group(2)  # preserve original case (e.g. XAUUSDm)

    # ── 2. Optional entry price  (e.g. "entry: 1920.50" or "@ 1920.50") ─────
    entry = None
    entry_match = re.search(
        r"(?:entry|@)\s*:?\s*([\d]{2,6}(?:\.\d+)?)",
        raw,
        re.IGNORECASE,
    )
    if entry_match:
        entry = float(entry_match.group(1))

    # ── 3. Stop loss ─────────────────────────────────────────────────────────
    sl_match = re.search(r"sl\s*:?\s*([\d]{2,6}(?:\.\d+)?)", raw, re.IGNORECASE)
    if not sl_match:
        return None
    sl = float(sl_match.group(1))

    # ── 4. Take profits (multiple allowed, "open" → None) ────────────────────
    tps = []
    for m in re.finditer(r"tp\s*:?\s*([\d]{2,6}(?:\.\d+)?|open)", raw, re.IGNORECASE):
        val = m.group(1).lower()
        tps.append(None if val == "open" else float(val))

    if not tps:
        return None

    return {
        "direction": direction,
        "symbol": symbol,
        "sl": sl,
        "entry": entry,          # None → market order
        "tps": tps,
        "num_trades": len(tps),
    }
