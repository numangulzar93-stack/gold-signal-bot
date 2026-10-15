"""
Backtest for the gold signal bot's trigger rules.

Replays the bot's own triggers (EMA cross / RSI bounce / momentum spike) over the
last ~50 days of 15-minute XAU/USD candles and checks, for every signal, whether the
take-profit or the stop-loss was reached first. It then repeats the test with extra
filters (1H trend agreement, chop filter, session) to see which filters really help.

It places NO trades. It only reads candles and sends a summary to Telegram.
Run it from GitHub Actions (workflow: backtest.yml) using the same secrets as the bot.
"""

import os
import sys
import csv
import html
import datetime
import requests

# ---- Settings ----
SYMBOL = "XAU/USD"
INTERVAL = "15min"
OUTPUT_SIZE = 5000           # max candles the free TwelveData plan returns (~50 trading days)

EMA_FAST = 20
EMA_SLOW = 50
RSI_PERIOD = 14
RSI_OVERSOLD = 30.0
RSI_OVERBOUGHT = 70.0
ATR_PERIOD = 14
SPIKE_ATR_MULT = 2.0

DEFAULT_SL = 1.5             # same as the live bot
DEFAULT_TP = 2.5
SL_TP_GRID = [(1.0, 2.0), (1.5, 2.5), (2.0, 3.0), (2.0, 4.0), (3.0, 3.0)]

HORIZON = 96                 # candles (24h) to wait for TP/SL; after that the trade counts as "open"
CHOP_LOOKBACK = 8            # candles (2 hours) used to judge whether the market is flat
CHOP_SLOPE_LIGHT = 0.25      # slow EMA must have moved at least this x ATR over that time (light filter)
CHOP_SLOPE_STRICT = 0.40     # same, stricter filter
SPREAD = 0.26                # Exness spread on gold; applied to SELL exits
WARMUP = 60


# ---------------------------------------------------------------- indicators
def ema(values, period):
    result = [None] * len(values)
    if len(values) < period:
        return result
    multiplier = 2 / (period + 1)
    prev = sum(values[:period]) / period
    result[period - 1] = prev
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
        trs.append(max(highs[i] - lows[i],
                       abs(highs[i] - closes[i - 1]),
                       abs(lows[i] - closes[i - 1])))
    if len(trs) < period:
        return result
    avg = sum(trs[:period]) / period
    result[period - 1] = avg
    for i in range(period, len(trs)):
        avg = (avg * (period - 1) + trs[i]) / period
        result[i] = avg
    return result


# ---------------------------------------------------------------- data
def fetch_candles():
    resp = requests.get(
        "https://api.twelvedata.com/time_series",
        params={
            "symbol": SYMBOL,
            "interval": INTERVAL,
            "outputsize": OUTPUT_SIZE,
            "apikey": os.environ["TWELVEDATA_API_KEY"],
            "timezone": "UTC",
        },
        timeout=60,
    )
    data = resp.json()
    if "values" not in data:
        raise RuntimeError(f"TwelveData error: {data}")
    values = list(reversed(data["values"]))
    times = [v["datetime"] for v in values]
    opens = [float(v["open"]) for v in values]
    highs = [float(v["high"]) for v in values]
    lows = [float(v["low"]) for v in values]
    closes = [float(v["close"]) for v in values]

    # drop the candle that is still forming, like the live bot does
    last_start = datetime.datetime.fromisoformat(times[-1]).replace(tzinfo=datetime.timezone.utc)
    if datetime.datetime.now(datetime.timezone.utc) < last_start + datetime.timedelta(minutes=15):
        times, opens, highs, lows, closes = times[:-1], opens[:-1], highs[:-1], lows[:-1], closes[:-1]
    return times, opens, highs, lows, closes


def htf_trend_series(times, closes):
    """
    1-hour trend (EMA20 vs EMA50 on hourly closes) known at each 15-min candle,
    using only hourly bars that were already finished at that moment.
    """
    n = len(times)
    dts = [datetime.datetime.fromisoformat(t) for t in times]
    kf, ks = 2 / (EMA_FAST + 1), 2 / (EMA_SLOW + 1)
    ema_f = ema_s = None
    count = 0
    state = None
    out = [None] * n
    for i in range(n):
        hour_now = dts[i].replace(minute=0, second=0, microsecond=0)
        next_hour = dts[i + 1].replace(minute=0, second=0, microsecond=0) if i + 1 < n else None
        hour_done = dts[i].minute >= 45 or (next_hour is not None and next_hour != hour_now)
        if hour_done:
            c = closes[i]
            count += 1
            ema_f = c if ema_f is None else ema_f + kf * (c - ema_f)
            ema_s = c if ema_s is None else ema_s + ks * (c - ema_s)
            if count >= EMA_SLOW:
                state = "up" if ema_f > ema_s else "down"
        out[i] = state
    return out


# ---------------------------------------------------------------- signals
def generate_signals(times, opens, highs, lows, closes):
    n = len(closes)
    ema_f = ema(closes, EMA_FAST)
    ema_s = ema(closes, EMA_SLOW)
    rsi_v = rsi(closes, RSI_PERIOD)
    atr_v = atr(highs, lows, closes, ATR_PERIOD)
    htf = htf_trend_series(times, closes)
    dts = [datetime.datetime.fromisoformat(t) for t in times]

    first_htf = next((i for i in range(n) if htf[i] is not None), n)
    start = max(WARMUP, first_htf)

    signals = []
    for i in range(start, n - 1):
        p = i - 1
        if None in (ema_f[i], ema_f[p], ema_s[i], ema_s[p], rsi_v[i], rsi_v[p], atr_v[i]):
            continue

        trend_up = ema_f[i] > ema_s[i]
        trend_down = ema_f[i] < ema_s[i]
        bull_cross = ema_f[p] <= ema_s[p] and ema_f[i] > ema_s[i]
        bear_cross = ema_f[p] >= ema_s[p] and ema_f[i] < ema_s[i]
        bounce_up = rsi_v[p] <= RSI_OVERSOLD and rsi_v[i] > RSI_OVERSOLD
        bounce_down = rsi_v[p] >= RSI_OVERBOUGHT and rsi_v[i] < RSI_OVERBOUGHT

        direction, trigger = 0, None
        if bull_cross or (trend_up and bounce_up):
            direction, trigger = 1, "trend"
        elif bear_cross or (trend_down and bounce_down):
            direction, trigger = -1, "trend"
        else:
            move = closes[i] - opens[i]
            if abs(move) >= SPIKE_ATR_MULT * atr_v[i]:
                direction, trigger = (1 if move > 0 else -1), "spike"
        if direction == 0:
            continue

        hour = dts[i].hour
        signals.append({
            "i": i,
            "time": times[i],
            "dir": direction,
            "trigger": trigger,
            "htf_agree": (htf[i] == "up") == (direction == 1),
            "slope": abs(ema_s[i] - ema_s[i - CHOP_LOOKBACK]) / atr_v[i] if ema_s[i - CHOP_LOOKBACK] is not None else 0.0,
            "session": (7 <= hour < 10) or (12 <= hour < 15),
        })
    return signals, atr_v, start


# ---------------------------------------------------------------- outcomes
def evaluate(signals, highs, lows, closes, atr_v, sl_mult, tp_mult):
    """Return 'win' / 'loss' / 'open' for each signal. If SL and TP are both touched
    inside the same candle it is counted as a loss (the cautious assumption)."""
    n = len(closes)
    results = []
    for s in signals:
        i, d = s["i"], s["dir"]
        entry, a = closes[i], atr_v[i]
        if d == 1:
            sl, tp = entry - sl_mult * a, entry + tp_mult * a
        else:
            sl, tp = entry + sl_mult * a, entry - tp_mult * a
        res = "open"
        for j in range(i + 1, min(n, i + 1 + HORIZON)):
            if d == 1:
                hit_sl, hit_tp = lows[j] <= sl, highs[j] >= tp
            else:  # SELL exits at the ask = chart price + spread
                hit_sl, hit_tp = highs[j] + SPREAD >= sl, lows[j] + SPREAD <= tp
            if hit_sl:
                res = "loss"
                break
            if hit_tp:
                res = "win"
                break
        results.append(res)
    return results


def stats(results, sl_mult, tp_mult):
    wins = results.count("win")
    losses = results.count("loss")
    done = wins + losses
    win_rate = 100.0 * wins / done if done else None
    exp_r = (wins * (tp_mult / sl_mult) - losses) / done if done else None
    return wins, losses, results.count("open"), win_rate, exp_r


def half_rates(results):
    mid = len(results) // 2
    out = []
    for part in (results[:mid], results[mid:]):
        w, l = part.count("win"), part.count("loss")
        out.append(100.0 * w / (w + l) if (w + l) else None)
    return out


def flip(signals):
    return [dict(s, dir=-s["dir"], htf_agree=not s["htf_agree"]) for s in signals]


VARIANTS = [
    ("Current bot: all signals", lambda s: True),
    ("Trend signals only", lambda s: s["trigger"] == "trend"),
    ("Spike signals only", lambda s: s["trigger"] == "spike"),
    ("All + 1H trend agrees", lambda s: s["htf_agree"]),
    ("Trend + 1H agrees", lambda s: s["trigger"] == "trend" and s["htf_agree"]),
    ("Spike + 1H agrees", lambda s: s["trigger"] == "spike" and s["htf_agree"]),
    ("All + 1H agrees + not flat", lambda s: s["htf_agree"] and s["slope"] >= CHOP_SLOPE_LIGHT),
    ("Trend + 1H agrees + not flat",
     lambda s: s["trigger"] == "trend" and s["htf_agree"] and s["slope"] >= CHOP_SLOPE_LIGHT),
    ("All + 1H agrees + strictly not flat", lambda s: s["htf_agree"] and s["slope"] >= CHOP_SLOPE_STRICT),
    ("All + London/NY only", lambda s: s["session"]),
    ("All + 1H agrees + London/NY", lambda s: s["htf_agree"] and s["session"]),
]


def fmt(x, nd=0):
    return "-" if x is None else f"{x:.{nd}f}"


def table(title, rows):
    head = f"{'Variant':<32}{'N':>5}{'/day':>6}{'Win%':>6}{'Open':>5}{'ExpR':>7}{'H1%':>5}{'H2%':>5}"
    lines = [title, head, "-" * len(head)]
    for r in rows:
        lines.append(
            f"{(r['name'][:30] + ('*' if r['n'] < 30 else '')):<32}{r['n']:>5}{r['per_day']:>6.1f}{fmt(r['win_rate']):>6}"
            f"{r['open']:>5}{fmt(r['exp_r'], 2):>7}{fmt(r['h1']):>5}{fmt(r['h2']):>5}"
        )
    return "\n".join(lines)


def run_variants(signals, highs, lows, closes, atr_v, sl_mult, tp_mult, days, variants=VARIANTS):
    rows = []
    for name, keep in variants:
        subset = [s for s in signals if keep(s)]
        res = evaluate(subset, highs, lows, closes, atr_v, sl_mult, tp_mult)
        wins, losses, opn, wr, er = stats(res, sl_mult, tp_mult)
        h1, h2 = half_rates(res)
        rows.append({"name": name, "n": len(subset), "per_day": len(subset) / days if days else 0,
                     "win_rate": wr, "open": opn, "exp_r": er, "h1": h1, "h2": h2})
    return rows


def send_telegram(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("Telegram not configured, skipping send.")
        return
    body = "<pre>" + html.escape(text[:3800]) + "</pre>"
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      data={"chat_id": chat, "text": body, "parse_mode": "HTML"}, timeout=30)
    if r.status_code != 200:
        print(f"Telegram send failed: {r.text}")


def main():
    times, opens, highs, lows, closes = fetch_candles()
    signals, atr_v, start = generate_signals(times, opens, highs, lows, closes)
    days = len({t[:10] for t in times[start:]})
    be = 100.0 * DEFAULT_SL / (DEFAULT_SL + DEFAULT_TP)

    header = (f"BACKTEST {SYMBOL} {INTERVAL}\n"
              f"{times[start][:10]} to {times[-1][:10]}  ({days} trading days, {len(signals)} signals)\n"
              f"SL {DEFAULT_SL}xATR / TP {DEFAULT_TP}xATR. Break-even win rate = {be:.1f}%\n"
              f"Win% = TP hit before SL (open trades excluded). ExpR = avg result per trade in R.\n"
              f"H1/H2 = win% in first/second half of the data (should be similar if the edge is real).\n"
              f"* = fewer than 30 signals: too few to trust, can be pure luck.")

    main_rows = run_variants(signals, highs, lows, closes, atr_v, DEFAULT_SL, DEFAULT_TP, days)
    t1 = table("A) Each trigger and filter at the live SL/TP", main_rows)

    diag_variants = [
        ("DIAGNOSTIC: trend FADED", lambda s: s["trigger"] == "trend"),
        ("DIAGNOSTIC: spike FADED", lambda s: s["trigger"] == "spike"),
    ]
    diag_rows = run_variants(flip(signals), highs, lows, closes, atr_v, DEFAULT_SL, DEFAULT_TP, days, diag_variants)
    t2 = table("B) Diagnostic only: same signals traded the OPPOSITE way", diag_rows)

    grid_rows = []
    for sl_m, tp_m in SL_TP_GRID:
        r = run_variants(signals, highs, lows, closes, atr_v, sl_m, tp_m, days,
                         [(f"All+1H+notflat SL{sl_m}/TP{tp_m}", VARIANTS[6][1])])[0]
        grid_rows.append(r)
    t3 = table("C) Stop/target sizes for 'All + 1H agrees + not flat'", grid_rows)

    report = "\n\n".join([header, t1, t2, t3])
    print(report)

    with open("backtest_results.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["time_utc", "direction", "trigger", "htf_agrees", "ema_slope_atr", "session", "result"])
        res = evaluate(signals, highs, lows, closes, atr_v, DEFAULT_SL, DEFAULT_TP)
        for s, r in zip(signals, res):
            w.writerow([s["time"], "BUY" if s["dir"] == 1 else "SELL", s["trigger"],
                        s["htf_agree"], round(s["slope"], 3), s["session"], r])

    send_telegram(report)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        raise
