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
import datetime
import requests

# ---- Config (read from environment / GitHub Secrets) ----
TWELVEDATA_API_KEY = os.environ["TWELVEDATA_API_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

SYMBOL = "XAU/USD"
INTERVAL = "15min"
OUTPUT_SIZE = 300  # enough bars for EMA(200) to stabilize

EMA_FAST = 50
EMA_SLOW = 200
RSI_PERIOD = 14
RSI_OVERSOLD = 30.0
RSI_OVERBOUGHT = 70.0
ATR_PERIOD = 14
ENTRY_RANGE_ATR_MULT = 0.3
SL_ATR_MULT = 1.5
TP_ATR_MULT = 2.5

NEWS_BUFFER_MIN = 30  # flag caution if a high-impact USD event is within +/- this many minutes
NEWS_FEED_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"


def fetch_candles():
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": SYMBOL,
        "interval": INTERVAL,
        "outputsize": OUTPUT_SIZE,
        "apikey": TWELVEDATA_API_KEY,
    }
    resp = requests.get(url, params=params, timeout=20)
    data = resp.json()
    if "values" not in data:
        raise RuntimeError(f"TwelveData error: {data}")
    # API returns newest-first; reverse to chronological order
    values = list(reversed(data["values"]))
    closes = [float(v["close"]) for v in values]
    highs = [float(v["high"]) for v in values]
    lows = [float(v["low"]) for v in values]
    times = [v["datetime"] for v in values]
    return times, highs, lows, closes


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
    times, highs, lows, closes = fetch_candles()
    if len(closes) < EMA_SLOW + 5:
        print("Not enough candle history yet, skipping this run.")
        return

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

    news_flag, news_title = is_news_window(NEWS_BUFFER_MIN)

    message = (
        f"XAU/USD {direction} zone\n"
        f"Zone: {entry_low:.2f} - {entry_high:.2f}\n"
        f"SL: {sl:.2f}   TP: {tp:.2f}\n"
        f"Time: {times[i]} UTC\n"
    )
    if news_flag:
        message += f"\n⚠️ CAUTION: high-impact USD news nearby ({news_title})\n"
    message += "\n(Guidance only — no trade placed automatically.)"

    print(message)
    send_telegram(message)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        raise
