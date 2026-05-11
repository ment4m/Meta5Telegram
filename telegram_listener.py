import asyncio
import json
import time
from pathlib import Path
from telethon import TelegramClient, events
from telethon.sessions import StringSession

import config
from signal_classifier import classify
from signal_state import add_pending, find_pending, mark_active, remove_expired
from sl_predictor import record
from mt5_bridge import write_open, write_update, write_update_sl_only, write_breakeven, write_close
from logger import get_logger

log = get_logger(__name__)

_LAST_SYMBOL_FILE  = Path(__file__).parent / "last_symbol.json"
_LAST_MSG_ID_FILE  = Path(__file__).parent / "last_msg_id.json"

# Text-based deduplication: block identical message text within the window.
# Checked BEFORE classify() so there is no async gap between check and lock.
_RECENT_TEXTS: dict = {}  # text -> timestamp
_DEDUP_WINDOW_SEC = 10  # 10 seconds — just enough to prevent event+poll double-fire


def _is_duplicate_text(text: str) -> bool:
    """Return True if this exact message text was seen within the dedup window."""
    now = time.time()
    # Clean up expired entries
    expired = [k for k, ts in _RECENT_TEXTS.items() if now - ts > _DEDUP_WINDOW_SEC]
    for k in expired:
        del _RECENT_TEXTS[k]
    if text in _RECENT_TEXTS:
        elapsed = int(now - _RECENT_TEXTS[text])
        log.warning("Duplicate message blocked (same text seen %ds ago)", elapsed)
        return True
    _RECENT_TEXTS[text] = now
    return False


def _get_last_symbol() -> str:
    if _LAST_SYMBOL_FILE.exists():
        return json.loads(_LAST_SYMBOL_FILE.read_text()).get("symbol", "")
    return ""


def _save_last_symbol(symbol: str):
    _LAST_SYMBOL_FILE.write_text(json.dumps({"symbol": symbol}))


_last_open_time: dict = {}  # (symbol, direction) -> timestamp of last open
_SL_UPDATE_WINDOW = 600    # 10 minutes — new signal within this window updates SL instead of opening


def _handle_new_signal(msg: dict):
    """
    New signal from channel.
    - Complete (has SL + TPs): execute immediately.
    - Incomplete (no SL/TP numbers): predict SL, open trades, store pending state.
    - Complete signal within 10 min of last open → update SL only (don't open new trades).
    - Complete signal after 10 min → open new trades even if existing ones are open.
    """
    direction = (msg.get("direction") or "").lower()
    symbol    = msg.get("symbol") or ""
    sl        = msg.get("sl")
    tps       = msg.get("tps") or []

    if not symbol:
        symbol = _get_last_symbol()
        if symbol:
            log.info("No symbol in message — using last traded symbol: %s", symbol)
    if not direction or not symbol:
        log.warning("new_signal missing direction or symbol — skipping")
        return

    # Apply broker symbol suffix (e.g. "m" for Exness: XAUUSD → XAUUSDm)
    if config.SYMBOL_SUFFIX and not symbol.endswith(config.SYMBOL_SUFFIX):
        symbol = symbol + config.SYMBOL_SUFFIX

    # "TP every N pips" — let the EA generate TPs from actual entry price
    tp_step_pips = msg.get("tp_step_pips")
    tp_step = None
    if tp_step_pips:
        pip_value = config.PIP_VALUE_MAP.get(symbol, 0.10)
        tp_step = round(tp_step_pips * pip_value, 5)
        # TPs will be generated from entry by the EA — send nulls for all slots
        tps = [None] * config.MAX_TRADES
        log.info("Pip-step signal: step=%.5f (%d pips) — EA will generate TPs from entry", tp_step, tp_step_pips)

    _save_last_symbol(symbol)

    if msg.get("is_complete"):
        import time as _time
        key = (symbol, direction)
        now = _time.time()
        last_open = _last_open_time.get(key, 0)
        within_window = (now - last_open) < _SL_UPDATE_WINDOW

        # Check if there's already a pending signal for same symbol+direction
        existing_id, existing_pending = find_pending(symbol, direction)

        if within_window and (existing_pending or last_open > 0):
            # Within 10 min — update SL on existing trades instead of opening new ones
            if existing_pending:
                log.info("Within 10min window — updating SL on pending %s: %s %s | sl=%.5f",
                         existing_id, direction.upper(), symbol, sl)
                write_update_sl_only(symbol=symbol, direction=direction, new_sl=sl, signal_id=existing_id)
                mark_active(existing_id)
            else:
                log.info("Within 10min window — updating SL on active trades: %s %s | sl=%.5f",
                         direction.upper(), symbol, sl)
                write_update_sl_only(symbol=symbol, direction=direction, new_sl=sl, signal_id="sl_update")
            if sl and tps:
                _record_history(symbol, direction, sl, tps)
        else:
            # After 10 min or first signal — open fresh trades (force past EA position check)
            log.info("Complete signal: %s %s | SL=%.5f | %d TPs | tp_step=%s", direction.upper(), symbol, sl, len(tps), tp_step)
            sig_id = f"sig_{int(_time.time())}"
            _last_open_time[key] = now
            write_open(symbol=symbol, direction=direction, tps=tps, sl=sl, tp_step=tp_step, signal_id=sig_id, force=True)
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
        import time as _time
        _last_open_time[(symbol, direction)] = _time.time()
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
    direction = (msg.get("direction") or "").lower()
    symbol    = msg.get("symbol") or ""
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

# Per-channel last message ID tracking
_last_msg_ids: dict = {}  # channel_id -> last processed message id


def _load_last_msg_id() -> int:
    if _LAST_MSG_ID_FILE.exists():
        return json.loads(_LAST_MSG_ID_FILE.read_text()).get("id", 0)
    return 0


def _save_last_msg_id_for(channel_id: int, msg_id: int):
    """Save last message ID for a specific channel."""
    _last_msg_ids[channel_id] = msg_id
    # Also persist the global one (most recent across all channels)
    _LAST_MSG_ID_FILE.write_text(json.dumps({"id": msg_id}))


def _load_session() -> StringSession:
    if _SESSION_FILE.exists():
        return StringSession(_SESSION_FILE.read_text().strip())
    return StringSession()


def _save_session(client):
    _SESSION_FILE.write_text(client.session.save())
    log.info("Session saved to %s", _SESSION_FILE)


async def _process_message(text: str):
    """Shared handler for both real-time events and poll fallback."""
    remove_expired()
    msg = classify(text)
    msg_type = msg.get("type", "ignore")
    if msg_type not in ("close", "breakeven") and _is_duplicate_text(text):
        return
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
    log.info("Logged in as %s | monitoring %d channel(s): %s",
             me.username or me.phone, len(config.CHANNEL_LIST), config.CHANNEL_LIST)

    # Resolve all channel entities at startup
    channel_entities = []
    for ch in config.CHANNEL_LIST:
        try:
            entity = await client.get_entity(ch)
            title = getattr(entity, "title", str(ch))
            log.info("Channel resolved: %s (id=%s)", title, entity.id)
            channel_entities.append(entity)
        except Exception as e:
            log.error("Failed to resolve channel %s: %s", ch, e)

    if not channel_entities:
        log.error("No channels could be resolved — exiting")
        return

    # Load per-channel last message IDs; each channel starts from its own latest message
    for entity in channel_entities:
        ch_id = entity.id
        if ch_id not in _last_msg_ids:
            try:
                history = await client.get_messages(entity, limit=1)
                # Always start from this channel's own latest message to avoid cross-channel ID confusion
                _last_msg_ids[ch_id] = history[0].id if history else 0
            except Exception as e:
                log.warning("Could not fetch history for channel %s: %s", ch_id, e)
                _last_msg_ids[ch_id] = 0
        log.info("Channel %s: starting from message ID %d", ch_id, _last_msg_ids[ch_id])

    # Replay missed messages for each channel
    for entity in channel_entities:
        ch_id = entity.id
        last_id = _last_msg_ids[ch_id]
        try:
            missed = await client.get_messages(entity, limit=20, min_id=last_id)
            if missed:
                title = getattr(entity, "title", str(ch_id))
                log.info("Replaying %d missed messages from [%s]...", len(missed), title)
                for m in reversed(missed):
                    if not m.text:
                        continue
                    log.info("Replay [%s] msg id=%d: %s", title, m.id, m.text[:80])
                    _last_msg_ids[ch_id] = m.id
                    _save_last_msg_id_for(ch_id, m.id)
                    remove_expired()
                    msg = classify(m.text)
                    msg_type = msg.get("type", "ignore")
                    if msg_type not in ("close", "breakeven") and _is_duplicate_text(m.text):
                        continue
                    if msg_type == "new_signal":
                        _handle_new_signal(msg)
                    elif msg_type == "signal_update":
                        _handle_signal_update(msg)
                    elif msg_type == "breakeven":
                        log.info("Replay breakeven")
                        write_breakeven()
                    elif msg_type == "close":
                        log.info("Replay close")
                        write_close()
                    else:
                        log.debug("Replay ignored (type=%s)", msg_type)
        except Exception as e:
            log.warning("Replay error for channel %s: %s", ch_id, e)

    @client.on(events.NewMessage(chats=channel_entities))
    async def on_message(event):
        ch_id = event.message.peer_id.channel_id if hasattr(event.message.peer_id, "channel_id") else 0
        last_id = _last_msg_ids.get(ch_id, 0)

        # Only process messages newer than last processed
        if event.message.id <= last_id:
            log.info("Skipping replay msg id=%d (last=%d) ch=%s", event.message.id, last_id, ch_id)
            return
        # Mark as processed immediately to prevent re-delivery duplicates
        _last_msg_ids[ch_id] = event.message.id
        _save_last_msg_id_for(ch_id, event.message.id)

        text = event.message.text
        if not text:
            log.debug("Non-text message (voice/media) — ignored")
            return

        log.debug("New message [ch=%s id=%d]:\n%s", ch_id, event.message.id, text[:200])
        await _process_message(text)

    @client.on(events.MessageEdited(chats=channel_entities))
    async def on_edit(event):
        """Detect when a signal message is edited (e.g. SL corrected) and update trades."""
        text = event.message.text
        if not text:
            return
        log.info("Message edited [id=%d]: %s", event.message.id, text[:120])
        msg = classify(text)
        if msg.get("type") != "new_signal":
            return
        sl = msg.get("sl")
        if not sl:
            return
        # Update SL on all open trades for this symbol+direction
        symbol    = msg.get("symbol") or ""
        direction = msg.get("direction") or ""
        if symbol and direction:
            log.info("Edit detected — updating SL to %.5f for %s %s", sl, direction.upper(), symbol)
            write_update_sl_only(symbol=symbol, direction=direction, new_sl=sl, signal_id="edit")

    async def _poll_fallback():
        """Poll every 5s to catch messages the event handler missed."""
        while True:
            await asyncio.sleep(5)
            for entity in channel_entities:
                ch_id = entity.id
                last_id = _last_msg_ids.get(ch_id, 0)
                try:
                    missed = await client.get_messages(entity, limit=10, min_id=last_id)
                    if missed:
                        title = getattr(entity, "title", str(ch_id))
                        log.warning("Poll fallback caught %d missed message(s) from [%s]", len(missed), title)
                        for m in reversed(missed):
                            if not m.text:
                                continue
                            log.info("Poll [%s] msg id=%d: %s", title, m.id, m.text[:80])
                            _last_msg_ids[ch_id] = m.id
                            _save_last_msg_id_for(ch_id, m.id)
                            await _process_message(m.text)
                except Exception as e:
                    log.warning("Poll fallback error for channel %s: %s", ch_id, e)

    log.info("Bot is running. Press Ctrl+C to stop.")
    asyncio.ensure_future(_poll_fallback())
    await client.run_until_disconnected()
