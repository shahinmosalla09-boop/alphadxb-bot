"""
Public-channel SHORT-TERM (1H) signal engine + signal journal.

Strategy:
  - 4H/1D give the bias and the key zones (Order Blocks)
  - 1H gives the entry trigger (reversal pattern, FVG, momentum close)
  - Tight stops, fast turnover, 1.5–2.5R targets
  - Designed for short-term traders (hold a few hours to ~2 days)

Includes a JSON-based signal journal:
  - Every fired signal is recorded (with Telegram message_id for reply-back)
  - Open signals are checked periodically against current price
  - When SL/TP is hit, a reply is posted to the original signal message
  - End of week, a performance report is posted to the public channel

Public API:
    scan_and_post(telegram_token: str, public_channel: str) -> None
    update_open_signals(telegram_token: str = "") -> None   # call every ~30 min
    weekly_report(token, public_channel, admin_token=None, admin_id=None) -> None

Edit at the top of this file to change watched coins, cooldowns, R:R, etc.
"""

import json
import time
import uuid
from datetime import datetime

# Reuse heavy lifters from vip_signals so we don't duplicate logic.
from vip_signals import (
    get_klines,
    calc_atr,
    find_swings,
    find_order_blocks,
    find_fvgs,
    latest_fvg_zone,
    has_recent_fvg,
    bullish_pattern,
    bearish_pattern,
    htf_bias,
    liquidity_targets,
    _send,
    _send_photo,
    build_signal_chart,
    send_reply,
)


# ---------- Tweakable settings ----------

COINS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"]

COOLDOWN_HOURS  = 6        # per coin, between consecutive 1H signals
JOURNAL_FILE    = "signals_journal.json"
PUB_STATE_FILE  = "public_signal_state.json"

LTF_INTERVAL    = "1h"     # entry timeframe
HTF_INTERVAL    = "4h"     # context timeframe
LTF_LIMIT       = 200
HTF_LIMIT       = 150

MAX_SL_PCT      = 0.025    # tighter than VIP (was 5%)
SL_BUFFER_ATR   = 0.30     # SL = swing low/high ± (this * ATR_1H)
TP1_RR          = 1.5
TP2_RR          = 2.5
REQUIRED_CONFLUENCES = 4   # of 5

EXPIRE_HOURS    = 96       # mark signals as 'expired' after this if neither SL nor TP hit


# ---------- 1H signal detection ----------

def detect_1h_signal(symbol: str):
    """Look for a 1H entry inside a 4H Order Block aligned with the daily bias."""
    candles_1h = get_klines(symbol, LTF_INTERVAL, LTF_LIMIT)
    candles_4h = get_klines(symbol, HTF_INTERVAL, HTF_LIMIT)
    if not candles_1h or len(candles_1h) < 60:
        print(f"[PUB]   {symbol}: ❌ not enough 1H candles "
              f"({len(candles_1h) if candles_1h else 0})")
        return None
    if not candles_4h or len(candles_4h) < 50:
        print(f"[PUB]   {symbol}: ❌ not enough 4H candles "
              f"({len(candles_4h) if candles_4h else 0})")
        return None

    bias = htf_bias(candles_4h)
    if bias == "neutral":
        highs, lows = find_swings(candles_4h, left=2, right=2)
        print(f"[PUB]   {symbol}: ❌ 4H bias neutral "
              f"(swings — highs:{len(highs)}, lows:{len(lows)})")
        return None

    print(f"[PUB]   {symbol}: 4H bias={bias}")

    atr_4h = calc_atr(candles_4h)
    atr_1h = calc_atr(candles_1h)
    if atr_4h is None or atr_1h is None:
        print(f"[PUB]   {symbol}: ❌ ATR calculation failed")
        return None

    obs_4h = find_order_blocks(candles_4h, atr_4h)
    print(f"[PUB]   {symbol}: found {len(obs_4h)} valid 4H OB(s)")
    last  = candles_1h[-1]
    prev  = candles_1h[-2]
    price = last["close"]

    # ----- LONG side -----
    if bias == "bullish":
        bull_obs = [ob for ob in obs_4h if ob["type"] == "bullish"]
        print(f"[PUB]   {symbol}: bullish 4H OBs={len(bull_obs)}, price=${price:,.2f}")
        active_ob = None
        prev_4h = candles_4h[-2] if len(candles_4h) >= 2 else None
        for ob in sorted(bull_obs, key=lambda x: x["index"], reverse=True):
            if ob["low"] <= price <= ob["high"]:
                # Fresh touch: prev 4H candle was outside OB (first time entering zone)
                if prev_4h and ob["low"] <= prev_4h["close"] <= ob["high"]:
                    print(f"[PUB]   {symbol}: ⚠️ 4H OB already being tested — skip stale entry")
                    continue
                active_ob = ob
                break
        if not active_ob:
            if bull_obs:
                nearest = sorted(bull_obs, key=lambda x: abs((x["low"]+x["high"])/2 - price))
                ob0 = nearest[0]
                print(f"[PUB]   {symbol}: ❌ price not in any bullish 4H OB "
                      f"(nearest: ${ob0['low']:,.2f}–${ob0['high']:,.2f})")
            else:
                print(f"[PUB]   {symbol}: ❌ no active bullish 4H OBs found")
            return None

        pat = bullish_pattern(prev, last)
        fvg_present = has_recent_fvg(candles_1h, "bullish", lookback=5)
        bullish_close = last["close"] > last["open"] and last["close"] > prev["close"]

        # SL: just below the most recent 1H swing low
        _, swings_low_1h = find_swings(candles_1h, left=2, right=2)
        recent_swing_low = swings_low_1h[-1][1] if swings_low_1h else price - atr_1h * 2
        sl = recent_swing_low - atr_1h * SL_BUFFER_ATR
        risk = price - sl
        if risk <= 0 or risk / price > MAX_SL_PCT:
            print(f"[PUB]   {symbol}: ❌ SL invalid "
                  f"(risk={risk:.4f}, {risk/price*100:.2f}% vs max {MAX_SL_PCT*100:.1f}%)")
            return None

        tp1 = price + risk * TP1_RR
        tp2 = price + risk * TP2_RR
        # Bump TP1 to natural 1H liquidity if it gives extra room (but stay below TP2)
        tg = liquidity_targets(candles_1h, "LONG")
        if tg and tp1 < tg[0] < tp2:
            tp1 = tg[0]

        confluences = {
            "4H bias bullish":        True,
            "Inside 4H bullish OB":   True,
            "1H reversal pattern":    pat is not None,
            "1H bullish FVG":         fvg_present,
            "1H bullish close":       bullish_close,
        }
        score = sum(1 for v in confluences.values() if v)
        print(f"[PUB]   {symbol}: confluences {score}/{len(confluences)} — "
              + ", ".join(f"{k}={'✅' if v else '❌'}" for k, v in confluences.items()))
        if score < REQUIRED_CONFLUENCES:
            print(f"[PUB]   {symbol}: ❌ insufficient confluences ({score} < {REQUIRED_CONFLUENCES})")
            return None

        swing_highs, swing_lows = find_swings(candles_1h, left=2, right=2)
        return {
            "direction":  "LONG",
            "symbol":     symbol,
            "price":      price,
            "entry":      price,
            "sl":         sl,
            "tp1":        tp1,
            "tp2":        tp2,
            "ob_zone":    (active_ob["low"], active_ob["high"]),
            "ob_index":   len(candles_1h) - 1,
            "fvg_zone":   latest_fvg_zone(candles_1h, "bullish", lookback=10),
            "candles":    candles_1h,
            "swing_highs": swing_highs,
            "swing_lows":  swing_lows,
            "confluences": confluences,
            "pattern":     pat,
            "timeframe":   "1H",
        }

    # ----- SHORT side -----
    if bias == "bearish":
        bear_obs = [ob for ob in obs_4h if ob["type"] == "bearish"]
        print(f"[PUB]   {symbol}: bearish 4H OBs={len(bear_obs)}, price=${price:,.2f}")
        active_ob = None
        prev_4h = candles_4h[-2] if len(candles_4h) >= 2 else None
        for ob in sorted(bear_obs, key=lambda x: x["index"], reverse=True):
            if ob["low"] <= price <= ob["high"]:
                # Fresh touch: prev 4H candle was outside OB
                if prev_4h and ob["low"] <= prev_4h["close"] <= ob["high"]:
                    print(f"[PUB]   {symbol}: ⚠️ 4H OB already being tested — skip stale entry")
                    continue
                active_ob = ob
                break
        if not active_ob:
            if bear_obs:
                nearest = sorted(bear_obs, key=lambda x: abs((x["low"]+x["high"])/2 - price))
                ob0 = nearest[0]
                print(f"[PUB]   {symbol}: ❌ price not in any bearish 4H OB "
                      f"(nearest: ${ob0['low']:,.2f}–${ob0['high']:,.2f})")
            else:
                print(f"[PUB]   {symbol}: ❌ no active bearish 4H OBs found")
            return None

        pat = bearish_pattern(prev, last)
        fvg_present = has_recent_fvg(candles_1h, "bearish", lookback=5)
        bearish_close = last["close"] < last["open"] and last["close"] < prev["close"]

        swings_high_1h, _ = find_swings(candles_1h, left=2, right=2)
        recent_swing_high = swings_high_1h[-1][1] if swings_high_1h else price + atr_1h * 2
        sl = recent_swing_high + atr_1h * SL_BUFFER_ATR
        risk = sl - price
        if risk <= 0 or risk / price > MAX_SL_PCT:
            print(f"[PUB]   {symbol}: ❌ SL invalid "
                  f"(risk={risk:.4f}, {risk/price*100:.2f}% vs max {MAX_SL_PCT*100:.1f}%)")
            return None

        tp1 = price - risk * TP1_RR
        tp2 = price - risk * TP2_RR
        tg = liquidity_targets(candles_1h, "SHORT")
        if tg and tp2 < tg[0] < tp1:
            tp1 = tg[0]

        confluences = {
            "4H bias bearish":        True,
            "Inside 4H bearish OB":   True,
            "1H reversal pattern":    pat is not None,
            "1H bearish FVG":         fvg_present,
            "1H bearish close":       bearish_close,
        }
        score = sum(1 for v in confluences.values() if v)
        print(f"[PUB]   {symbol}: confluences {score}/{len(confluences)} — "
              + ", ".join(f"{k}={'✅' if v else '❌'}" for k, v in confluences.items()))
        if score < REQUIRED_CONFLUENCES:
            print(f"[PUB]   {symbol}: ❌ insufficient confluences ({score} < {REQUIRED_CONFLUENCES})")
            return None

        swing_highs, swing_lows = find_swings(candles_1h, left=2, right=2)
        return {
            "direction":  "SHORT",
            "symbol":     symbol,
            "price":      price,
            "entry":      price,
            "sl":         sl,
            "tp1":        tp1,
            "tp2":        tp2,
            "ob_zone":    (active_ob["low"], active_ob["high"]),
            "ob_index":   len(candles_1h) - 1,
            "fvg_zone":   latest_fvg_zone(candles_1h, "bearish", lookback=10),
            "candles":    candles_1h,
            "swing_highs": swing_highs,
            "swing_lows":  swing_lows,
            "confluences": confluences,
            "pattern":     pat,
            "timeframe":   "1H",
        }

    return None


# ---------- Public message format ----------

def format_public_signal(sig) -> str:
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

    return f"""{arrow} <b>SHORT-TERM SIGNAL — {coin}/USDT ({direction})</b>

⏱ Timeframe: 1H entry · 4H context
📐 Method: Smart Money Concepts + Price Action

📍 Entry: ${sig['entry']:,.2f}
🛑 Stop Loss: ${sig['sl']:,.2f} (−{risk_pct:.2f}%)
🎯 TP1: ${sig['tp1']:,.2f} (+{tp1_pct:.2f}%) | R:R 1:{rr1:.2f}
🎯 TP2: ${sig['tp2']:,.2f} (+{tp2_pct:.2f}%) | R:R 1:{rr2:.2f}

<b>Setup confirmation:</b>
{confluences}

⚠️ <b>Disclaimer</b>
This is not financial advice. Past performance does not guarantee future results. Always do your own research and use proper risk management (1–2% per trade).

🇦🇪 AlphaDXB | Dubai Crypto Signals
#Signal #{coin.lower()} #crypto #SMC #priceaction #shortterm #AlphaDXB"""


# ---------- Signal journal ----------

def _load_journal():
    try:
        with open(JOURNAL_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {"signals": []}


def _save_journal(journal):
    try:
        with open(JOURNAL_FILE, "w") as f:
            json.dump(journal, f, indent=2)
    except Exception as e:
        print(f"[JOURNAL] ❌ save error: {e}")


def record_signal(sig, channel_name: str,
                  message_id=None, channel_id: str = "") -> str:
    """Record a fired signal in the journal.

    message_id  — Telegram message_id returned by sendPhoto/sendMessage.
                  Stored so we can reply to the original signal message when
                  SL/TP is hit.
    channel_id  — Telegram chat_id of the channel where the signal was posted
                  (e.g. '@AlphaDXBcrypto' or '-1003795059124').
    """
    journal = _load_journal()
    record = {
        "id":            str(uuid.uuid4()),
        "timestamp":     time.time(),
        "coin":          sig["symbol"],
        "direction":     sig["direction"],
        "channel":       channel_name,
        "channel_id":    channel_id,      # Telegram chat_id for reply
        "message_id":    message_id,      # Telegram message_id for reply
        "timeframe":     sig.get("timeframe", "4H"),
        "entry":         sig["entry"],
        "sl":            sig["sl"],
        "tp1":           sig["tp1"],
        "tp2":           sig["tp2"],
        "status":        "open",
        "outcome_time":  None,
        "outcome_price": None,
        "rr_achieved":   0.0,
    }
    journal["signals"].append(record)
    _save_journal(journal)
    print(f"[JOURNAL] recorded {record['coin']} {record['direction']} @ ${record['entry']:,.2f} "
          f"(msg_id={message_id}, channel={channel_id or channel_name})")
    return record["id"]


# ---------- Outcome reply helpers ----------

def _post_outcome_reply(telegram_token: str, sig: dict) -> None:
    """Send a reply to the original signal message with the outcome.

    Only fires if we have both channel_id and message_id stored in the journal.
    Older journal entries (before this feature) will simply be skipped.
    """
    channel_id = sig.get("channel_id", "")
    message_id = sig.get("message_id")
    if not channel_id or not message_id:
        print(f"[JOURNAL] ℹ️ No message_id/channel_id for {sig['coin']} — skipping reply")
        return

    coin = sig["coin"].replace("USDT", "")
    status = sig["status"]
    rr = sig.get("rr_achieved", 0)

    if status == "tp1_hit":
        text = (
            f"✅ <b>TP1 Hit — {coin}/USDT</b>\n"
            f"Partial profit secured! 🎯\n"
            f"R:R achieved: <b>+{rr:.2f}R</b>\n\n"
            f"▶️ Recommended: move SL to entry (breakeven) and let TP2 run.\n"
            f"🇦🇪 AlphaDXB"
        )
    elif status == "tp2_hit":
        text = (
            f"🎯 <b>TP2 Hit — {coin}/USDT</b>\n"
            f"Full target reached! Excellent trade! 🏆\n"
            f"R:R achieved: <b>+{rr:.2f}R</b>\n\n"
            f"Trade closed. Well done for following the plan. 💪\n"
            f"🇦🇪 AlphaDXB"
        )
    elif status == "sl_hit":
        text = (
            f"❌ <b>Stop Loss Hit — {coin}/USDT</b>\n"
            f"Setup invalidated. Loss: −1R\n\n"
            f"Stay disciplined — protect capital and move on. "
            f"One loss doesn't define the week. 💪\n"
            f"🇦🇪 AlphaDXB"
        )
    elif status == "expired":
        text = (
            f"⌛ <b>Signal Expired — {coin}/USDT</b>\n"
            f"Setup did not trigger within {EXPIRE_HOURS}h. No trade taken.\n"
            f"🇦🇪 AlphaDXB"
        )
    else:
        return  # unknown status, nothing to post

    ok = send_reply(telegram_token, channel_id, message_id, text)
    if ok:
        print(f"[JOURNAL] ✅ Reply sent for {coin} {status}")
    else:
        print(f"[JOURNAL] ❌ Reply failed for {coin} {status}")


def update_open_signals(telegram_token: str = "") -> None:
    """Walk all open signals, fetch recent price action, update status if SL/TP hit.

    When a signal's status changes, sends a reply to the original signal message
    in Telegram. Pass telegram_token to enable reply notifications.
    """
    journal = _load_journal()
    if not journal.get("signals"):
        return
    changed = False
    now_ts = time.time()

    # Cache klines per coin so we don't hit the API repeatedly
    klines_cache = {}

    # Collect signals that just changed status in this run
    newly_closed = []

    for sig in journal["signals"]:
        if sig["status"] != "open":
            continue
        # Expire old signals
        if now_ts - sig["timestamp"] > EXPIRE_HOURS * 3600:
            sig["status"] = "expired"
            sig["outcome_time"] = now_ts
            changed = True
            newly_closed.append(sig)
            continue

        coin = sig["coin"]
        if coin not in klines_cache:
            # 15-minute candles, recent ~5 day window (500 candles × 15m = 125h)
            klines_cache[coin] = get_klines(coin, "15m", 500)
        candles = klines_cache[coin]
        if not candles:
            continue

        signal_dt = datetime.fromtimestamp(sig["timestamp"])
        relevant = [c for c in candles if c["time"] >= signal_dt]
        if not relevant:
            continue

        for c in relevant:
            if sig["direction"] == "LONG":
                if c["low"] <= sig["sl"]:
                    sig["status"] = "sl_hit"
                    sig["outcome_time"] = c["time"].timestamp()
                    sig["outcome_price"] = sig["sl"]
                    sig["rr_achieved"] = -1.0
                    changed = True
                    newly_closed.append(sig)
                    break
                if c["high"] >= sig["tp2"]:
                    sig["status"] = "tp2_hit"
                    sig["outcome_time"] = c["time"].timestamp()
                    sig["outcome_price"] = sig["tp2"]
                    sig["rr_achieved"] = (sig["tp2"] - sig["entry"]) / max(1e-9, (sig["entry"] - sig["sl"]))
                    changed = True
                    newly_closed.append(sig)
                    break
                if c["high"] >= sig["tp1"]:
                    sig["status"] = "tp1_hit"
                    sig["outcome_time"] = c["time"].timestamp()
                    sig["outcome_price"] = sig["tp1"]
                    sig["rr_achieved"] = (sig["tp1"] - sig["entry"]) / max(1e-9, (sig["entry"] - sig["sl"]))
                    changed = True
                    newly_closed.append(sig)
                    break
            else:  # SHORT
                if c["high"] >= sig["sl"]:
                    sig["status"] = "sl_hit"
                    sig["outcome_time"] = c["time"].timestamp()
                    sig["outcome_price"] = sig["sl"]
                    sig["rr_achieved"] = -1.0
                    changed = True
                    newly_closed.append(sig)
                    break
                if c["low"] <= sig["tp2"]:
                    sig["status"] = "tp2_hit"
                    sig["outcome_time"] = c["time"].timestamp()
                    sig["outcome_price"] = sig["tp2"]
                    sig["rr_achieved"] = (sig["entry"] - sig["tp2"]) / max(1e-9, (sig["sl"] - sig["entry"]))
                    changed = True
                    newly_closed.append(sig)
                    break
                if c["low"] <= sig["tp1"]:
                    sig["status"] = "tp1_hit"
                    sig["outcome_time"] = c["time"].timestamp()
                    sig["outcome_price"] = sig["tp1"]
                    sig["rr_achieved"] = (sig["entry"] - sig["tp1"]) / max(1e-9, (sig["sl"] - sig["entry"]))
                    changed = True
                    newly_closed.append(sig)
                    break

    if changed:
        _save_journal(journal)
        print("[JOURNAL] open signals updated")

    # Send outcome replies AFTER saving journal (so even if replies fail, journal is safe)
    if telegram_token and newly_closed:
        for sig in newly_closed:
            try:
                _post_outcome_reply(telegram_token, sig)
            except Exception as e:
                print(f"[JOURNAL] ❌ Reply error for {sig['coin']}: {e}")


# ---------- Weekly performance report ----------

def weekly_report(telegram_token: str, public_channel: str,
                  admin_token: str = "", admin_id=None) -> None:
    journal = _load_journal()
    week_ago = time.time() - 7 * 86400
    recent = [s for s in journal.get("signals", []) if s["timestamp"] >= week_ago]
    total = len(recent)

    if total == 0:
        msg = (f"📊 <b>Weekly Performance Report</b>\n"
               f"{datetime.now().strftime('%b %d, %Y')}\n\n"
               f"No signals this week. Quiet markets.\n\n"
               f"🇦🇪 AlphaDXB | Dubai Crypto Signals\n#weeklyreport #AlphaDXB")
    else:
        tp1_n   = sum(1 for s in recent if s["status"] == "tp1_hit")
        tp2_n   = sum(1 for s in recent if s["status"] == "tp2_hit")
        sl_n    = sum(1 for s in recent if s["status"] == "sl_hit")
        open_n  = sum(1 for s in recent if s["status"] == "open")
        exp_n   = sum(1 for s in recent if s["status"] == "expired")

        wins = tp1_n + tp2_n
        decided = wins + sl_n
        win_rate = (wins / decided * 100) if decided else 0
        cumulative_r = sum(s["rr_achieved"] for s in recent
                           if s["status"] in ("tp1_hit", "tp2_hit", "sl_hit"))
        avg_r = cumulative_r / decided if decided else 0

        # Per-coin breakdown
        coins_dict = {}
        for s in recent:
            coin = s["coin"].replace("USDT", "")
            coins_dict.setdefault(coin, {"win": 0, "loss": 0})
            if s["status"] in ("tp1_hit", "tp2_hit"):
                coins_dict[coin]["win"] += 1
            elif s["status"] == "sl_hit":
                coins_dict[coin]["loss"] += 1
        coin_lines = "\n".join(
            f"  • {c}: {v['win']}W / {v['loss']}L"
            for c, v in sorted(coins_dict.items())
        )

        msg = (
            f"📊 <b>Weekly Performance Report</b>\n"
            f"{datetime.now().strftime('%b %d, %Y')}\n\n"
            f"📡 Total signals: {total}\n"
            f"✅ TP1 hit: {tp1_n}\n"
            f"🎯 TP2 hit: {tp2_n}\n"
            f"❌ SL hit: {sl_n}\n"
            f"⏳ Still open: {open_n}\n"
            f"⌛ Expired: {exp_n}\n\n"
            f"🏆 <b>Win rate: {win_rate:.1f}%</b>\n"
            f"⚖️ Avg outcome: {avg_r:+.2f}R per trade\n"
            f"💰 <b>Cumulative: {cumulative_r:+.2f}R total</b>\n\n"
            f"<b>By coin:</b>\n{coin_lines}\n\n"
            f"⚠️ Past performance does not guarantee future results.\n\n"
            f"🇦🇪 AlphaDXB | Dubai Crypto Signals\n"
            f"#weeklyreport #performance #AlphaDXB"
        )

    _send(telegram_token, public_channel, msg)
    if admin_token and admin_id is not None:
        _send(admin_token, str(admin_id), f"📊 Weekly report sent.\n\n{msg}")


# ---------- Cooldown state ----------

def _load_state():
    try:
        with open(PUB_STATE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(state):
    try:
        with open(PUB_STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception as e:
        print(f"[PUB] ❌ state save error: {e}")


# ---------- Public scan + post ----------

def _has_open_pub_signal(coin: str) -> bool:
    """Return True if there is already an open 1H signal for this coin in the journal."""
    try:
        journal = _load_journal()
        for s in journal.get("signals", []):
            if (s["coin"] == coin
                    and s["status"] == "open"
                    and s.get("channel") == "public"):
                print(f"[PUB]   {coin}: ⏸ open signal already in journal — skipping")
                return True
    except Exception as e:
        print(f"[PUB]   {coin}: journal check error: {e}")
    return False


def scan_and_post(telegram_token: str, public_channel: str) -> None:
    """Scan for 1H setups; post any signal (with chart) to public_channel and journal it."""
    state = _load_state()
    now_ts = time.time()
    cooldown = COOLDOWN_HOURS * 3600

    print(f"\n[PUB] [{datetime.now().strftime('%H:%M')}] 1H scan ({len(COINS)} coins)...")
    for coin in COINS:
        last = state.get(coin, {}).get("timestamp", 0)
        if now_ts - last < cooldown:
            print(f"[PUB]   {coin}: cooldown ({(now_ts - last)/3600:.1f}h)")
            continue
        if _has_open_pub_signal(coin):
            continue
        try:
            sig = detect_1h_signal(coin)
        except Exception as e:
            print(f"[PUB]   {coin}: detect error: {e}")
            continue
        if not sig:
            continue  # detect_1h_signal already printed the reason

        print(f"[PUB]   {coin}: ✅ {sig['direction']} @ ${sig['price']:,.2f}")
        caption = format_public_signal(sig)
        message_id = None
        try:
            chart = build_signal_chart(sig)
            message_id = _send_photo(telegram_token, public_channel, chart, caption)
        except Exception as e:
            print(f"[PUB]   {coin}: chart failed: {e} — text only")
            message_id = _send(telegram_token, public_channel, caption)

        # Record in journal with message_id so we can reply when SL/TP hits
        try:
            record_signal(sig, "public",
                          message_id=message_id,
                          channel_id=public_channel)
        except Exception as e:
            print(f"[PUB]   {coin}: journal error: {e}")

        state[coin] = {"timestamp": now_ts, "direction": sig["direction"]}
        _save_state(state)
