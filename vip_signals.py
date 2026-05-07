"""
VIP swing-signal engine.

Self-contained module: fetches OHLCV from Binance, computes indicators,
detects swing-trade setups, and posts signals to the VIP channel.

Public API:
    scan_and_post(telegram_token: str, vip_channel: str) -> None
        Scan all watched coins once and post any qualifying signals.

Edit this file when you want to change:
    - Which coins are watched (COINS list below)
    - Indicator thresholds (RSI ranges, ATR multiplier, EMA periods)
    - Signal-message format (format_signal_message)
    - Cooldown between signals (COOLDOWN_HOURS)
"""

import json
from datetime import datetime
from io import BytesIO

import requests


# ---------- Tweakable settings ----------

COINS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"]
COOLDOWN_HOURS = 12          # don't repeat a signal for the same coin within X hours
SIGNAL_STATE_FILE = "signal_state.json"
ATR_STOP_MULT = 1.5          # SL distance = ATR * this
TP1_RR = 2                   # TP1 = entry +/- (risk * 2)
TP2_RR = 3                   # TP2 = entry +/- (risk * 3)
NEAR_LEVEL_PCT = 0.04        # "near" support/resistance = within 4%
RSI_LONG_MIN, RSI_LONG_MAX = 30, 55
RSI_SHORT_MIN, RSI_SHORT_MAX = 45, 70
MIN_CONDITIONS = 4           # out of 5 to trigger a signal


# ---------- Telegram send ----------

def _send(token: str, channel: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        r = requests.post(
            url,
            json={"chat_id": channel, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
        print(f"[VIP] Send: {r.status_code}")
    except Exception as e:
        print(f"[VIP] ❌ Send error: {e}")


# ---------- Market data (Bybit, geo-friendly) ----------

# Bybit interval mapping (minutes for intraday, "D" for daily, "W" for weekly)
_BYBIT_INTERVAL = {
    "1m":  "1",   "3m":  "3",   "5m":  "5",   "15m": "15", "30m": "30",
    "1h":  "60",  "2h":  "120", "4h":  "240", "6h":  "360", "12h": "720",
    "1d":  "D",   "1w":  "W",   "1M":  "M",
}


def get_klines(symbol: str, interval: str, limit: int = 100):
    """Fetch OHLCV candles from Bybit public API (no geo block on US Railway).

    Bybit returns newest-first; we reverse so the list ends with the most
    recent candle (same shape the rest of the code expects).
    """
    bybit_interval = _BYBIT_INTERVAL.get(interval, interval)
    try:
        r = requests.get(
            "https://api.bybit.com/v5/market/kline",
            params={
                "category": "spot",
                "symbol": symbol,
                "interval": bybit_interval,
                "limit": limit,
            },
            timeout=15,
        )
        if r.status_code != 200:
            print(f"[VIP] ❌ Bybit {symbol} {interval}: HTTP {r.status_code}")
            return None
        data = r.json()
        if data.get("retCode") != 0:
            print(f"[VIP] ❌ Bybit {symbol} {interval}: {data.get('retMsg')}")
            return None
        rows = data.get("result", {}).get("list", [])
        # Bybit format: [start_ms, open, high, low, close, volume, turnover]
        # Newest first → reverse so newest is last (consistent with rest of code).
        rows = list(reversed(rows))
        return [
            {
                "time":   datetime.fromtimestamp(int(k[0]) / 1000),
                "open":   float(k[1]),
                "high":   float(k[2]),
                "low":    float(k[3]),
                "close":  float(k[4]),
                "volume": float(k[5]),
            }
            for k in rows
        ]
    except Exception as e:
        print(f"[VIP] ❌ Bybit error {symbol}: {e}")
        return None


# ---------- Indicators ----------

def calc_ema(values, period):
    if len(values) < period:
        return [None] * len(values)
    multiplier = 2 / (period + 1)
    ema = [None] * (period - 1)
    ema.append(sum(values[:period]) / period)
    for i in range(period, len(values)):
        ema.append((values[i] - ema[-1]) * multiplier + ema[-1])
    return ema


def calc_rsi(values, period=14):
    if len(values) < period + 1:
        return [None] * len(values)
    gains, losses = [], []
    for i in range(1, len(values)):
        d = values[i] - values[i - 1]
        gains.append(max(d, 0))
        losses.append(abs(min(d, 0)))
    rsi = [None] * period
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    rsi.append(100 if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l))
    for i in range(period, len(gains)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
        rsi.append(100 if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l))
    return rsi


def calc_macd(values, fast=12, slow=26, signal=9):
    ema_fast = calc_ema(values, fast)
    ema_slow = calc_ema(values, slow)
    macd_line = [None if (f is None or s is None) else f - s for f, s in zip(ema_fast, ema_slow)]
    valid = [x for x in macd_line if x is not None]
    if len(valid) < signal:
        return macd_line, [None] * len(macd_line), [None] * len(macd_line)
    leading = len(macd_line) - len(valid)
    sig_ema = calc_ema(valid, signal)
    signal_line = [None] * leading + sig_ema
    histogram = [None if (m is None or s is None) else m - s for m, s in zip(macd_line, signal_line)]
    return macd_line, signal_line, histogram


def calc_atr(highs, lows, closes, period=14):
    if len(highs) < period + 1:
        return [None] * len(highs)
    trs = [None]
    for i in range(1, len(highs)):
        trs.append(max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        ))
    atr = [None] * period
    atr.append(sum(trs[1:period + 1]) / period)
    for i in range(period + 1, len(trs)):
        atr.append((atr[-1] * (period - 1) + trs[i]) / period)
    return atr


# ---------- State (cooldown) ----------

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


# ---------- Signal detection ----------

def _swing_levels(candles, lookback=30):
    recent = candles[-lookback:]
    return min(c["low"] for c in recent), max(c["high"] for c in recent)


def detect_signal(symbol: str):
    """Return a signal dict or None."""
    c4 = get_klines(symbol, "4h", 100)
    cd = get_klines(symbol, "1d", 60)
    if not c4 or len(c4) < 60 or not cd or len(cd) < 50:
        return None

    closes_4h = [c["close"] for c in c4]
    highs_4h  = [c["high"]  for c in c4]
    lows_4h   = [c["low"]   for c in c4]
    closes_1d = [c["close"] for c in cd]

    e20 = calc_ema(closes_4h, 20)[-1]
    e50 = calc_ema(closes_4h, 50)[-1]
    rsi = calc_rsi(closes_4h, 14)[-1]
    _, _, hist = calc_macd(closes_4h)
    atr = calc_atr(highs_4h, lows_4h, closes_4h, 14)[-1]
    daily_e50 = calc_ema(closes_1d, 50)[-1]
    price = closes_4h[-1]
    daily_close = closes_1d[-1]
    hist_now = hist[-1]
    hist_prev = hist[-2] if len(hist) >= 2 else None

    if any(x is None for x in [e20, e50, rsi, hist_now, hist_prev, atr, daily_e50]):
        return None

    support, resistance = _swing_levels(c4, 30)

    daily_up = daily_close > daily_e50
    h4_up    = e20 > e50
    rsi_rec  = RSI_LONG_MIN <= rsi <= RSI_LONG_MAX
    macd_bull = hist_prev <= 0 and hist_now > 0
    near_sup = (price - support) / price < NEAR_LEVEL_PCT
    long_score = sum([daily_up, h4_up, rsi_rec, macd_bull, near_sup])

    daily_dn = daily_close < daily_e50
    h4_dn    = e20 < e50
    rsi_dec  = RSI_SHORT_MIN <= rsi <= RSI_SHORT_MAX
    macd_bear = hist_prev >= 0 and hist_now < 0
    near_res = (resistance - price) / price < NEAR_LEVEL_PCT
    short_score = sum([daily_dn, h4_dn, rsi_dec, macd_bear, near_res])

    if long_score >= MIN_CONDITIONS:
        risk = atr * ATR_STOP_MULT
        return {
            "direction": "LONG", "symbol": symbol, "price": price, "entry": price,
            "sl": price - risk, "tp1": price + risk * TP1_RR, "tp2": price + risk * TP2_RR,
            "rsi": rsi, "support": support, "resistance": resistance,
            "reasons": {
                "Daily trend": "bullish" if daily_up else "neutral",
                "4H trend":    "bullish (EMA20 > EMA50)" if h4_up else "neutral",
                "RSI":         f"recovering ({rsi:.1f})" if rsi_rec else f"{rsi:.1f}",
                "MACD":        "bullish crossover" if macd_bull else "still negative",
                "Support":     f"near ${support:,.2f}" if near_sup else f"above ${support:,.2f}",
            },
        }

    if short_score >= MIN_CONDITIONS:
        risk = atr * ATR_STOP_MULT
        return {
            "direction": "SHORT", "symbol": symbol, "price": price, "entry": price,
            "sl": price + risk, "tp1": price - risk * TP1_RR, "tp2": price - risk * TP2_RR,
            "rsi": rsi, "support": support, "resistance": resistance,
            "reasons": {
                "Daily trend": "bearish" if daily_dn else "neutral",
                "4H trend":    "bearish (EMA20 < EMA50)" if h4_dn else "neutral",
                "RSI":         f"declining ({rsi:.1f})" if rsi_dec else f"{rsi:.1f}",
                "MACD":        "bearish crossover" if macd_bear else "still positive",
                "Resistance":  f"near ${resistance:,.2f}" if near_res else f"below ${resistance:,.2f}",
            },
        }
    return None


# ---------- Signal formatting ----------

def format_signal_message(sig):
    coin = sig["symbol"].replace("USDT", "")
    direction = sig["direction"]
    arrow = "🟢" if direction == "LONG" else "🔴"
    risk_pct = abs((sig["entry"] - sig["sl"]) / sig["entry"] * 100)
    tp1_pct = abs((sig["tp1"] - sig["entry"]) / sig["entry"] * 100)
    tp2_pct = abs((sig["tp2"] - sig["entry"]) / sig["entry"] * 100)
    reasons = "\n".join(f"✓ {k}: {v}" for k, v in sig["reasons"].items())

    return f"""{arrow} <b>NEW SWING SIGNAL — {coin}/USDT ({direction})</b>

📍 Entry: ${sig['entry']:,.2f}
🛑 Stop Loss: ${sig['sl']:,.2f} (-{risk_pct:.2f}%)
🎯 TP1: ${sig['tp1']:,.2f} (+{tp1_pct:.2f}%)
🎯 TP2: ${sig['tp2']:,.2f} (+{tp2_pct:.2f}%)

📊 <b>R:R = 1:{TP1_RR} / 1:{TP2_RR}</b>
⏱ Timeframe: 4H Swing

<b>Setup confirmation:</b>
{reasons}

⚠️ <b>Risk Management:</b>
• Use only 1–2% of portfolio per trade
• Always respect your Stop Loss
• This is not financial advice

🇦🇪 AlphaDXB | Dubai Crypto Signals
#{coin.lower()} #crypto #signals #AlphaDXB"""


# ---------- Public entry point ----------

def scan_and_post(telegram_token: str, vip_channel: str) -> None:
    """Scan all watched coins once. Post any signals to vip_channel."""
    state = _load_state()
    now_ts = datetime.now().timestamp()
    cooldown = COOLDOWN_HOURS * 3600

    print(f"\n[VIP] [{datetime.now().strftime('%H:%M')}] Scanning {len(COINS)} coins...")
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
            print(f"[VIP]   {coin}: {sig['direction']} @ ${sig['price']:,.2f}")
            _send(telegram_token, vip_channel, format_signal_message(sig))
            state[coin] = {
                "timestamp": now_ts,
                "direction": sig["direction"],
                "entry": sig["entry"],
            }
            _save_state(state)
        else:
            print(f"[VIP]   {coin}: no signal")
