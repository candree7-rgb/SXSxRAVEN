"""
Send signal to webhook for testing
"""
import requests

signal = """FET/USDT 📈 BUY

🔹Entry zone: 0.2830 - 0.2910

💰TP1 0.3022
💰TP2 0.3082
💰TP3 0.3112
💰TP4 0.3144
💰TP5 0.3176
💰TP6 0.3219
🚫SL 0.2720

〽️Leverage cross 10x

⚠️Respect the entry zone. Check the bio of the channel for all the info required to follow our signals

Recommended Exchange: BingX
Register Now (https://bingx.com/partner/RavenPro)"""

url = "https://sxsxraven-production.up.railway.app/webhook"

response = requests.post(url, data=signal, headers={"Content-Type": "text/plain"})

print(f"Status: {response.status_code}")
print(f"Response: {response.text}")
