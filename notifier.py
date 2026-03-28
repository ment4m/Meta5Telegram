"""
Pushover + Telegram self-message notifications.
- Emergency alert: repeats every 30s until dismissed (bypasses silent mode).
- Telegram summary: sends trade details to your Saved Messages.
"""

import urllib.request
import urllib.parse
import asyncio
import ssl
import config
from logger import get_logger

log = get_logger(__name__)

_PUSHOVER_URL = "https://api.pushover.net/1/messages.json"
_tg_client = None


def set_telegram_client(client):
    """Call once at startup with the Telethon client."""
    global _tg_client
    _tg_client = client


async def _send_self(message: str):
    """Send a message to the user's own Saved Messages."""
    if _tg_client is None:
        return
    try:
        await _tg_client.send_message("me", message, parse_mode="md")
        log.info("Telegram self-message sent")
    except Exception as e:
        log.warning("Telegram self-message failed: %s", e)


def _tg_notify(message: str):
    """Fire-and-forget Telegram self-message."""
    if _tg_client is None:
        return
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.ensure_future(_send_self(message))
        else:
            loop.run_until_complete(_send_self(message))
    except Exception as e:
        log.warning("Telegram notify error: %s", e)


def _pushover(title: str, message: str, priority: int = 1):
    """
    priority 2 = emergency (repeats every 30s until dismissed, bypasses silent)
    priority 1 = high (plays sound, bypasses quiet hours, no repeat)
    priority 0 = normal (sound only if not in quiet hours)
    """
    if not config.PUSHOVER_TOKEN or not config.PUSHOVER_USER:
        log.debug("Pushover not configured — skipping")
        return
    try:
        params = {
            "token":    config.PUSHOVER_TOKEN,
            "user":     config.PUSHOVER_USER,
            "title":    title,
            "message":  message,
            "priority": priority,
            "sound":    "persistent",
        }
        if priority == 2:
            params["retry"]  = 30
            params["expire"] = 3600
        data = urllib.parse.urlencode(params).encode()
        req = urllib.request.Request(_PUSHOVER_URL, data=data)
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        urllib.request.urlopen(req, timeout=10, context=ctx)
        log.info("Pushover alert sent: %s", title)
    except Exception as e:
        log.warning("Pushover failed: %s", e)


def alert_new_signal(direction: str, symbol: str, sl, tps: list, num_trades: int, sig_id: str):
    """Emergency alert when a new signal opens trades."""
    sl_str  = f"{sl:.5f}" if sl else "auto"
    tp_list = ", ".join(f"{t:.2f}" if t else "OPEN" for t in tps[:3])
    if len(tps) > 3:
        tp_list += f" +{len(tps)-3} more"

    title  = f"🔔 {direction.upper()} {symbol} — {num_trades} trades opened"
    detail = f"SL: {sl_str}\nTPs: {tp_list}\nID: {sig_id}"
    _pushover(title, detail, priority=2)  # emergency — repeats until dismissed
    _tg_notify(
        f"🔔 **{direction.upper()} {symbol}**\n"
        f"Trades: {num_trades}\n"
        f"SL: {sl_str}\n"
        f"TPs: {tp_list}\n"
        f"ID: `{sig_id}`"
    )


def alert_signal_update(direction: str, symbol: str, new_sl: float, sig_id: str):
    """High priority alert when SL/TPs are updated."""
    title  = f"✏️ Updated: {direction.upper()} {symbol}"
    detail = f"New SL: {new_sl:.5f}\nID: {sig_id}"
    _pushover(title, detail, priority=1)  # high — plays sound
    _tg_notify(
        f"✏️ **SL Updated — {direction.upper()} {symbol}**\n"
        f"New SL: `{new_sl:.5f}`\n"
        f"ID: `{sig_id}`"
    )


def alert_breakeven(symbol: str = ""):
    """Emergency alert for breakeven action."""
    title  = "↔️ Breakeven triggered"
    detail = f"SL moved to entry{' — ' + symbol if symbol else ''}"
    _pushover(title, detail, priority=2)  # emergency — repeats until dismissed
    _tg_notify(f"↔️ **Breakeven triggered**\nSL moved to entry price")


def alert_close():
    """Emergency alert when all trades are closed."""
    title  = "🔴 All trades closed"
    detail = "Close instruction received — all bot trades closed"
    _pushover(title, detail, priority=2)  # emergency — repeats until dismissed
    _tg_notify("🔴 **All trades closed**\nClose instruction received")
