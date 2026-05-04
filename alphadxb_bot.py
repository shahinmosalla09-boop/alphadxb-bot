import requests
import schedule
import time
import os
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from datetime import datetime
from io import BytesIO

TELEGRAM_TOKEN = "8774593158:AAEtqggo7ReinWqO9rkUw6v74jA9HCEJcA4"
print(f"TOKEN CHECK: {TELEGRAM_TOKEN[:20]}")
PUBLIC_CHANNEL = "@AlphaDXBcrypto"
VIP_CHANNEL = "@AlphaDXBcryptoPRO"

def send_message(channel, text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": channel, "text": text, "parse_mode": "HTML"}
    try:
        r = requests.post(url, json=payload, timeout=10)
        print(f"✅ Message: {r.status_code}")
    except Exception as e:
        print(f"❌ Message error: {e}")

def send_photo(channel, photo_bytes, caption=""):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
    try:
        r = requests.post(
            url,
            files={"photo": ("chart.png", BytesIO(photo_bytes), "image/png")},
            data={"chat_id": channel, "caption": caption, "parse_mode": "HTML"},
            timeout=60
        )
        print(f"✅ Photo: {r.status_code} {r.text[:200]}")
        if r.status_code != 200:
            send_message(channel, caption)
    except Exception as e:
        print(f"❌ Photo error: {e}")
        send_message(channel, caption)

def get_prices():
    apis = [
        "https://api.coinbase.com/v2/exchange-rates?currency=BTC",
    ]
    # روش اول - Coinbase
    try:
        r = requests.get("https://api.coinbase.com/v2/exchange-rates?currency=BTC", timeout=10)
        btc_usd = 1 / float(r.json()["data"]["rates"]["USD"]) * 1000000
        btc = float(r.json()["data"]["rates"]["USD"])
        btc = 1/btc * 100000000
        # ساده‌تر:
        rates = r.json()["data"]["rates"]
        btc_price = 1 / float(rates["BTC"]) if "BTC" in rates else None
        print(f"Coinbase raw: {list(rates.items())[:5]}")
    except Exception as e:
        print(f"Coinbase error: {e}")

    # روش دوم - Kraken
    try:
        symbols = {
            "BTC": "XXBTZUSD",
            "ETH": "XETHZUSD",
            "SOL": "SOLUSD",
            "BNB": "BNBUSD"
        }
        prices = {}
        for coin, pair in symbols.items():
            r = requests.get(f"https://api.kraken.com/0/public/Ticker?pair={pair}", timeout=10)
            data = r.json()
            if not data.get("error"):
                result = data["result"]
                key = list(result.keys())[0]
                price = float(result[key]["c"][0])
                prices[coin] = price
                print(f"✅ {coin}: ${price:,.2f}")
        if len(prices) >= 3:
            return prices
    except Exception as e:
        print(f"Kraken error: {e}")

    # روش سوم - Mexc
    try:
        pairs = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "BNB": "BNBUSDT", "SOL": "SOLUSDT"}
        prices = {}
        for coin, pair in pairs.items():
            r = requests.get(f"https://api.mexc.com/api/v3/ticker/price?symbol={pair}", timeout=10)
            prices[coin] = float(r.json()["price"])
            print(f"✅ MEXC {coin}: ${prices[coin]:,.2f}")
        if len(prices) >= 3:
            return prices
    except Exception as e:
        print(f"MEXC error: {e}")

    return None

def get_fear_greed():
    try:
        r = requests.get("https://api.alternative.me/fng/", timeout=10)
        d = r.json()
        return int(d["data"][0]["value"]), d["data"][0]["value_classification"]
    except:
        return 50, "Neutral"

def get_ohlc():
    try:
        r = requests.get("https://api.kraken.com/0/public/OHLC?pair=XBTUSD&interval=240", timeout=10)
        data = r.json()
        if not data.get("error"):
            result = data["result"]
            key = [k for k in result.keys() if k != "last"][0]
            candles = result[key]
            ohlc = []
            for c in candles[-60:]:
                ohlc.append({
                    "time": datetime.fromtimestamp(c[0]),
                    "open": float(c[1]),
                    "high": float(c[2]),
                    "low": float(c[3]),
                    "close": float(c[4]),
                    "volume": float(c[6])
                })
            print(f"✅ Got {len(ohlc)} candles from Kraken")
            return ohlc
    except Exception as e:
        print(f"OHLC error: {e}")
    return None

def find_levels(ohlc):
    highs = [c["high"] for c in ohlc]
    lows = [c["low"] for c in ohlc]
    current = ohlc[-1]["close"]
    resistance, support = [], []
    for i in range(2, len(highs)-2):
        if highs[i] > highs[i-1] and highs[i] > highs[i-2] and highs[i] > highs[i+1] and highs[i] > highs[i+2]:
            if highs[i] > current:
                resistance.append(highs[i])
        if lows[i] < lows[i-1] and lows[i] < lows[i-2] and lows[i] < lows[i+1] and lows[i] < lows[i+2]:
            if lows[i] < current:
                support.append(lows[i])
    r = sorted(resistance)[:2] if resistance else [current*1.03, current*1.06]
    s = sorted(support, reverse=True)[:2] if support else [current*0.97, current*0.94]
    return s, r

def create_chart(ohlc, supports, resistances):
    plt.style.use('dark_background')
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 8), gridspec_kw={'height_ratios': [3, 1]})
    fig.patch.set_facecolor('#0d1117')
    ax1.set_facecolor('#0d1117')
    ax2.set_facecolor('#0d1117')
    closes = [c["close"] for c in ohlc]
    opens = [c["open"] for c in ohlc]
    highs = [c["high"] for c in ohlc]
    lows = [c["low"] for c in ohlc]
    volumes = [c["volume"] for c in ohlc]
    for i in range(len(ohlc)):
        color = '#26a69a' if closes[i] >= opens[i] else '#ef5350'
        ax1.plot([i, i], [lows[i], highs[i]], color=color, linewidth=0.8)
        ax1.bar(i, abs(closes[i]-opens[i]), bottom=min(opens[i],closes[i]), color=color, width=0.6, alpha=0.9)
    for i, s in enumerate(supports):
        ax1.axhline(y=s, color='#00ff88', linewidth=1.5, linestyle='--', alpha=0.8)
        ax1.text(len(ohlc)+1, s, f'S{i+1}: ${s:,.0f}', color='#00ff88', fontsize=8, fontweight='bold', va='center')
    for i, r in enumerate(resistances):
        ax1.axhline(y=r, color='#ff4444', linewidth=1.5, linestyle='--', alpha=0.8)
        ax1.text(len(ohlc)+1, r, f'R{i+1}: ${r:,.0f}', color='#ff4444', fontsize=8, fontweight='bold', va='center')
    ax1.set_title('AlphaDXB | BTC/USDT - 4H', color='#FFD700', fontsize=14, fontweight='bold', pad=15)
    ax1.set_xlim(-1, len(ohlc)+10)
    ax1.grid(color='#1e2d3d', linewidth=0.5, alpha=0.5)
    ax1.tick_params(colors='#8b949e', labelsize=8)
    ax1.set_xticks([])
    ax1.yaxis.tick_right()
    for s in ['top','left']: ax1.spines[s].set_visible(False)
    for s in ['bottom','right']: ax1.spines[s].set_color('#30363d')
    for i in range(len(ohlc)):
        color = '#26a69a' if closes[i] >= opens[i] else '#ef5350'
        ax2.bar(i, volumes[i], color=color, width=0.6, alpha=0.6)
    ax2.set_xlim(-1, len(ohlc)+10)
    ax2.tick_params(colors='#8b949e', labelsize=7)
    ax2.set_xticks([])
    ax2.yaxis.tick_right()
    ax2.grid(color='#1e2d3d', linewidth=0.5, alpha=0.5)
    for s in ['top','left']: ax2.spines[s].set_visible(False)
    for s in ['bottom','right']: ax2.spines[s].set_color('#30363d')
    fig.text(0.5, 0.5, 'AlphaDXB', fontsize=40, color='white', alpha=0.04, ha='center', va='center', fontweight='bold', rotation=30)
    plt.tight_layout(pad=2)
    buf = BytesIO()
    plt.savefig(buf, format='png', dpi=150, bbox_inches='tight', facecolor='#0d1117')
    buf.seek(0)
    plt.close()
    return buf.read()

def price_arrow(change):
    return f"📈 +{change:.2f}%" if change >= 0 else f"📉 {change:.2f}%"

def morning_update():
    print(f"\n[{datetime.now().strftime('%H:%M')}] Morning update...")
    prices = get_prices()
    fg_val, fg_label = get_fear_greed()
    if not prices:
        print("❌ Could not get prices")
        send_message(PUBLIC_CHANNEL, "Good morning! Market update coming soon. Stay tuned!")
        return
    date_str = datetime.now().strftime("%b %d, %Y")
    btc = prices.get("BTC", 0)
    eth = prices.get("ETH", 0)
    bnb = prices.get("BNB", 0)
    sol = prices.get("SOL", 0)
    ohlc = get_ohlc()
    if ohlc:
        supports, resistances = find_levels(ohlc)
        chart = create_chart(ohlc, supports, resistances)
        if fg_val < 30:
            bias = "Extreme Fear - possible bounce"
            scenario = f"If holds above ${supports[0]:,.0f} -> target ${resistances[0]:,.0f}\nIf breaks ${supports[0]:,.0f} -> next ${supports[1]:,.0f}" if len(supports)>1 else f"If holds above ${supports[0]:,.0f} -> target ${resistances[0]:,.0f}"
        elif fg_val > 70:
            bias = "Greed - watch for pullback"
            scenario = f"If breaks ${resistances[0]:,.0f} -> strong move up\nIf rejects -> pullback to ${supports[0]:,.0f}"
        else:
            bias = "Neutral - wait for breakout"
            scenario = f"Break above ${resistances[0]:,.0f} -> Bullish\nBreak below ${supports[0]:,.0f} -> Bearish"
        caption = f"""GM! AlphaDXB Morning Update
{date_str}

BTC: ${btc:,.0f}
ETH: ${eth:,.0f}
BNB: ${bnb:,.0f}
SOL: ${sol:,.2f}

Market Sentiment: {fg_label.upper()} ({fg_val})

BTC Analysis 4H:
Resistance: ${resistances[0]:,.0f}
Support: ${supports[0]:,.0f}
Bias: {bias}

Scenarios:
{scenario}

Full signals -> {VIP_CHANNEL}
#crypto #bitcoin #dubai #AlphaDXB"""
        send_photo(PUBLIC_CHANNEL, chart, caption)
    else:
        msg = f"""GM! AlphaDXB Morning Update
{date_str}

BTC: ${btc:,.0f}
ETH: ${eth:,.0f}
BNB: ${bnb:,.0f}
SOL: ${sol:,.2f}

Sentiment: {fg_label.upper()} ({fg_val})

Full signals -> {VIP_CHANNEL}
#crypto #bitcoin #dubai #AlphaDXB"""
        send_message(PUBLIC_CHANNEL, msg)

def evening_update():
    print(f"\n[{datetime.now().strftime('%H:%M')}] Evening update...")
    prices = get_prices()
    fg_val, fg_label = get_fear_greed()
    if not prices:
        return
    date_str = datetime.now().strftime("%b %d, %Y")
    btc = prices.get("BTC", 0)
    eth = prices.get("ETH", 0)
    bnb = prices.get("BNB", 0)
    sol = prices.get("SOL", 0)
    msg = f"""AlphaDXB Daily Wrap-Up
{date_str}

BTC: ${btc:,.0f}
ETH: ${eth:,.0f}
BNB: ${bnb:,.0f}
SOL: ${sol:,.2f}

Sentiment: {fg_label.upper()} ({fg_val})

Tomorrow's signals -> {VIP_CHANNEL}
#crypto #bitcoin #dubai #AlphaDXB"""
    send_message(PUBLIC_CHANNEL, msg)

if __name__ == "__main__":
    print("AlphaDXB Bot Starting...")
    morning_update()
    schedule.every().day.at("04:00").do(morning_update)
    schedule.every().day.at("16:00").do(evening_update)
    print("Bot is running!")
    while True:
        schedule.run_pending()
        time.sleep(30)
        print(f"{datetime.now().strftime('%H:%M:%S')} - Waiting...", end="\r")
