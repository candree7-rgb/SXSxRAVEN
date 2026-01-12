#!/usr/bin/env python3
"""
Quick test script to verify Telegram Bot configuration.
"""

import sys
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Import config
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

print("=" * 60)
print("Telegram Bot Configuration Test")
print("=" * 60)

# Check if variables are set
print(f"\n1. Environment Variables:")
print(f"   TELEGRAM_BOT_TOKEN: {'✅ SET' if TELEGRAM_BOT_TOKEN else '❌ NOT SET'}")
print(f"   TELEGRAM_CHAT_ID: {'✅ SET' if TELEGRAM_CHAT_ID else '❌ NOT SET'}")

if TELEGRAM_BOT_TOKEN:
    token_preview = TELEGRAM_BOT_TOKEN[:10] + "..." + TELEGRAM_BOT_TOKEN[-10:]
    print(f"   Token preview: {token_preview}")

if TELEGRAM_CHAT_ID:
    print(f"   Chat ID: {TELEGRAM_CHAT_ID}")

# Try to import telegram_alerts
print(f"\n2. Module Import:")
try:
    import telegram_alerts
    print(f"   telegram_alerts: ✅ Imported")
    print(f"   is_enabled(): {telegram_alerts.is_enabled()}")
except Exception as e:
    print(f"   telegram_alerts: ❌ Import failed: {e}")
    sys.exit(1)

# Try to send a test message
print(f"\n3. Test Message:")
if telegram_alerts.is_enabled():
    success = telegram_alerts.send_message(
        "🧪 <b>Bot Test</b>\n\n"
        "This is a test message from your Raven Trading Bot.\n\n"
        "If you see this, Telegram alerts are working! ✅"
    )

    if success:
        print(f"   ✅ Test message sent successfully!")
        print(f"   Check your Telegram for the message.")
    else:
        print(f"   ❌ Failed to send message (check logs above)")
else:
    print(f"   ⏭️  Skipped (bot not configured)")

print(f"\n" + "=" * 60)
