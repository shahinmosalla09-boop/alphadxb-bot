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
# Public-channel short-term (1H) signal engine + journal + weekly report
import public_signals
# Scalping engine — 15M SMC setups posted to public channel
import scalp_signals

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
VIP_LINK         = os.environ.get("VIP_LINK", "").strip()  # optional invite link for teaser CTA


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


# ---------- Morning state (shared between posts) ----------

import json as _json

_MORNING_STATE_FILE = "morning_state.json"

def _save_morning_state(data: dict):
    try:
        with open(_MORNING_STATE_FILE, "w") as f:
            _json.dump(data, f)
    except Exception:
        pass

def _load_morning_state() -> dict:
    try:
        with open(_MORNING_STATE_FILE, "r") as f:
            return _json.load(f)
    except Exception:
        return {}


# ---------- SMC educational content (rotates daily) ----------

_SMC_CONCEPTS = [
    {   # Monday
        "title": "Order Blocks (OB) 📦",
        "body": (
            "An Order Block is the last opposing candle before a strong impulse move.\n\n"
            "Example: The last green candle before a sharp drop → that's a Bearish OB.\n"
            "Institutions placed their sell orders there. Price often returns to fill remaining orders."
        ),
        "key": "When price revisits an OB zone and shows a reaction (wick, engulfing candle) — "
               "that's where smart money is active. That's our entry zone."
    },
    {   # Tuesday
        "title": "Fair Value Gaps (FVG) 🕳",
        "body": (
            "A Fair Value Gap is a 3-candle imbalance where price moved so fast it left a gap.\n\n"
            "Look at candle 1 and candle 3 — if there's no overlap between their wicks, "
            "that space in between is an FVG.\n"
            "Price is magnetically drawn back to fill these gaps."
        ),
        "key": "FVGs act as magnets. When an FVG aligns with an Order Block — "
               "that confluence is one of the strongest setups in SMC."
    },
    {   # Wednesday
        "title": "Break of Structure (BOS) 🔨",
        "body": (
            "BOS happens when price breaks a previous swing high (bullish) or swing low (bearish).\n\n"
            "In a bullish trend: each BOS above a swing high = confirmation the trend continues.\n"
            "In a bearish trend: each BOS below a swing low = confirmation of downtrend."
        ),
        "key": "BOS tells you WHO is in control. Trade in the direction of BOS — "
               "not against it. Simple but powerful."
    },
    {   # Thursday
        "title": "Change of Character (CHoCH) 🔄",
        "body": (
            "CHoCH is the FIRST sign the trend is reversing.\n\n"
            "In a downtrend: when price breaks ABOVE the most recent swing high for the first time — "
            "that's a CHoCH. The bears just lost control.\n"
            "Smart money uses CHoCH to get in early on reversals."
        ),
        "key": "BOS = trend continuation. CHoCH = potential reversal. "
               "Knowing the difference saves you from fighting the wrong side."
    },
    {   # Friday
        "title": "Liquidity Sweeps 💧",
        "body": (
            "Most retail traders place stop losses just below swing lows (for longs) "
            "or above swing highs (for shorts).\n\n"
            "Smart money KNOWS this. They push price into those levels to trigger stops, "
            "collect liquidity — then reverse hard in the opposite direction."
        ),
        "key": "When you see a sharp wick below a key low that immediately reverses — "
               "that's a liquidity sweep. It's not a breakdown. It's a trap. "
               "The smart play: wait for confirmation, then enter with the reversal."
    },
    {   # Saturday
        "title": "Higher Timeframe Bias 🗺",
        "body": (
            "Before ANY trade, ask: what is the daily/4H structure telling me?\n\n"
            "Higher Highs + Higher Lows = Bullish bias → only take LONG setups.\n"
            "Lower Highs + Lower Lows = Bearish bias → only take SHORT setups.\n\n"
            "Trading against the HTF bias is like swimming against the current."
        ),
        "key": "At AlphaDXB, every signal is filtered through HTF bias first. "
               "If the daily is bearish, we don't post long signals — no matter how good the 15M looks."
    },
    {   # Sunday
        "title": "Risk Management — The Rule That Matters Most 🛡",
        "body": (
            "The best setup in the world means nothing without risk management.\n\n"
            "Rule: Never risk more than 1–2% of your account per trade.\n\n"
            "If you risk 1% and lose 10 trades in a row — you're down 10%. Recoverable.\n"
            "If you risk 10% and lose 3 trades — you're down 30%. Hard to recover."
        ),
        "key": "Professionals don't try to win big. They try to NOT lose big. "
               "The stop loss is not a failure — it's your insurance policy."
    },
]


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
    plt.style.use('default')
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 8), gridspec_kw={'height_ratios': [3, 1]})
    fig.patch.set_facecolor('white')
    ax1.set_facecolor('white')
    ax2.set_facecolor('white')
    closes = [c["close"] for c in ohlc]
    opens = [c["open"] for c in ohlc]
    highs = [c["high"] for c in ohlc]
    lows = [c["low"] for c in ohlc]
    volumes = [c["volume"] for c in ohlc]
    for i in range(len(ohlc)):
        color = '#16a085' if closes[i] >= opens[i] else '#e74c3c'
        ax1.plot([i, i], [lows[i], highs[i]], color=color, linewidth=0.8)
        ax1.bar(i, abs(closes[i] - opens[i]), bottom=min(opens[i], closes[i]), color=color, width=0.6, alpha=0.9)
    for i, s in enumerate(supports):
        ax1.axhline(y=s, color='#16a085', linewidth=1.5, linestyle='--', alpha=0.8)
        ax1.text(len(ohlc) + 1, s, f'S{i+1}: ${s:,.0f}', color='#16a085', fontsize=8, fontweight='bold', va='center')
    for i, r in enumerate(resistances):
        ax1.axhline(y=r, color='#e74c3c', linewidth=1.5, linestyle='--', alpha=0.8)
        ax1.text(len(ohlc) + 1, r, f'R{i+1}: ${r:,.0f}', color='#e74c3c', fontsize=8, fontweight='bold', va='center')
    ax1.set_title('AlphaDXB | BTC/USDT - 4H', color='#1a1a2e', fontsize=14, fontweight='bold', pad=15)
    ax1.set_xlim(-1, len(ohlc) + 10)
    ax1.grid(color='#e0e0e0', linewidth=0.5, alpha=0.8)
    ax1.tick_params(colors='#333333', labelsize=8)
    ax1.set_xticks([])
    ax1.yaxis.tick_right()
    for s in ['top', 'left']:
        ax1.spines[s].set_visible(False)
    for s in ['bottom', 'right']:
        ax1.spines[s].set_color('#cccccc')
    for i in range(len(ohlc)):
        color = '#16a085' if closes[i] >= opens[i] else '#e74c3c'
        ax2.bar(i, volumes[i], color=color, width=0.6, alpha=0.6)
    ax2.set_xlim(-1, len(ohlc) + 10)
    ax2.tick_params(colors='#333333', labelsize=7)
    ax2.set_xticks([])
    ax2.yaxis.tick_right()
    ax2.grid(color='#e0e0e0', linewidth=0.5, alpha=0.8)
    for s in ['top', 'left']:
        ax2.spines[s].set_visible(False)
    for s in ['bottom', 'right']:
        ax2.spines[s].set_color('#cccccc')
    fig.text(0.5, 0.5, 'AlphaDXB', fontsize=40, color='black', alpha=0.04,
             ha='center', va='center', fontweight='bold', rotation=30)
    plt.tight_layout(pad=2)
    buf = BytesIO()
    plt.savefig(buf, format='png', dpi=150, bbox_inches='tight', facecolor='white')
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

    btc = prices.get("BTC", 0)
    date_str = datetime.now().strftime("%b %d, %Y")
    ohlc = get_ohlc()

    if not ohlc:
        send_message(TELEGRAM_TOKEN, PUBLIC_CHANNEL,
                     f"📊 <b>AlphaDXB Morning Brief</b>\n{date_str}\n\n"
                     f"BTC: ${btc:,.0f} | Sentiment: {fg_label} ({fg_val})\n\n"
                     f"🇦🇪 AlphaDXB | Dubai Crypto Signals\n#AlphaDXB")
        return

    supports, resistances = find_levels(ohlc)

    # Pick the key level closest to current price
    all_levels = [(s, "support") for s in supports] + [(r, "resistance") for r in resistances]
    all_levels.sort(key=lambda x: abs(x[0] - btc))
    key_price, key_type = all_levels[0] if all_levels else (btc * 0.97, "support")

    # Count how many 4H candles touched this level (within 0.4%)
    touches = sum(
        1 for c in ohlc
        if abs(c["high"] - key_price) / key_price <= 0.004
        or abs(c["low"] - key_price) / key_price <= 0.004
    )
    touches = max(touches, 1)

    dist_pct = abs(btc - key_price) / btc * 100

    if key_type == "support":
        level_label = "support zone"
        hold_target = resistances[0] if resistances else btc * 1.03
        break_target = supports[1] if len(supports) > 1 else key_price * 0.97
        scenario_a = f"Buyers defend ${key_price:,.0f} → push toward <b>${hold_target:,.0f}</b>"
        scenario_b = f"Clean close below ${key_price:,.0f} → next support at <b>${break_target:,.0f}</b>"
        position = f"BTC is sitting <b>${abs(btc - key_price):,.0f} above</b> a key {level_label}"
    else:
        level_label = "resistance zone"
        hold_target = supports[0] if supports else btc * 0.97
        break_target = resistances[1] if len(resistances) > 1 else key_price * 1.03
        scenario_a = f"Sellers hold ${key_price:,.0f} → pullback to <b>${hold_target:,.0f}</b>"
        scenario_b = f"Breakout above ${key_price:,.0f} → next target <b>${break_target:,.0f}</b>"
        position = f"BTC is approaching a key {level_label} at <b>${key_price:,.0f}</b>"

    if fg_val < 30:
        sentiment_note = "Extreme Fear in the market — historically, this is when smart money accumulates. Stay calm."
    elif fg_val > 70:
        sentiment_note = "Greed is elevated — be selective. Don't chase extended moves. Wait for pullbacks to key levels."
    else:
        sentiment_note = "Neutral sentiment — market is undecided. Let price come to your level, don't force entries."

    caption = (
        f"📊 <b>AlphaDXB Morning Brief — {date_str}</b>\n\n"
        f"{position}.\n"
        f"This level has been tested <b>{touches}x</b> in the last 60 candles — the more it's tested, the more significant it becomes.\n\n"
        f"📍 <b>Key level today: ${key_price:,.0f}</b> ({level_label})\n"
        f"Distance from current price: {dist_pct:.1f}%\n\n"
        f"<b>Two scenarios to watch:</b>\n"
        f"📈 A) {scenario_a}\n"
        f"📉 B) {scenario_b}\n\n"
        f"🧠 <b>Sentiment:</b> {fg_label.upper()} ({fg_val})\n"
        f"{sentiment_note}\n\n"
        f"🇦🇪 AlphaDXB | Dubai Crypto Signals\n"
        f"#btc #crypto #SMC #priceaction #AlphaDXB"
    )

    # Save morning state for mid-day reference
    _save_morning_state({
        "btc": btc,
        "key_price": key_price,
        "key_type": key_type,
        "hold_target": hold_target,
        "break_target": break_target,
        "timestamp": datetime.now().isoformat(),
    })

    try:
        chart = create_chart(ohlc, supports, resistances)
        send_photo(PUBLIC_CHANNEL, chart, caption)
    except Exception as e:
        print(f"❌ Chart error: {e}")
        send_message(TELEGRAM_TOKEN, PUBLIC_CHANNEL, caption)


def midday_update():
    print(f"\n[{datetime.now().strftime('%H:%M')}] Mid-day update...")
    prices = get_prices()
    if not prices:
        return

    btc = prices.get("BTC", 0)
    fg_val, fg_label = get_fear_greed()
    state = _load_morning_state()
    date_str = datetime.now().strftime("%b %d, %Y")

    if state and state.get("key_price"):
        key_price = state["key_price"]
        key_type  = state["key_type"]
        morning_btc = state.get("btc", btc)
        move = btc - morning_btc
        move_str = f"+${move:,.0f}" if move >= 0 else f"-${abs(move):,.0f}"
        direction_word = "up" if move >= 0 else "down"

        if key_type == "support":
            dist = btc - key_price
            if dist > 0:
                level_status = (
                    f"✅ The <b>${key_price:,.0f} support</b> we flagged this morning has held so far.\n"
                    f"BTC is ${dist:,.0f} above it. Buyers are in control at that zone."
                )
                watch_next = f"Watch for a push toward <b>${state.get('hold_target', btc*1.02):,.0f}</b> if momentum continues."
            else:
                level_status = (
                    f"⚠️ BTC has broken below the <b>${key_price:,.0f} support</b> we flagged.\n"
                    f"This is a bearish sign. Next level to watch: <b>${state.get('break_target', btc*0.97):,.0f}</b>"
                )
                watch_next = "Look for a retest of the broken level as resistance before considering any short entries."
        else:
            dist = key_price - btc
            if dist > 0:
                level_status = (
                    f"🔴 BTC is still <b>${dist:,.0f} below the ${key_price:,.0f} resistance</b> we flagged.\n"
                    f"Sellers are holding that zone for now."
                )
                watch_next = f"If BTC breaks above <b>${key_price:,.0f}</b> with volume, target <b>${state.get('hold_target', btc*1.03):,.0f}</b>."
            else:
                level_status = (
                    f"✅ BTC has broken above the <b>${key_price:,.0f} resistance</b> — bullish momentum.\n"
                    f"New target: <b>${state.get('break_target', btc*1.03):,.0f}</b>"
                )
                watch_next = "Breakout traders: wait for a clean retest of the broken level as support before entry."

        msg = (
            f"☀️ <b>AlphaDXB Mid-session Update — {date_str}</b>\n\n"
            f"BTC is now at <b>${btc:,.0f}</b> — {direction_word} {move_str} since this morning.\n\n"
            f"{level_status}\n\n"
            f"📌 {watch_next}\n\n"
            f"Sentiment: {fg_label} ({fg_val})\n\n"
            f"🇦🇪 AlphaDXB | Dubai Crypto Signals\n"
            f"#btc #crypto #SMC #AlphaDXB"
        )
    else:
        # Fallback if no morning state
        msg = (
            f"☀️ <b>AlphaDXB Mid-session Update — {date_str}</b>\n\n"
            f"BTC: <b>${btc:,.0f}</b>\n"
            f"Sentiment: {fg_label} ({fg_val})\n\n"
            f"Market is in price discovery mode. No clear setup yet — patience is a position.\n\n"
            f"🇦🇪 AlphaDXB | Dubai Crypto Signals\n#AlphaDXB"
        )

    send_message(TELEGRAM_TOKEN, PUBLIC_CHANNEL, msg)


def evening_update():
    print(f"\n[{datetime.now().strftime('%H:%M')}] Evening update...")
    prices = get_prices()
    fg_val, fg_label = get_fear_greed()
    if not prices:
        return

    btc = prices.get("BTC", 0)
    date_str = datetime.now().strftime("%b %d, %Y")
    ohlc = get_ohlc()
    state = _load_morning_state()

    if ohlc:
        supports, resistances = find_levels(ohlc)
        # Day's range from last 6 candles (~24h on 4H)
        last_day = ohlc[-6:] if len(ohlc) >= 6 else ohlc
        day_high = max(c["high"] for c in last_day)
        day_low  = min(c["low"]  for c in last_day)
        day_range = day_high - day_low

        overnight_level = supports[0] if supports else btc * 0.97
        overnight_target = resistances[0] if resistances else btc * 1.03

        if fg_val < 30:
            session_tone = "Fear is dominating — but remember, market bottoms are built in fear. Watch for structure recovery."
        elif fg_val > 70:
            session_tone = "Sentiment is greedy — extended positions are at risk of a sharp correction. Manage risk carefully."
        else:
            session_tone = "Market is balanced. Structure will decide the next direction — let it develop."

        # Morning level follow-up
        morning_recap = ""
        if state and state.get("key_price"):
            kp = state["key_price"]
            kt = state["key_type"]
            if kt == "support" and btc > kp:
                morning_recap = f"✅ The ${kp:,.0f} support we flagged this morning held perfectly.\n"
            elif kt == "support" and btc < kp:
                morning_recap = f"📉 BTC broke below the ${kp:,.0f} support we called this morning.\n"
            elif kt == "resistance" and btc < kp:
                morning_recap = f"🔴 The ${kp:,.0f} resistance held — sellers defended it all session.\n"
            else:
                morning_recap = f"✅ BTC broke through the ${kp:,.0f} resistance we flagged — bullish.\n"

        caption = (
            f"🌙 <b>AlphaDXB Evening Breakdown — {date_str}</b>\n\n"
            f"BTC closed the session at <b>${btc:,.0f}</b>\n"
            f"Today's range: ${day_low:,.0f} → ${day_high:,.0f} (${day_range:,.0f} spread)\n\n"
            f"{morning_recap}"
            f"\n📌 <b>Overnight watch:</b>\n"
            f"Key support: <b>${overnight_level:,.0f}</b>\n"
            f"• Hold above → bias toward <b>${overnight_target:,.0f}</b>\n"
            f"• Break below → structure weakens significantly\n\n"
            f"🧠 {session_tone}\n\n"
            f"🇦🇪 AlphaDXB | Dubai Crypto Signals\n"
            f"#btc #crypto #SMC #AlphaDXB"
        )
        try:
            chart = create_chart(ohlc, supports, resistances)
            send_photo(PUBLIC_CHANNEL, chart, caption)
        except Exception as e:
            print(f"❌ Chart error: {e}")
            send_message(TELEGRAM_TOKEN, PUBLIC_CHANNEL, caption)
    else:
        send_message(TELEGRAM_TOKEN, PUBLIC_CHANNEL,
                     f"🌙 <b>AlphaDXB Evening Breakdown — {date_str}</b>\n\n"
                     f"BTC: ${btc:,.0f} | Sentiment: {fg_label} ({fg_val})\n\n"
                     f"🇦🇪 AlphaDXB | Dubai Crypto Signals\n#AlphaDXB")


def latenight_update():
    print(f"\n[{datetime.now().strftime('%H:%M')}] Late-night update...")
    day_of_week = datetime.now().weekday()  # 0=Mon, 6=Sun
    concept = _SMC_CONCEPTS[day_of_week]
    date_str = datetime.now().strftime("%b %d, %Y")

    msg = (
        f"🎓 <b>AlphaDXB Night School — {date_str}</b>\n\n"
        f"<b>{concept['title']}</b>\n\n"
        f"{concept['body']}\n\n"
        f"🔑 <b>Why it matters:</b>\n"
        f"{concept['key']}\n\n"
        f"Save this post — understanding this concept will change how you read charts.\n\n"
        f"🇦🇪 AlphaDXB | Dubai Crypto Signals\n"
        f"#AlphaDXBSchool #SMC #education #trading #AlphaDXB"
    )
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
    document = msg.get("document", {})
    forward_from_chat = msg.get("forward_from_chat")
    forward_message_id = msg.get("forward_from_message_id")
    file_id = photo[-1]["file_id"] if photo else None
    target = PUBLIC_CHANNEL
    content = caption if photo else text

    # ── Admin uploads signals_journal.json → save to disk + pin it ──────────
    if document.get("file_name") == "signals_journal.json":
        try:
            import json as _jj
            doc_file_id = document["file_id"]
            fr = requests.get(
                f"https://api.telegram.org/bot{ADMIN_BOT_TOKEN}/getFile",
                params={"file_id": doc_file_id}, timeout=10,
            )
            file_path_tg = fr.json()["result"]["file_path"]
            content_r = requests.get(
                f"https://api.telegram.org/file/bot{ADMIN_BOT_TOKEN}/{file_path_tg}",
                timeout=15,
            )
            journal_data = content_r.json()
            with open("signals_journal.json", "w") as f:
                _jj.dump(journal_data, f, indent=2)
            sig_count = len(journal_data.get("signals", []))
            # Pin this message so future restores can find it via getChat
            msg_id = msg.get("message_id")
            if msg_id:
                requests.post(
                    f"https://api.telegram.org/bot{ADMIN_BOT_TOKEN}/pinChatMessage",
                    json={"chat_id": ADMIN_CHAT_ID, "message_id": msg_id,
                          "disable_notification": True},
                    timeout=10,
                )
            send_message(ADMIN_BOT_TOKEN, ADMIN_CHAT_ID,
                         f"✅ Journal restored! {sig_count} signals loaded & message pinned.\n"
                         f"Now type /journal to check.")
        except Exception as e:
            send_message(ADMIN_BOT_TOKEN, ADMIN_CHAT_ID, f"❌ Journal restore error: {e}")
        return
    # ─────────────────────────────────────────────────────────────────────────
    if content.startswith("/vip"):
        target = VIP_CHANNEL
        content = content.replace("/vip", "", 1).strip()
    if content.startswith("/start"):
        send_message(ADMIN_BOT_TOKEN, ADMIN_CHAT_ID,
                     "✅ Admin Bot ready!\n\nSend or forward any post.\nAdd /vip at start for VIP channel.\n\n/journal — see all open signals")
        return

    if content.startswith("/journal"):
        # Send journal summary ONLY to admin — never to public channel
        try:
            import json as _j
            with open("signals_journal.json", "r") as f:
                journal = _j.load(f)
            signals = journal.get("signals", [])
            if not signals:
                send_message(ADMIN_BOT_TOKEN, ADMIN_CHAT_ID, "📒 Journal empty — no signals yet.")
                return
            open_sigs   = [s for s in signals if s["status"] == "open"]
            closed_sigs = [s for s in signals if s["status"] != "open"]
            lines = [f"📒 <b>Signal Journal</b>\n"]
            lines.append(f"Open: {len(open_sigs)} | Closed: {len(closed_sigs)} | Total: {len(signals)}\n")
            if open_sigs:
                lines.append("─── <b>OPEN</b> ───")
                for s in open_sigs:
                    coin = s['coin'].replace('USDT','')
                    dt = datetime.fromtimestamp(s['timestamp']).strftime('%m/%d %H:%M')
                    lines.append(f"• {coin} {s['direction']} @ ${s['entry']:,.2f} | SL ${s['sl']:,.2f} | TP1 ${s['tp1']:,.2f} [{dt}]")
            if closed_sigs[-10:]:
                lines.append("\n─── <b>LAST 10 CLOSED</b> ───")
                for s in closed_sigs[-10:]:
                    coin = s['coin'].replace('USDT','')
                    dt = datetime.fromtimestamp(s['timestamp']).strftime('%m/%d')
                    status_icon = {"tp1_hit":"✅","tp2_hit":"🎯","sl_hit":"❌","expired":"⌛"}.get(s['status'],'?')
                    rr = s.get('rr_achieved', 0)
                    lines.append(f"{status_icon} {coin} {s['direction']} ({s.get('timeframe','?')}) {dt} → {rr:+.2f}R")

            # Win rate summary
            wins    = sum(1 for s in closed_sigs if "tp" in s["status"])
            losses  = sum(1 for s in closed_sigs if s["status"] == "sl_hit")
            expired = sum(1 for s in closed_sigs if s["status"] == "expired")
            total   = wins + losses
            net_r   = sum(s.get("rr_achieved", 0) for s in closed_sigs)
            wr_pct  = round(wins / total * 100) if total else 0
            lines.append(f"\n─── <b>PERFORMANCE</b> ───")
            lines.append(f"✅ Wins: {wins}  ❌ Losses: {losses}  ⌛ Expired: {expired}")
            lines.append(f"🎯 Win Rate: <b>{wr_pct}%</b>  ({wins}/{total})")
            lines.append(f"📈 Net R: <b>{net_r:+.2f}R</b>")
            send_message(ADMIN_BOT_TOKEN, ADMIN_CHAT_ID, "\n".join(lines))
        except FileNotFoundError:
            send_message(ADMIN_BOT_TOKEN, ADMIN_CHAT_ID, "📒 Journal file not found yet.")
        except Exception as e:
            send_message(ADMIN_BOT_TOKEN, ADMIN_CHAT_ID, f"❌ Journal error: {e}")
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

    # Configure Telegram journal backup (admin bot → admin chat)
    public_signals.configure_journal_backup(ADMIN_BOT_TOKEN, str(ADMIN_CHAT_ID))

    # Restore journal from Telegram on every startup — survives Railway redeploys
    print("[STARTUP] Restoring journal from Telegram backup...")
    _safe("journal_restore", lambda: public_signals.restore_journal_from_telegram(
        ADMIN_BOT_TOKEN, str(ADMIN_CHAT_ID)))

    admin_thread = threading.Thread(target=run_admin_bot, daemon=True)
    admin_thread.start()

    # ----- Scheduled content posts -----
    # Morning / midday / evening BTC price updates removed — low added value.
    # Night School (educational SMC concepts) stays — differentiated content.
    schedule.every().day.at("18:00").do(lambda: _safe("latenight_update", latenight_update))  # 22:00 Dubai

    # ----- VIP swing-signal scanner (4H setups → VIP only) -----
    def _vip_scan():
        # Callback that journals each VIP signal with its Telegram message_id.
        # This runs in the same thread as scan_and_post, after each signal posts.
        def _on_vip_posted(sig, message_id, channel):
            try:
                public_signals.record_signal(
                    sig, "vip",
                    message_id=message_id,
                    channel_id=channel,
                )
            except Exception as e:
                print(f"[VIP] journal callback error: {e}")

        _safe("vip_scan", lambda: vip_signals.scan_and_post(
            TELEGRAM_TOKEN, VIP_CHANNEL, on_posted=_on_vip_posted))

    schedule.every().hour.do(_vip_scan)
    threading.Timer(60.0, _vip_scan).start()  # one run 60s after startup

    # ----- Public 1H short-term scanner (full signals → PUBLIC) -----
    def _public_scan():
        _safe("public_scan", lambda: public_signals.scan_and_post(
            TELEGRAM_TOKEN, PUBLIC_CHANNEL))

    schedule.every().hour.do(_public_scan)
    threading.Timer(120.0, _public_scan).start()  # one run 2 min after startup

    # ----- Scalping engine — 15M SMC setups → public channel -----
    def _scalp_scan():
        _safe("scalp_scan", lambda: scalp_signals.scan_and_post(
            TELEGRAM_TOKEN, PUBLIC_CHANNEL))

    schedule.every(30).minutes.do(_scalp_scan)
    threading.Timer(90.0, _scalp_scan).start()   # first run 90s after startup

    # ----- Open-signal tracker — runs every 30 min to update SL/TP outcomes -----
    def _journal_check():
        # Pass TELEGRAM_TOKEN so the tracker can reply to original signal messages
        _safe("journal_check", lambda: public_signals.update_open_signals(TELEGRAM_TOKEN))

    schedule.every(30).minutes.do(_journal_check)
    threading.Timer(180.0, _journal_check).start()

    # ----- Weekly performance report — Sunday 18:00 UTC (~22:00 Dubai) -----
    def _weekly_report():
        _safe("weekly_report", lambda: public_signals.weekly_report(
            TELEGRAM_TOKEN, PUBLIC_CHANNEL,
            admin_token=ADMIN_BOT_TOKEN, admin_id=ADMIN_CHAT_ID))

    schedule.every().sunday.at("18:00").do(_weekly_report)

    # ----- Heartbeat in logs so we can see the process is alive -----
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
