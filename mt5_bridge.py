"""
MT5 Bridge — writes JSON action files for the MQL5 EA to execute.

Actions:
  open      — open new trades (one per TP). EA calculates lot from account balance.
  update    — modify SL/TP on existing open trades for a symbol+direction.
  breakeven — move all open trades' SL to entry price + spread buffer.
"""

import json
import time
from pathlib import Path

import config
from logger import get_logger

log = get_logger(__name__)


def _signals_dir() -> Path:
    path = Path(config.MT5_FILES_PATH)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write(payload: dict, prefix: str) -> Path:
    filename = f"{prefix}_{int(time.time())}.json"
    filepath = _signals_dir() / filename
    with open(filepath, "w") as f:
        json.dump(payload, f, indent=2)
    log.info("Written → %s | %s", filename, payload)
    return filepath


# ── Actions ───────────────────────────────────────────────────────────────────

def write_open(
    symbol: str,
    direction: str,
    tps: list,
    sl: float = None,
    sl_points: int = None,
    signal_id: str = "",
) -> Path:
    """
    Open one trade per TP.
    - sl: actual price (used when signal is complete)
    - sl_points: distance from entry in points (used when signal is incomplete/predicted)
    EA calculates lot size = AccountBalance / LOT_BALANCE_DIVISOR / num_tps.
    """
    payload = {
        "action":            "open",
        "symbol":            symbol,
        "direction":         direction.lower(),
        "sl":                sl,          # None if predicted
        "sl_points":         sl_points,   # None if actual sl provided
        "tps":               tps,         # list of floats or None (None = no TP)
        "magic":             config.MAGIC_NUMBER,
        "deviation":         config.DEVIATION,
        "lot_balance_div":   config.LOT_BALANCE_DIVISOR,
        "signal_id":         signal_id,
        "timestamp":         int(time.time()),
    }
    log.info("OPEN %s %s | sl=%s | sl_pts=%s | %d TPs",
             direction.upper(), symbol, sl, sl_points, len(tps))
    return _write(payload, "open")


def write_update(
    symbol: str,
    direction: str,
    new_sl: float,
    tps: list,
    signal_id: str = "",
) -> Path:
    """
    Update SL and TPs on all open trades matching symbol+direction+magic.
    Called when the channel sends the actual SL/TP numbers after an incomplete signal.
    """
    payload = {
        "action":    "update",
        "symbol":    symbol,
        "direction": direction.lower(),
        "new_sl":    new_sl,
        "tps":       tps,
        "magic":     config.MAGIC_NUMBER,
        "signal_id": signal_id,
        "timestamp": int(time.time()),
    }
    log.info("UPDATE %s %s | new_sl=%.5f | %d TPs", direction.upper(), symbol, new_sl, len(tps))
    return _write(payload, "update")


def write_breakeven() -> Path:
    """
    Move SL to entry + spread buffer on ALL open trades opened by this bot.
    Buffer = current spread × 1.5 (calculated per-trade in the EA).
    """
    payload = {
        "action":    "breakeven",
        "magic":     config.MAGIC_NUMBER,
        "timestamp": int(time.time()),
    }
    log.info("BREAKEVEN — moving all SL to entry + spread buffer")
    return _write(payload, "breakeven")
