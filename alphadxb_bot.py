import os
import sys
import time
import threading
from datetime import datetime
from io import BytesIO

import requests
import schedule
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

# Auto-load values from a ".env" file in the same folder, if it exists.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


# ---------- DEBUG: print which env vars Python can see ----------
print("=" * 60)
print("ENV VAR DEBUG START")
print("=" * 60)
expected = ["TELEGRAM_TOKEN", "ADMIN_BOT_TOKEN", "ADMIN_CHAT_ID", "PUBLIC_CHANNEL", "VIP_CHANNEL"]
for name in expected:
    raw = os.environ.get(name)
    if raw is None:
        print(f"  {name}: <NOT FOUND>")
    elif raw == "":
        print(f"  {name}: <EMPTY STRING>")
    else:
        print(f"  {name}: <SET, length={len(raw)}>")
print("---")
print("All env vars currently visible to Python (names only):")
all_names = sorted(os.environ.keys())
print("  " + ", ".join(all_names))
print("=" * 60)
print("ENV VAR DEBUG END")
print("=" * 60)
sys.stdout.flush()


# ---------- Config (loaded from environment / .env file) ----------

def _require_env(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        print(f"FATAL: environment variable {name} is not set.", file=sys.stderr)
        sys.exit(1)
    return val


def _require_int_env(name: str) -> int:
    raw = _require_env(name)
    try:
        return int(raw)
    except ValueError:
        print(f"FATAL: environment variable {name} must be an integer, got: {raw!r}", file=sys.stderr)
        sys.exit(1)


TELEGRAM_TOKEN   = _require_env("TELEGRAM_TOKEN")       # main bot — posts to channels
ADMIN_BOT_TOKEN  = _require_env("ADMIN_BOT_TOKEN")      # admin bot — receives admin commands
ADMIN_CHAT_ID    = _require_int_env("ADMIN_CHAT_ID")    # numeric chat id of the admin
PUBLIC_CHANNEL   = _require_env("PUBLIC_CHANNEL")       # e.g. @AlphaDXBcrypto
VIP_CHANNEL      = _require_env("VIP_CHANNEL")          # e.g. @AlphaDXBcryptoPRO


# ---------- Telegram helpers ----------

def send_message(token: str, channel, text: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": channel, "text": text, "parse_mode": "HTML"}
    try:
        r = requests.post(url, json=payload, timeout=10)
        print(f"✅ Message: {r.status_code}")
    except Exception as e:
        print(f"❌ Error: {e}")


def send_photo(channel, photo_bytes: bytes, caption: str = "") -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
    try:
        r = requests.post(
            url,
            files={"photo": ("chart.png", BytesIO(photo_bytes), "image/png")},
            data={"chat_id": channel, "caption": caption, "parse_mode": "HTML"},
            timeout=60,
        )
        print(f"✅ Photo: {r.status_code}")
        if r.status_code != 200:
            send_message(TELEGRAM_TOKEN, channel, caption)
    except Exception as e:
        print(f"❌ Photo error: {e}")
        send_message(TELEGRAM_TOKEN, channel, caption)


# ---------- Market data ----------

def get_prices():
    try:
        symbols = {"BTC": "XXBTZUSD", "ETH": "XETHZUSD", "SOL": "SOLUSD", "BNB": "BNBUSD"}
        prices = {}
        for coin, pair in symbols.items():
            r = requests.get(f"https://api.kraken.com/0/public/Ticker?pair={pair}", timeout=10)
            data = r.json()
            if not data.get("error"):
                result = data["result"]
                key = list(result.keys())[0]
                prices[coin] = float(result[key]["c"][0])
        return prices if len(prices) >= 3 else None
    except Exception as e:
        print(f"❌ Price error: {e}")
        return None


def get_fear_greed():
    try:
        r = requests.get("https://api.alternative.me/fng/", timeout=10)
        d = r.json()
        return int(d["data"][0]["value"]), d["data"][0]["value_classification"]
    except Exception:
        return 50, "Neutral"


def get_ohlc():
    try:
        r = requests.get("https://api.kraken.com/0/public/OHLC?pair=XBTUSD&interval=240", timeout=10)
        data = r.json()
        if not data.get("error"):
            result = data["result"]
            key = [k for k in result.keys() if k != "last"][0]
            ohlc = []
            for c in result[key][-60:]:
                ohlc.append({
                    "time": datetime.fromtimestamp(c[0]),
                    "open": float(c[1]),
                    "high": float(c[2]),
                    "low": float(c[3]),
                    "close": float(c[4]),
                    "volume": float(c[6]),
                })
            return ohlc
    except Exception as e:
        print(f"❌ OHLC error: {e}")
    return None


# ---------- Analysis ----------

def find_levels(ohlc):
    highs = [c["high"] for c in ohlc]
    lows = [c["low"] for c in ohlc]
    current = ohlc[-1]["close"]
    resistance, support = [], []
    for i in range(2, len(highs) - 2):
        if highs[i] > highs[i - 1] and highs[i] > highs[i - 2] and highs[i] > highs[i + 1] and highs[i] > highs[i + 2]:
            if highs[i] > current:
                resistance.append(highs[i])
        if lows[i] < lows[i - 1] and lows[i] < lows[i - 2] and lows[i] < lows[i + 1] and lows[i] < lows[i + 2]:
            if lows[i] < current:
                support.append(lows[i])
    r = sorted(resistance)[:2] if resistance else [current * 1.03, current * 1.06]
    s = sorted(support, reverse=True)[:2] if support else [current * 0.97, current * 0.94]
    return s, r


def create_chart(ohlc, supports, resistances) -> bytes:
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
        ax1.bar(i, abs(closes[i] - opens[i]), bottom=min(opens[i], closes[i]), color=color, width=0.6, alpha=0.9)
    for i, s in enumerate(supports):
        ax1.axhline(y=s, color='#00ff88', linewidth=1.5, linestyle='--', alpha=0.8)
        ax1.text(len(ohlc) + 1, s, f'S{i+1}: ${s:,.0f}', color='#00ff88', fontsize=8, fontweight='bold', va='center')
    for i, r in enumerate(resistances):
        ax1.axhline(y=r, color='#ff4444', linewidth=1.5, linestyle='--', alpha=0.8)
        ax1.text(len(ohlc) + 1, r, f'R{i+1}: ${r:,.0f}', color='#ff4444', fontsize=8, fontweight='bold', va='center')
    ax1.set_title('AlphaDXB | BTC/USDT - 4H', color='#FFD700', fontsize=14, fontweight='bold', pad=15)
    ax1.set_xlim(-1, len(ohlc) + 10)
    ax1.grid(color='#1e2d3d', linewidth=0.5, alpha=0.5)
    ax1.tick_params(colors='#8b949e', labelsize=8)
    ax1.set_xticks([])
    ax1.yaxis.tick_right()
    for s in ['top', 'left']:
        ax1.spines[s].set_visible(False)
    for s in ['bottom', 'right']:
        ax1.spines[s].set_color('#30363d')
    for i in range(len(ohlc)):
        color = '#26a69a' if closes[i] >= opens[i] else '#ef5350'
        ax2.bar(i, volumes[i], color=color, width=0.6, alpha=0.6)
    ax2.set_xlim(-1, len(ohlc) + 10)
    ax2.tick_params(colors='#8b949e', labelsize=7)
    ax2.set_xticks([])
    ax2.yaxis.tick_right()
    ax2.grid(color='#1e2d3d', linewidth=0.5, alpha=0.5)
    for s in ['top', 'left']:
        ax2.spines[s].set_visible(False)
    for s in ['bottom', 'right']:
        ax2.spines[s].set_color('#30363d')
    fig.text(0.5, 0.5, 'AlphaDXB', fontsize=40, color='white', alpha=0.04,
             ha='center', va='center', fontweight='bold', rotation=30)
    plt.tight_layout(pad=2)
    buf = BytesIO()
    plt.savefig(buf, format='png', dpi=150, bbox_inches='tight', facecolor='#0d1117')
    buf.seek(0)
    plt.close()
    return buf.read()


# ---------- Scheduled posts ----------

def morning_update():
    print(f"\n[{datetime.now().strftime('%H:%M')}] Morning update...")
    prices = get_prices()
    fg_val, fg_label = get_fear_greed()
    if not prices:
        return
    date_str = datetime.now().strftime("%b %d, %Y")
    btc = prices.get("BTC", 0)
    eth = prices.get("ETH", 0)
    bnb = prices.get("BNB", 0)
    sol = prices.get("SOL", 0)
    ohlc = get_ohlc()
    if ohlc:
        supports, resistances = find_levels(ohlc)
        try:
            chart = create_chart(ohlc, supports, resistances)
        except Exception as e:
            print(f"❌ Chart error: {e}")
            chart = None
        if fg_val < 30:
            bias = "Extreme Fear - possible bounce"
            scenario = (
                f"If holds above ${supports[0]:,.0f} -> target ${resistances[0]:,.0f}\n"
                f"If breaks ${supports[0]:,.0f} -> next ${supports[1] if len(supports) > 1 else supports[0]*0.97:,.0f}"
            )
        elif fg_val > 70:
            bias = "Greed - watch for pullback"
            scenario = (
                f"If breaks ${resistances[0]:,.0f} -> strong move up\n"
                f"If rejects -> pullback to ${supports[0]:,.0f}"
            )
        else:
            bias = "Neutral - wait for breakout"
            scenario = (
                f"Break above ${resistances[0]:,.0f} -> Bullish\n"
                f"Break below ${supports[0]:,.0f} -> Bearish"
            )
        caption = f"""GM! AlphaDXB Morning Update
{date_str}
BTC: ${btc:,.0f}
ETH: ${eth:,.0f}
BNB: ${bnb:,.0f}
SOL: ${sol:,.2f}
Sentiment: {fg_label.upper()} ({fg_val})
BTC 4H Analysis:
Resistance: ${resistances[0]:,.0f}
Support: ${supports[0]:,.0f}
Bias: {bias}
Scenarios:
{scenario}
Full signals -> {VIP_CHANNEL}
#crypto #bitcoin #dubai #AlphaDXB"""
        if chart:
            send_photo(PUBLIC_CHANNEL, chart, caption)
        else:
            send_message(TELEGRAM_TOKEN, PUBLIC_CHANNEL, caption)
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
        send_message(TELEGRAM_TOKEN, PUBLIC_CHANNEL, msg)


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
    send_message(TELEGRAM_TOKEN, PUBLIC_CHANNEL, msg)


# ---------- Admin bot ----------

def format_analysis(text: str) -> str:
    lines = [l.strip() for l in text.strip().split('\n') if l.strip()]
    coins = [l for l in lines if l.startswith('#')]
    analysis = [l for l in lines if not l.startswith('#')]
    coin_str = ' '.join(coins) if coins else '#crypto'
    analysis_str = '\n'.join(analysis)
    return f"""📊 <b>AlphaDXB Market Analysis</b>
{analysis_str}
🇦🇪 AlphaDXB | Dubai Crypto Signals
{coin_str} #dubai #AlphaDXB"""


def process_update(update):
    msg = update.get("message", {})
    chat_id = msg.get("chat", {}).get("id")
    if chat_id != ADMIN_CHAT_ID:
        return
    text = msg.get("text", "") or ""
    caption = msg.get("caption", "") or ""
    photo = msg.get("photo")
    forward_from_chat = msg.get("forward_from_chat")
    forward_message_id = msg.get("forward_from_message_id")
    file_id = photo[-1]["file_id"] if photo else None
    target = PUBLIC_CHANNEL
    content = caption if photo else text
    if content.startswith("/vip"):
        target = VIP_CHANNEL
        content = content.replace("/vip", "", 1).strip()
    if content.startswith("/start"):
        send_message(ADMIN_BOT_TOKEN, ADMIN_CHAT_ID,
                     "✅ Admin Bot ready!\n\nSend or forward any post.\nAdd /vip at start for VIP channel.")
        return
    if file_id:
        try:
            file_url = f"https://api.telegram.org/bot{ADMIN_BOT_TOKEN}/getFile?file_id={file_id}"
            r = requests.get(file_url, timeout=10)
            file_path = r.json()["result"]["file_path"]
            photo_r = requests.get(
                f"https://api.telegram.org/file/bot{ADMIN_BOT_TOKEN}/{file_path}", timeout=30
            )
            photo_bytes = photo_r.content
            formatted = format_analysis(content) if content else (
                "📊 AlphaDXB Market Analysis\n\n🇦🇪 AlphaDXB | Dubai Crypto Signals\n#crypto #dubai #AlphaDXB"
            )
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
            r2 = requests.post(
                url,
                files={"photo": ("image.jpg", BytesIO(photo_bytes), "image/jpeg")},
                data={"chat_id": target, "caption": formatted, "parse_mode": "HTML"},
                timeout=60,
            )
            print(f"✅ Admin photo sent: {r2.status_code}")
            send_message(ADMIN_BOT_TOKEN, ADMIN_CHAT_ID, f"✅ Posted to {target}!")
        except Exception as e:
            print(f"❌ Admin photo error: {e}")
            send_message(ADMIN_BOT_TOKEN, ADMIN_CHAT_ID, f"❌ Error: {e}")
    elif forward_from_chat and forward_message_id:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/copyMessage"
            r = requests.post(url, json={
                "chat_id": target,
                "from_chat_id": forward_from_chat["id"],
                "message_id": forward_message_id,
            }, timeout=10)
            print(f"✅ Forwarded: {r.status_code}")
            send_message(ADMIN_BOT_TOKEN, ADMIN_CHAT_ID, f"✅ Posted to {target}!")
        except Exception as e:
            print(f"❌ Forward error: {e}")
            send_message(ADMIN_BOT_TOKEN, ADMIN_CHAT_ID, f"❌ Error: {e}")
    elif text and not text.startswith("/"):
        formatted = format_analysis(text)
        send_message(TELEGRAM_TOKEN, target, formatted)
        send_message(ADMIN_BOT_TOKEN, ADMIN_CHAT_ID, f"✅ Posted to {target}!")


def run_admin_bot():
    print("Admin Bot Starting...")
    offset = 0
    while True:
        try:
            url = f"https://api.telegram.org/bot{ADMIN_BOT_TOKEN}/getUpdates"
            r = requests.get(url, params={"offset": offset, "timeout": 30}, timeout=35)
            updates = r.json().get("result", [])
            for update in updates:
                process_update(update)
                offset = update["update_id"] + 1
        except Exception as e:
            print(f"❌ Admin error: {e}")
            time.sleep(5)


# ---------- Entry point ----------

if __name__ == "__main__":
    print("AlphaDXB Bot Starting...")
    admin_thread = threading.Thread(target=run_admin_bot, daemon=True)
    admin_thread.start()
    morning_update()
    schedule.every().day.at("04:00").do(morning_update)
    schedule.every().day.at("16:00").do(evening_update)
    print("Bot is running!")
    while True:
        schedule.run_pending()
        time.sleep(30)
        print(f"{datetime.now().strftime('%H:%M:%S')} - Waiting...", end="\r")
