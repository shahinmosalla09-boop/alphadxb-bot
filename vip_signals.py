"""
VIP swing-signal engine — Smart Money / Order Block edition.

Methodology (in priority order):
  1. Higher-timeframe bias (1D structure)         → only trade with the bias
  2. Active Order Block (OB) on 4H                → unmitigated demand/supply
  3. Price testing the OB zone right now          → mitigation entry
  4. Bullish/bearish reaction inside the zone     → candlestick confirmation
  5. Fair Value Gap (FVG) in trade direction      → imbalance confluence
  6. Tight stop just beyond OB extremes           → low-error entry
  7. Take-profit at opposing liquidity (swing)    → realistic target

Signals only fire when 4 of these 5 confluences are true (HTF, OB,
mitigation+pattern, FVG, R:R ≥ 1:2). Result: fewer signals, far higher quality.

Public API:
    scan_and_post(telegram_token: str, vip_channel: str) -> None
"""

import json
import time
from datetime import datetime

import requests


# ---------- Tweakable settings ----------

COINS            = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"]
COOLDOWN_HOURS   = 12
SIGNAL_STATE_FILE = "signal_state.json"

HTF_INTERVAL     = "1d"
LTF_INTERVAL     = "4h"
HTF_LIMIT        = 100
LTF_LIMIT        = 250

SWING_LEFT       = 3
SWING_RIGHT      = 3

IMPULSE_LOOKAHEAD = 5     # candles after a candidate OB to confirm an impulse
IMPULSE_ATR_MULT  = 2.0   # impulse high-low / ATR threshold
OB_MAX_AGE_BARS   = 80    # ignore OBs older than this
OB_MAX_TOUCHES    = 2     # an OB tested ≥ this many times is exhausted

SL_BUFFER_ATR    = 0.5    # SL distance beyond OB extreme = this * ATR
MIN_RR_TP1       = 2.0    # require TP1 ≥ 1:2 R:R, else reject the setup
TP1_RR           = 2
TP2_RR           = 3
MAX_SL_PCT       = 0.05   # never accept a setup whose SL is > 5% from entry

REQUIRED_CONFLUENCES = 4  # of 5 (HTF, OB, mitigation+pattern, FVG, R:R)


# ---------- Telegram (with retry) ----------

def _send(token: str, channel: str, text: str, retries: int = 3) -> bool:
    """Send a Telegram message, retrying transient failures with backoff.

    Returns True on success, False after all retries fail. 4xx (e.g. bad
    chat_id) is treated as permanent — we don't retry those.
    """
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": channel, "text": text, "parse_mode": "HTML"}
    for attempt in range(1, retries + 1):
        try:
            r = requests.post(url, json=payload, timeout=10)
            print(f"[VIP] Send attempt {attempt}: {r.status_code}")
            if r.status_code == 200:
                return True
            if 400 <= r.status_code < 500:
                print(f"[VIP]   Body: {r.text[:200]} (4xx — not retrying)")
                return False
        except Exception as e:
            print(f"[VIP] ❌ Send error attempt {attempt}: {e}")
        time.sleep(2 ** attempt)  # 2s, 4s, 8s
    return False


# ---------- Market data (KuCoin, no US block) ----------

_KUCOIN_INTERVAL = {
    "1m":  "1min",  "3m":  "3min",  "5m":  "5min",  "15m": "15min", "30m": "30min",
    "1h":  "1hour", "2h":  "2hour", "4h":  "4hour", "6h":  "6hour", "8h":  "8hour", "12h": "12hour",
    "1d":  "1day",  "1w":  "1week",
}


def get_klines(symbol: str, interval: str, limit: int = 100, retries: int = 3):
    """Fetch OHLCV from KuCoin with retry. Returns None on permanent failure."""
    kucoin_interval = _KUCOIN_INTERVAL.get(interval, interval)
    if symbol.endswith("USDT"):
        kucoin_symbol = f"{symbol[:-4]}-USDT"
    else:
        kucoin_symbol = symbol

    last_err = None
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(
                "https://api.kucoin.com/api/v1/market/candles",
                params={"type": kucoin_interval, "symbol": kucoin_symbol},
                timeout=15,
            )
            if r.status_code != 200:
                last_err = f"HTTP {r.status_code}"
                # 4xx is usually a bad request (won't fix itself), 5xx might
                if 400 <= r.status_code < 500:
                    print(f"[VIP] ❌ KuCoin {symbol} {interval}: {last_err} (4xx, not retrying)")
                    return None
            else:
                data = r.json()
                if data.get("code") != "200000":
                    print(f"[VIP] ❌ KuCoin {symbol} {interval}: {data.get('msg')}")
                    return None
                rows = list(reversed(data.get("data", []) or []))
                if len(rows) > limit:
                    rows = rows[-limit:]
                return [
                    {
                        "time":   datetime.fromtimestamp(int(k[0])),
                        "open":   float(k[1]),
                        "close":  float(k[2]),
                        "high":   float(k[3]),
                        "low":    float(k[4]),
                        "volume": float(k[5]),
                    }
                    for k in rows
                ]
        except Exception as e:
            last_err = str(e)
        if attempt < retries:
            time.sleep(2 ** attempt)  # 2s, 4s
    print(f"[VIP] ❌ KuCoin {symbol} {interval}: failed after {retries} attempts ({last_err})")
    return None


# ---------- ATR ----------

def calc_atr(candles, period=14):
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        h, l, prev_c = candles[i]["high"], candles[i]["low"], candles[i - 1]["close"]
        trs.append(max(h - l, abs(h - prev_c), abs(l - prev_c)))
    if len(trs) < period:
        return None
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


# ---------- Swing points ----------

def find_swings(candles, left=SWING_LEFT, right=SWING_RIGHT):
    highs, lows = [], []
    n = len(candles)
    for i in range(left, n - right):
        h = candles[i]["high"]
        l = candles[i]["low"]
        if (all(candles[j]["high"] < h for j in range(i - left, i)) and
            all(candles[j]["high"] < h for j in range(i + 1, i + right + 1))):
            highs.append((i, h))
        if (all(candles[j]["low"] > l for j in range(i - left, i)) and
            all(candles[j]["low"] > l for j in range(i + 1, i + right + 1))):
            lows.append((i, l))
    return highs, lows


# ---------- Higher-timeframe bias ----------

def htf_bias(candles_htf):
    """Return 'bullish' / 'bearish' / 'neutral' from the daily structure."""
    highs, lows = find_swings(candles_htf, left=2, right=2)
    if len(highs) < 2 or len(lows) < 2:
        return "neutral"
    h1, h2 = highs[-2][1], highs[-1][1]
    l1, l2 = lows[-2][1],  lows[-1][1]
    if h2 > h1 and l2 > l1:
        return "bullish"
    if h2 < h1 and l2 < l1:
        return "bearish"
    return "neutral"


# ---------- Order Blocks ----------

def find_order_blocks(candles, atr):
    """Return all OBs detected in the candle series.

    Definition we use:
      Bullish OB = the last bearish candle before a strong bullish impulse
                   that closes above the OB candle's high.
      Bearish OB = the last bullish candle before a strong bearish impulse
                   that closes below the OB candle's low.

    We also drop OBs that are clearly "broken" (closed beyond their extreme
    by a confirmed candle after formation).
    """
    obs = []
    n = len(candles)
    if atr is None:
        return obs

    for i in range(2, n - IMPULSE_LOOKAHEAD - 1):
        c = candles[i]
        body_dir = c["close"] - c["open"]

        # ---- Bullish OB ----
        if body_dir < 0:  # bearish candle
            window = candles[i + 1: i + 1 + IMPULSE_LOOKAHEAD]
            if not window:
                continue
            future_high = max(w["high"] for w in window)
            impulse = future_high - c["low"]
            move_close = window[-1]["close"]
            if impulse >= atr * IMPULSE_ATR_MULT and move_close > c["high"]:
                obs.append({
                    "type":  "bullish",
                    "index": i,
                    "high":  c["high"],
                    "low":   c["low"],
                })

        # ---- Bearish OB ----
        elif body_dir > 0:  # bullish candle
            window = candles[i + 1: i + 1 + IMPULSE_LOOKAHEAD]
            if not window:
                continue
            future_low = min(w["low"] for w in window)
            impulse = c["high"] - future_low
            move_close = window[-1]["close"]
            if impulse >= atr * IMPULSE_ATR_MULT and move_close < c["low"]:
                obs.append({
                    "type":  "bearish",
                    "index": i,
                    "high":  c["high"],
                    "low":   c["low"],
                })

    # Drop broken / exhausted OBs
    fresh = []
    for ob in obs:
        if ob["index"] < n - 1 - OB_MAX_AGE_BARS:
            continue  # too old
        # Count touches & detect break
        touches, broken = 0, False
        for j in range(ob["index"] + IMPULSE_LOOKAHEAD, n):
            cj = candles[j]
            if ob["type"] == "bullish":
                # Touch = price entered the OB zone
                if cj["low"] <= ob["high"]:
                    touches += 1
                # Broken = a candle closed below the OB low
                if cj["close"] < ob["low"]:
                    broken = True
                    break
            else:
                if cj["high"] >= ob["low"]:
                    touches += 1
                if cj["close"] > ob["high"]:
                    broken = True
                    break
        if not broken and touches < OB_MAX_TOUCHES:
            ob["touches"] = touches
            fresh.append(ob)
    return fresh


# ---------- Fair Value Gaps ----------

def find_fvgs(candles):
    """3-candle imbalance gaps. Returns list of dicts with type, low, high, index."""
    fvgs = []
    for i in range(1, len(candles) - 1):
        c1 = candles[i - 1]
        c3 = candles[i + 1]
        if c3["low"] > c1["high"]:
            fvgs.append({"type": "bullish", "index": i,
                         "low": c1["high"], "high": c3["low"]})
        elif c3["high"] < c1["low"]:
            fvgs.append({"type": "bearish", "index": i,
                         "low": c3["high"], "high": c1["low"]})
    return fvgs


def has_recent_fvg(candles, direction: str, lookback: int = 10) -> bool:
    """True if a same-direction FVG formed in the last `lookback` candles."""
    fvgs = find_fvgs(candles)
    n = len(candles)
    for fvg in fvgs[-15:]:
        if fvg["type"] == direction and (n - 1 - fvg["index"]) <= lookback:
            return True
    return False


# ---------- Candlestick patterns (entry confirmation) ----------

def is_bullish_engulfing(p, c):
    return (p["close"] < p["open"] and c["close"] > c["open"]
            and c["close"] >= p["open"] and c["open"] <= p["close"])


def is_bearish_engulfing(p, c):
    return (p["close"] > p["open"] and c["close"] < c["open"]
            and c["close"] <= p["open"] and c["open"] >= p["close"])


def is_hammer(c):
    body = abs(c["close"] - c["open"])
    if body == 0:
        return False
    upper = c["high"] - max(c["open"], c["close"])
    lower = min(c["open"], c["close"]) - c["low"]
    return lower >= body * 2 and upper <= body


def is_shooting_star(c):
    body = abs(c["close"] - c["open"])
    if body == 0:
        return False
    upper = c["high"] - max(c["open"], c["close"])
    lower = min(c["open"], c["close"]) - c["low"]
    return upper >= body * 2 and lower <= body


def bullish_pattern(p, c):
    if is_bullish_engulfing(p, c): return "Bullish Engulfing"
    if is_hammer(c):              return "Hammer"
    return None


def bearish_pattern(p, c):
    if is_bearish_engulfing(p, c): return "Bearish Engulfing"
    if is_shooting_star(c):       return "Shooting Star"
    return None


# ---------- State ----------

def _load_state():
    try:
        with open(SIGNAL_STATE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(state):
    try:
        with open(SIGNAL_STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception as e:
        print(f"[VIP] ❌ State save error: {e}")


# ---------- Liquidity targets ----------

def liquidity_targets(candles, direction: str):
    """Return a list of price levels above (LONG) or below (SHORT) current price.

    These are swing highs/lows — natural take-profit zones because liquidity
    sits there.
    """
    highs, lows = find_swings(candles, left=2, right=2)
    price = candles[-1]["close"]
    if direction == "LONG":
        return sorted({h for _, h in highs if h > price})
    else:
        return sorted({l for _, l in lows if l < price}, reverse=True)


# ---------- Signal detection ----------

def detect_signal(symbol: str):
    candles_ltf = get_klines(symbol, LTF_INTERVAL, LTF_LIMIT)
    candles_htf = get_klines(symbol, HTF_INTERVAL, HTF_LIMIT)
    if not candles_ltf or len(candles_ltf) < 60:
        return None
    if not candles_htf or len(candles_htf) < 30:
        return None

    bias = htf_bias(candles_htf)
    if bias == "neutral":
        return None  # no clear HTF direction → skip

    atr = calc_atr(candles_ltf)
    if atr is None or atr <= 0:
        return None

    obs = find_order_blocks(candles_ltf, atr)

    last  = candles_ltf[-1]
    prev  = candles_ltf[-2]
    price = last["close"]

    # ---- LONG side ----
    if bias == "bullish":
        # 1) Find a fresh bullish OB whose zone the current price is inside
        bull_obs = [ob for ob in obs if ob["type"] == "bullish"]
        active_ob = None
        for ob in sorted(bull_obs, key=lambda x: x["index"], reverse=True):
            if ob["low"] <= price <= ob["high"] * 1.005:
                active_ob = ob
                break
        if not active_ob:
            return None

        # 2) Candle reaction inside / off the zone
        pat = bullish_pattern(prev, last)
        green_close = last["close"] > last["open"] and last["close"] > prev["close"]
        reaction = pat is not None or green_close

        # 3) Recent bullish FVG above the OB (confluence)
        fvg = has_recent_fvg(candles_ltf, "bullish", lookback=10)

        # 4) Compute SL / TPs
        sl   = active_ob["low"] - atr * SL_BUFFER_ATR
        risk = price - sl
        if risk <= 0:
            return None
        if risk / price > MAX_SL_PCT:
            return None

        targets = liquidity_targets(candles_ltf, "LONG")
        tp1_natural = targets[0] if targets else price + risk * TP1_RR
        tp1 = max(tp1_natural, price + risk * TP1_RR)  # at minimum 1:2
        tp2 = price + risk * TP2_RR

        # 5) Confluence count
        confluences = {
            "HTF bias bullish": True,
            "Active bullish OB": True,
            "Reaction inside zone": reaction,
            "Bullish FVG nearby": fvg,
            "R:R ≥ 1:2": tp1 / price >= 1 + (MIN_RR_TP1 * risk / price) - 1e-9,
        }
        score = sum(1 for v in confluences.values() if v)
        if score < REQUIRED_CONFLUENCES:
            return None

        return {
            "direction": "LONG",
            "symbol": symbol,
            "price": price,
            "entry": price,
            "sl": sl,
            "tp1": tp1,
            "tp2": tp2,
            "ob_zone": (active_ob["low"], active_ob["high"]),
            "confluences": confluences,
            "pattern": pat,
        }

    # ---- SHORT side ----
    if bias == "bearish":
        bear_obs = [ob for ob in obs if ob["type"] == "bearish"]
        active_ob = None
        for ob in sorted(bear_obs, key=lambda x: x["index"], reverse=True):
            if ob["low"] * 0.995 <= price <= ob["high"]:
                active_ob = ob
                break
        if not active_ob:
            return None

        pat = bearish_pattern(prev, last)
        red_close = last["close"] < last["open"] and last["close"] < prev["close"]
        reaction = pat is not None or red_close

        fvg = has_recent_fvg(candles_ltf, "bearish", lookback=10)

        sl   = active_ob["high"] + atr * SL_BUFFER_ATR
        risk = sl - price
        if risk <= 0:
            return None
        if risk / price > MAX_SL_PCT:
            return None

        targets = liquidity_targets(candles_ltf, "SHORT")
        tp1_natural = targets[0] if targets else price - risk * TP1_RR
        tp1 = min(tp1_natural, price - risk * TP1_RR)
        tp2 = price - risk * TP2_RR

        confluences = {
            "HTF bias bearish": True,
            "Active bearish OB": True,
            "Reaction inside zone": reaction,
            "Bearish FVG nearby": fvg,
            "R:R ≥ 1:2": (price - tp1) / price >= MIN_RR_TP1 * (risk / price) - 1e-9,
        }
        score = sum(1 for v in confluences.values() if v)
        if score < REQUIRED_CONFLUENCES:
            return None

        return {
            "direction": "SHORT",
            "symbol": symbol,
            "price": price,
            "entry": price,
            "sl": sl,
            "tp1": tp1,
            "tp2": tp2,
            "ob_zone": (active_ob["low"], active_ob["high"]),
            "confluences": confluences,
            "pattern": pat,
        }

    return None


# ---------- Format & post ----------

def format_signal_message(sig):
    coin = sig["symbol"].replace("USDT", "")
    direction = sig["direction"]
    arrow = "🟢" if direction == "LONG" else "🔴"
    risk_pct = abs((sig["entry"] - sig["sl"]) / sig["entry"] * 100)
    tp1_pct  = abs((sig["tp1"] - sig["entry"]) / sig["entry"] * 100)
    tp2_pct  = abs((sig["tp2"] - sig["entry"]) / sig["entry"] * 100)
    rr1 = abs((sig["tp1"] - sig["entry"]) / (sig["entry"] - sig["sl"]))
    rr2 = abs((sig["tp2"] - sig["entry"]) / (sig["entry"] - sig["sl"]))

    confluences = "\n".join(
        f"{'✅' if v else '◻️'} {k}" for k, v in sig["confluences"].items()
    )

    ob_lo, ob_hi = sig["ob_zone"]

    return f"""{arrow} <b>SMC SWING SIGNAL — {coin}/USDT ({direction})</b>

📍 Entry: ${sig['entry']:,.2f}
🛑 Stop Loss: ${sig['sl']:,.2f} (−{risk_pct:.2f}%)
🎯 TP1: ${sig['tp1']:,.2f} (+{tp1_pct:.2f}%) | R:R 1:{rr1:.2f}
🎯 TP2: ${sig['tp2']:,.2f} (+{tp2_pct:.2f}%) | R:R 1:{rr2:.2f}

📦 Order Block: ${ob_lo:,.2f} – ${ob_hi:,.2f}
🕯 Candle pattern: {sig.get('pattern') or 'In-zone reaction'}
⏱ Timeframe: {LTF_INTERVAL.upper()} (HTF bias: {HTF_INTERVAL.upper()})
📐 Method: Smart Money Concepts (Order Block + FVG + Liquidity)

<b>Confluence checklist:</b>
{confluences}

⚠️ <b>Risk Management</b>
• Use 1–2% of portfolio per trade
• Always respect Stop Loss
• This is not financial advice

🇦🇪 AlphaDXB | Dubai Crypto Signals
#{coin.lower()} #crypto #SMC #orderblock #signals #AlphaDXB"""


# ---------- Public entry point ----------

def scan_and_post(telegram_token: str, vip_channel: str) -> None:
    state  = _load_state()
    now_ts = datetime.now().timestamp()
    cooldown = COOLDOWN_HOURS * 3600

    print(f"\n[VIP] [{datetime.now().strftime('%H:%M')}] SMC scan ({len(COINS)} coins)...")
    for coin in COINS:
        last = state.get(coin, {}).get("timestamp", 0)
        if now_ts - last < cooldown:
            print(f"[VIP]   {coin}: cooldown ({(now_ts - last)/3600:.1f}h)")
            continue
        try:
            sig = detect_signal(coin)
        except Exception as e:
            print(f"[VIP]   {coin}: detect error: {e}")
            continue
        if sig:
            print(f"[VIP]   {coin}: {sig['direction']} @ ${sig['price']:,.2f} "
                  f"(OB ${sig['ob_zone'][0]:,.2f}–${sig['ob_zone'][1]:,.2f})")
            _send(telegram_token, vip_channel, format_signal_message(sig))
            state[coin] = {
                "timestamp": now_ts,
                "direction": sig["direction"],
                "entry": sig["entry"],
            }
            _save_state(state)
        else:
            print(f"[VIP]   {coin}: no setup")
