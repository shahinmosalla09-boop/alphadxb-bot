"""
Scalping signal engine — 15M Smart Money Concepts.

Strategy:
  1. 1H structure gives bias (bullish/bearish) — only trade with the trend
  2. 15M Order Block = key entry zone (unmitigated demand/supply)
  3. 15M Fair Value Gap = imbalance confluence
  4. 15M confirmation candle = bullish/bearish close inside the OB
  5. SL: just beyond OB extreme + ATR buffer (max 1.2% from entry)
  6. TP1: 1:1.5 R:R | TP2: 1:2.5 R:R (or next 15M liquidity)

Signal fires when 3 of 4 confluences are met.
Cooldown: 2 hours per coin (faster cycle than swing signals).
Signals posted to public channel with full chart.
All signals journaled — auto reply sent when SL/TP is hit.

Public API:
    scan_and_post(telegram_token: str, channel: str) -> None
"""

import time
from datetime import datetime
from io import BytesIO

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from vip_signals import (
    get_klines,
    calc_atr,
    find_swings,
    find_order_blocks,
    find_fvgs,
    has_recent_fvg,
    latest_fvg_zone,
    bullish_pattern,
    bearish_pattern,
    htf_bias,
    liquidity_targets,
    _send,
    _send_photo,
)
# public_signals imported lazily inside functions to avoid circular import


# ---------- Settings ----------

COINS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "AVAXUSDT"]

LTF_INTERVAL    = "15m"    # entry timeframe
HTF_INTERVAL    = "1h"     # LTF bias timeframe
HTF2_INTERVAL   = "4h"     # HTF confirmation — must align with 1H bias
LTF_LIMIT       = 200      # ~50 hours of 15m data
HTF_LIMIT       = 100      # ~100 hours of 1h data
HTF2_LIMIT      = 100      # ~400 hours of 4h data

COOLDOWN_HOURS  = 2        # per coin between scalp signals
STATE_FILE      = "scalp_state.json"

MAX_SL_PCT      = 0.012    # 1.2% max — tight scalp stops
SL_BUFFER_ATR   = 0.15     # SL = OB extreme ± (0.15 × ATR_15m)
TP1_RR          = 1.5
TP2_RR          = 2.5
REQUIRED_CONFLUENCES = 4   # of 5 — raised from 3; all key confluences must align

SCALP_CHANNEL_NAME = "public_scalp"  # journal tag


# ---------- State ----------

import json

def _load_state():
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}

def _save_state(state):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception as e:
        print(f"[SCALP] ❌ state save error: {e}")


# ---------- Price formatter ----------

def _fmt(price: float) -> str:
    """Format price with appropriate decimal places."""
    if price >= 1000:
        return f"${price:,.2f}"
    elif price >= 1:
        return f"${price:,.4f}"
    else:
        return f"${price:,.6f}"


# ---------- Signal detection ----------

def detect_scalp_signal(symbol: str):
    """Detect a 15M scalp setup using 4H+1H bias alignment + 15M OB + FVG + real pattern."""
    candles_15m = get_klines(symbol, LTF_INTERVAL, LTF_LIMIT)
    candles_1h  = get_klines(symbol, HTF_INTERVAL, HTF_LIMIT)
    candles_4h  = get_klines(symbol, HTF2_INTERVAL, HTF2_LIMIT)

    if not candles_15m or len(candles_15m) < 60:
        print(f"[SCALP]   {symbol}: ❌ not enough 15M candles "
              f"({len(candles_15m) if candles_15m else 0})")
        return None
    if not candles_1h or len(candles_1h) < 30:
        print(f"[SCALP]   {symbol}: ❌ not enough 1H candles "
              f"({len(candles_1h) if candles_1h else 0})")
        return None
    if not candles_4h or len(candles_4h) < 30:
        print(f"[SCALP]   {symbol}: ❌ not enough 4H candles "
              f"({len(candles_4h) if candles_4h else 0})")
        return None

    bias_1h = htf_bias(candles_1h)
    bias_4h = htf_bias(candles_4h)

    # Both timeframes must agree — no trading against the 4H trend
    if bias_1h == "neutral":
        print(f"[SCALP]   {symbol}: ❌ 1H bias neutral")
        return None
    if bias_4h == "neutral":
        print(f"[SCALP]   {symbol}: ❌ 4H bias neutral — no scalp in ranging market")
        return None
    if bias_1h != bias_4h:
        print(f"[SCALP]   {symbol}: ❌ bias conflict — 1H={bias_1h}, 4H={bias_4h} — skip")
        return None

    bias = bias_1h
    print(f"[SCALP]   {symbol}: 1H={bias_1h} 4H={bias_4h} — aligned ✅")

    atr_15m = calc_atr(candles_15m)
    if atr_15m is None or atr_15m <= 0:
        print(f"[SCALP]   {symbol}: ❌ ATR failed")
        return None

    obs = find_order_blocks(candles_15m, atr_15m)
    print(f"[SCALP]   {symbol}: {len(obs)} valid 15M OB(s)")

    last  = candles_15m[-1]
    prev  = candles_15m[-2]
    price = last["close"]

    # ---- LONG ----
    if bias == "bullish":
        bull_obs = [ob for ob in obs if ob["type"] == "bullish"]
        print(f"[SCALP]   {symbol}: bullish 15M OBs={len(bull_obs)}, price={_fmt(price)}")
        active_ob = None
        for ob in sorted(bull_obs, key=lambda x: x["index"], reverse=True):
            # Price must be INSIDE the OB — not above it (no late entries)
            if ob["low"] <= price <= ob["high"]:
                active_ob = ob
                break
        if not active_ob:
            if bull_obs:
                nearest = sorted(bull_obs, key=lambda x: abs((x["low"]+x["high"])/2 - price))
                o = nearest[0]
                print(f"[SCALP]   {symbol}: ❌ price not inside bullish OB "
                      f"(nearest: {_fmt(o['low'])}–{_fmt(o['high'])})")
            else:
                print(f"[SCALP]   {symbol}: ❌ no active bullish 15M OBs")
            return None

        pat         = bullish_pattern(prev, last)   # real pattern only (engulfing, pin bar)
        fvg_present = has_recent_fvg(candles_15m, "bullish", lookback=4)
        fvg_zone    = latest_fvg_zone(candles_15m, "bullish", lookback=8)
        # Price in lower 60% of OB = better risk, deeper in zone
        ob_range    = active_ob["high"] - active_ob["low"]
        price_in_lower_ob = price <= active_ob["low"] + ob_range * 0.6

        sl   = active_ob["low"] - atr_15m * SL_BUFFER_ATR
        risk = price - sl
        if risk <= 0 or risk / price > MAX_SL_PCT:
            print(f"[SCALP]   {symbol}: ❌ SL too wide or zero "
                  f"({risk/price*100:.2f}% vs max {MAX_SL_PCT*100:.1f}%)")
            return None

        tp1 = price + risk * TP1_RR
        tp2 = price + risk * TP2_RR
        targets = liquidity_targets(candles_15m, "LONG")
        if targets and tp1 < targets[0] < tp2:
            tp1 = targets[0]

        confluences = {
            "4H+1H bias bullish":        True,
            "15M bullish OB active":     True,
            "Price in lower half of OB": price_in_lower_ob,
            "15M bullish FVG nearby":    fvg_present,
            "15M bullish pattern":       pat is not None,
        }
        score = sum(1 for v in confluences.values() if v)
        print(f"[SCALP]   {symbol}: confluences {score}/{len(confluences)} — "
              + ", ".join(f"{k}={'✅' if v else '❌'}" for k, v in confluences.items()))
        if score < REQUIRED_CONFLUENCES:
            print(f"[SCALP]   {symbol}: ❌ insufficient confluences ({score} < {REQUIRED_CONFLUENCES})")
            return None

        reason_parts = []
        if fvg_present: reason_parts.append("FVG fill")
        if pat:         reason_parts.append(pat)
        reason = "15M Bullish OB + " + " + ".join(reason_parts) if reason_parts else "15M Bullish OB entry"

        swing_highs, swing_lows = find_swings(candles_15m, left=2, right=2)
        return {
            "direction":   "LONG",
            "symbol":      symbol,
            "price":       price,
            "entry":       price,
            "sl":          sl,
            "tp1":         tp1,
            "tp2":         tp2,
            "ob_zone":     (active_ob["low"], active_ob["high"]),
            "ob_index":    active_ob["index"],
            "fvg_zone":    fvg_zone,
            "candles":     candles_15m,
            "swing_highs": swing_highs,
            "swing_lows":  swing_lows,
            "confluences": confluences,
            "pattern":     pat,
            "timeframe":   "15M",
            "reason":      reason,
        }

    # ---- SHORT ----
    if bias == "bearish":
        bear_obs = [ob for ob in obs if ob["type"] == "bearish"]
        print(f"[SCALP]   {symbol}: bearish 15M OBs={len(bear_obs)}, price={_fmt(price)}")
        active_ob = None
        for ob in sorted(bear_obs, key=lambda x: x["index"], reverse=True):
            # Price must be INSIDE the OB — not below it (no late entries)
            if ob["low"] <= price <= ob["high"]:
                active_ob = ob
                break
        if not active_ob:
            if bear_obs:
                nearest = sorted(bear_obs, key=lambda x: abs((x["low"]+x["high"])/2 - price))
                o = nearest[0]
                print(f"[SCALP]   {symbol}: ❌ price not inside bearish OB "
                      f"(nearest: {_fmt(o['low'])}–{_fmt(o['high'])})")
            else:
                print(f"[SCALP]   {symbol}: ❌ no active bearish 15M OBs")
            return None

        pat         = bearish_pattern(prev, last)   # real pattern only (engulfing, shooting star)
        fvg_present = has_recent_fvg(candles_15m, "bearish", lookback=4)
        fvg_zone    = latest_fvg_zone(candles_15m, "bearish", lookback=8)
        # Price in upper 60% of OB = better risk, deeper in zone
        ob_range    = active_ob["high"] - active_ob["low"]
        price_in_upper_ob = price >= active_ob["high"] - ob_range * 0.6

        sl   = active_ob["high"] + atr_15m * SL_BUFFER_ATR
        risk = sl - price
        if risk <= 0 or risk / price > MAX_SL_PCT:
            print(f"[SCALP]   {symbol}: ❌ SL too wide or zero "
                  f"({risk/price*100:.2f}% vs max {MAX_SL_PCT*100:.1f}%)")
            return None

        tp1 = price - risk * TP1_RR
        tp2 = price - risk * TP2_RR
        targets = liquidity_targets(candles_15m, "SHORT")
        if targets and tp2 < targets[0] < tp1:
            tp1 = targets[0]

        confluences = {
            "4H+1H bias bearish":        True,
            "15M bearish OB active":     True,
            "Price in upper half of OB": price_in_upper_ob,
            "15M bearish FVG nearby":    fvg_present,
            "15M bearish pattern":       pat is not None,
        }
        score = sum(1 for v in confluences.values() if v)
        print(f"[SCALP]   {symbol}: confluences {score}/{len(confluences)} — "
              + ", ".join(f"{k}={'✅' if v else '❌'}" for k, v in confluences.items()))
        if score < REQUIRED_CONFLUENCES:
            print(f"[SCALP]   {symbol}: ❌ insufficient confluences ({score} < {REQUIRED_CONFLUENCES})")
            return None

        reason_parts = []
        if fvg_present: reason_parts.append("FVG fill")
        if pat:         reason_parts.append(pat)
        reason = "15M Bearish OB + " + " + ".join(reason_parts) if reason_parts else "15M Bearish OB entry"

        swing_highs, swing_lows = find_swings(candles_15m, left=2, right=2)
        return {
            "direction":   "SHORT",
            "symbol":      symbol,
            "price":       price,
            "entry":       price,
            "sl":          sl,
            "tp1":         tp1,
            "tp2":         tp2,
            "ob_zone":     (active_ob["low"], active_ob["high"]),
            "ob_index":    active_ob["index"],
            "fvg_zone":    fvg_zone,
            "candles":     candles_15m,
            "swing_highs": swing_highs,
            "swing_lows":  swing_lows,
            "confluences": confluences,
            "pattern":     pat,
            "timeframe":   "15M",
            "reason":      reason,
        }

    return None


# ---------- Message format ----------

def format_scalp_signal(sig) -> str:
    coin      = sig["symbol"].replace("USDT", "")
    direction = sig["direction"]
    arrow     = "🟢" if direction == "LONG" else "🔴"
    entry     = sig["entry"]
    sl        = sig["sl"]
    tp1       = sig["tp1"]
    tp2       = sig["tp2"]

    risk_pct = abs((entry - sl) / entry * 100)
    tp1_pct  = abs((tp1 - entry) / entry * 100)
    tp2_pct  = abs((tp2 - entry) / entry * 100)
    rr1      = abs((tp1 - entry) / (entry - sl)) if entry != sl else 0
    rr2      = abs((tp2 - entry) / (entry - sl)) if entry != sl else 0

    ob_lo, ob_hi = sig["ob_zone"]
    reason       = sig.get("reason", "15M OB confluence")
    pat          = sig.get("pattern") or "In-zone reaction"

    confluences = "\n".join(
        f"{'✅' if v else '◻️'} {k}" for k, v in sig["confluences"].items()
    )

    return (
        f"{arrow} <b>⚡ SCALP SIGNAL — {coin}/USDT ({direction})</b>\n"
        f"\n"
        f"⏱ Timeframe: <b>15M entry · 1H context</b>\n"
        f"📐 Method: Smart Money Concepts (15M OB + FVG)\n"
        f"\n"
        f"📍 Entry: <b>{_fmt(entry)}</b>\n"
        f"🛑 Stop Loss: {_fmt(sl)} (−{risk_pct:.2f}%)\n"
        f"🎯 TP1: {_fmt(tp1)} (+{tp1_pct:.2f}%) | R:R 1:{rr1:.2f}\n"
        f"🎯 TP2: {_fmt(tp2)} (+{tp2_pct:.2f}%) | R:R 1:{rr2:.2f}\n"
        f"\n"
        f"📦 Order Block: {_fmt(ob_lo)} – {_fmt(ob_hi)}\n"
        f"🕯 Pattern: {pat}\n"
        f"💡 Reason: {reason}\n"
        f"\n"
        f"<b>Confluence checklist:</b>\n"
        f"{confluences}\n"
        f"\n"
        f"⏳ <b>Scalp trade</b> — expected hold: 1–6 hours\n"
        f"⚠️ Risk max 0.5–1% of portfolio. Respect the Stop Loss.\n"
        f"This is not financial advice.\n"
        f"\n"
        f"🇦🇪 AlphaDXB | Dubai Crypto Signals\n"
        f"#Signal #{coin.lower()} #scalp #SMC #priceaction #AlphaDXB"
    )


# ---------- Chart ----------

def build_scalp_chart(sig) -> bytes:
    """White-background 15M chart with OB zone, FVG, Entry/SL/TP lines + entry arrow."""
    candles       = sig["candles"]
    direction     = sig["direction"]
    coin          = sig["symbol"].replace("USDT", "")
    ob_low, ob_high = sig["ob_zone"]
    ob_idx_global = sig["ob_index"]

    show_n = 65
    n      = len(candles)
    start  = max(0, min(n - show_n, ob_idx_global - 8))
    show   = candles[start:n]
    ob_idx = ob_idx_global - start

    plt.style.use("default")
    fig, ax = plt.subplots(figsize=(13, 7))
    fig.patch.set_facecolor("white")
    ax.set_facecolor("white")

    bull_clr, bear_clr = "#16a085", "#e74c3c"

    # ---- Candlesticks ----
    for i, c in enumerate(show):
        clr = bull_clr if c["close"] >= c["open"] else bear_clr
        ax.plot([i, i], [c["low"], c["high"]], color=clr, linewidth=1.0)
        body_h = abs(c["close"] - c["open"]) or (c["high"] - c["low"]) * 0.01
        ax.bar(i, body_h, bottom=min(c["open"], c["close"]),
               color=clr, width=0.7, alpha=0.95)

    x_right = len(show) + 10
    ax.set_xlim(-1, x_right)

    ob_clr = bull_clr if direction == "LONG" else bear_clr

    # ---- OB zone ----
    ax.axhspan(ob_low, ob_high, alpha=0.13, color=ob_clr, zorder=0)
    if 0 <= ob_idx < len(show):
        ax.scatter([ob_idx], [(ob_low + ob_high) / 2],
                   marker="o", s=120, edgecolors=ob_clr,
                   facecolors="none", linewidths=2, zorder=5)
    ax.text(x_right - 1, (ob_low + ob_high) / 2,
            f" OB {_fmt(ob_low)}–{_fmt(ob_high)}",
            color=ob_clr, fontsize=9, fontweight="bold",
            va="center", ha="right")

    # ---- FVG zone ----
    if sig.get("fvg_zone"):
        fvg_low, fvg_high, fvg_idx = sig["fvg_zone"]
        ax.axhspan(fvg_low, fvg_high, alpha=0.10, color="#2980b9", zorder=0)
        local = fvg_idx - start
        if 0 <= local < len(show):
            ax.text(local, fvg_high, " FVG",
                    color="#2980b9", fontsize=8, fontweight="bold",
                    va="bottom", ha="left")

    # ---- Entry / SL / TP lines ----
    levels = [
        (sig["entry"], "Entry", "#f39c12", "-",  1.8),
        (sig["sl"],    "SL",    "#e74c3c", "--", 1.6),
        (sig["tp1"],   "TP1",   "#16a085", "--", 1.4),
        (sig["tp2"],   "TP2",   "#16a085", ":",  1.2),
    ]
    for price_v, label, clr, ls, lw in levels:
        ax.axhline(price_v, color=clr, linewidth=lw, linestyle=ls, alpha=0.95)
        ax.text(x_right - 1, price_v, f" {label}: {_fmt(price_v)}",
                color=clr, fontsize=9, fontweight="bold",
                va="center", ha="right",
                bbox=dict(boxstyle="round,pad=0.2", fc="white", ec=clr, alpha=0.9))

    # ---- Entry arrow at the last candle ----
    entry_x   = len(show) - 1
    risk_dist = abs(sig["entry"] - sig["sl"])
    if direction == "LONG":
        arrow_start = sig["entry"] - risk_dist * 0.6
        arrow_end   = sig["entry"]
    else:
        arrow_start = sig["entry"] + risk_dist * 0.6
        arrow_end   = sig["entry"]
    ax.annotate(
        "", xy=(entry_x, arrow_end), xytext=(entry_x, arrow_start),
        arrowprops=dict(arrowstyle="->", color=ob_clr, lw=2.5),
    )

    # ---- Swing highs/lows as dots ----
    for idx, h in sig.get("swing_highs", []):
        local = idx - start
        if 0 <= local < len(show):
            ax.scatter([local], [h], marker="^", s=30,
                       color="#888888", zorder=3, alpha=0.6)
    for idx, l in sig.get("swing_lows", []):
        local = idx - start
        if 0 <= local < len(show):
            ax.scatter([local], [l], marker="v", s=30,
                       color="#888888", zorder=3, alpha=0.6)

    # ---- Title + reason subtitle ----
    arrow_sym = "▲" if direction == "LONG" else "▼"
    ax.set_title(
        f"AlphaDXB | {coin}/USDT  {arrow_sym} SCALP {direction}  —  15M SMC",
        color="#1a1a2e", fontsize=14, fontweight="bold", pad=15,
    )
    reason = sig.get("reason", "15M OB confluence")
    ax.text(0.5, 1.005, reason, transform=ax.transAxes,
            color="#555555", fontsize=8, ha="center", va="bottom")

    # ---- Cosmetics ----
    ax.grid(color="#e0e0e0", linewidth=0.5, alpha=0.8)
    ax.tick_params(colors="#333333", labelsize=8)
    ax.set_xticks([])
    ax.yaxis.tick_right()
    for s in ["top", "left"]:
        ax.spines[s].set_visible(False)
    for s in ["bottom", "right"]:
        ax.spines[s].set_color("#cccccc")

    # ---- Watermark ----
    fig.text(0.5, 0.5, "AlphaDXB", fontsize=55, color="black", alpha=0.04,
             ha="center", va="center", fontweight="bold", rotation=30)

    plt.tight_layout(pad=2)
    buf = BytesIO()
    plt.savefig(buf, format="png", dpi=150, bbox_inches="tight", facecolor="white")
    buf.seek(0)
    plt.close(fig)
    return buf.read()


# ---------- Scan + post ----------

def _has_open_signal(coin: str) -> bool:
    """Return True if there is already an open scalp signal for this coin in the journal."""
    try:
        from public_signals import _load_journal
        journal = _load_journal()
        for s in journal.get("signals", []):
            if (s["coin"] == coin
                    and s["status"] == "open"
                    and s.get("channel") == SCALP_CHANNEL_NAME):
                print(f"[SCALP]   {coin}: ⏸ open signal already in journal — skipping")
                return True
    except Exception as e:
        print(f"[SCALP]   {coin}: journal check error: {e}")
    return False


def scan_and_post(telegram_token: str, channel: str) -> None:
    """Scan all coins for 15M scalp setups. Post chart + message, journal the signal."""
    state   = _load_state()
    now_ts  = time.time()
    cooldown = COOLDOWN_HOURS * 3600

    print(f"\n[SCALP] [{datetime.now().strftime('%H:%M')}] 15M scan ({len(COINS)} coins)...")
    for coin in COINS:
        last = state.get(coin, {}).get("timestamp", 0)
        if now_ts - last < cooldown:
            print(f"[SCALP]   {coin}: cooldown ({(now_ts - last)/3600:.1f}h)")
            continue
        # Skip if a scalp signal for this coin is still open (not yet SL/TP hit)
        if _has_open_signal(coin):
            continue
        try:
            sig = detect_scalp_signal(coin)
        except Exception as e:
            print(f"[SCALP]   {coin}: detect error: {e}")
            continue
        if not sig:
            continue

        print(f"[SCALP]   {coin}: ✅ {sig['direction']} @ {_fmt(sig['price'])}")
        caption = format_scalp_signal(sig)

        message_id = None
        try:
            chart = build_scalp_chart(sig)
            message_id = _send_photo(telegram_token, channel, chart, caption)
        except Exception as e:
            print(f"[SCALP]   {coin}: chart error: {e} — text only")
            message_id = _send(telegram_token, channel, caption)

        # Journal the signal for auto reply-back on SL/TP
        try:
            from public_signals import record_signal
            record_signal(sig, SCALP_CHANNEL_NAME,
                          message_id=message_id,
                          channel_id=channel)
        except Exception as e:
            print(f"[SCALP]   {coin}: journal error: {e}")

        state[coin] = {"timestamp": now_ts, "direction": sig["direction"]}
        _save_state(state)
