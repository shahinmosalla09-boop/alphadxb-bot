import requests
import schedule
import time
import os
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.patches import FancyArrowPatch
from datetime import datetime, timedelta
from io import BytesIO

# ============================================
# تنظیمات
# ============================================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
PUBLIC_CHANNEL = "@AlphaDXBcrypto"
VIP_CHANNEL = "@AlphaDXBcryptoPRO"

# ============================================
# ارسال پیام
# ============================================
def send_message(channel, text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": channel, "text": text, "parse_mode": "HTML"}
    try:
        r = requests.post(url, json=payload, timeout=10)
        print(f"✅ Message sent to {channel}: {r.status_code}")
    except Exception as e:
        print(f"❌ Error sending message: {e}")

# ============================================
# ارسال عکس
# ============================================
def send_photo(channel, photo_bytes, caption=""):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
    try:
        r = requests.post(url, data={"chat_id": channel, "caption": caption, "parse_mode": "HTML"},
                         files={"photo": ("chart.png", photo_bytes, "image/png")}, timeout=30)
        print(f"✅ Chart sent to {channel}: {r.status_code}")
    except Exception as e:
        print(f"❌ Error sending chart: {e}")

# ============================================
# گرفتن قیمت‌ها
# ============================================
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

# ============================================
# گرفتن Fear & Greed
# ============================================
def get_fear_greed():
    try:
        r = requests.get("https://api.alternative.me/fng/", timeout=10)
        data = r.json()
        return int(data["data"][0]["value"]), data["data"][0]["value_classification"]
    except:
        return 50, "Neutral"

# ============================================
# گرفتن داده چارت از Bybit
# ============================================
def get_ohlc_data(symbol="BTCUSDT", interval="240", limit=60):
    try:
        url = "https://api.coingecko.com/api/v3/coins/bitcoin/ohlc"
        params = {"vs_currency": "usd", "days": "7"}
        r = requests.get(url, params=params, timeout=10)
        data = r.json()
        ohlc = []
        for c in data[-limit:]:
            ohlc.append({
                "time": datetime.fromtimestamp(c[0] / 1000),
                "open": float(c[1]),
                "high": float(c[2]),
                "low": float(c[3]),
                "close": float(c[4]),
                "volume": 0
            })
        return ohlc
    except Exception as e:
        print(f"❌ OHLC error: {e}")
        return None
# ============================================
# محاسبه Support و Resistance
# ============================================
def find_support_resistance(ohlc):
    highs = [c["high"] for c in ohlc]
    lows = [c["low"] for c in ohlc]
    closes = [c["close"] for c in ohlc]

    current_price = closes[-1]
    
    # پیدا کردن سطوح مهم
    resistance_levels = []
    support_levels = []
    
    for i in range(2, len(highs)-2):
        if highs[i] > highs[i-1] and highs[i] > highs[i-2] and highs[i] > highs[i+1] and highs[i] > highs[i+2]:
            resistance_levels.append(highs[i])
        if lows[i] < lows[i-1] and lows[i] < lows[i-2] and lows[i] < lows[i+1] and lows[i] < lows[i+2]:
            support_levels.append(lows[i])

    # فیلتر کردن نزدیک به قیمت فعلی
    resistance = [r for r in resistance_levels if r > current_price]
    support = [s for s in support_levels if s < current_price]

    # مهم‌ترین سطوح
    key_resistance = sorted(resistance)[:2] if resistance else [current_price * 1.03, current_price * 1.06]
    key_support = sorted(support, reverse=True)[:2] if support else [current_price * 0.97, current_price * 0.94]

    return key_support, key_resistance

# ============================================
# ساخت چارت
# ============================================
def create_chart(ohlc, supports, resistances, title="BTC/USDT - 4H"):
    plt.style.use('dark_background')
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 8), gridspec_kw={'height_ratios': [3, 1]})
    fig.patch.set_facecolor('#0d1117')
    ax1.set_facecolor('#0d1117')
    ax2.set_facecolor('#0d1117')

    times = [c["time"] for c in ohlc]
    opens = [c["open"] for c in ohlc]
    highs = [c["high"] for c in ohlc]
    lows = [c["low"] for c in ohlc]
    closes = [c["close"] for c in ohlc]
    volumes = [c["volume"] for c in ohlc]

    # رسم کندل‌ها
    for i, (t, o, h, l, c) in enumerate(zip(times, opens, highs, lows, closes)):
        color = '#26a69a' if c >= o else '#ef5350'
        ax1.plot([i, i], [l, h], color=color, linewidth=0.8, alpha=0.8)
        rect_height = abs(c - o)
        rect_bottom = min(o, c)
        ax1.bar(i, rect_height, bottom=rect_bottom, color=color, width=0.6, alpha=0.9)

    # Support lines
    for i, s in enumerate(supports):
        ax1.axhline(y=s, color='#00ff88', linewidth=1.5, linestyle='--', alpha=0.8)
        ax1.text(len(ohlc) - 1, s, f'  S{i+1}: ${s:,.0f}', color='#00ff88',
                fontsize=9, fontweight='bold', va='center')

    # Resistance lines
    for i, r in enumerate(resistances):
        ax1.axhline(y=r, color='#ff4444', linewidth=1.5, linestyle='--', alpha=0.8)
        ax1.text(len(ohlc) - 1, r, f'  R{i+1}: ${r:,.0f}', color='#ff4444',
                fontsize=9, fontweight='bold', va='center')

    # قیمت فعلی
    current = closes[-1]
    ax1.axhline(y=current, color='#FFD700', linewidth=1, linestyle='-', alpha=0.6)
    ax1.text(0, current, f'${current:,.0f}  ', color='#FFD700',
            fontsize=9, fontweight='bold', va='center', ha='right')

    # تنظیمات ax1
    ax1.set_title(f'🇦🇪 AlphaDXB | {title}', color='#FFD700', fontsize=14, fontweight='bold', pad=15)
    ax1.set_xlim(-1, len(ohlc) + 5)
    ax1.grid(color='#1e2d3d', linewidth=0.5, alpha=0.5)
    ax1.tick_params(colors='#8b949e', labelsize=8)
    ax1.spines['bottom'].set_color('#30363d')
    ax1.spines['left'].set_color('#30363d')
    ax1.spines['top'].set_visible(False)
    ax1.spines['right'].set_visible(False)
    ax1.set_xticks([])
    ax1.yaxis.set_label_position('right')
    ax1.yaxis.tick_right()

    # Volume bars
    for i, (t, c, o, v) in enumerate(zip(times, closes, opens, volumes)):
        color = '#26a69a' if c >= o else '#ef5350'
        ax2.bar(i, v, color=color, width=0.6, alpha=0.6)

    ax2.set_xlim(-1, len(ohlc) + 5)
    ax2.set_ylabel('Volume', color='#8b949e', fontsize=8)
    ax2.tick_params(colors='#8b949e', labelsize=7)
    ax2.spines['bottom'].set_color('#30363d')
    ax2.spines['left'].set_color('#30363d')
    ax2.spines['top'].set_visible(False)
    ax2.spines['right'].set_visible(False)
    ax2.set_xticks([])
    ax2.yaxis.set_label_position('right')
    ax2.yaxis.tick_right()
    ax2.grid(color='#1e2d3d', linewidth=0.5, alpha=0.5)

    # watermark
    fig.text(0.5, 0.5, 'AlphaDXB', fontsize=40, color='white',
            alpha=0.04, ha='center', va='center', fontweight='bold', rotation=30)

    plt.tight_layout(pad=2)

    buf = BytesIO()
    plt.savefig(buf, format='png', dpi=150, bbox_inches='tight',
               facecolor='#0d1117', edgecolor='none')
    buf.seek(0)
    plt.close()
    return buf.read()

# ============================================
# arrow قیمت
# ============================================
def price_arrow(change):
    return f"📈 +{change:.2f}%" if change >= 0 else f"📉 {change:.2f}%"

# ============================================
# تولید سناریو هوشمند
# ============================================
def generate_scenario(price, supports, resistances, fg_value):
    s1 = supports[0] if supports else price * 0.97
    r1 = resistances[0] if resistances else price * 1.03

    if fg_value < 30:
        bias = "⚠️ Extreme Fear — possible bounce"
        scenario = f"🟢 IF holds above ${s1:,.0f} → target ${r1:,.0f}\n🔴 IF breaks ${s1:,.0f} → next support ${supports[1]:,.0f}" if len(supports) > 1 else f"🟢 IF holds above ${s1:,.0f} → target ${r1:,.0f}"
    elif fg_value > 70:
        bias = "⚠️ Greed zone — watch for pullback"
        scenario = f"🟢 IF breaks ${r1:,.0f} → momentum to ${resistances[1]:,.0f}\n🔴 IF rejects ${r1:,.0f} → pullback to ${s1:,.0f}" if len(resistances) > 1 else f"🟢 IF breaks ${r1:,.0f} → strong move up\n🔴 IF rejects → pullback to ${s1:,.0f}"
    else:
        bias = "📊 Neutral — wait for breakout"
        scenario = f"🟢 Break above ${r1:,.0f} → Bullish\n🔴 Break below ${s1:,.0f} → Bearish"

    return bias, scenario

# ============================================
# پست صبحگاهی با چارت
# ============================================
def morning_update():
    print(f"\n🌅 [{datetime.now().strftime('%H:%M')}] Morning update...")
    prices = get_prices()
    fg_value, fg_label = get_fear_greed()

    if not prices:
        return

    date_str = datetime.now().strftime("%b %d, %Y")
    btc_price = prices['BTC']['price']

    # ساخت چارت
    ohlc = get_ohlc_data("BTCUSDT", "240", 60)
    if ohlc:
        supports, resistances = find_support_resistance(ohlc)
        chart = create_chart(ohlc, supports, resistances, "BTC/USDT - 4H")
        bias, scenario = generate_scenario(btc_price, supports, resistances, fg_value)

        caption = f"""🌅 <b>GM! AlphaDXB Morning Update</b>
📅 {date_str}

💰 <b>BTC:</b> ${prices['BTC']['price']:,.0f} {price_arrow(prices['BTC']['change'])}
💰 <b>ETH:</b> ${prices['ETH']['price']:,.0f} {price_arrow(prices['ETH']['change'])}
💰 <b>BNB:</b> ${prices['BNB']['price']:,.0f} {price_arrow(prices['BNB']['change'])}
💰 <b>SOL:</b> ${prices['SOL']['price']:,.2f} {price_arrow(prices['SOL']['change'])}

😱 <b>Sentiment:</b> {fg_label.upper()} ({fg_value})

📊 <b>BTC Analysis (4H):</b>
🔴 Resistance: ${resistances[0]:,.0f}
🟢 Support: ${supports[0]:,.0f}
⚡ Bias: {bias}

🎯 <b>Scenarios:</b>
{scenario}

💎 Full signals 👇
{VIP_CHANNEL}

#crypto #bitcoin #dubai #AlphaDXB"""

        send_photo(PUBLIC_CHANNEL, chart, caption)
    else:
        # اگه چارت نشد، فقط text بفرست
        msg = f"""🌅 <b>GM! AlphaDXB Morning Update</b>
📅 {date_str}

💰 <b>BTC:</b> ${prices['BTC']['price']:,.0f} {price_arrow(prices['BTC']['change'])}
💰 <b>ETH:</b> ${prices['ETH']['price']:,.0f} {price_arrow(prices['ETH']['change'])}
💰 <b>BNB:</b> ${prices['BNB']['price']:,.0f} {price_arrow(prices['BNB']['change'])}
💰 <b>SOL:</b> ${prices['SOL']['price']:,.2f} {price_arrow(prices['SOL']['change'])}

😱 <b>Sentiment:</b> {fg_label.upper()} ({fg_value})

💎 Full signals 👇
{VIP_CHANNEL}

#crypto #bitcoin #dubai #AlphaDXB"""
        send_message(PUBLIC_CHANNEL, msg)

# ============================================
# پست شبانه
# ============================================
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

# ============================================
# سیگنال VIP
# ============================================
def send_vip_signal(coin, direction, entry, target1, target2, stop_loss, analysis=""):
    emoji = "📈" if direction == "LONG" else "📉"
    msg = f"""🚨 <b>VIP SIGNAL — AlphaDXB Pro</b>

📌 <b>Coin:</b> {coin}
{emoji} <b>Direction:</b> {direction}
💵 <b>Entry:</b> ${entry:,}
🎯 <b>Target 1:</b> ${target1:,}
🎯 <b>Target 2:</b> ${target2:,}
🛑 <b>Stop Loss:</b> ${stop_loss:,}

⚡ <b>Source:</b> Bybit Top Trader
{f'📝 {analysis}' if analysis else ''}

⚠️ <i>DYOR — Not financial advice</i>
🇦🇪 AlphaDXB Pro"""

    send_message(VIP_CHANNEL, msg)

# ============================================
# اجرا
# ============================================
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
