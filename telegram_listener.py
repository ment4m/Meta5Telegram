import json
from pathlib import Path
from telethon import TelegramClient, events
from telethon.sessions import StringSession

import config
from signal_classifier import classify
from signal_state import add_pending, find_pending, mark_active, remove_expired
from sl_predictor import predict, record
from mt5_bridge import write_open, write_update, write_update_sl_only, write_breakeven, write_close
from logger import get_logger

log = get_logger(__name__)

_LAST_SYMBOL_FILE = Path(__file__).parent / "last_symbol.json"


def _get_last_symbol() -> str:
    if _LAST_SYMBOL_FILE.exists():
        return json.loads(_LAST_SYMBOL_FILE.read_text()).get("symbol", "")
    return ""


def _save_last_symbol(symbol: str):
    _LAST_SYMBOL_FILE.write_text(json.dumps({"symbol": symbol}))


def _handle_new_signal(msg: dict):
    """
    New signal from channel.
    - Complete (has SL + TPs): execute immediately.
    - Incomplete (no SL/TP numbers): predict SL, open trades, store pending state.
    """
    direction = msg.get("direction", "").lower()
    symbol    = msg.get("symbol", "")
    sl        = msg.get("sl")
    tps       = msg.get("tps") or []

    if not symbol:
        symbol = _get_last_symbol()
        if symbol:
            log.info("No symbol in message — using last traded symbol: %s", symbol)
    if not direction or not symbol:
        log.warning("new_signal missing direction or symbol — skipping")
        return

    # Expand "TP every N pips" into actual TP prices if we have at least one anchor TP
    tp_step_pips = msg.get("tp_step_pips")
    if tp_step_pips and tps:
        pip_value = config.PIP_VALUE_MAP.get(symbol, 0.10)
        step = tp_step_pips * pip_value
        sign = 1 if direction == "buy" else -1
        anchor = next((t for t in tps if t is not None), None)
        if anchor is not None:
            # Fill remaining slots up to MAX_TRADES-1 (last is always open)
            slots_needed = config.MAX_TRADES - 1 - len([t for t in tps if t is not None])
            for i in range(1, slots_needed + 1):
                tps.append(round(anchor + sign * step * i, 5))
            log.info("Expanded pip-step TPs: %s", tps)

    _save_last_symbol(symbol)

    if msg.get("is_complete"):
        # Complete signal — execute right away
        log.info("Complete signal: %s %s | SL=%.5f | %d TPs", direction.upper(), symbol, sl, len(tps))
        sig_id = f"sig_{int(__import__('time').time())}"
        write_open(symbol=symbol, direction=direction, tps=tps, sl=sl, signal_id=sig_id)
        # Record for future SL prediction
        if sl and tps:
            _record_history(symbol, direction, sl, tps)
    else:
        # Incomplete — EA will calculate SL from risk, open immediately, wait for update.
        # Always open exactly MAX_TRADES regardless of what the predictor or signal says.
        num_tps = config.MAX_TRADES
        # auto_tp=True when classifier returned no TP list at all (short message like "Sell gold")
        # auto_tp=False when classifier returned TP labels with no numbers (full signal, TPs coming)
        auto_tp = msg.get("tps") is None
        log.info("Incomplete signal: %s %s — no SL | trades=%d | auto_tp=%s",
                 direction.upper(), symbol, num_tps, auto_tp)
        predicted_tps = [None] * num_tps
        sig_id = add_pending(symbol, direction, 0, num_tps, auto_tp=auto_tp)
        write_open(
            symbol=symbol,
            direction=direction,
            tps=predicted_tps,
            sl=None,       # EA calculates SL from risk
            sl_points=None,
            tp_step=config.TP_STEP_MAP.get(symbol, config.TP_STEP_DEFAULT) if auto_tp else None,
            signal_id=sig_id,
        )


def _handle_signal_update(msg: dict):
    """
    Update message with actual SL and TPs.
    Find matching pending signal and update existing trades.
    If no pending signal found, treat as a fresh complete signal.
    """
    direction = msg.get("direction", "").lower()
    symbol    = msg.get("symbol", "")
    sl        = msg.get("sl")
    tps       = msg.get("tps") or []

    if not sl or not tps:
        log.warning("signal_update has no SL or TPs — skipping")
        return

    # Try to match a pending signal
    if direction and symbol:
        sig_id, pending = find_pending(symbol, direction)
    else:
        # Update may not repeat direction/symbol, search all pending
        sig_id, pending = None, None
        # Fallback: find any single pending signal
        import signal_state as ss
        state = ss._load()
        pending_list = [(k, v) for k, v in state.items() if v["status"] == "pending"]
        if len(pending_list) == 1:
            sig_id, pending = pending_list[0]
            direction = pending["direction"]
            symbol = pending["symbol"]

    if pending:
        if pending.get("auto_tp"):
            # TPs were auto-calculated from entry — only update the SL, keep TPs
            log.info("Updating auto-TP signal %s: %s %s | new_sl=%.5f (TPs kept)",
                     sig_id, direction.upper(), symbol, sl)
            write_update_sl_only(symbol=symbol, direction=direction, new_sl=sl, signal_id=sig_id)
        else:
            log.info("Updating pending signal %s: %s %s | new_sl=%.5f | %d TPs",
                     sig_id, direction.upper(), symbol, sl, len(tps))
            write_update(symbol=symbol, direction=direction, new_sl=sl, tps=tps, signal_id=sig_id)
        mark_active(sig_id)
        _record_history(symbol, direction, sl, tps)
    else:
        # No pending found — treat as a standalone complete signal
        log.info("No pending signal found — treating update as new complete signal")
        sig_id = f"sig_{int(__import__('time').time())}"
        write_open(symbol=symbol, direction=direction, tps=tps, sl=sl, signal_id=sig_id)
        _record_history(symbol, direction, sl, tps)


def _record_history(symbol: str, direction: str, sl: float, tps: list):
    """Record completed signal data for future SL prediction."""
    try:
        real_tps = [t for t in tps if t is not None]
        num_tps = len(tps)
        if real_tps and sl:
            # Estimate average SL distance in points (will be refined by EA actual entry)
            # Use average TP distance as a rough proxy for now
            avg_tp = sum(real_tps) / len(real_tps)
            sl_distance = abs(avg_tp - sl) * 0.3  # rough estimate, 30% of avg TP distance
            record(symbol, direction, sl_distance, num_tps)
    except Exception as e:
        log.debug("History record error (non-critical): %s", e)


_SESSION_FILE = Path(__file__).parent / "tg_session.string"


def _load_session() -> StringSession:
    if _SESSION_FILE.exists():
        return StringSession(_SESSION_FILE.read_text().strip())
    return StringSession()


def _save_session(client):
    _SESSION_FILE.write_text(client.session.save())
    log.info("Session saved to %s", _SESSION_FILE)


async def start():
    client = TelegramClient(_load_session(), config.API_ID, config.API_HASH)
    await client.connect()
    if not await client.is_user_authorized():
        await client.send_code_request(config.PHONE_NUMBER)
        code = input("Enter the Telegram code: ")
        try:
            await client.sign_in(config.PHONE_NUMBER, code)
        except Exception as e:
            if "SessionPasswordNeeded" in type(e).__name__:
                pw = input("Enter 2FA password: ")
                await client.sign_in(password=pw)
            else:
                raise
        _save_session(client)

    me = await client.get_me()
    log.info("Logged in as %s | monitoring: %s", me.username or me.phone, config.CHANNEL_USERNAME)

    @client.on(events.NewMessage(chats=config.CHANNEL_USERNAME))
    async def on_message(event):
        text = event.message.text
        if not text:
            log.debug("Non-text message (voice/media) — ignored")
            return

        remove_expired()

        log.debug("New message:\n%s", text[:200])
        msg = classify(text)
        msg_type = msg.get("type", "ignore")

        if msg_type == "new_signal":
            _handle_new_signal(msg)

        elif msg_type == "signal_update":
            _handle_signal_update(msg)

        elif msg_type == "breakeven":
            log.info("Breakeven instruction detected")
            write_breakeven()

        elif msg_type == "close":
            log.info("Close/cancel instruction detected")
            write_close()

        else:
            log.debug("Message ignored (type=%s)", msg_type)

    log.info("Bot is running. Press Ctrl+C to stop.")
    await client.run_until_disconnected()
