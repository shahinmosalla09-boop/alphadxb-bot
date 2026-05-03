import requests
import schedule
import time
from datetime import datetime

TELEGRAM_TOKEN = "8774593158:AAEtqggo7ReinWqO9rkUw6v74jA9HCEJcA4"
PUBLIC_CHANNEL = "@AlphaDXBcrypto"
VIP_CHANNEL = "@AlphaDXBcryptoPRO"

def send_message(channel, text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": channel, "text": text, "parse_mode": "HTML"}
    try:
        r = requests.post(url, json=payload, timeout=10)
        print(f"✅ Sent to {channel}: {r.status_code}")
    except Exception as e:
        print(f"❌ Error: {e}")

def get_prices():
    try:
        url = "https://api.coingecko.com/api/v3/simple/price"
        params = {"ids": "bitcoin,ethereum,binancecoin,solana", "vs_currencies": "usd", "include_24hr_change": "true"}
        r = requests.get(url, params=params, timeout=10)
        data = r.json()
        return {
            "BTC": {"price": data["bitcoin"]["usd"], "change": data["bitcoin"]["usd_24h_change"]},
            "ETH": {"price": data["ethereum"]["usd"], "change": data["ethereum"]["usd_24h_change"]},
            "BNB": {"price": data["binancecoin"]["usd"], "change": data["binancecoin"]["usd_24h_change"]},
            "SOL": {"price": data["solana"]["usd"], "change": data["solana"]["usd_24h_change"]},
        }
    except Exception as e:
        print(f"❌ Price error: {e}")
        return None

def get_fear_greed():
    try:
        r = requests.get("https://api.alternative.me/fng/", timeout=10)
        data = r.json()
        return int(data["data"][0]["value"]), data["data"][0]["value_classification"]
    except:
        return 50, "Neutral"

def price_arrow(change):
    return f"📈 +{change:.2f}%" if change >= 0 else f"📉 {change:.2f}%"

def morning_update():
    print(f"\n🌅 [{datetime.now().strftime('%H:%M')}] Morning update...")
    prices = get_prices()
    fg_value, fg_label = get_fear_greed()
    if not prices:
        return
    date_str = datetime.now().strftime("%b %d, %Y")
    msg = f"""🌅 <b>GM! AlphaDXB Morning Update</b>
📅 {date_str}

💰 <b>BTC:</b> ${prices['BTC']['price']:,.0f} {price_arrow(prices['BTC']['change'])}
💰 <b>ETH:</b> ${prices['ETH']['price']:,.0f} {price_arrow(prices['ETH']['change'])}
💰 <b>BNB:</b> ${prices['BNB']['price']:,.0f} {price_arrow(prices['BNB']['change'])}
💰 <b>SOL:</b> ${prices['SOL']['price']:,.2f} {price_arrow(prices['SOL']['change'])}

😱 <b>Market Sentiment:</b> {fg_label.upper()} ({fg_value})

💎 Full signals with Entry & SL 👇
{VIP_CHANNEL}

#crypto #bitcoin #dubai #AlphaDXB"""
    send_message(PUBLIC_CHANNEL, msg)

def evening_update():
    print(f"\n🌙 [{datetime.now().strftime('%H:%M')}] Evening update...")
    prices = get_prices()
    fg_value, fg_label = get_fear_greed()
    if not prices:
        return
    date_str = datetime.now().strftime("%b %d, %Y")
    msg = f"""🌙 <b>AlphaDXB Daily Wrap-Up</b>
📅 {date_str}

💰 <b>BTC:</b> ${prices['BTC']['price']:,.0f} {price_arrow(prices['BTC']['change'])}
💰 <b>ETH:</b> ${prices['ETH']['price']:,.0f} {price_arrow(prices['ETH']['change'])}
💰 <b>BNB:</b> ${prices['BNB']['price']:,.0f} {price_arrow(prices['BNB']['change'])}
💰 <b>SOL:</b> ${prices['SOL']['price']:,.2f} {price_arrow(prices['SOL']['change'])}

😱 <b>Sentiment:</b> {fg_label.upper()} ({fg_value})

💎 Tomorrow's full signals in VIP 👇
{VIP_CHANNEL}

#crypto #bitcoin #dubai #AlphaDXB"""
    send_message(PUBLIC_CHANNEL, msg)

if __name__ == "__main__":
    print("🚀 AlphaDXB Bot Starting...")
    morning_update()
    schedule.every().day.at("08:00").do(morning_update)
    schedule.every().day.at("20:00").do(evening_update)
    print("\n✅ Bot is running!")
    print("⚠️  Don't close this window!")
    while True:
        schedule.run_pending()
        time.sleep(30)
        print(f"⏰ {datetime.now().strftime('%H:%M:%S')} - Waiting...", end="\r")