import os
from dotenv import load_dotenv

load_dotenv()

# Telegram
API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
API_HASH = os.getenv("TELEGRAM_API_HASH", "")
PHONE_NUMBER = os.getenv("TELEGRAM_PHONE", "")
_channel = os.getenv("CHANNEL_USERNAME", "")
CHANNEL_USERNAME = int(_channel) if _channel.lstrip("-").isdigit() else _channel

# MT5
MT5_LOGIN = int(os.getenv("MT5_LOGIN", "0"))
MT5_PASSWORD = os.getenv("MT5_PASSWORD", "")
MT5_SERVER = os.getenv("MT5_SERVER", "")

# MT5 Files path (where EA reads signal JSON files)
MT5_FILES_PATH = os.getenv(
    "MT5_FILES_PATH",
    os.path.expanduser("~/Library/Application Support/net.metaquotes.wine.metatrader5/drive_c/users/user/AppData/Roaming/MetaQuotes/Terminal/D0E8209F77C8CF37AD8BF550E51FF075/MQL5/Files/signals")
)

# Claude AI
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# Trading
DEVIATION = int(os.getenv("DEVIATION", "20"))
MAGIC_NUMBER = int(os.getenv("MAGIC_NUMBER", "234000"))
LOT_BALANCE_DIVISOR = int(os.getenv("LOT_BALANCE_DIVISOR", "4"))
