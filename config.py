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

# Second MT5 path for compound EA (signals_compound folder alongside main signals folder)
_compound_default = str(MT5_FILES_PATH).replace("/signals", "/signals_compound")
MT5_FILES_PATH_COMPOUND = os.getenv("MT5_FILES_PATH_COMPOUND", _compound_default)
COMPOUND_ENABLED = os.getenv("COMPOUND_ENABLED", "true").lower() == "true"

# Claude AI
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# Trading
DEVIATION = int(os.getenv("DEVIATION", "20"))
MAGIC_NUMBER = int(os.getenv("MAGIC_NUMBER", "234000"))
LOT_BALANCE_DIVISOR = int(os.getenv("LOT_BALANCE_DIVISOR", "4"))
TP_STEP_DEFAULT = float(os.getenv("TP_STEP_DEFAULT", "5.0"))  # fallback step
TP_STEP_MAP = {
    "XAUUSD": float(os.getenv("TP_STEP_XAUUSD", "5.0")),
    "BTCUSD": float(os.getenv("TP_STEP_BTCUSD", "500.0")),
    "EURUSD": float(os.getenv("TP_STEP_EURUSD", "0.0005")),
    "GBPUSD": float(os.getenv("TP_STEP_GBPUSD", "0.0005")),
    "USDJPY": float(os.getenv("TP_STEP_USDJPY", "0.05")),
    "ETHUSD": float(os.getenv("TP_STEP_ETHUSD", "25.0")),
}
MAX_TRADES = int(os.getenv("MAX_TRADES", "6"))  # max trades to open per signal

# Price value of 1 pip per symbol (used to convert "every N pips" to price step)
PIP_VALUE_MAP = {
    "XAUUSD": 0.10,    # 1 pip = $0.10
    "BTCUSD": 1.0,     # 1 pip = $1
    "EURUSD": 0.0001,
    "GBPUSD": 0.0001,
    "USDJPY": 0.01,
    "ETHUSD": 0.10,
}
