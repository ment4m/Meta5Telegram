from telethon import TelegramClient, events

import config
from signal_classifier import classify
from signal_state import add_pending, find_pending, mark_active, remove_expired
from sl_predictor import predict, record
from mt5_bridge import write_open, write_update, write_breakeven
from logger import get_logger

log = get_logger(__name__)


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

    if not direction or not symbol:
        log.warning("new_signal missing direction or symbol — skipping")
        return

    if msg.get("is_complete"):
        # Complete signal — execute right away
        log.info("Complete signal: %s %s | SL=%.5f | %d TPs", direction.upper(), symbol, sl, len(tps))
        sig_id = f"sig_{int(__import__('time').time())}"
        write_open(symbol=symbol, direction=direction, tps=tps, sl=sl, signal_id=sig_id)
        # Record for future SL prediction
        if sl and tps:
            _record_history(symbol, direction, sl, tps)
    else:
        # Incomplete — EA will calculate SL from risk, open immediately, wait for update
        _, num_tps = predict(symbol, direction)  # only use num_tps prediction
        log.info("Incomplete signal: %s %s — no SL, EA will auto-calculate | num_tps=%d",
                 direction.upper(), symbol, num_tps)
        predicted_tps = [None] * num_tps
        sig_id = add_pending(symbol, direction, 0, num_tps)
        write_open(
            symbol=symbol,
            direction=direction,
            tps=predicted_tps,
            sl=None,       # EA calculates SL from risk
            sl_points=None,
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


async def start():
    client = TelegramClient("tg_session", config.API_ID, config.API_HASH)
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

        else:
            log.debug("Message ignored (type=%s)", msg_type)

    log.info("Bot is running. Press Ctrl+C to stop.")
    await client.run_until_disconnected()
