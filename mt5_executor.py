"""
MT5 trade executor.

NOTE: The MetaTrader5 Python package only works on Windows.
      Run this on a Windows machine or a Windows VPS.
"""

import MetaTrader5 as mt5
import config
from logger import get_logger

log = get_logger(__name__)


# ── Connection ────────────────────────────────────────────────────────────────

def connect() -> None:
    ok = mt5.initialize(
        login=config.MT5_LOGIN,
        password=config.MT5_PASSWORD,
        server=config.MT5_SERVER,
    )
    if not ok:
        raise RuntimeError(f"MT5 init failed: {mt5.last_error()}")
    info = mt5.account_info()
    log.info("Connected to MT5 | account=%s balance=%.2f %s",
             info.login, info.balance, info.currency)


def disconnect() -> None:
    mt5.shutdown()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _ensure_symbol(symbol: str) -> mt5.SymbolInfo:
    info = mt5.symbol_info(symbol)
    if info is None:
        raise ValueError(f"Symbol '{symbol}' not found in MT5")
    if not info.visible:
        mt5.symbol_select(symbol, True)
        info = mt5.symbol_info(symbol)
    return info


def _round_lot(lot: float, sym_info: mt5.SymbolInfo) -> float:
    step = sym_info.volume_step
    lot = round(round(lot / step) * step, 10)
    lot = max(lot, sym_info.volume_min)
    lot = min(lot, sym_info.volume_max)
    return round(lot, 2)


def _current_price(symbol: str, direction: str) -> float:
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        raise RuntimeError(f"Cannot get tick for {symbol}")
    return tick.ask if direction == "buy" else tick.bid


# ── Main executor ─────────────────────────────────────────────────────────────

def execute_signal(signal: dict, total_lot: float) -> list:
    """
    Open one market trade per TP level, splitting total_lot equally.

    signal keys: direction, symbol, sl, entry, tps, num_trades
    Returns list of mt5.OrderSendResult objects.
    """
    symbol    = signal["symbol"]
    direction = signal["direction"]
    sl        = signal["sl"]
    tps       = signal["tps"]

    sym_info  = _ensure_symbol(symbol)
    lot_each  = _round_lot(total_lot / len(tps), sym_info)

    order_type = mt5.ORDER_TYPE_BUY if direction == "buy" else mt5.ORDER_TYPE_SELL
    price      = _current_price(symbol, direction)

    log.info(
        "Executing signal | %s %s | SL=%.5f | %d TPs | %.2f lot each",
        direction.upper(), symbol, sl, len(tps), lot_each,
    )

    results = []
    for i, tp in enumerate(tps):
        tp_label = f"{tp:.5f}" if tp is not None else "OPEN"

        request = {
            "action":      mt5.TRADE_ACTION_DEAL,
            "symbol":      symbol,
            "volume":      lot_each,
            "type":        order_type,
            "price":       price,
            "sl":          sl,
            "deviation":   config.DEVIATION,
            "magic":       config.MAGIC_NUMBER + i,
            "comment":     f"TG_TP{i + 1}",
            "type_time":   mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        if tp is not None:
            request["tp"] = tp

        result = mt5.order_send(request)
        results.append(result)

        if result.retcode == mt5.TRADE_RETCODE_DONE:
            log.info("  TP%d placed | ticket=%d | tp=%s", i + 1, result.order, tp_label)
        else:
            log.error(
                "  TP%d FAILED | retcode=%d | %s",
                i + 1, result.retcode, result.comment,
            )

    placed = sum(1 for r in results if r.retcode == mt5.TRADE_RETCODE_DONE)
    log.info("Done: %d/%d trades placed for %s", placed, len(tps), symbol)
    return results
