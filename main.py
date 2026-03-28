import asyncio
import config
from telegram_listener import start
from logger import get_logger

log = get_logger("main")


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
    validate_config()
    log.info("Starting Telegram → MT5 signal bot...")
    log.info("Signal files will be written to: %s", config.MT5_FILES_PATH)
    try:
        asyncio.run(start())
    except KeyboardInterrupt:
        log.info("Bot stopped by user.")
