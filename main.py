import asyncio
import os
import signal
import subprocess
from pathlib import Path
import config
from telegram_listener import start
from logger import get_logger

log = get_logger("main")


def cleanup_session():
    """Kill any other running instances and remove session lock files."""
    current_pid = os.getpid()
    try:
        result = subprocess.run(
            ["pgrep", "-f", "python.*main.py"],
            capture_output=True, text=True
        )
        for pid_str in result.stdout.strip().splitlines():
            pid = int(pid_str)
            if pid != current_pid:
                os.kill(pid, signal.SIGKILL)
                log.info("Killed old instance (pid=%d)", pid)
    except Exception:
        pass
    # Remove SQLite lock files
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
