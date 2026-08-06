import asyncio
import os
import signal
import subprocess
import atexit
from pathlib import Path
import config
from telegram_listener import start
from logger import get_logger

log = get_logger("main")

_LOCK_FILE = Path(__file__).parent / "bot.lock"


def _acquire_lock():
    """Ensure only one instance runs. Exit immediately if another is running."""
    if _LOCK_FILE.exists():
        try:
            existing_pid = int(_LOCK_FILE.read_text().strip())
            # Check if that process is still alive
            os.kill(existing_pid, 0)
            log.error("Another instance is already running (pid=%d). Exiting.", existing_pid)
            raise SystemExit(1)
        except ProcessLookupError:
            # Stale lock — old process is gone
            log.info("Removing stale lock file (pid=%s)", _LOCK_FILE.read_text().strip())
            _LOCK_FILE.unlink()
    _LOCK_FILE.write_text(str(os.getpid()))
    atexit.register(_release_lock)
    log.info("Lock acquired (pid=%d)", os.getpid())


def _release_lock():
    if _LOCK_FILE.exists():
        _LOCK_FILE.unlink()


def cleanup_session():
    """Remove session lock files."""
    session = Path("tg_session.session")
    for suffix in ["-journal", "-wal", "-shm"]:
        lock = Path(str(session) + suffix)
        if lock.exists():
            lock.unlink()
            log.info("Removed lock file: %s", lock)


def validate_config():
    missing = []
    if not config.API_ID:
        missing.append("TELEGRAM_API_ID")
    if not config.API_HASH:
        missing.append("TELEGRAM_API_HASH")
    if not config.PHONE_NUMBER:
        missing.append("TELEGRAM_PHONE")
    if not config.CHANNEL_USERNAME:
        missing.append("CHANNEL_USERNAME")
    if missing:
        raise EnvironmentError(f"Missing required .env variables: {', '.join(missing)}")


if __name__ == "__main__":
    _acquire_lock()
    cleanup_session()
    validate_config()
    log.info("Starting Telegram → MT5 signal bot...")
    log.info("Signal files will be written to: %s", config.MT5_FILES_PATH)
    retry_delay = 5
    while True:
        try:
            asyncio.run(start())
            log.warning("Bot disconnected — reconnecting in %ds...", retry_delay)
        except KeyboardInterrupt:
            log.info("Bot stopped by user.")
            break
        except Exception as e:
            log.error("Bot crashed: %s — reconnecting in %ds...", e, retry_delay)
        import time as _time
        _time.sleep(retry_delay)
