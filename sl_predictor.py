"""
Predicts SL distance (in points) and number of TPs for incomplete signals,
based on historical data from past signals on the same symbol/direction.
"""

import json
from pathlib import Path
from logger import get_logger

log = get_logger(__name__)

HISTORY_FILE = Path(__file__).parent / "sl_history.json"

# Fallback defaults when no history exists yet
_DEFAULTS = {
    "XAUUSD": {"sl_points": 2000,   "num_tps": 8},  # ~$20 SL (SYMBOL_POINT=0.01)
    "BTCUSD": {"sl_points": 150000, "num_tps": 6},  # ~$1500 SL (SYMBOL_POINT=0.01)
    "EURUSD": {"sl_points": 200,    "num_tps": 4},  # ~20 pips (SYMBOL_POINT=0.00001)
    "default": {"sl_points": 1000,  "num_tps": 6},
}


def _load() -> list:
    if not HISTORY_FILE.exists():
        return []
    with open(HISTORY_FILE) as f:
        return json.load(f)


def _save(history: list):
    with open(HISTORY_FILE, "w") as f:
        json.dump(history[-100:], f, indent=2)  # keep last 100


def _base_symbol(symbol: str) -> str:
    """Strip broker suffixes like 'm', '.micro', '_' to get base symbol."""
    s = symbol.upper()
    for suffix in ["M", ".MICRO", "_MICRO", "."]:
        if s.endswith(suffix):
            s = s[: -len(suffix)]
    return s


def record(symbol: str, direction: str, sl_points: float, num_tps: int):
    """Save a completed signal to history for future predictions."""
    history = _load()
    history.append({
        "symbol": _base_symbol(symbol),
        "direction": direction.lower(),
        "sl_points": round(sl_points),
        "num_tps": num_tps,
    })
    _save(history)
    log.debug("Recorded signal history: %s %s sl_pts=%d tps=%d",
              symbol, direction, sl_points, num_tps)


def predict(symbol: str, direction: str) -> tuple[int, int]:
    """
    Returns (predicted_sl_points, predicted_num_tps) based on past signals.
    Falls back to defaults if no history exists for this symbol.
    """
    base = _base_symbol(symbol)
    history = _load()
    dir_lower = direction.lower()

    relevant = [
        h for h in history
        if h["symbol"] == base and h["direction"] == dir_lower
    ][-10:]  # use last 10 matching signals

    if not relevant:
        defaults = _DEFAULTS.get(base, _DEFAULTS["default"])
        log.info("No history for %s %s — using defaults: %s", symbol, direction, defaults)
        return defaults["sl_points"], defaults["num_tps"]

    avg_sl = round(sum(h["sl_points"] for h in relevant) / len(relevant))
    avg_tps = round(sum(h["num_tps"] for h in relevant) / len(relevant))
    avg_tps = max(avg_tps, 1)

    log.info("Predicted for %s %s: sl_points=%d, num_tps=%d (from %d samples)",
             symbol, direction, avg_sl, avg_tps, len(relevant))
    return avg_sl, avg_tps
