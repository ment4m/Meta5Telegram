"""
Tracks pending signals (first message arrived, waiting for SL/TP update).
Persisted to disk so restarts don't lose state.
"""

import json
import time
import uuid
from pathlib import Path
from logger import get_logger

log = get_logger(__name__)

STATE_FILE = Path(__file__).parent / "signal_state.json"
PENDING_TIMEOUT_SEC = 900  # 15 minutes — if no update arrives, discard


def _load() -> dict:
    if not STATE_FILE.exists():
        return {}
    with open(STATE_FILE) as f:
        return json.load(f)


def _save(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def _base_symbol(symbol: str) -> str:
    return symbol.upper()[:6]  # first 6 chars covers XAUUSD, BTCUSD, EURUSD


def add_pending(symbol: str, direction: str, predicted_sl_points: int, num_tps: int) -> str:
    """Store a pending signal and return its signal_id."""
    state = _load()
    signal_id = str(uuid.uuid4())[:8]
    state[signal_id] = {
        "symbol": symbol,
        "direction": direction.lower(),
        "predicted_sl_points": predicted_sl_points,
        "num_tps": num_tps,
        "timestamp": int(time.time()),
        "status": "pending",
    }
    _save(state)
    log.info("Pending signal stored: id=%s %s %s", signal_id, direction, symbol)
    return signal_id


def find_pending(symbol: str, direction: str):
    """
    Find the most recent pending signal matching symbol + direction.
    Returns (signal_id, signal_dict) or (None, None).
    """
    state = _load()
    now = int(time.time())
    best_id, best_sig = None, None

    for sig_id, sig in state.items():
        if sig["status"] != "pending":
            continue
        if sig["direction"] != direction.lower():
            continue
        if not _base_symbol(symbol).startswith(_base_symbol(sig["symbol"])):
            continue
        if now - sig["timestamp"] > PENDING_TIMEOUT_SEC:
            continue
        # Pick the most recent one
        if best_sig is None or sig["timestamp"] > best_sig["timestamp"]:
            best_id, best_sig = sig_id, sig

    return best_id, best_sig


def mark_active(signal_id: str):
    state = _load()
    if signal_id in state:
        state[signal_id]["status"] = "active"
        _save(state)


def remove_expired():
    """Clean up old pending signals."""
    state = _load()
    now = int(time.time())
    before = len(state)
    state = {
        k: v for k, v in state.items()
        if now - v["timestamp"] < PENDING_TIMEOUT_SEC or v["status"] == "active"
    }
    if len(state) < before:
        log.debug("Cleaned %d expired signals", before - len(state))
    _save(state)
