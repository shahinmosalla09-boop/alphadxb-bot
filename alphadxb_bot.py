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

# VIP signal engine — all swing-trade logic lives in this separate file
import vip_signals

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


# ---------- Telegram helpers (with retry) ----------

def send_message(token: str, channel, text: str, retries: int = 3) -> bool:
    """Send a Telegram message with simple exponential-backoff retry.

    Returns True on success, False after all retries fail. Logs every attempt.
    """
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": channel, "text": text, "parse_mode": "HTML"}
    for attempt in range(1, retries + 1):
        try:
            r = requests.post(url, json=payload, timeout=10)
            print(f"✅ Message attempt {attempt}: {r.status_code}")
            if r.status_code == 200:
                return True
            # 4xx errors are usually permanent (bad chat_id, format) — don't retry forever
            if 400 <= r.status_code < 500:
                print(f"❌ Telegram 4xx, will not retry: {r.text[:200]}")
                return False
        except Exception as e:
            print(f"❌ Send error attempt {attempt}: {e}")
        time.sleep(2 ** attempt)  # 2s, 4s, 8s
    return False


def send_photo(channel, photo_bytes: bytes, caption: str = "", retries: int = 3) -> bool:
    """Send a photo with retry. Falls back to text-only on failure."""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
    for attempt in range(1, retries + 1):
        try:
            r = requests.post(
                url,
                files={"photo": ("chart.png", BytesIO(photo_bytes), "image/png")},
                data={"chat_id": channel, "caption": caption, "parse_mode": "HTML"},
                timeout=60,
            )
            print(f"✅ Photo attempt {attempt}: {r.status_code}")
            if r.status_code == 200:
                return True
            if 400 <= r.status_code < 500:
                print(f"❌ Telegram 4xx photo, falling back to text: {r.text[:200]}")
                return send_message(TELEGRAM_TOKEN, channel, caption)
        except Exception as e:
            print(f"❌ Photo error attempt {attempt}: {e}")
        time.sleep(2 ** attempt)
    # All retries failed → fall back to text
    print("⚠️ Photo retries exhausted, sending caption as text")
    return send_message(TELEGRAM_TOKEN, channel, caption)


# ---------- Market data ----------

def _get_with_retry(url: str, params=None, retries: int = 3, timeout: int = 10):
    """Tiny helper that retries an HTTP GET with exponential backoff."""
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, params=params, timeout=timeout)
            if r.status_code == 200:
                return r
            print(f"⚠️ HTTP {r.status_code} on {url} (attempt {attempt})")
        except Exception as e:
            print(f"⚠️ HTTP error {url} attempt {attempt}: {e}")
        time.sleep(2 ** attempt)
    return None


def get_prices():
    """Pull spot prices from Kraken with retry per symbol."""
    symbols = {"BTC": "XXBTZUSD", "ETH": "XETHZUSD", "SOL": "SOLUSD", "BNB": "BNBUSD"}
    prices = {}
    for coin, pair in symbols.items():
        try:
            r = _get_with_retry(
                f"https://api.kraken.com/0/public/Ticker?pair={pair}",
                retries=2,
            )
            if r is None:
                continue
            data = r.json()
            if not data.get("error"):
                result = data["result"]
                key = list(result.keys())[0]
                prices[coin] = float(result[key]["c"][0])
        except Exception as e:
            print(f"❌ Price error for {coin}: {e}")
    return prices if len(prices) >= 3 else None


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

def is_likely_english(text: str) -> bool:
    """Return True if text has no Persian/Arabic characters."""
    if not text:
        return True
    for ch in text:
        code = ord(ch)
        # Arabic block (covers Persian) + Arabic Presentation Forms
        if (0x0600 <= code <= 0x06FF) or (0xFB50 <= code <= 0xFDFF) or (0xFE70 <= code <= 0xFEFF):
            return False
    return True


def translate_to_english(text: str) -> str:
    """Translate Persian/Arabic text to English using MyMemory free API."""
    if not text or not text.strip():
        return text
    if is_likely_english(text):
        return text  # already English, skip translation
    try:
        url = "https://api.mymemory.translated.net/get"
        # MyMemory has a 500-char limit per request. For longer text, split by lines.
        if len(text) <= 480:
            params = {"q": text, "langpair": "fa|en"}
            r = requests.get(url, params=params, timeout=15)
            if r.status_code == 200:
                data = r.json()
                translated = data.get("responseData", {}).get("translatedText", "").strip()
                if translated and "MYMEMORY WARNING" not in translated.upper():
                    return translated
        else:
            # Split by lines and translate piece by piece
            lines = text.split("\n")
            translated_lines = []
            for line in lines:
                if not line.strip() or is_likely_english(line):
                    translated_lines.append(line)
                    continue
                params = {"q": line, "langpair": "fa|en"}
                r = requests.get(url, params=params, timeout=15)
                if r.status_code == 200:
                    data = r.json()
                    t = data.get("responseData", {}).get("translatedText", "").strip()
                    translated_lines.append(t if t else line)
                else:
                    translated_lines.append(line)
            return "\n".join(translated_lines)
    except Exception as e:
        print(f"❌ Translate error: {e}")
    return text  # fallback: return original


def format_analysis(text: str) -> str:
    # First, translate Persian/Arabic content to English
    translated = translate_to_english(text)

    lines = [l.strip() for l in translated.strip().split('\n') if l.strip()]
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
    elif content and not content.startswith("/"):
        # `content` already has the optional "/vip" prefix stripped, so a
        # message like "/vip test" reaches this branch with content="test"
        # and target=VIP_CHANNEL.
        formatted = format_analysis(content)
        send_message(TELEGRAM_TOKEN, target, formatted)
        send_message(ADMIN_BOT_TOKEN, ADMIN_CHAT_ID, f"✅ Posted to {target}!")


def run_admin_bot():
    """Admin-bot polling loop. Survives transient errors and keeps running."""
    print("Admin Bot Starting...")
    offset = 0
    consecutive_errors = 0
    while True:
        try:
            url = f"https://api.telegram.org/bot{ADMIN_BOT_TOKEN}/getUpdates"
            r = requests.get(url, params={"offset": offset, "timeout": 30}, timeout=35)
            updates = r.json().get("result", []) if r.status_code == 200 else []
            for update in updates:
                try:
                    process_update(update)
                except Exception as inner:
                    # An error inside one update should not kill the whole loop.
                    print(f"❌ process_update error: {inner}")
                offset = update["update_id"] + 1
            consecutive_errors = 0
        except Exception as e:
            consecutive_errors += 1
            print(f"❌ Admin loop error #{consecutive_errors}: {e}")
            # Back off harder if errors persist (5s, 10s, 20s, 40s, max 60s)
            time.sleep(min(5 * (2 ** min(consecutive_errors - 1, 4)), 60))


# ---------- Entry point ----------

def _safe(label: str, fn):
    """Call fn() but never propagate an exception out of the schedule loop."""
    try:
        fn()
    except Exception as e:
        print(f"❌ {label} crashed: {e}")


if __name__ == "__main__":
    print("AlphaDXB Bot Starting...")
    admin_thread = threading.Thread(target=run_admin_bot, daemon=True)
    admin_thread.start()

    # Initial morning post on cold start, behind a try/except.
    _safe("startup morning_update", morning_update)

    # Wrap scheduled jobs so a crash in one doesn't halt the scheduler.
    schedule.every().day.at("04:00").do(lambda: _safe("morning_update", morning_update))
    schedule.every().day.at("16:00").do(lambda: _safe("evening_update", evening_update))

    # VIP swing-signal scanner — logic lives in vip_signals.py
    def _vip_scan():
        _safe("vip_scan", lambda: vip_signals.scan_and_post(TELEGRAM_TOKEN, VIP_CHANNEL))

    schedule.every().hour.do(_vip_scan)
    threading.Timer(60.0, _vip_scan).start()  # also run once 60s after startup

    # Heartbeat every 6 hours so the log shows the process is alive even when
    # nothing else is happening.
    schedule.every(6).hours.do(lambda: print(
        f"💓 Heartbeat {datetime.now().strftime('%Y-%m-%d %H:%M')}"))

    print("Bot is running!")
    # Main scheduler loop. Wrapped so an unexpected error in one tick doesn't
    # take the whole process down — Railway would restart anyway, but this
    # keeps us alive through transient hiccups (DNS blips, schedule lib bugs).
    while True:
        try:
            schedule.run_pending()
        except Exception as e:
            print(f"❌ Scheduler tick error: {e}")
        time.sleep(30)
