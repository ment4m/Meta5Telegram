"""
Run this to verify the signal parser handles your channel's messages correctly.
Usage:  python test_parser.py
"""
from signal_parser import parse_signal

SAMPLE = """
RiskY traDE ☠️
👉 sell XAUUSD now
🔴 SL 4869
✅ TP 4853
✅ TP 4848
✅ TP 4843
✅ TP 4838
✅ TP 4833
✅ TP 4828
✅ TP 4823
✅ TP 4818
✅ TP 4813
✅ TP 4808
✅ TP open
"""

result = parse_signal(SAMPLE)

if result:
    print(f"Direction : {result['direction'].upper()}")
    print(f"Symbol    : {result['symbol']}")
    print(f"Stop Loss : {result['sl']}")
    print(f"Entry     : {result['entry'] or 'MARKET ORDER'}")
    print(f"Num trades: {result['num_trades']}")
    print("Take Profits:")
    for i, tp in enumerate(result["tps"], 1):
        print(f"  TP{i}: {tp if tp is not None else 'OPEN (no TP)'}")
else:
    print("❌ Parser returned None — message not recognised as a signal")
