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
    send_reply(token, channel, reply_to_message_id, text) -> bool
"""

import json
import time
from datetime import datetime
from io import BytesIO

import requests
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle


# ---------- Tweakable settings ----------

COINS = [
    # Tier 1 — always included
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
    # Tier 2 — high-liquidity alts
    "AVAXUSDT", "LINKUSDT", "DOTUSDT", "ADAUSDT", "ATOMUSDT",
    "NEARUSDT", "LTCUSDT", "SUIUSDT", "APTUSDT",
]
COOLDOWN_HOURS   = 12
SIGNAL_STATE_FILE = "signal_state.json"

HTF_INTERVAL     = "1d"    # daily bias — entry direction
WTF_INTERVAL     = "1w"    # weekly bias — must align with daily for swing trades
LTF_INTERVAL     = "4h"
HTF_LIMIT        = 100
WTF_LIMIT        = 52      # 1 year of weekly candles
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
OB_APPROACH_PCT  = 0.02   # allow entry up to 2% outside OB zone (approaching)

REQUIRED_CONFLUENCES = 3  # of 5 (HTF, OB, mitigation+pattern, FVG, R:R)


# ---------- Telegram (with retry) ----------

def _send(token: str, channel: str, text: str, retries: int = 3):
    """Send a Telegram message. Returns message_id (int) on success, None on failure."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": channel, "text": text, "parse_mode": "HTML"}
    for attempt in range(1, retries + 1):
        try:
            r = requests.post(url, json=payload, timeout=10)
            print(f"[VIP] Send attempt {attempt}: {r.status_code}")
            if r.status_code == 200:
                return r.json().get("result", {}).get("message_id")
            if 400 <= r.status_code < 500:
                print(f"[VIP]   Body: {r.text[:200]} (4xx — not retrying)")
                return None
        except Exception as e:
            print(f"[VIP] ❌ Send error attempt {attempt}: {e}")
        time.sleep(2 ** attempt)
    return None


def _send_photo(token: str, channel: str, photo_bytes: bytes, caption: str,
                retries: int = 3):
    """Post a photo with caption. Returns message_id (int) on success, None on failure.
    Falls back to text-only if photo upload fails."""
    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    for attempt in range(1, retries + 1):
        try:
            r = requests.post(
                url,
                files={"photo": ("signal.png", BytesIO(photo_bytes), "image/png")},
                data={"chat_id": channel, "caption": caption, "parse_mode": "HTML"},
                timeout=60,
            )
            print(f"[VIP] Photo attempt {attempt}: {r.status_code}")
            if r.status_code == 200:
                return r.json().get("result", {}).get("message_id")
            if 400 <= r.status_code < 500:
                print(f"[VIP]   Photo 4xx body: {r.text[:200]} → falling back to text")
                return _send(token, channel, caption)
        except Exception as e:
            print(f"[VIP] ❌ Photo error attempt {attempt}: {e}")
        time.sleep(2 ** attempt)
    print("[VIP] ⚠️ Photo retries exhausted → text fallback")
    return _send(token, channel, caption)


def send_reply(token: str, channel: str, reply_to_message_id: int, text: str,
               retries: int = 3) -> bool:
    """Reply to a specific message in a Telegram channel/group.

    Used by the journal tracker to notify when TP/SL is hit.
    Returns True on success, False on failure.
    """
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": channel,
        "text": text,
        "parse_mode": "HTML",
        "reply_to_message_id": reply_to_message_id,
    }
    for attempt in range(1, retries + 1):
        try:
            r = requests.post(url, json=payload, timeout=10)
            print(f"[REPLY] Attempt {attempt}: {r.status_code}")
            if r.status_code == 200:
                return True
            if 400 <= r.status_code < 500:
                print(f"[REPLY] 4xx: {r.text[:200]}")
                return False
        except Exception as e:
            print(f"[REPLY] ❌ Error attempt {attempt}: {e}")
        time.sleep(2 ** attempt)
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
    fvgs = find_fvgs(candles)
    n = len(candles)
    for fvg in fvgs[-15:]:
        if fvg["type"] == direction and (n - 1 - fvg["index"]) <= lookback:
            return True
    return False


def latest_fvg_zone(candles, direction: str, lookback: int = 10):
    """Return (low, high, index) of the most recent same-direction FVG, or None."""
    fvgs = find_fvgs(candles)
    n = len(candles)
    for fvg in reversed(fvgs[-20:]):
        if fvg["type"] == direction and (n - 1 - fvg["index"]) <= lookback:
            return (fvg["low"], fvg["high"], fvg["index"])
    return None


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
    candles_wtf = get_klines(symbol, WTF_INTERVAL, WTF_LIMIT)
    if not candles_ltf or len(candles_ltf) < 60:
        print(f"[VIP]   {symbol}: ❌ not enough LTF candles "
              f"({len(candles_ltf) if candles_ltf else 0})")
        return None
    if not candles_htf or len(candles_htf) < 30:
        print(f"[VIP]   {symbol}: ❌ not enough HTF candles "
              f"({len(candles_htf) if candles_htf else 0})")
        return None

    # Weekly bias — swing trades must align with weekly trend
    bias_weekly = htf_bias(candles_wtf) if candles_wtf and len(candles_wtf) >= 6 else "neutral"
    bias = htf_bias(candles_htf)

    if bias == "neutral":
        highs, lows = find_swings(candles_htf, left=2, right=2)
        print(f"[VIP]   {symbol}: ❌ Daily bias neutral "
              f"(swings found — highs:{len(highs)}, lows:{len(lows)})")
        return None

    # Daily and Weekly must agree — no swing trading against the weekly trend
    if bias_weekly != "neutral" and bias != bias_weekly:
        print(f"[VIP]   {symbol}: ❌ Trend conflict — Daily={bias}, Weekly={bias_weekly} — no swing against weekly trend")
        return None

    print(f"[VIP]   {symbol}: Daily={bias} Weekly={bias_weekly} ✅")

    print(f"[VIP]   {symbol}: HTF bias={bias}")

    atr = calc_atr(candles_ltf)
    if atr is None or atr <= 0:
        print(f"[VIP]   {symbol}: ❌ ATR calculation failed")
        return None

    obs = find_order_blocks(candles_ltf, atr)
    print(f"[VIP]   {symbol}: found {len(obs)} valid OB(s) on {LTF_INTERVAL}")

    last  = candles_ltf[-1]
    prev  = candles_ltf[-2]
    price = last["close"]

    # ---- LONG side ----
    if bias == "bullish":
        bull_obs = [ob for ob in obs if ob["type"] == "bullish"]
        print(f"[VIP]   {symbol}: bullish OBs={len(bull_obs)}, price=${price:,.2f}")
        active_ob = None
        for ob in sorted(bull_obs, key=lambda x: x["index"], reverse=True):
            # Price inside OB OR within OB_APPROACH_PCT above OB high (falling toward zone)
            if ob["low"] <= price <= ob["high"] * (1 + OB_APPROACH_PCT):
                active_ob = ob
                break
        if not active_ob:
            if bull_obs:
                nearest = sorted(bull_obs, key=lambda x: abs((x["low"]+x["high"])/2 - price))
                ob0 = nearest[0]
                print(f"[VIP]   {symbol}: ❌ price not in any bullish OB "
                      f"(nearest OB: ${ob0['low']:,.2f}–${ob0['high']:,.2f})")
            else:
                print(f"[VIP]   {symbol}: ❌ no active bullish OBs found")
            return None

        pat = bullish_pattern(prev, last)
        reaction = pat is not None  # require real pattern — no weak "green close"

        fvg = has_recent_fvg(candles_ltf, "bullish", lookback=10)
        fvg_zone = latest_fvg_zone(candles_ltf, "bullish", lookback=10)

        sl   = active_ob["low"] - atr * SL_BUFFER_ATR
        risk = price - sl
        if risk <= 0:
            print(f"[VIP]   {symbol}: ❌ risk <= 0")
            return None
        if risk / price > MAX_SL_PCT:
            print(f"[VIP]   {symbol}: ❌ SL too wide ({risk/price*100:.2f}% > {MAX_SL_PCT*100:.0f}%)")
            return None

        targets = liquidity_targets(candles_ltf, "LONG")
        tp1_natural = targets[0] if targets else price + risk * TP1_RR
        tp1 = max(tp1_natural, price + risk * TP1_RR)
        tp2 = price + risk * TP2_RR

        confluences = {
            "HTF bias bullish": True,
            "Active bullish OB": True,
            "Reaction inside zone": reaction,
            "Bullish FVG nearby": fvg,
            "R:R ≥ 1:2": tp1 / price >= 1 + (MIN_RR_TP1 * risk / price) - 1e-9,
        }
        score = sum(1 for v in confluences.values() if v)
        print(f"[VIP]   {symbol}: confluences {score}/{len(confluences)} — "
              + ", ".join(f"{k}={'✅' if v else '❌'}" for k, v in confluences.items()))
        if score < REQUIRED_CONFLUENCES:
            print(f"[VIP]   {symbol}: ❌ insufficient confluences ({score} < {REQUIRED_CONFLUENCES})")
            return None

        swing_highs, swing_lows = find_swings(candles_ltf, left=2, right=2)
        return {
            "direction": "LONG",
            "symbol": symbol,
            "price": price,
            "entry": price,
            "sl": sl,
            "tp1": tp1,
            "tp2": tp2,
            "ob_zone": (active_ob["low"], active_ob["high"]),
            "ob_index": active_ob["index"],
            "fvg_zone": fvg_zone,
            "candles": candles_ltf,
            "swing_highs": swing_highs,
            "swing_lows": swing_lows,
            "confluences": confluences,
            "pattern": pat,
        }

    # ---- SHORT side ----
    if bias == "bearish":
        bear_obs = [ob for ob in obs if ob["type"] == "bearish"]
        print(f"[VIP]   {symbol}: bearish OBs={len(bear_obs)}, price=${price:,.2f}")
        active_ob = None
        for ob in sorted(bear_obs, key=lambda x: x["index"], reverse=True):
            # Price inside OB OR within OB_APPROACH_PCT below OB low (rising toward zone)
            if ob["low"] * (1 - OB_APPROACH_PCT) <= price <= ob["high"]:
                active_ob = ob
                break
        if not active_ob:
            if bear_obs:
                nearest = sorted(bear_obs, key=lambda x: abs((x["low"]+x["high"])/2 - price))
                ob0 = nearest[0]
                print(f"[VIP]   {symbol}: ❌ price not in any bearish OB "
                      f"(nearest OB: ${ob0['low']:,.2f}–${ob0['high']:,.2f})")
            else:
                print(f"[VIP]   {symbol}: ❌ no active bearish OBs found")
            return None

        pat = bearish_pattern(prev, last)
        reaction = pat is not None  # require real pattern — no weak "red close"

        fvg = has_recent_fvg(candles_ltf, "bearish", lookback=10)
        fvg_zone = latest_fvg_zone(candles_ltf, "bearish", lookback=10)

        sl   = active_ob["high"] + atr * SL_BUFFER_ATR
        risk = sl - price
        if risk <= 0:
            print(f"[VIP]   {symbol}: ❌ risk <= 0")
            return None
        if risk / price > MAX_SL_PCT:
            print(f"[VIP]   {symbol}: ❌ SL too wide ({risk/price*100:.2f}% > {MAX_SL_PCT*100:.0f}%)")
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
        print(f"[VIP]   {symbol}: confluences {score}/{len(confluences)} — "
              + ", ".join(f"{k}={'✅' if v else '❌'}" for k, v in confluences.items()))
        if score < REQUIRED_CONFLUENCES:
            print(f"[VIP]   {symbol}: ❌ insufficient confluences ({score} < {REQUIRED_CONFLUENCES})")
            return None

        swing_highs, swing_lows = find_swings(candles_ltf, left=2, right=2)
        return {
            "direction": "SHORT",
            "symbol": symbol,
            "price": price,
            "entry": price,
            "sl": sl,
            "tp1": tp1,
            "tp2": tp2,
            "ob_zone": (active_ob["low"], active_ob["high"]),
            "ob_index": active_ob["index"],
            "fvg_zone": fvg_zone,
            "candles": candles_ltf,
            "swing_highs": swing_highs,
            "swing_lows": swing_lows,
            "confluences": confluences,
            "pattern": pat,
        }

    return None


# ---------- Chart drawing ----------

def build_signal_chart(sig) -> bytes:
    """Render a chart that visually explains the setup.

    Marks: candlesticks, the Order Block zone, the FVG zone (if any),
    Entry/SL/TP1/TP2 horizontal lines, and the OB-origin candle.
    """
    candles = sig["candles"]
    direction = sig["direction"]
    coin = sig["symbol"].replace("USDT", "")
    ob_low, ob_high = sig["ob_zone"]
    ob_idx_global = sig["ob_index"]

    # Show last N candles (centered around the OB so it's always visible).
    show_n = 70
    n = len(candles)
    start = max(0, min(n - show_n, ob_idx_global - 10))
    end = n
    show = candles[start:end]
    ob_idx = ob_idx_global - start  # local index

    plt.style.use("default")
    fig, ax = plt.subplots(figsize=(13, 8))
    fig.patch.set_facecolor("white")
    ax.set_facecolor("white")

    bull_clr, bear_clr = "#16a085", "#e74c3c"

    # Candlesticks
    for i, c in enumerate(show):
        clr = bull_clr if c["close"] >= c["open"] else bear_clr
        ax.plot([i, i], [c["low"], c["high"]], color=clr, linewidth=1.0)
        body_h = abs(c["close"] - c["open"]) or (c["high"] - c["low"]) * 0.01
        ax.bar(i, body_h, bottom=min(c["open"], c["close"]),
               color=clr, width=0.7, alpha=0.95)

    x_right = len(show) + 12
    ax.set_xlim(-1, x_right)

    # OB zone — strong color band
    ob_clr = bull_clr if direction == "LONG" else bear_clr
    ax.axhspan(ob_low, ob_high, alpha=0.12, color=ob_clr, zorder=0)
    # Mark the OB origin candle
    if 0 <= ob_idx < len(show):
        ax.scatter([ob_idx], [(ob_low + ob_high) / 2],
                   marker="o", s=120, edgecolors=ob_clr,
                   facecolors="none", linewidths=2, zorder=5)
    ax.text(x_right - 1, (ob_low + ob_high) / 2,
            f" OB ${ob_low:,.0f}–${ob_high:,.0f}",
            color=ob_clr, fontsize=10, fontweight="bold",
            va="center", ha="right")

    # FVG zone
    if sig.get("fvg_zone"):
        fvg_low, fvg_high, fvg_idx = sig["fvg_zone"]
        ax.axhspan(fvg_low, fvg_high, alpha=0.12, color="#2980b9", zorder=0)
        local = fvg_idx - start
        if 0 <= local < len(show):
            ax.text(local, fvg_high, " FVG",
                    color="#2980b9", fontsize=9, fontweight="bold",
                    va="bottom", ha="left")

    # Entry / SL / TP lines
    levels = [
        (sig["entry"], "Entry", "#f39c12", "-",  1.8),
        (sig["sl"],    "SL",    "#e74c3c", "--", 1.6),
        (sig["tp1"],   "TP1",   "#16a085", "--", 1.4),
        (sig["tp2"],   "TP2",   "#16a085", ":",  1.2),
    ]
    for price_v, label, clr, ls, lw in levels:
        ax.axhline(price_v, color=clr, linewidth=lw, linestyle=ls, alpha=0.95)
        ax.text(x_right - 1, price_v, f" {label}: ${price_v:,.2f}",
                color=clr, fontsize=9, fontweight="bold",
                va="center", ha="right",
                bbox=dict(boxstyle="round,pad=0.2", fc="white", ec=clr, alpha=0.9))

    # Title + subtitle
    arrow = "▲" if direction == "LONG" else "▼"
    ax.set_title(f"AlphaDXB | {coin}/USDT  {arrow} {direction}  —  4H SMC Setup",
                 color="#1a1a2e", fontsize=14, fontweight="bold", pad=15)
    confluence_str = ", ".join(k for k, v in sig["confluences"].items() if v)
    ax.text(0.5, 1.005, confluence_str, transform=ax.transAxes,
            color="#555555", fontsize=8, ha="center", va="bottom")

    # Cosmetics
    ax.grid(color="#e0e0e0", linewidth=0.5, alpha=0.8)
    ax.tick_params(colors="#333333", labelsize=8)
    ax.set_xticks([])
    ax.yaxis.tick_right()
    for s in ["top", "left"]:
        ax.spines[s].set_visible(False)
    for s in ["bottom", "right"]:
        ax.spines[s].set_color("#cccccc")

    # Watermark
    fig.text(0.5, 0.5, "AlphaDXB", fontsize=55, color="black", alpha=0.04,
             ha="center", va="center", fontweight="bold", rotation=30)

    plt.tight_layout(pad=2)
    buf = BytesIO()
    plt.savefig(buf, format="png", dpi=150, bbox_inches="tight", facecolor="white")
    buf.seek(0)
    plt.close(fig)
    return buf.read()


# ---------- Public-channel teaser ----------

def build_teaser_chart(sig) -> bytes:
    """Lighter chart for the public channel — shows only the OB and FVG zones,
    NOT the exact Entry/SL/TP lines (those are VIP value)."""
    candles = sig["candles"]
    direction = sig["direction"]
    coin = sig["symbol"].replace("USDT", "")
    ob_low, ob_high = sig["ob_zone"]
    ob_idx_global = sig["ob_index"]

    show_n = 70
    n = len(candles)
    start = max(0, min(n - show_n, ob_idx_global - 10))
    show = candles[start:n]
    ob_idx = ob_idx_global - start

    plt.style.use("default")
    fig, ax = plt.subplots(figsize=(13, 7.5))
    fig.patch.set_facecolor("white")
    ax.set_facecolor("white")

    bull_clr, bear_clr = "#16a085", "#e74c3c"

    for i, c in enumerate(show):
        clr = bull_clr if c["close"] >= c["open"] else bear_clr
        ax.plot([i, i], [c["low"], c["high"]], color=clr, linewidth=1.0)
        body_h = abs(c["close"] - c["open"]) or (c["high"] - c["low"]) * 0.01
        ax.bar(i, body_h, bottom=min(c["open"], c["close"]),
               color=clr, width=0.7, alpha=0.95)

    x_right = len(show) + 12
    ax.set_xlim(-1, x_right)

    # OB zone — clearly highlighted
    ob_clr = bull_clr if direction == "LONG" else bear_clr
    ax.axhspan(ob_low, ob_high, alpha=0.15, color=ob_clr, zorder=0)
    if 0 <= ob_idx < len(show):
        ax.scatter([ob_idx], [(ob_low + ob_high) / 2],
                   marker="o", s=140, edgecolors=ob_clr,
                   facecolors="none", linewidths=2.2, zorder=5)
    ax.text(x_right - 1, (ob_low + ob_high) / 2, " Watch zone",
            color=ob_clr, fontsize=11, fontweight="bold",
            va="center", ha="right",
            bbox=dict(boxstyle="round,pad=0.25", fc="white", ec=ob_clr, alpha=0.9))

    # FVG zone
    if sig.get("fvg_zone"):
        fvg_low, fvg_high, fvg_idx = sig["fvg_zone"]
        ax.axhspan(fvg_low, fvg_high, alpha=0.12, color="#2980b9", zorder=0)
        local = fvg_idx - start
        if 0 <= local < len(show):
            ax.text(local, fvg_high, " FVG",
                    color="#2980b9", fontsize=9, fontweight="bold",
                    va="bottom", ha="left")

    arrow = "▲" if direction == "LONG" else "▼"
    ax.set_title(
        f"AlphaDXB | {coin}/USDT  {arrow} {direction} bias  —  4H Price Action",
        color="#1a1a2e", fontsize=14, fontweight="bold", pad=15,
    )
    ax.text(0.5, 1.005,
            "Full Entry / SL / TP available in VIP channel",
            transform=ax.transAxes, color="#555555", fontsize=9,
            ha="center", va="bottom")

    ax.grid(color="#e0e0e0", linewidth=0.5, alpha=0.8)
    ax.tick_params(colors="#333333", labelsize=8)
    ax.set_xticks([])
    ax.yaxis.tick_right()
    for s in ["top", "left"]:
        ax.spines[s].set_visible(False)
    for s in ["bottom", "right"]:
        ax.spines[s].set_color("#cccccc")

    fig.text(0.5, 0.5, "AlphaDXB", fontsize=55, color="black", alpha=0.04,
             ha="center", va="center", fontweight="bold", rotation=30)

    plt.tight_layout(pad=2)
    buf = BytesIO()
    plt.savefig(buf, format="png", dpi=150, bbox_inches="tight", facecolor="white")
    buf.seek(0)
    plt.close(fig)
    return buf.read()


def format_public_teaser(sig, vip_link: str = "") -> str:
    coin = sig["symbol"].replace("USDT", "")
    direction = sig["direction"]
    arrow = "🟢" if direction == "LONG" else "🔴"
    ob_lo, ob_hi = sig["ob_zone"]
    bias_word = "Bullish" if direction == "LONG" else "Bearish"
    confluences = "\n".join(
        f"✓ {k}" for k, v in sig["confluences"].items() if v and "R:R" not in k
    )
    vip_call = (
        f"🔓 Full Entry / SL / TP details available in our VIP channel:\n👉 {vip_link}"
        if vip_link else
        "🔓 Full Entry / SL / TP details available in our VIP channel."
    )

    return f"""{arrow} <b>Price Action Setup — {coin}/USDT</b>

📍 Direction: <b>{bias_word} bias (4H)</b>
📦 Watch zone: ${ob_lo:,.2f} – ${ob_hi:,.2f}

<b>Why this level matters:</b>
{confluences}

⚠️ This is a notable price-action level — <b>not financial advice</b>. Always do your own research.

{vip_call}

🇦🇪 AlphaDXB | Dubai Crypto Signals
#{coin.lower()} #priceaction #crypto #AlphaDXB"""


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


# ---------- Manual test helper ----------

def send_test_signal(telegram_token: str, vip_channel: str, symbol: str = "BTCUSDT") -> None:
    """Build a synthetic but realistic signal + chart and post it to VIP.

    Useful to verify the pipeline (chart rendering, photo upload, channel
    delivery) without waiting for a real setup. The signal is clearly marked
    as TEST in the caption so subscribers don't trade it.
    """
    print(f"[VIP] Building TEST signal for {symbol}...")
    candles = get_klines(symbol, LTF_INTERVAL, LTF_LIMIT)
    if not candles or len(candles) < 60:
        print("[VIP] ❌ TEST: could not fetch candles")
        _send(telegram_token, vip_channel,
              "🧪 <b>TEST SIGNAL</b>\nCould not fetch market data right now.")
        return

    atr = calc_atr(candles) or candles[-1]["close"] * 0.01
    price = candles[-1]["close"]
    # Pick a synthetic OB just below current price, ~1% wide
    ob_high = price * 0.995
    ob_low  = price * 0.985
    sl   = ob_low - atr * SL_BUFFER_ATR
    risk = price - sl
    tp1  = price + risk * TP1_RR
    tp2  = price + risk * TP2_RR

    swing_highs, swing_lows = find_swings(candles, left=2, right=2)
    fvg_zone = latest_fvg_zone(candles, "bullish", lookback=15)

    fake_sig = {
        "direction": "LONG",
        "symbol": symbol,
        "price": price,
        "entry": price,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "ob_zone": (ob_low, ob_high),
        "ob_index": len(candles) - 5,
        "fvg_zone": fvg_zone,
        "candles": candles,
        "swing_highs": swing_highs,
        "swing_lows": swing_lows,
        "confluences": {
            "HTF bias bullish": True,
            "Active bullish OB": True,
            "Reaction inside zone": True,
            "Bullish FVG nearby": fvg_zone is not None,
            "R:R ≥ 1:2": True,
        },
        "pattern": "Bullish Engulfing",
    }

    test_caption = "🧪 <b>TEST SIGNAL — DO NOT TRADE</b>\nPipeline / chart visualization check.\n\n" + \
                   format_signal_message(fake_sig)

    try:
        chart = build_signal_chart(fake_sig)
        _send_photo(telegram_token, vip_channel, chart, test_caption)
    except Exception as e:
        print(f"[VIP] ❌ TEST chart error: {e}")
        _send(telegram_token, vip_channel, test_caption)


# ---------- Public entry point ----------

_JOURNAL_FILE = "signals_journal.json"

def _has_open_vip_signal(coin: str) -> bool:
    """Return True if there is already an open VIP signal for this coin in the journal."""
    try:
        with open(_JOURNAL_FILE, "r") as f:
            journal = json.load(f)
        for s in journal.get("signals", []):
            if (s["coin"] == coin
                    and s["status"] == "open"
                    and s.get("channel") == "vip"):
                print(f"[VIP]   {coin}: ⏸ open signal already in journal — skipping")
                return True
    except Exception as e:
        print(f"[VIP]   {coin}: journal check error: {e}")
    return False


def scan_and_post(telegram_token: str, vip_channel: str,
                  public_channel: str = "", vip_link: str = "",
                  on_posted=None) -> None:
    """Scan for setups. When one fires:
       - full chart + caption → vip_channel
       - teaser chart + teaser caption → public_channel (if provided)
       - on_posted(sig, message_id, channel) called if provided, for journaling
    """
    state  = _load_state()
    now_ts = datetime.now().timestamp()
    cooldown = COOLDOWN_HOURS * 3600

    print(f"\n[VIP] [{datetime.now().strftime('%H:%M')}] SMC scan ({len(COINS)} coins)...")
    for coin in COINS:
        last = state.get(coin, {}).get("timestamp", 0)
        if now_ts - last < cooldown:
            print(f"[VIP]   {coin}: cooldown ({(now_ts - last)/3600:.1f}h)")
            continue
        if _has_open_vip_signal(coin):
            continue
        try:
            sig = detect_signal(coin)
        except Exception as e:
            print(f"[VIP]   {coin}: detect error: {e}")
            continue
        if not sig:
            continue  # detect_signal already printed the reason

        print(f"[VIP]   {coin}: ✅ {sig['direction']} @ ${sig['price']:,.2f} "
              f"(OB ${sig['ob_zone'][0]:,.2f}–${sig['ob_zone'][1]:,.2f})")

        # 1) Full signal → VIP
        vip_caption = format_signal_message(sig)
        message_id = None
        try:
            vip_chart = build_signal_chart(sig)
            message_id = _send_photo(telegram_token, vip_channel, vip_chart, vip_caption)
        except Exception as e:
            print(f"[VIP]   {coin}: VIP chart build failed: {e} — text only")
            message_id = _send(telegram_token, vip_channel, vip_caption)

        # Notify caller (alphadxb_bot) so it can journal the signal with message_id
        if on_posted and message_id:
            try:
                on_posted(sig, message_id, vip_channel)
            except Exception as e:
                print(f"[VIP]   {coin}: on_posted callback error: {e}")

        # 2) Teaser → public (if a public channel was provided)
        if public_channel:
            try:
                teaser_caption = format_public_teaser(sig, vip_link=vip_link)
                teaser_chart   = build_teaser_chart(sig)
                _send_photo(telegram_token, public_channel, teaser_chart, teaser_caption)
            except Exception as e:
                print(f"[VIP]   {coin}: teaser failed: {e}")

        state[coin] = {
            "timestamp": now_ts,
            "direction": sig["direction"],
            "entry": sig["entry"],
        }
        _save_state(state)
