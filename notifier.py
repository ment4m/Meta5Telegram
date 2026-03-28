"""
Pushover notifications.
- Emergency (priority 2): repeats every 30s until dismissed.
- High (priority 1): plays sound once, bypasses quiet hours.
"""

import urllib.request
import urllib.parse
import ssl
import config
from logger import get_logger

log = get_logger(__name__)

_PUSHOVER_URL = "https://api.pushover.net/1/messages.json"


def _pushover(title: str, message: str, priority: int = 1):
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
    sl_str  = f"{sl:.5f}" if sl else "auto"
    tp_list = ", ".join(f"{t:.2f}" if t else "OPEN" for t in tps[:3])
    if len(tps) > 3:
        tp_list += f" +{len(tps)-3} more"
    title  = f"🔔 {direction.upper()} {symbol} — {num_trades} trades opened"
    detail = f"SL: {sl_str}\nTPs: {tp_list}\nID: {sig_id}"
    _pushover(title, detail, priority=2)


def alert_signal_update(direction: str, symbol: str, new_sl: float, sig_id: str):
    title  = f"✏️ Updated: {direction.upper()} {symbol}"
    detail = f"New SL: {new_sl:.5f}\nID: {sig_id}"
    _pushover(title, detail, priority=1)


def alert_breakeven(symbol: str = ""):
    title  = "↔️ Breakeven triggered"
    detail = f"SL moved to entry{' — ' + symbol if symbol else ''}"
    _pushover(title, detail, priority=2)


def alert_close():
    title  = "🔴 All trades closed"
    detail = "Close instruction received — all bot trades closed"
    _pushover(title, detail, priority=2)
