"""
Gold (XAU/USD) signal guide bot.
- Fetches 15-min candles from TwelveData
- Computes EMA(50)/EMA(200) trend, RSI(14) momentum, ATR(14) for zone sizing
- Checks a public weekly economic calendar for high-impact USD news nearby
- Sends a Telegram message with a suggested BUY/SELL zone, SL, TP
- Places NO trades. Guidance only. Run on a schedule (e.g. GitHub Actions, every 15 min).
"""

import os
import sys
import json
import datetime
import requests

# ---- Config (read from environment / GitHub Secrets) ----
TWELVEDATA_API_KEY = os.environ["TWELVEDATA_API_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

SYMBOL = "XAU/USD"
INTERVAL = "15min"
INTERVAL_MINUTES = 15
OUTPUT_SIZE = 300  # enough bars for EMA(200) to stabilize

STATE_FILE = "state.json"  # persisted between runs via GitHub Actions cache

EMA_FAST = 20
EMA_SLOW = 50
RSI_PERIOD = 14
RSI_OVERSOLD = 30.0
RSI_OVERBOUGHT = 70.0
ATR_PERIOD = 14
ENTRY_RANGE_ATR_MULT = 0.3
SL_ATR_MULT = 1.5
TP_ATR_MULT = 2.5

HTF_INTERVAL = "1h"  # higher timeframe used for trend context

NEWS_BUFFER_MIN = 30  # flag caution if a high-impact USD event is within +/- this many minutes
NEWS_FEED_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"


def fetch_candles(interval=None, outputsize=None):
    interval = interval or INTERVAL
    outputsize = outputsize or OUTPUT_SIZE
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": SYMBOL,
        "interval": interval,
        "outputsize": outputsize,
        "apikey": TWELVEDATA_API_KEY,
        "timezone": "UTC",
    }
    resp = requests.get(url, params=params, timeout=20)
    data = resp.json()
    if "values" not in data:
        raise RuntimeError(f"TwelveData error: {data}")
    # API returns newest-first; reverse to chronological order
    values = list(reversed(data["values"]))
    opens = [float(v["open"]) for v in values]
    closes = [float(v["close"]) for v in values]
    highs = [float(v["high"]) for v in values]
    lows = [float(v["low"]) for v in values]
    times = [v["datetime"] for v in values]
    return times, opens, highs, lows, closes


def drop_incomplete_candle(times, opens, highs, lows, closes, interval_minutes):
    """
    Some data providers include the still-forming current candle as the last
    entry. Drop it if it hasn't finished yet, so we only ever act on fully
    closed candles (prevents re-firing the same signal as that candle updates).
    """
    if not times:
        return times, opens, highs, lows, closes
    last_start = datetime.datetime.fromisoformat(times[-1]).replace(tzinfo=datetime.timezone.utc)
    candle_end = last_start + datetime.timedelta(minutes=interval_minutes)
    now = datetime.datetime.now(datetime.timezone.utc)
    if now < candle_end:
        return times[:-1], opens[:-1], highs[:-1], lows[:-1], closes[:-1]
    return times, opens, highs, lows, closes


def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception as e:
        print(f"Could not write state file: {e}")


def ema(values, period):
    result = [None] * len(values)
    if len(values) < period:
        return result
    multiplier = 2 / (period + 1)
    seed = sum(values[:period]) / period
    result[period - 1] = seed
    prev = seed
    for i in range(period, len(values)):
        prev = (values[i] - prev) * multiplier + prev
        result[i] = prev
    return result


def rsi(closes, period):
    result = [None] * len(closes)
    if len(closes) <= period:
        return result
    gains, losses = [], []
    for i in range(1, len(closes)):
        change = closes[i] - closes[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    result[period] = 100 - (100 / (1 + (avg_gain / avg_loss))) if avg_loss != 0 else 100
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        rs = avg_gain / avg_loss if avg_loss != 0 else None
        result[i + 1] = 100 if rs is None else 100 - (100 / (1 + rs))
    return result


def atr(highs, lows, closes, period):
    result = [None] * len(closes)
    trs = [highs[0] - lows[0]]
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)
    if len(trs) < period:
        return result
    avg = sum(trs[:period]) / period
    result[period - 1] = avg
    for i in range(period, len(trs)):
        avg = (avg * (period - 1) + trs[i]) / period
        result[i] = avg
    return result


def find_last_swing_high(highs, up_to_index, lookback=5):
    """Most recent fractal swing high strictly before up_to_index."""
    for k in range(up_to_index - lookback, lookback - 1, -1):
        window = highs[k - lookback:k + lookback + 1]
        if window and highs[k] == max(window):
            return k, highs[k]
    return None, None


def find_last_swing_low(lows, up_to_index, lookback=5):
    """Most recent fractal swing low strictly before up_to_index."""
    for k in range(up_to_index - lookback, lookback - 1, -1):
        window = lows[k - lookback:k + lookback + 1]
        if window and lows[k] == min(window):
            return k, lows[k]
    return None, None


def detect_structure(highs, lows, closes, i, lookback=5):
    """
    Simple SMC-style break of structure (BOS) check:
    bullish BOS = current close breaks above the last swing high
    bearish BOS = current close breaks below the last swing low
    This is descriptive context, not a prediction.
    """
    sh_idx, sh_val = find_last_swing_high(highs, i - lookback, lookback)
    sl_idx, sl_val = find_last_swing_low(lows, i - lookback, lookback)

    bos_bull = sh_val is not None and closes[i] > sh_val
    bos_bear = sl_val is not None and closes[i] < sl_val
    return bos_bull, bos_bear, sh_val, sl_val


def find_nearest_unfilled_fvg(highs, lows, closes, i, lookback=100):
    """
    Classic 3-candle Fair Value Gap: a price gap between candle (k-2) and
    candle k that the market has not traded back through since. Returns the
    gap closest to the current price, or None.
    """
    start = max(2, i - lookback)
    gaps = []
    for k in range(start, i + 1):
        if lows[k] > highs[k - 2]:
            gaps.append({"type": "bullish", "low": highs[k - 2], "high": lows[k], "index": k})
        elif highs[k] < lows[k - 2]:
            gaps.append({"type": "bearish", "low": highs[k], "high": lows[k - 2], "index": k})

    unfilled = []
    for g in gaps:
        filled = False
        for j in range(g["index"] + 1, i + 1):
            if lows[j] <= g["high"] and highs[j] >= g["low"]:
                filled = True
                break
        if not filled:
            unfilled.append(g)

    if not unfilled:
        return None
    price_now = closes[i]
    return min(unfilled, key=lambda g: abs(price_now - (g["low"] + g["high"]) / 2))


def detect_liquidity_sweep(highs, lows, closes, i, lookback=5):
    """
    A 'sweep': price wicks beyond a recent swing high/low (grabbing resting
    stop orders) then closes back on the other side of it — a common SMC
    reversal cue.
    """
    sh_idx, sh_val = find_last_swing_high(highs, i - lookback, lookback)
    sl_idx, sl_val = find_last_swing_low(lows, i - lookback, lookback)

    swept_high = sh_val is not None and highs[i] > sh_val and closes[i] < sh_val
    swept_low = sl_val is not None and lows[i] < sl_val and closes[i] > sl_val
    return swept_high, swept_low, sh_val, sl_val


def find_last_order_block(opens, highs, lows, closes, atr_vals, i, impulse_mult=1.5, lookback=40):
    """
    A simple order block definition: the last opposing candle immediately
    before a strong impulsive move (range > impulse_mult * ATR). Returns the
    zone (that candle's high/low), or None if nothing qualifies nearby.
    """
    start = max(1, i - lookback)
    for k in range(i, start, -1):
        if atr_vals[k] is None:
            continue
        candle_range = abs(closes[k] - opens[k])
        if candle_range <= impulse_mult * atr_vals[k]:
            continue
        impulsive_up = closes[k] > opens[k]
        j = k - 1
        if j < 0:
            continue
        opposing_down = closes[j] < opens[j]
        opposing_up = closes[j] > opens[j]
        if impulsive_up and opposing_down:
            return {"type": "bullish", "low": lows[j], "high": highs[j], "index": j}
        if (not impulsive_up) and opposing_up:
            return {"type": "bearish", "low": lows[j], "high": highs[j], "index": j}
    return None


def premium_discount_zone(highs, lows, i, lookback=50):
    """
    Splits the recent trading range in half. SMC convention: the lower half
    is a 'discount' (buyers favored), the upper half a 'premium' (sellers
    favored). Purely descriptive of where price sits in its recent range.
    """
    start = max(0, i - lookback)
    recent_high = max(highs[start:i + 1])
    recent_low = min(lows[start:i + 1])
    midpoint = (recent_high + recent_low) / 2
    return recent_high, recent_low, midpoint


def fetch_htf_trend():
    """Higher-timeframe (1h) EMA trend, for multi-timeframe context."""
    try:
        _, _, htf_highs, htf_lows, htf_closes = fetch_candles(interval=HTF_INTERVAL, outputsize=250)
    except Exception as e:
        print(f"HTF fetch failed, skipping HTF context: {e}")
        return None

    htf_fast = ema(htf_closes, EMA_FAST)
    htf_slow = ema(htf_closes, EMA_SLOW)
    j = len(htf_closes) - 1
    if htf_fast[j] is None or htf_slow[j] is None:
        return None
    return "up" if htf_fast[j] > htf_slow[j] else "down"


def is_news_window(buffer_minutes):
    try:
        resp = requests.get(NEWS_FEED_URL, timeout=15)
        events = resp.json()
    except Exception as e:
        print(f"News feed unavailable, skipping news filter this run: {e}")
        return False, ""

    now = datetime.datetime.now(datetime.timezone.utc)
    for ev in events:
        try:
            if ev.get("country") != "USD" or ev.get("impact") != "High":
                continue
            date_str = ev["date"].replace("Z", "+00:00")
            ev_time = datetime.datetime.fromisoformat(date_str)
            delta_min = abs((ev_time - now).total_seconds()) / 60
            if delta_min <= buffer_minutes:
                return True, ev.get("title", "high-impact USD event")
        except Exception:
            continue
    return False, ""


def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message})
    if resp.status_code != 200:
        print(f"Telegram send failed: {resp.text}")


def main():
    times, opens, highs, lows, closes = fetch_candles()
    times, opens, highs, lows, closes = drop_incomplete_candle(
        times, opens, highs, lows, closes, INTERVAL_MINUTES
    )
    if len(closes) < EMA_SLOW + 5:
        print("Not enough candle history yet, skipping this run.")
        return

    state = load_state()

    ema_fast = ema(closes, EMA_FAST)
    ema_slow = ema(closes, EMA_SLOW)
    rsi_vals = rsi(closes, RSI_PERIOD)
    atr_vals = atr(highs, lows, closes, ATR_PERIOD)

    i = len(closes) - 1  # last CLOSED candle from the API
    prev = i - 1

    if None in (ema_fast[i], ema_fast[prev], ema_slow[i], ema_slow[prev],
                rsi_vals[i], rsi_vals[prev], atr_vals[i]):
        print("Indicators not fully warmed up yet, skipping this run.")
        return

    trend_up = ema_fast[i] > ema_slow[i]
    trend_down = ema_fast[i] < ema_slow[i]
    bull_cross = ema_fast[prev] <= ema_slow[prev] and ema_fast[i] > ema_slow[i]
    bear_cross = ema_fast[prev] >= ema_slow[prev] and ema_fast[i] < ema_slow[i]
    rsi_bounce_up = rsi_vals[prev] <= RSI_OVERSOLD and rsi_vals[i] > RSI_OVERSOLD
    rsi_bounce_down = rsi_vals[prev] >= RSI_OVERBOUGHT and rsi_vals[i] < RSI_OVERBOUGHT

    signal = 0
    if bull_cross or (trend_up and rsi_bounce_up):
        signal = 1
    elif bear_cross or (trend_down and rsi_bounce_down):
        signal = -1

    if signal == 0:
        print(f"No signal at {times[i]}. Trend up={trend_up}, RSI={rsi_vals[i]:.1f}")
        save_state(state)
        return

    close_now = closes[i]
    atr_now = atr_vals[i]
    half_width = atr_now * ENTRY_RANGE_ATR_MULT

    if signal == 1:
        direction = "BUY"
        entry_low = close_now - half_width
        entry_high = close_now + half_width
        sl = close_now - atr_now * SL_ATR_MULT
        tp = close_now + atr_now * TP_ATR_MULT
    else:
        direction = "SELL"
        entry_low = close_now - half_width
        entry_high = close_now + half_width
        sl = close_now + atr_now * SL_ATR_MULT
        tp = close_now - atr_now * TP_ATR_MULT

    candle_time = times[i]
    if state.get("last_alert_time") == candle_time and state.get("last_alert_direction") == direction:
        print(f"Already alerted this candle ({candle_time}, {direction}) — skipping duplicate.")
        save_state(state)
        return

    news_flag, news_title = is_news_window(NEWS_BUFFER_MIN)

    bos_bull, bos_bear, swing_high, swing_low = detect_structure(highs, lows, closes, i)
    if bos_bull:
        structure_note = "bullish break of structure (price closed above recent swing high)"
    elif bos_bear:
        structure_note = "bearish break of structure (price closed below recent swing low)"
    else:
        structure_note = "no confirmed break of recent structure yet"

    nearest_fvg = find_nearest_unfilled_fvg(highs, lows, closes, i)

    swept_high, swept_low, sh_val, sl_val = detect_liquidity_sweep(highs, lows, closes, i)
    if swept_high:
        sweep_note = f"recent liquidity sweep above {sh_val:.2f} (swept then rejected)"
    elif swept_low:
        sweep_note = f"recent liquidity sweep below {sl_val:.2f} (swept then rejected)"
    else:
        sweep_note = None

    order_block = find_last_order_block(opens, highs, lows, closes, atr_vals, i)

    recent_high, recent_low, midpoint = premium_discount_zone(highs, lows, i)
    zone_label = "premium (upper half of recent range)" if close_now > midpoint else "discount (lower half of recent range)"

    htf_trend = fetch_htf_trend()

    message = (
        f"XAU/USD {direction} zone\n"
        f"Zone: {entry_low:.2f} - {entry_high:.2f}\n"
        f"SL: {sl:.2f}   TP: {tp:.2f}\n"
        f"Time: {times[i]} UTC\n"
        f"\nStructure: {structure_note}\n"
    )
    if nearest_fvg:
        message += (
            f"Nearby unfilled FVG ({nearest_fvg['type']}): "
            f"{nearest_fvg['low']:.2f} - {nearest_fvg['high']:.2f}\n"
        )
    if sweep_note:
        message += f"Liquidity: {sweep_note}\n"
    if order_block:
        message += (
            f"Order block ({order_block['type']}): "
            f"{order_block['low']:.2f} - {order_block['high']:.2f}\n"
        )
    message += f"Price sits in {zone_label} (range {recent_low:.2f} - {recent_high:.2f})\n"
    if htf_trend:
        agreement = "agrees with" if (htf_trend == "up" and signal == 1) or (htf_trend == "down" and signal == -1) else "conflicts with"
        message += f"1H trend: {htf_trend} ({agreement} this signal)\n"
    if news_flag:
        message += f"\n⚠️ CAUTION: high-impact USD news nearby ({news_title})\n"
    message += (
        "\n(Guidance only — structure/FVG/order blocks/sweeps are added context, "
        "not a prediction. No trade placed automatically.)"
    )

    print(message)
    send_telegram(message)
    state["last_alert_time"] = candle_time
    state["last_alert_direction"] = direction
    save_state(state)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        raise
