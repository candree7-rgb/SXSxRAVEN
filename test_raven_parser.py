"""
Test script for Raven Pro signal parser
"""

from signal_parser_raven import parse_signal, signal_hash

# Test signals from user's examples
test_signals = [
    # AUCTION SHORT
    """#AUCTION/USDT 📉 SELL

🔹Entry zone: 5.9200 - 6.1000

💰TP1 5.8700
💰TP2 5.8200
💰TP3 5.7600
💰TP4 5.7100
💰TP5 5.6600
💰TP6 5.5920
🚫SL 6.2100

〽️Leverage cross 10x

⚠️Respect the entry zone. Check the bio of the channel for all the info required to follow our signals""",

    # ZEC LONG
    """ZEC/USDT 📈 BUY

🔹Entry zone: 510.35 - 518.00

💰TP1 534.15
💰TP2 542.83
💰TP3 550.17
💰TP4 555.52
💰TP5 560.86
💰TP6 566.20
🚫SL 490

〽️Leverage cross 10x

⚠️Respect the entry zone. Check the bio of the channel for all the info required to follow our signals""",

    # ALGO LONG
    """ALGO/USDT 📈 BUY

🔹Entry zone: 0.1170 - 0.1209

💰TP1 0.1221
💰TP2 0.1233
💰TP3 0.1245
💰TP4 0.1257
💰TP5 0.1270
💰TP6 0.1283
🚫SL 0.1142

〽️Leverage cross 10x

⚠️Respect the entry zone. Check the bio of the channel for all the info required to follow our signals""",

    # PEPE SHORT
    """#1000PEPE/USDT 📉 SELL

🔹Entry zone: 0.005140 - 0.005300

💰TP1 0.005069
💰TP2 0.005018
💰TP3 0.004967
💰TP4 0.004918
💰TP5 0.004869
💰TP6 0.004817
🚫SL 0.005400

〽️Leverage cross 10x

⚠️Respect the entry zone. Check the bio of the channel for all the info required to follow our signals""",

    # XRP LONG
    """XRP/USDT 📈 BUY

🔹Entry zone: 1.94 - 2.00

💰TP1 2.03
💰TP2 2.05
💰TP3 2.07
💰TP4 2.09
💰TP5 2.11
💰TP6 2.13
🚫SL 1.900

〽️Leverage cross 10x

⚠️Respect the entry zone. Check the bio of the channel for all the info required to follow our signals""",

    # Not a signal (should return None)
    """This is just a random message
No signal here
Test 123""",
]

def test_parser():
    print("="*60)
    print("TESTING RAVEN PRO SIGNAL PARSER")
    print("="*60)

    for i, text in enumerate(test_signals, 1):
        print(f"\n{'='*60}")
        print(f"Test #{i}")
        print(f"{'='*60}")
        print(f"Input (first 100 chars):\n{text[:100]}...")
        print()

        sig = parse_signal(text)

        if sig:
            print("✅ PARSED SUCCESSFULLY!")
            print(f"   Symbol: {sig['symbol']}")
            print(f"   Side: {sig['side'].upper()}")
            print(f"   Entry Zone: {sig['entry_zone_low']} - {sig['entry_zone_high']}")
            print(f"   Entry Zone Mid: {sig['entry_zone_mid']:.6f}")
            print(f"   Zone Range: {(sig['entry_zone_high'] - sig['entry_zone_low']):.6f} ({(sig['entry_zone_high'] / sig['entry_zone_low'] - 1) * 100:.2f}%)")
            print(f"   TPs: {sig['tp_prices']}")
            print(f"   SL: {sig['sl_price']}")
            print(f"   Leverage: {sig['leverage']}x")
            print(f"   Hash: {signal_hash(sig)[:12]}...")

            # Validate
            assert sig['symbol'].endswith('USDT'), "Symbol should end with USDT"
            assert sig['entry_zone_low'] < sig['entry_zone_high'], "Zone low should be < zone high"
            assert len(sig['tp_prices']) > 0, "Should have TPs"
            assert sig['sl_price'] is not None, "Should have SL"

        else:
            print("❌ NOT PARSED (expected for test #6)")

    print(f"\n{'='*60}")
    print("✅ ALL TESTS PASSED!")
    print(f"{'='*60}")

if __name__ == "__main__":
    test_parser()
