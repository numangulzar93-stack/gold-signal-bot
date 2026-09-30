"""
Gold (XAU/USD) signal guide bot — v5.5

NEW in v5.5 (built on v5.4, no removals): "don't miss opportunities" mode.
Every signal that clears basic data-quality checks is now sent — nothing is
silently withheld for being low-confidence, overextended, against 1H trend,
fighting a recent sweep/reclaim, or inside a cooldown window. Those checks
still run and still matter, but they now FLAG the message instead of
suppressing it, and they feed a single visible label:

    Setup strength: STRONG | MEDIUM | WEAK

computed from confluence (structure/FVG/order block/sweep/1H trend, with the
opposing-order-block penalty from v5.4) minus a point each for: fighting a
recent liquidity sweep, fighting a recent structure reclaim, being
overextended from EMA20, or a trend/pullback signal conflicting with the 1H
trend. You get every signal, with a clear read on which ones deserve
caution, and you apply your own judgment on top (SMC/ICT context — FVG,
order blocks, sweeps, structure, premium/discount, OTE, equal levels, S/R
levels — is still included in every message for that).

Still hard-suppressed (these are data-quality gates, not opportunity calls):
market closed, frozen/stale feed, indicators not warmed up, ATR too low,
recent range too tight/unreliable, exact duplicate alert on the same candle,
and a new signal whose zone overlaps an already-open unresolved signal in
the same direction (that isn't a new opportunity — it's the same one).

--- v5.4 notes (unchanged; the mechanisms below are now flag-only via the
config flips just below, not removed) ---

Accuracy fixes built on top of v5.2.1's logging, no removals): accuracy fixes
requested after reviewing live signals that got wicked out or fought a
reversal:

  1. Wider, ATR-scaled pullback stop buffer (was a thin 0.25x ATR beyond the
     rejection extreme; a live signal was stopped out by a wick that cleared
     the entry zone but not the actual invalidation level). PULLBACK_SL_BUFFER_ATR
     raised to 0.5.
  2. Sweep-cooldown: when a liquidity sweep + rejection is detected within the
     last SWEEP_COOLDOWN_CANDLES candles, trend/pullback (continuation)
     signals AGAINST the direction implied by that sweep are suppressed. A
     sweep-and-reject is itself a reversal signal; continuation signals
     fighting it are the exact failure mode this closes.
  3. Support/resistance levels + structure reclaim: recent swing highs/lows
     are clustered into levels touched 2+ times. If price has reclaimed a
     level it recently broke through (support -> resistance or vice versa),
     continuation signals into the reclaimed side are suppressed, and the
     level is shown in the message when relevant context (not suppressing).
  4. Opposing order block is now a confluence PENALTY, not just a label. A
     signal with an order block of the opposite type sitting on its entry
     zone loses a confluence point instead of that being cosmetic.
  5. Confidence tiers from the confluence score: LOW (<=2/5) is logged and
     tracked for stats but NOT sent to Telegram; MEDIUM (3/5) is sent with a
     "reduced confidence" flag; HIGH (4-5/5) sends as before.
  6. Zone-overlap dedup: a new signal is suppressed as a duplicate if its
     entry zone overlaps an existing UNRESOLVED same-direction signal by more
     than DEDUP_OVERLAP_PCT, on top of the existing time-based cooldown/cap.
  7. 15m/1H divergence note: if the 15m EMA20 slope points opposite the 1H
     trend, this is now flagged in the message (informational; does not
     suppress) so a turning lower-timeframe trend isn't silently ignored.

--- v5.2.1 notes (unchanged) ---

LOGGING ONLY. Nothing about when signals fire has changed by v5.2.1 itself. On every
"No signal" run the log now also prints a "Pullback check:" line explaining
why the pullback trigger did not fire (e.g. "bounce too small: 1.1 ATR (need
1.5)"), plus the current ATR. Use these lines to tune the pullback thresholds
from real runs instead of guessing.

--- v5.2 notes ---

NEW in v5.2 (everything else is identical to v5.1): a TREND-CONTINUATION
PULLBACK trigger, so a long trend is no longer missed just because the EMA cross
happened hours ago and the overextension filter blocks chasing.

  Trigger = "pullback". In an established trend (EMA20 on the trend side of
  EMA50 for 10+ candles, EMA50 sloping with the trend, price on the trend side
  of EMA50, all inside the current session segment) the bot waits for price to:
    1. make a leg (>= 4 ATR) and then a counter-trend bounce (>= 1.5 ATR and
       <= 78.6% of the leg) that fails to reclaim EMA50, then
    2. print a rejection/resumption candle in the trend direction that closes
       beyond the previous candle's extreme.
  It uses a structure-based stop just beyond the bounce extreme (capped at
  4 ATR, otherwise the signal is skipped as a late entry), and the same HTF
  enforcement, cooldown, latency, confluence, TP1/TP2, trailing plan and
  paper tracker as every other trigger. The overextension limit is looser for
  this trigger (6 ATR) because a retrace entry is not a chase.

--- v5.1 notes (unchanged) ---

Builds on v5 (market-hours gate, stale-feed check, ATR/SL/TP floors, HTF
enforcement, FVG size filter, confluence score). New in v5.1, marked "# NEW:"
or "# CHANGED:" in the code:

  1. Clean-data pipeline: closed-market candles and near-flat candles are
     removed before ANY indicator or SMC calculation. Time gaps (weekend,
     holidays) are recorded as "breaks".
  2. Session-segment SMC: structure, sweeps, FVGs, order blocks, premium/
     discount, equal levels and OTE are computed ONLY on candles since the
     last break, so pre-weekend/frozen prices can't leak into the tags.
     EMA-cross/RSI triggers are also blocked on the first candle after a break
     (the "cross" would just be the gap).
  3. DST-aware market hours (America/New_York, 17:00 Fri close / Sun open)
     instead of hard-coded UTC hours. Post-reopen window is flagged.
  4. Signal cooldown + per-direction cap, so one move can't spawn a stack of
     same-direction alerts.
  5. Overextension filter: suppresses signals when price is already many ATRs
     away from the EMA in the signal direction (chasing).
  6. Structure-based TP2 (nearest swing/equal-level liquidity beyond TP1) with
     an ATR fallback, R:R floor on TP1, and a written trailing plan.
  7. Context relevance filter: FVG/OB/equal levels/OTE farther than N ATRs from
     price are dropped; OTE must match the signal direction; OBs are labeled
     aligned/opposing.
  8. Alert latency measured and shown (flag/suppress if the alert is late),
     plus the signal candle's O/H/L/C so you can verify against your broker.
  9. Built-in paper tracker: every sent signal is followed forward on later
     candles (SL vs TP1, then breakeven-stop vs TP2), results are logged to CSV,
     sent to Telegram, and tallied in state.

Guidance only. Places NO trades. Run on a schedule (e.g. GitHub Actions every
15 min). state.json and signal_log.csv must persist between runs.
"""

import os
import sys
import csv
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
OUTPUT_SIZE = 500  # CHANGED: more raw bars, since closed/flat candles are filtered out

STATE_FILE = "state.json"
SIGNAL_LOG_FILE = "signal_log.csv"  # NEW

EMA_FAST = 20
EMA_SLOW = 50
RSI_PERIOD = 14
RSI_OVERSOLD = 30.0
RSI_OVERBOUGHT = 70.0
ATR_PERIOD = 14
ENTRY_RANGE_ATR_MULT = 0.3
SL_ATR_MULT = 1.5
TP_ATR_MULT = 2.5

MOMENTUM_SPIKE_ATR_MULT = 2.0
HTF_INTERVAL = "1h"

NEWS_BUFFER_MIN = 30
NEWS_FEED_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

# ---- v5 data-integrity config ----
MIN_ATR_DOLLARS = 1.0
MIN_SL_DOLLARS = 3.0
MIN_TP_DOLLARS = 3.0
MIN_RECENT_RANGE_DOLLARS = 5.0
MIN_FVG_DOLLARS = 0.5
EQUAL_LEVEL_MIN_ABS_TOLERANCE = 0.30
STALE_LOOKBACK_CANDLES = 3
STALE_MIN_RANGE_DOLLARS = 0.15
REQUIRE_HTF_AGREEMENT_FOR_TREND_SIGNALS = True
MIN_CONFLUENCE_TO_SEND = 0

# ---- NEW v5.1: session / clean-data config ----
MARKET_TZ = "America/New_York"  # spot FX/metals week runs Sun 17:00 -> Fri 17:00 New York time
MARKET_HOUR_LOCAL = 17
REOPEN_FLAG_HOURS = 2           # hours after the Sunday reopen treated as "post-reopen"
REOPEN_ACTION = "flag"          # "flag" | "suppress" | "off"

MIN_CANDLE_RANGE_DOLLARS = 0.40 # a 15m gold candle with high-low below this is treated as flat/dead
BREAK_GAP_MINUTES = 60          # a time gap larger than this between clean candles = session break
MIN_CLEAN_CANDLES = EMA_SLOW + 10
MIN_SEGMENT_CANDLES = 8         # min candles since last break for premium/discount, OTE, equal levels
SEGMENT_SHORT_ACTION = "flag"   # "flag" | "suppress" when segment is shorter than the minimum

# ---- NEW v5.1: signal-quality config ----
OVEREXTENSION_ATR_MULT = 3.5    # distance of close from EMA_FAST, in ATRs, in the signal direction
OVEREXTENSION_ACTION = "flag"   # CHANGED v5.5 (was "suppress"): "flag" | "suppress" | "off"

COOLDOWN_CANDLES = 4            # same-direction signals closer than this many candles are flagged
WINDOW_CANDLES = 16             # rolling window for the per-direction cap
MAX_SAME_DIRECTION_IN_WINDOW = 2
COOLDOWN_ACTION = "flag"        # NEW v5.5: "flag" | "suppress" — was a hard suppress pre-v5.5

MAX_CONTEXT_DISTANCE_ATR = 6.0  # FVG/OB/equal-level/OTE farther than this from price are dropped

# ---- NEW v5.1: targets / trailing ----
MIN_RR_TP1 = 1.2
TP2_FALLBACK_ATR_MULT = 4.5
TP2_MAX_ATR_MULT = 8.0
TP_BUFFER_ATR = 0.1             # stop TP2 slightly before the liquidity level
TRAIL_ATR_MULT = 1.5

# ---- NEW v5.1: latency + tracker ----
LATENCY_FLAG_MIN = 5
LATENCY_SUPPRESS_MIN = 20
LATENCY_ACTION = "flag"         # NEW v5.5: "flag" | "suppress" — was a hard suppress pre-v5.5
TRACK_MAX_CANDLES = 96          # 24h of 15m candles, then a tracked signal expires
SEND_OUTCOME_MESSAGES = True

# ---- NEW v5.5: HTF (1H) conflict for trend/pullback triggers ----
HTF_CONFLICT_ACTION = "flag"    # CHANGED v5.5 (was a hard suppress via
                                 # REQUIRE_HTF_AGREEMENT_FOR_TREND_SIGNALS): "flag" | "suppress"

# ---- NEW v5.2: trend-continuation pullback trigger ----
PULLBACK_ENABLED = True
PULLBACK_MIN_SEGMENT_CANDLES = 20   # need this many candles since the last session break
PULLBACK_TREND_MIN_CANDLES = 10     # EMA fast must stay on the trend side of EMA slow this long
PULLBACK_SLOPE_CANDLES = 5          # EMA slow must slope with the trend over this many candles
PULLBACK_LOW_WINDOW = 24            # candles scanned for the leg extreme before the current candle
PULLBACK_LEG_LOOKBACK = 30          # candles before the leg extreme used to find the leg start
PULLBACK_MIN_BOUNCE_ATR = 1.5       # counter-trend bounce must be at least this many ATR
PULLBACK_MIN_LEG_ATR = 4.0          # the leg being retraced must be at least this many ATR
PULLBACK_MAX_RETRACE = 0.786        # deeper than this = likely reversal, not a pullback
PULLBACK_MIN_BODY_ATR = 0.25        # confirmation candle body must be at least this many ATR
PULLBACK_SL_BUFFER_ATR = 0.5        # CHANGED v5.4 (was 0.25): stop sits this far beyond the
                                     # bounce extreme — a thin buffer let a live signal get
                                     # wicked out just past the entry zone, short of real invalidation
PULLBACK_MIN_SL_ATR = 1.0           # stop distance floor (in ATR)
PULLBACK_MAX_SL_ATR = 4.0           # stop wider than this = late entry, skip
PULLBACK_OVEREXTENSION_ATR_MULT = 6.0  # looser overextension limit for pullback entries

# ---- NEW v5.4: sweep cooldown ----
SWEEP_COOLDOWN_ENABLED = True
SWEEP_COOLDOWN_CANDLES = 6          # how far back to look for a sweep+reject
SWEEP_COOLDOWN_ACTION = "flag"      # CHANGED v5.5 (was "suppress"): "flag" | "suppress"

# ---- NEW v5.4: support/resistance levels + structure reclaim ----
SR_ENABLED = True
SR_LOOKBACK_CANDLES = 150           # how far back to look for swing highs/lows to cluster
SR_SWING_LOOKBACK = 5               # fractal lookback used to find swing points
SR_CLUSTER_TOL_ATR = 0.5            # swing points within this many ATR are the same level
SR_MIN_TOUCHES = 2                  # a level needs at least this many touches to count
SR_RECLAIM_LOOKBACK_CANDLES = 12    # how recently price must have been on the other side
SR_RECLAIM_ACTION = "flag"          # CHANGED v5.5 (was "suppress"): "flag" | "suppress"

# ---- NEW v5.4: opposing order block penalty ----
OPPOSING_OB_PENALTY = 1             # confluence points subtracted for an opposing OB on the zone

# ---- NEW v5.4/CHANGED v5.5: confidence tiers ----
# v5.5: these no longer gate whether a signal is SENT (everything sends now)
# — they only decide the visible STRONG/MEDIUM/WEAK label, computed later as
# an ADJUSTED score (confluence minus sweep/reclaim/overextension/HTF
# penalties). Kept here for reference/tuning.
CONFLUENCE_LOW_MAX = 2
CONFLUENCE_MEDIUM_MAX = 3

# ---- NEW v5.4: zone-overlap dedup ----
DEDUP_ENABLED = True
DEDUP_OVERLAP_PCT = 0.5             # >= this fraction of zone overlap with an open same-direction
                                     # signal = suppressed as a duplicate

# ---- NEW v5.5: setup-strength label (STRONG/MEDIUM/WEAK) ----
# adjusted_score = confluence aligned (0-5, already includes the opposing-OB
# penalty) minus 1 point each for: fighting a recent sweep, fighting a recent
# reclaim, overextension, HTF conflict (trend/pullback triggers only).
STRENGTH_STRONG_MIN = 4    # adjusted_score >= this -> STRONG
STRENGTH_MEDIUM_MIN = 2    # adjusted_score >= this (and < STRONG_MIN) -> MEDIUM
                            # below STRENGTH_MEDIUM_MIN -> WEAK

# ---- NEW v5.4: lower-timeframe / HTF divergence note ----
LTF_SLOPE_CANDLES = 5               # candles used to judge the 15m EMA20 slope

LOG_FIELDS = [
    "event", "time_utc", "direction", "trigger", "entry", "sl", "tp1", "tp2",
    "confluence", "confidence_tier", "latency_min", "flags", "outcome", "candles_to_resolve",
]


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------
def parse_dt(t):
    return datetime.datetime.fromisoformat(t).replace(tzinfo=datetime.timezone.utc)


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
    values = list(reversed(data["values"]))
    opens = [float(v["open"]) for v in values]
    closes = [float(v["close"]) for v in values]
    highs = [float(v["high"]) for v in values]
    lows = [float(v["low"]) for v in values]
    times = [v["datetime"] for v in values]
    return times, opens, highs, lows, closes


def drop_incomplete_candle(times, opens, highs, lows, closes, interval_minutes):
    """Drop the still-forming candle so we only act on fully closed candles."""
    if not times:
        return times, opens, highs, lows, closes
    last_start = parse_dt(times[-1])
    candle_end = last_start + datetime.timedelta(minutes=interval_minutes)
    now = datetime.datetime.now(datetime.timezone.utc)
    if now < candle_end:
        return times[:-1], opens[:-1], highs[:-1], lows[:-1], closes[:-1]
    return times, opens, highs, lows, closes


# ---------------------------------------------------------------------------
# Market hours (CHANGED: DST-aware)
# ---------------------------------------------------------------------------
_TZ_WARNED = False


def _to_market_local(dt_utc):
    """Convert a UTC datetime to New York time; fall back to a fixed EDT offset."""
    global _TZ_WARNED
    try:
        from zoneinfo import ZoneInfo
        return dt_utc.astimezone(ZoneInfo(MARKET_TZ))
    except Exception:
        if not _TZ_WARNED:
            print("zoneinfo/tzdata unavailable — assuming fixed UTC-4 (EDT). "
                  "Install tzdata for automatic DST handling.")
            _TZ_WARNED = True
        return dt_utc - datetime.timedelta(hours=4)


def is_market_open(dt_utc):
    """
    Spot FX/metals: closed from Friday 17:00 to Sunday 17:00 New York time.
    Using New York local time means the UTC equivalent shifts automatically
    with US daylight saving (21:00 UTC in summer, 22:00 UTC in winter).
    """
    local = _to_market_local(dt_utc)
    wd = local.weekday()
    if wd == 5:
        return False
    if wd == 6 and local.hour < MARKET_HOUR_LOCAL:
        return False
    if wd == 4 and local.hour >= MARKET_HOUR_LOCAL:
        return False
    return True


# NEW
def is_reopen_window(dt_utc):
    """True during the first REOPEN_FLAG_HOURS after the Sunday reopen."""
    local = _to_market_local(dt_utc)
    return (
        local.weekday() == 6
        and MARKET_HOUR_LOCAL <= local.hour < MARKET_HOUR_LOCAL + REOPEN_FLAG_HOURS
    )


def is_feed_stale(highs, lows, i, lookback=STALE_LOOKBACK_CANDLES, min_range=STALE_MIN_RANGE_DOLLARS):
    start = max(0, i - lookback + 1)
    seg_highs = highs[start:i + 1]
    seg_lows = lows[start:i + 1]
    if not seg_highs or not seg_lows:
        return False
    return (max(seg_highs) - min(seg_lows)) < min_range


# NEW: clean-data pipeline
def build_clean_series(times, opens, highs, lows, closes):
    """
    Remove closed-market candles and near-flat (dead/indicative) candles.
    Returns the cleaned series, a set of "break" indices (positions n where
    there is a time gap > BREAK_GAP_MINUTES between clean candle n-1 and n),
    and how many candles were dropped.
    """
    keep = []
    for k, t in enumerate(times):
        if not is_market_open(parse_dt(t)):
            continue
        if (highs[k] - lows[k]) < MIN_CANDLE_RANGE_DOLLARS:
            continue
        keep.append(k)

    ct = [times[k] for k in keep]
    co = [opens[k] for k in keep]
    ch = [highs[k] for k in keep]
    cl = [lows[k] for k in keep]
    cc = [closes[k] for k in keep]

    breaks = set()
    for n in range(1, len(ct)):
        gap_min = (parse_dt(ct[n]) - parse_dt(ct[n - 1])).total_seconds() / 60
        if gap_min > BREAK_GAP_MINUTES:
            breaks.add(n)
    return ct, co, ch, cl, cc, breaks, len(times) - len(keep)


# ---------------------------------------------------------------------------
# State + logging
# ---------------------------------------------------------------------------
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


# NEW
def append_log(row):
    try:
        exists = os.path.exists(SIGNAL_LOG_FILE)
        with open(SIGNAL_LOG_FILE, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=LOG_FIELDS)
            if not exists:
                w.writeheader()
            w.writerow({k: row.get(k, "") for k in LOG_FIELDS})
    except Exception as e:
        print(f"Could not write signal log: {e}")


# ---------------------------------------------------------------------------
# Indicators
# ---------------------------------------------------------------------------
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


# CHANGED: at session breaks, true range ignores the previous close so a
# weekend gap doesn't inflate ATR.
def atr(highs, lows, closes, period, breaks=None):
    breaks = breaks or set()
    result = [None] * len(closes)
    trs = [highs[0] - lows[0]]
    for i in range(1, len(closes)):
        if i in breaks:
            tr = highs[i] - lows[i]
        else:
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


# ---------------------------------------------------------------------------
# SMC concepts (unchanged logic; now fed segment-only data)
# ---------------------------------------------------------------------------
def find_last_swing_high(highs, up_to_index, lookback=5):
    for k in range(up_to_index - lookback, lookback - 1, -1):
        window = highs[k - lookback:k + lookback + 1]
        if window and highs[k] == max(window):
            return k, highs[k]
    return None, None


def find_last_swing_low(lows, up_to_index, lookback=5):
    for k in range(up_to_index - lookback, lookback - 1, -1):
        window = lows[k - lookback:k + lookback + 1]
        if window and lows[k] == min(window):
            return k, lows[k]
    return None, None


# NEW: all fractal swing levels (used for structure-based targets)
def find_swing_levels(values, mode, lookback=5):
    levels = []
    for k in range(lookback, len(values) - lookback):
        w = values[k - lookback:k + lookback + 1]
        if mode == "high" and values[k] == max(w):
            levels.append(values[k])
        elif mode == "low" and values[k] == min(w):
            levels.append(values[k])
    return levels


def detect_structure(highs, lows, closes, i, lookback=5):
    sh_idx, sh_val = find_last_swing_high(highs, i - lookback, lookback)
    sl_idx, sl_val = find_last_swing_low(lows, i - lookback, lookback)
    bos_bull = sh_val is not None and closes[i] > sh_val
    bos_bear = sl_val is not None and closes[i] < sl_val
    return bos_bull, bos_bear, sh_val, sl_val


def find_nearest_unfilled_fvg(highs, lows, closes, i, lookback=100, min_gap_dollars=MIN_FVG_DOLLARS):
    start = max(2, i - lookback)
    gaps = []
    for k in range(start, i + 1):
        if lows[k] > highs[k - 2] and (lows[k] - highs[k - 2]) >= min_gap_dollars:
            gaps.append({"type": "bullish", "low": highs[k - 2], "high": lows[k], "index": k})
        elif highs[k] < lows[k - 2] and (lows[k - 2] - highs[k]) >= min_gap_dollars:
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
    sh_idx, sh_val = find_last_swing_high(highs, i - lookback, lookback)
    sl_idx, sl_val = find_last_swing_low(lows, i - lookback, lookback)
    swept_high = sh_val is not None and highs[i] > sh_val and closes[i] < sh_val
    swept_low = sl_val is not None and lows[i] < sl_val and closes[i] > sl_val
    return swept_high, swept_low, sh_val, sl_val


# NEW v5.4: recent sweep lookback (detect_liquidity_sweep above only checks
# the current candle; this scans back several candles so a sweep from a
# couple of bars ago can still gate the current signal)
def find_recent_sweep(highs, lows, closes, i, candles_back=SWEEP_COOLDOWN_CANDLES, swing_lookback=5):
    """
    Returns the most recent sweep within `candles_back` candles of i, as
    (kind, index, level) where kind is "high" (swept a high, bearish-for-
    price-that-was-there / bullish rejection) or "low" (swept a low, bullish
    rejection), or (None, None, None) if none found.
    """
    start = max(swing_lookback + 1, i - candles_back + 1)
    best = (None, None, None)
    for k in range(start, i + 1):
        swept_high, swept_low, sh_val, sl_val = detect_liquidity_sweep(highs, lows, closes, k, swing_lookback)
        if swept_high:
            best = ("high", k, sh_val)
        elif swept_low:
            best = ("low", k, sl_val)
    return best


# NEW v5.4: support/resistance level clustering
def find_sr_levels(highs, lows, i, atr_now, lookback=SR_LOOKBACK_CANDLES,
                   swing_lookback=SR_SWING_LOOKBACK, tol_atr=SR_CLUSTER_TOL_ATR,
                   min_touches=SR_MIN_TOUCHES):
    """
    Cluster recent fractal swing highs/lows into horizontal levels. A level
    is kept only if at least `min_touches` swing points (from either highs or
    lows, since old support often becomes resistance) fall within tol_atr of
    each other. Returns a list of dicts: {"price": avg, "touches": n}.
    """
    start = max(0, i - lookback)
    points = (find_swing_levels(highs[start:i + 1], "high", swing_lookback) +
              find_swing_levels(lows[start:i + 1], "low", swing_lookback))
    if not points:
        return []
    tol = max(tol_atr * atr_now, 0.01)
    points.sort()
    clusters = []
    current = [points[0]]
    for p in points[1:]:
        if p - current[-1] <= tol:
            current.append(p)
        else:
            clusters.append(current)
            current = [p]
    clusters.append(current)
    levels = [{"price": sum(c) / len(c), "touches": len(c)} for c in clusters if len(c) >= min_touches]
    return levels


# NEW v5.4: nearest S/R level to current price (for context/messaging)
def nearest_sr_level(levels, price, max_distance_atr, atr_now):
    if not levels:
        return None
    lvl = min(levels, key=lambda L: abs(L["price"] - price))
    if abs(lvl["price"] - price) > max_distance_atr * atr_now:
        return None
    return lvl


# NEW v5.4: structure reclaim — did price recently sit on the other side of a
# known level and has now closed back across it?
def detect_level_reclaim(levels, closes, i, atr_now, lookback=SR_RECLAIM_LOOKBACK_CANDLES,
                         min_gap_atr=0.15):
    """
    Returns a dict {"level": price, "direction": "bullish"|"bearish"} for the
    most significant recent reclaim, or None. "bullish" means price was
    below the level within `lookback` candles and has now closed above it
    (old resistance likely flipping to support) — this argues AGAINST fresh
    SELL continuation signals near that level. "bearish" is the mirror.
    """
    if not levels:
        return None
    start = max(0, i - lookback)
    min_gap = min_gap_atr * atr_now
    best = None
    for lvl in levels:
        p = lvl["price"]
        was_below = any(c < p - min_gap for c in closes[start:i])
        was_above = any(c > p + min_gap for c in closes[start:i])
        now_above = closes[i] > p + min_gap
        now_below = closes[i] < p - min_gap
        if was_below and now_above:
            best = {"level": p, "direction": "bullish"}
        elif was_above and now_below:
            best = {"level": p, "direction": "bearish"}
    return best


# NEW v5.4: zone-overlap dedup against open (unresolved) signals
def overlapping_open_signal(state, direction, entry_low, entry_high, min_overlap_pct=DEDUP_OVERLAP_PCT):
    width = max(entry_high - entry_low, 1e-6)
    for s in state.get("open_signals", []):
        if s.get("direction") != direction:
            continue
        s_entry = s.get("entry")
        if s_entry is None:
            continue
        # open_signals stores a single entry price, not a saved zone width;
        # treat it as a point and check whether it falls inside the new zone
        # (a cheap, conservative overlap check without changing state schema)
        if entry_low <= s_entry <= entry_high:
            return s
        # also catch the case where the new zone is fully outside but very
        # close (near-duplicate re-signal a few candles later)
        dist = min(abs(entry_low - s_entry), abs(entry_high - s_entry))
        if dist <= width * (1 - min_overlap_pct):
            return s
    return None


def find_last_order_block(opens, highs, lows, closes, atr_vals, i, impulse_mult=1.5, lookback=40):
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
    start = max(0, i - lookback)
    recent_high = max(highs[start:i + 1])
    recent_low = min(lows[start:i + 1])
    midpoint = (recent_high + recent_low) / 2
    return recent_high, recent_low, midpoint


def detect_momentum_spike(opens, closes, atr_vals, i, spike_mult=MOMENTUM_SPIKE_ATR_MULT):
    if atr_vals[i] is None:
        return None
    move = closes[i] - opens[i]
    if abs(move) >= spike_mult * atr_vals[i]:
        return "bullish" if move > 0 else "bearish"
    return None


def find_equal_levels(values, i, lookback=50, tolerance=0.0008, mode="high",
                      min_abs_tolerance=EQUAL_LEVEL_MIN_ABS_TOLERANCE):
    start = max(0, i - lookback)
    window = values[start:i + 1]
    if len(window) < 3:
        return None

    extremes = []
    for k in range(1, len(window) - 1):
        if mode == "high" and window[k] >= window[k - 1] and window[k] >= window[k + 1]:
            extremes.append(window[k])
        elif mode == "low" and window[k] <= window[k - 1] and window[k] <= window[k + 1]:
            extremes.append(window[k])

    if len(extremes) < 2:
        return None

    extremes_sorted = sorted(extremes, reverse=(mode == "high"))
    anchor = extremes_sorted[0]
    allowed = max(abs(anchor) * tolerance, min_abs_tolerance)
    for v in extremes_sorted[1:]:
        if abs(anchor - v) <= allowed:
            return (anchor + v) / 2
    return None


def get_killzone(time_str):
    hour = datetime.datetime.fromisoformat(time_str).hour
    if 7 <= hour < 10:
        return "London killzone"
    if 12 <= hour < 15:
        return "New York killzone"
    return None


def compute_ote_zone(highs, lows, i, lookback=5):
    sh_idx, sh_val = find_last_swing_high(highs, i, lookback)
    sl_idx, sl_val = find_last_swing_low(lows, i, lookback)
    if sh_val is None or sl_val is None or sh_idx is None or sl_idx is None:
        return None
    rng = sh_val - sl_val
    if rng <= 0:
        return None
    if sh_idx > sl_idx:
        return {"type": "bullish (pullback buy zone)",
                "low": sh_val - rng * 0.79, "high": sh_val - rng * 0.618}
    return {"type": "bearish (pullback sell zone)",
            "low": sl_val + rng * 0.618, "high": sl_val + rng * 0.79}


# CHANGED: HTF candles are now also stripped of the forming candle and any
# closed-market candles before the EMA trend is computed.
def fetch_htf_trend():
    try:
        h_t, h_o, h_h, h_l, h_c = fetch_candles(interval=HTF_INTERVAL, outputsize=250)
    except Exception as e:
        print(f"HTF fetch failed, skipping HTF context: {e}")
        return None

    h_t, h_o, h_h, h_l, h_c = drop_incomplete_candle(h_t, h_o, h_h, h_l, h_c, 60)
    keep = [k for k, t in enumerate(h_t) if is_market_open(parse_dt(t))]
    h_c = [h_c[k] for k in keep]

    htf_fast = ema(h_c, EMA_FAST)
    htf_slow = ema(h_c, EMA_SLOW)
    j = len(h_c) - 1
    if j < 0 or htf_fast[j] is None or htf_slow[j] is None:
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
            ev_time = datetime.datetime.fromisoformat(ev["date"].replace("Z", "+00:00"))
            if abs((ev_time - now).total_seconds()) / 60 <= buffer_minutes:
                return True, ev.get("title", "high-impact USD event")
        except Exception:
            continue
    return False, ""


def compute_confluence(signal, bos_bull, bos_bear, nearest_fvg, order_block,
                       swept_high, swept_low, htf_trend):
    aligned = 0
    total = 5
    if (signal == 1 and bos_bull) or (signal == -1 and bos_bear):
        aligned += 1
    if nearest_fvg and ((signal == 1 and nearest_fvg["type"] == "bullish") or
                        (signal == -1 and nearest_fvg["type"] == "bearish")):
        aligned += 1
    ob_aligned = order_block and ((signal == 1 and order_block["type"] == "bullish") or
                                  (signal == -1 and order_block["type"] == "bearish"))
    if ob_aligned:
        aligned += 1
    # CHANGED v5.4: an order block of the OPPOSITE type sitting on the zone is
    # now a penalty, not just a cosmetic label — it was previously ignored by
    # the score entirely even when flagged "opposing" in the message.
    elif order_block:
        aligned -= OPPOSING_OB_PENALTY
    if (signal == 1 and swept_low) or (signal == -1 and swept_high):
        aligned += 1
    if htf_trend and ((signal == 1 and htf_trend == "up") or (signal == -1 and htf_trend == "down")):
        aligned += 1
    aligned = max(aligned, 0)
    return aligned, total


# ---------------------------------------------------------------------------
# NEW v5.1 helpers
# ---------------------------------------------------------------------------
def detect_pullback_entry(opens, highs, lows, closes, ema_fast, ema_slow, atr_vals, i, diag=None):
    """
    Detect a DOWN-trend continuation (sell-side) pullback that just resumed on
    candle i. For an up-trend the caller passes mirrored (negated) data, so the
    same logic serves both directions.

    Returns a dict (values in the passed-in price space) or None. When it
    returns None and `diag` is a list, ONE human-readable reason is appended
    (v5.2.1 logging only; it never changes the result).
      leg_start  - high of the leg being retraced
      leg_end    - low of that leg (the extreme before the bounce)
      pb_extreme - high of the counter-trend bounce
      bounce, leg_height, retrace
    Reasons starting with "trend:" mean no established trend on this side;
    "n/a:" means there was not enough data to evaluate.
    """
    def no(msg):
        if diag is not None:
            diag.append(msg)
        return None

    atr_now = atr_vals[i]
    need = max(PULLBACK_TREND_MIN_CANDLES, PULLBACK_SLOPE_CANDLES + 1, 8)
    if atr_now is None or i < need:
        return no("n/a: not enough candles in this session yet")

    # 1) established trend, inside this session segment
    for k in range(i - PULLBACK_TREND_MIN_CANDLES + 1, i + 1):
        if ema_fast[k] is None or ema_slow[k] is None or not (ema_fast[k] < ema_slow[k]):
            return no(f"trend: EMA20 not on the trend side of EMA50 for the last {PULLBACK_TREND_MIN_CANDLES} candles")
    if ema_slow[i - PULLBACK_SLOPE_CANDLES] is None or not (ema_slow[i] < ema_slow[i - PULLBACK_SLOPE_CANDLES]):
        return no("trend: EMA50 is not sloping with the trend")
    if closes[i] >= ema_slow[i]:
        return no("trend: price is on the wrong side of EMA50")

    # 2) the leg extreme (lowest low before the current candle) and the bounce after it
    lo_start = max(0, i - PULLBACK_LOW_WINDOW)
    window = lows[lo_start:i]
    if len(window) < 4:
        return no("n/a: not enough candles to find the leg extreme")
    leg_end = min(window)
    j = lo_start + max(n for n, v in enumerate(window) if v == leg_end)  # latest occurrence
    if i - j < 3:  # need at least two bounce candles between the extreme and now
        return no(f"no bounce yet: the latest extreme was only {i - j} candle(s) ago (need 3+), price is still pushing the trend")

    pb_extreme = max(highs[j + 1:i])
    bounce = pb_extreme - leg_end
    if bounce < PULLBACK_MIN_BOUNCE_ATR * atr_now:
        return no(f"bounce too small: {bounce / atr_now:.1f} ATR (need {PULLBACK_MIN_BOUNCE_ATR})")
    if pb_extreme > ema_slow[i] + 0.5 * atr_now:  # bounce reclaimed the slow EMA = trend weakening
        return no("bounce reclaimed EMA50 - trend looks weakened, skipping")

    leg_start = max(highs[max(0, j - PULLBACK_LEG_LOOKBACK):j + 1])
    leg_height = leg_start - leg_end
    if leg_height < PULLBACK_MIN_LEG_ATR * atr_now:
        return no(f"leg too small: {leg_height / atr_now:.1f} ATR (need {PULLBACK_MIN_LEG_ATR})")
    retrace = bounce / leg_height
    if retrace > PULLBACK_MAX_RETRACE:
        return no(f"retrace too deep: {retrace * 100:.0f}% of the leg (max {PULLBACK_MAX_RETRACE * 100:.0f}%) - possible reversal")

    # 3) confirmation: trend-direction candle that closes beyond the prior candle's low
    body = opens[i] - closes[i]
    missing = []
    if not closes[i] < opens[i]:
        missing.append("last candle is not in the trend direction")
    if not closes[i] < lows[i - 1]:
        missing.append("it has not closed beyond the previous candle's extreme")
    if closes[i] < opens[i] and body < PULLBACK_MIN_BODY_ATR * atr_now:
        missing.append(f"body too small ({body / atr_now:.2f} ATR, need {PULLBACK_MIN_BODY_ATR})")
    if missing:
        return no(f"setup qualifies (bounce {bounce / atr_now:.1f} ATR, {retrace * 100:.0f}% of leg) but waiting for confirmation: "
                  + "; ".join(missing))

    return {"leg_start": leg_start, "leg_end": leg_end, "pb_extreme": pb_extreme,
            "bounce": bounce, "leg_height": leg_height, "retrace": retrace}


def zone_distance(price, low, high):
    if low <= price <= high:
        return 0.0
    return min(abs(price - low), abs(price - high))


def check_cooldown(state, direction, candle_dt):
    """
    Returns (blocked, reason). Blocks a same-direction signal if one was sent
    fewer than COOLDOWN_CANDLES ago, or if MAX_SAME_DIRECTION_IN_WINDOW were
    already sent inside the rolling window. Also prunes old history.
    """
    window = datetime.timedelta(minutes=WINDOW_CANDLES * INTERVAL_MINUTES)
    cooldown = datetime.timedelta(minutes=COOLDOWN_CANDLES * INTERVAL_MINUTES)

    sent = [s for s in state.get("sent", []) if candle_dt - parse_dt(s["t"]) <= window * 2]
    state["sent"] = sent

    same = [s for s in sent if s["d"] == direction]
    for s in same:
        diff = candle_dt - parse_dt(s["t"])
        if datetime.timedelta(0) < diff < cooldown:
            return True, f"cooldown: {direction} already sent {int(diff.total_seconds() // 60)} min ago"
    in_window = [s for s in same if datetime.timedelta(0) < candle_dt - parse_dt(s["t"]) <= window]
    if len(in_window) >= MAX_SAME_DIRECTION_IN_WINDOW:
        return True, (f"cap: {len(in_window)} {direction} signals already sent in the last "
                      f"{WINDOW_CANDLES * INTERVAL_MINUTES // 60}h")
    return False, ""


def compute_targets(signal, entry, atr_now, tp1_distance, s_highs, s_lows, eq_high, eq_low, use_recent_extreme):
    """
    TP2: nearest structural liquidity (swing level, equal level, or recent
    extreme) beyond TP1 in the signal direction, stopped TP_BUFFER_ATR short of
    the level. Falls back to TP2_FALLBACK_ATR_MULT x ATR if none qualifies.
    """
    buf = TP_BUFFER_ATR * atr_now
    candidates = []  # (target_price, label)
    if signal == -1:
        for v in find_swing_levels(s_lows, "low"):
            candidates.append((v + buf, f"swing low {v:.2f}"))
        if eq_low is not None:
            candidates.append((eq_low + buf, f"equal lows {eq_low:.2f}"))
        if use_recent_extreme and s_lows:
            v = min(s_lows)
            candidates.append((v + buf, f"recent low {v:.2f}"))
        valid = [(p, lab) for p, lab in candidates
                 if entry - p >= tp1_distance * 1.1 and entry - p <= TP2_MAX_ATR_MULT * atr_now]
        if valid:
            p, lab = max(valid, key=lambda x: x[0])  # nearest to entry
            return p, lab
        return entry - TP2_FALLBACK_ATR_MULT * atr_now, f"ATR fallback ({TP2_FALLBACK_ATR_MULT}x ATR)"

    for v in find_swing_levels(s_highs, "high"):
        candidates.append((v - buf, f"swing high {v:.2f}"))
    if eq_high is not None:
        candidates.append((eq_high - buf, f"equal highs {eq_high:.2f}"))
    if use_recent_extreme and s_highs:
        v = max(s_highs)
        candidates.append((v - buf, f"recent high {v:.2f}"))
    valid = [(p, lab) for p, lab in candidates
             if p - entry >= tp1_distance * 1.1 and p - entry <= TP2_MAX_ATR_MULT * atr_now]
    if valid:
        p, lab = min(valid, key=lambda x: x[0])
        return p, lab
    return entry + TP2_FALLBACK_ATR_MULT * atr_now, f"ATR fallback ({TP2_FALLBACK_ATR_MULT}x ATR)"


def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message})
    if resp.status_code != 200:
        print(f"Telegram send failed: {resp.text}")


# NEW: paper tracker
def resolve_open_signals(state, ctimes, chighs, clows):
    """
    Follow every previously sent signal forward on later clean candles.
      Stage 0: SL vs TP1 (if both are touched in one candle, SL is assumed first).
      Stage 1 (after TP1): stop moves to entry; TP2 vs breakeven stop.
    Returns a list of resolved signal dicts (with "outcome" set).
    """
    resolved, still_open = [], []
    for s in state.get("open_signals", []):
        last = s.get("last_checked", s["candle_time"])
        outcome = None
        for n, t in enumerate(ctimes):
            if t <= last:
                continue
            hi, lo = chighs[n], clows[n]
            s["last_checked"] = t
            s["candles_seen"] = s.get("candles_seen", 0) + 1
            sell = s["direction"] == "SELL"

            if s.get("stage", 0) == 0:
                sl_hit = hi >= s["sl"] if sell else lo <= s["sl"]
                tp1_hit = lo <= s["tp1"] if sell else hi >= s["tp1"]
                if sl_hit:
                    outcome = "SL"
                elif tp1_hit:
                    s["stage"] = 1
                    tp2_hit = lo <= s["tp2"] if sell else hi >= s["tp2"]
                    if tp2_hit:
                        outcome = "TP2"
            else:
                be_hit = hi >= s["entry"] if sell else lo <= s["entry"]
                tp2_hit = lo <= s["tp2"] if sell else hi >= s["tp2"]
                if be_hit:
                    outcome = "TP1_then_BE"
                elif tp2_hit:
                    outcome = "TP2"

            if outcome is None and s["candles_seen"] >= TRACK_MAX_CANDLES:
                outcome = "expired" if s.get("stage", 0) == 0 else "TP1_then_expired"
            if outcome:
                s["outcome"] = outcome
                break
        (resolved if outcome else still_open).append(s)

    state["open_signals"] = still_open
    return resolved


def tally_line(state):
    t = state.get("tally", {})
    sl = t.get("SL", 0)
    be = t.get("TP1_then_BE", 0)
    tp2 = t.get("TP2", 0)
    exp = t.get("expired", 0)
    tp1x = t.get("TP1_then_expired", 0)
    total = sl + be + tp2 + exp + tp1x
    reached = be + tp2 + tp1x
    return (f"Tally: SL {sl} | TP1->BE {be} | TP2 {tp2} | expired {exp + tp1x} "
            f"(TP1+ reached {reached}/{total})")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def run(state, now_utc):
    times, opens, highs, lows, closes = fetch_candles()
    times, opens, highs, lows, closes = drop_incomplete_candle(
        times, opens, highs, lows, closes, INTERVAL_MINUTES
    )
    if len(closes) < 10:
        print("Not enough candle history, skipping this run.")
        return

    raw_last_time = times[-1]

    if is_feed_stale(highs, lows, len(closes) - 1):
        print(f"Feed looks frozen/stale near {raw_last_time} UTC — skipping run.")
        return

    # NEW: clean data — drop closed-market + flat candles, record session breaks
    times, opens, highs, lows, closes, breaks, dropped = build_clean_series(
        times, opens, highs, lows, closes
    )
    if dropped:
        print(f"Filtered out {dropped} closed-market/flat candles; {len(breaks)} session break(s).")

    # NEW: follow earlier signals forward (paper tracker) before anything else
    resolved = resolve_open_signals(state, times, highs, lows)
    for s in resolved:
        tally = state.setdefault("tally", {})
        tally[s["outcome"]] = tally.get(s["outcome"], 0) + 1
        append_log({
            "event": "result", "time_utc": s["candle_time"], "direction": s["direction"],
            "trigger": s.get("trigger", ""), "entry": f"{s['entry']:.2f}", "sl": f"{s['sl']:.2f}",
            "tp1": f"{s['tp1']:.2f}", "tp2": f"{s['tp2']:.2f}", "confluence": s.get("confluence", ""),
            "confidence_tier": s.get("confidence_tier", ""),
            "outcome": s["outcome"], "candles_to_resolve": s.get("candles_seen", ""),
        })
        # CHANGED v5.5: every signal is sent now, so every result has a
        # matching entry alert — no need to gate this by tier anymore.
        if SEND_OUTCOME_MESSAGES:
            send_telegram(
                f"Result: {s['direction']} @ {s['entry']:.2f} ({s['candle_time']} UTC) -> {s['outcome']} "
                f"after {s.get('candles_seen', '?')} candles\n{tally_line(state)}"
            )

    if len(closes) < MIN_CLEAN_CANDLES:
        print(f"Only {len(closes)} clean candles available (need {MIN_CLEAN_CANDLES}), skipping.")
        return
    if times[-1] != raw_last_time:
        print(f"Latest candle ({raw_last_time} UTC) is closed-market/flat — skipping run.")
        return

    i = len(closes) - 1
    candle_dt = parse_dt(times[i])

    ema_fast = ema(closes, EMA_FAST)
    ema_slow = ema(closes, EMA_SLOW)
    rsi_vals = rsi(closes, RSI_PERIOD)
    atr_vals = atr(highs, lows, closes, ATR_PERIOD, breaks)
    prev = i - 1

    if None in (ema_fast[i], ema_fast[prev], ema_slow[i], ema_slow[prev],
                rsi_vals[i], rsi_vals[prev], atr_vals[i]):
        print("Indicators not fully warmed up yet, skipping this run.")
        return

    atr_now = atr_vals[i]
    if atr_now < MIN_ATR_DOLLARS:
        print(f"ATR too low (${atr_now:.2f}) — likely low-liquidity/frozen data, skipping run.")
        return

    # NEW: segment = candles since the last session break
    seg_start = max(breaks) if breaks else 0
    seg_len = i - seg_start + 1
    prev_contiguous = prev >= seg_start

    # segment-only views (used by the pullback trigger and the SMC context)
    s_o, s_h, s_l, s_c = opens[seg_start:], highs[seg_start:], lows[seg_start:], closes[seg_start:]
    s_atr = atr_vals[seg_start:]
    s_ef, s_es = ema_fast[seg_start:], ema_slow[seg_start:]
    s_i = len(s_c) - 1

    trend_up = ema_fast[i] > ema_slow[i]
    trend_down = ema_fast[i] < ema_slow[i]
    bull_cross = ema_fast[prev] <= ema_slow[prev] and ema_fast[i] > ema_slow[i]
    bear_cross = ema_fast[prev] >= ema_slow[prev] and ema_fast[i] < ema_slow[i]
    rsi_bounce_up = rsi_vals[prev] <= RSI_OVERSOLD and rsi_vals[i] > RSI_OVERSOLD
    rsi_bounce_down = rsi_vals[prev] >= RSI_OVERBOUGHT and rsi_vals[i] < RSI_OVERBOUGHT

    signal = 0
    trigger_type = "trend"
    # CHANGED: EMA/RSI triggers need a contiguous previous candle; otherwise the
    # "cross" is just the session gap.
    if prev_contiguous:
        if bull_cross or (trend_up and rsi_bounce_up):
            signal = 1
        elif bear_cross or (trend_down and rsi_bounce_down):
            signal = -1

    # NEW v5.2: trend-continuation pullback (checked before the momentum spike,
    # because it is a better-located entry than chasing a spike)
    pullback = None
    pb_diag_msg = None  # v5.2.1: why the pullback trigger did not fire (logging only)
    if signal == 0 and PULLBACK_ENABLED and seg_len < PULLBACK_MIN_SEGMENT_CANDLES:
        pb_diag_msg = (f"not evaluated: only {seg_len} candles since session start/gap "
                       f"(need {PULLBACK_MIN_SEGMENT_CANDLES})")
    if signal == 0 and PULLBACK_ENABLED and seg_len >= PULLBACK_MIN_SEGMENT_CANDLES:
        d_sell, d_buy = [], []
        info = detect_pullback_entry(s_o, s_h, s_l, s_c, s_ef, s_es, s_atr, s_i, diag=d_sell)
        if info:
            signal = -1
            pullback = info
        else:
            neg = lambda arr: [(-x if x is not None else None) for x in arr]
            # mirrored data: buy-side pullback == sell-side pullback on negated prices
            m = detect_pullback_entry(neg(s_o), neg(s_l), neg(s_h), neg(s_c),
                                      neg(s_ef), neg(s_es), s_atr, s_i, diag=d_buy)
            if signal == 0 and not m:
                # report the side whose trend conditions held; otherwise say there is no trend
                if d_sell and not d_sell[0].startswith(("trend:", "n/a:")):
                    pb_diag_msg = "SELL side - " + d_sell[0]
                elif d_buy and not d_buy[0].startswith(("trend:", "n/a:")):
                    pb_diag_msg = "BUY side - " + d_buy[0]
                elif d_sell and d_sell[0].startswith("n/a:"):
                    pb_diag_msg = d_sell[0][5:]
                else:
                    pb_diag_msg = (f"no established {PULLBACK_TREND_MIN_CANDLES}-candle trend "
                                   f"(EMA20/EMA50 not aligned and sloping) on either side")
            if m:
                signal = 1
                pullback = {"leg_start": -m["leg_start"], "leg_end": -m["leg_end"],
                            "pb_extreme": -m["pb_extreme"], "bounce": m["bounce"],
                            "leg_height": m["leg_height"], "retrace": m["retrace"]}
        if signal != 0:
            trigger_type = "pullback"

    if signal == 0:
        spike = detect_momentum_spike(opens, closes, atr_vals, i)
        if spike == "bullish":
            signal, trigger_type = 1, "momentum spike"
        elif spike == "bearish":
            signal, trigger_type = -1, "momentum spike"

    if signal == 0:
        print(f"No signal at {times[i]}. Trend up={trend_up}, RSI={rsi_vals[i]:.1f}, ATR=${atr_now:.2f}")
        if pb_diag_msg:
            print(f"Pullback check: {pb_diag_msg}")
        return

    direction = "BUY" if signal == 1 else "SELL"
    flags = []

    # NEW: overextension filter (chasing) — CHANGED v5.5: flags by default, no
    # longer suppresses (see OVEREXTENSION_ACTION); boolean carried forward
    # into the setup-strength score.
    overextended_flag = False
    if OVEREXTENSION_ACTION != "off":
        ext = ((closes[i] - ema_fast[i]) if signal == 1 else (ema_fast[i] - closes[i])) / atr_now
        ext_limit = PULLBACK_OVEREXTENSION_ATR_MULT if trigger_type == "pullback" else OVEREXTENSION_ATR_MULT
        if ext > ext_limit:
            overextended_flag = True
            if OVEREXTENSION_ACTION == "suppress":
                print(f"{direction} suppressed: price is {ext:.1f} ATR from EMA{EMA_FAST} "
                      f"(limit {ext_limit}) — overextended, likely chasing.")
                return
            flags.append(f"overextended: {ext:.1f} ATR from EMA{EMA_FAST}")

    # NEW v5.4/CHANGED v5.5: sweep cooldown — a sweep+reject is itself a
    # reversal signal; a continuation signal fighting it (sell right after a
    # swept LOW got rejected upward, or buy right after a swept HIGH got
    # rejected downward) is flagged (default) rather than blocked. The
    # boolean feeds the setup-strength score computed later.
    fights_sweep = False
    if SWEEP_COOLDOWN_ENABLED and trigger_type in ("trend", "pullback"):
        sweep_kind, sweep_idx, sweep_level = find_recent_sweep(s_h, s_l, s_c, s_i, SWEEP_COOLDOWN_CANDLES)
        fights_sweep = (sweep_kind == "low" and signal == -1) or (sweep_kind == "high" and signal == 1)
        if fights_sweep:
            candles_ago = s_i - sweep_idx
            msg = (f"{direction} vs recent liquidity sweep: {sweep_kind} swept at {sweep_level:.2f} "
                  f"and rejected {candles_ago} candle(s) ago")
            if SWEEP_COOLDOWN_ACTION == "suppress":
                print(f"{direction} suppressed — {msg}.")
                return
            flags.append(msg)

    # NEW v5.4: support/resistance levels — computed whenever enabled so the
    # nearest level can be shown as context in the message later, not only
    # used to gate continuation trades.
    sr_levels = find_sr_levels(s_h, s_l, s_i, atr_now) if SR_ENABLED else []
    sr_reclaim = detect_level_reclaim(sr_levels, s_c, s_i, atr_now) if sr_levels else None

    # CHANGED v5.5: structure reclaim now flags by default instead of
    # blocking. Only applies to trend/pullback (continuation) triggers.
    fights_reclaim = False
    if SR_ENABLED and trigger_type in ("trend", "pullback") and sr_reclaim:
        fights_reclaim = (
            (sr_reclaim["direction"] == "bullish" and signal == -1) or
            (sr_reclaim["direction"] == "bearish" and signal == 1)
        )
        if fights_reclaim:
            msg = (f"{direction} vs structure reclaim: price reclaimed {sr_reclaim['level']:.2f} "
                  f"({sr_reclaim['direction']}) within the last {SR_RECLAIM_LOOKBACK_CANDLES} candles")
            if SR_RECLAIM_ACTION == "suppress":
                print(f"{direction} suppressed — {msg}.")
                return
            flags.append(msg)

    # NEW: cooldown / per-direction cap — CHANGED v5.5: flags by default
    blocked, reason = check_cooldown(state, direction, candle_dt)
    cooldown_flag = blocked
    if blocked:
        if COOLDOWN_ACTION == "suppress":
            print(f"{direction} suppressed — {reason}")
            return
        flags.append(reason)

    # NEW: alert latency — CHANGED v5.5: flags by default instead of suppressing
    latency_min = (now_utc - (candle_dt + datetime.timedelta(minutes=INTERVAL_MINUTES))).total_seconds() / 60
    if latency_min > LATENCY_SUPPRESS_MIN:
        if LATENCY_ACTION == "suppress":
            print(f"{direction} suppressed: candle closed {latency_min:.0f} min ago (limit {LATENCY_SUPPRESS_MIN}).")
            return
        flags.append(f"very late alert: candle closed {latency_min:.0f} min ago — entry zone may be stale")
    elif latency_min > LATENCY_FLAG_MIN:
        flags.append(f"late alert: candle closed {latency_min:.0f} min ago")

    # NEW: post-reopen handling
    if REOPEN_ACTION != "off" and is_reopen_window(candle_dt):
        if REOPEN_ACTION == "suppress":
            print(f"{direction} suppressed: inside post-reopen window.")
            return
        flags.append("post-reopen window: spreads/slippage often elevated")

    # v5 range gate, now on cleaned data
    lb = max(0, i - 50)
    if (max(highs[lb:i + 1]) - min(lows[lb:i + 1])) < MIN_RECENT_RANGE_DOLLARS:
        print("Recent clean range too tight — treating as unreliable data, skipping run.")
        return

    # CHANGED v5.5: HTF conflict now flags by default instead of suppressing;
    # boolean feeds the setup-strength score below.
    htf_trend = fetch_htf_trend()
    htf_conflict = False
    if trigger_type in ("trend", "pullback") and htf_trend is not None:
        htf_conflict = (htf_trend == "up" and signal == -1) or (htf_trend == "down" and signal == 1)
        if htf_conflict:
            if HTF_CONFLICT_ACTION == "suppress":
                print(f"Trend-trigger {direction} conflicts with 1H trend ({htf_trend}) — suppressed.")
                return
            flags.append(f"{direction} conflicts with 1H trend ({htf_trend})")

    # NEW v5.4: 15m/1H divergence note (informational only, does not suppress)
    # — flags when the working-timeframe EMA20 is sloping against the 1H
    # trend, so a turning lower-timeframe trend isn't silently invisible.
    ltf_divergence_note = None
    if htf_trend is not None and i - LTF_SLOPE_CANDLES >= 0 and ema_fast[i - LTF_SLOPE_CANDLES] is not None:
        ltf_slope_up = ema_fast[i] > ema_fast[i - LTF_SLOPE_CANDLES]
        if (htf_trend == "down" and ltf_slope_up) or (htf_trend == "up" and not ltf_slope_up):
            ltf_dir = "up" if ltf_slope_up else "down"
            ltf_divergence_note = (f"15m EMA{EMA_FAST} sloping {ltf_dir} while 1H trend is {htf_trend} "
                                   f"— lower timeframe may be turning")

    close_now = closes[i]
    half_width = atr_now * ENTRY_RANGE_ATR_MULT
    entry_low, entry_high = close_now - half_width, close_now + half_width

    # NEW v5.4: zone-overlap dedup — suppress a near-repeat of an unresolved
    # same-direction signal instead of resending essentially the same trade.
    if DEDUP_ENABLED:
        dup = overlapping_open_signal(state, direction, entry_low, entry_high)
        if dup is not None:
            print(f"{direction} suppressed: zone overlaps an unresolved {direction} signal "
                  f"from {dup.get('candle_time', '?')} (entry {dup.get('entry')}).")
            return

    sl_distance = max(atr_now * SL_ATR_MULT, MIN_SL_DOLLARS)
    tp1_distance = max(atr_now * TP_ATR_MULT, MIN_TP_DOLLARS)
    if pullback is not None:  # NEW v5.2: stop just beyond the bounce extreme
        sl_distance = max(abs(pullback["pb_extreme"] - close_now) + PULLBACK_SL_BUFFER_ATR * atr_now,
                          MIN_SL_DOLLARS, PULLBACK_MIN_SL_ATR * atr_now)
        if sl_distance > PULLBACK_MAX_SL_ATR * atr_now:
            print(f"Pullback {direction} skipped: structural stop {sl_distance:.1f} pts is "
                  f"{sl_distance / atr_now:.1f} ATR away (limit {PULLBACK_MAX_SL_ATR}) — late entry.")
            return
    if tp1_distance / sl_distance < MIN_RR_TP1:  # NEW: R:R floor
        tp1_distance = sl_distance * MIN_RR_TP1

    candle_time = times[i]
    if state.get("last_alert_time") == candle_time and state.get("last_alert_direction") == direction:
        print(f"Already alerted this candle ({candle_time}, {direction}) — skipping duplicate.")
        return

    news_flag, news_title = is_news_window(NEWS_BUFFER_MIN)

    # ---- NEW: SMC context computed on the current session segment only ----

    bos_bull, bos_bear, _, _ = detect_structure(s_h, s_l, s_c, s_i)
    if bos_bull:
        structure_note = "bullish break of structure (price closed above recent swing high)"
    elif bos_bear:
        structure_note = "bearish break of structure (price closed below recent swing low)"
    else:
        structure_note = "no confirmed break of recent structure yet"

    def relevant_zone(z):
        return z is not None and zone_distance(close_now, z["low"], z["high"]) <= MAX_CONTEXT_DISTANCE_ATR * atr_now

    nearest_fvg = find_nearest_unfilled_fvg(s_h, s_l, s_c, s_i)
    if not relevant_zone(nearest_fvg):
        nearest_fvg = None

    swept_high, swept_low, sh_val, sl_val = detect_liquidity_sweep(s_h, s_l, s_c, s_i)
    if swept_high:
        sweep_note = f"recent liquidity sweep above {sh_val:.2f} (swept then rejected)"
    elif swept_low:
        sweep_note = f"recent liquidity sweep below {sl_val:.2f} (swept then rejected)"
    else:
        sweep_note = None

    order_block = find_last_order_block(s_o, s_h, s_l, s_c, s_atr, s_i)
    if not relevant_zone(order_block):
        order_block = None

    seg_high, seg_low, seg_mid = premium_discount_zone(s_h, s_l, s_i)
    smc_range_ok = seg_len >= MIN_SEGMENT_CANDLES and (seg_high - seg_low) >= MIN_RECENT_RANGE_DOLLARS
    if not smc_range_ok:
        if SEGMENT_SHORT_ACTION == "suppress":
            print(f"{direction} suppressed: only {seg_len} candles since last session break.")
            return
        flags.append(f"limited SMC context: {seg_len} candles since session start/gap "
                     f"(premium/discount, OTE, equal-level tags omitted)")

    eq_high = eq_low = ote = None
    if smc_range_ok:
        eq_high = find_equal_levels(s_h, s_i, mode="high")
        eq_low = find_equal_levels(s_l, s_i, mode="low")
        if eq_high is not None and abs(eq_high - close_now) > MAX_CONTEXT_DISTANCE_ATR * atr_now:
            eq_high = None
        if eq_low is not None and abs(eq_low - close_now) > MAX_CONTEXT_DISTANCE_ATR * atr_now:
            eq_low = None
        ote = compute_ote_zone(s_h, s_l, s_i)
        # only keep an OTE that matches the signal direction and is near price
        if ote is not None:
            ote_is_bull = ote["type"].startswith("bullish")
            if ote_is_bull != (signal == 1) or not relevant_zone(ote):
                ote = None

    # confluence uses only the filtered (relevant) items
    aligned, total = compute_confluence(
        signal, bos_bull, bos_bear, nearest_fvg, order_block, swept_high, swept_low, htf_trend
    )
    if aligned < MIN_CONFLUENCE_TO_SEND:
        print(f"Confluence {aligned}/{total} below MIN_CONFLUENCE_TO_SEND={MIN_CONFLUENCE_TO_SEND} — suppressing.")
        return

    # NEW v5.5: setup-strength label — replaces the old send/no-send tier.
    # Every signal is sent now; this just tells you how much to trust it.
    # adjusted_score = confluence (0-5, already includes the opposing-OB
    # penalty from v5.4) minus 1 point each for the "fighting the tape"
    # conditions flagged above.
    strength_penalty = sum([overextended_flag, fights_sweep, fights_reclaim, htf_conflict])
    adjusted_score = max(aligned - strength_penalty, 0)
    if adjusted_score >= STRENGTH_STRONG_MIN:
        setup_strength = "STRONG"
    elif adjusted_score >= STRENGTH_MEDIUM_MIN:
        setup_strength = "MEDIUM"
    else:
        setup_strength = "WEAK"
    confidence_tier = setup_strength  # kept for CSV/state field-name continuity

    # ---- NEW: targets ----
    if signal == 1:
        sl = close_now - sl_distance
        tp1 = close_now + tp1_distance
    else:
        sl = close_now + sl_distance
        tp1 = close_now - tp1_distance
    tp2, tp2_source = compute_targets(
        signal, close_now, atr_now, tp1_distance, s_h, s_l, eq_high, eq_low, smc_range_ok
    )
    rr1 = tp1_distance / sl_distance
    rr2 = abs(tp2 - close_now) / sl_distance
    trail = TRAIL_ATR_MULT * atr_now

    counter_trend_note = ""
    if trigger_type == "momentum spike" and htf_trend is not None:
        if (htf_trend == "up" and signal == -1) or (htf_trend == "down" and signal == 1):
            counter_trend_note = " (counter-trend spike)"

    trigger_label = "trend-continuation pullback" if pullback else trigger_type
    strength_emoji = {"STRONG": "🟢", "MEDIUM": "🟡", "WEAK": "🔴"}[setup_strength]
    message = (
        f"{strength_emoji} Setup strength: {setup_strength}  (confluence {aligned}/{total}, "
        f"adjusted {adjusted_score} after {strength_penalty} caution flag(s))\n\n"
        f"XAU/USD {direction} zone{counter_trend_note}\n"
        f"Trigger: {trigger_label}\n"
        f"Zone: {entry_low:.2f} - {entry_high:.2f}\n"
        f"SL: {sl:.2f}   TP1: {tp1:.2f} ({rr1:.1f}R)   TP2: {tp2:.2f} ({rr2:.1f}R, {tp2_source})\n"
        f"Plan: at TP1 move SL to entry; then trail ~{trail:.1f} pts behind price toward TP2\n"
        f"Time: {times[i]} UTC (alert {max(latency_min, 0):.0f} min after candle close)\n"
        f"Candle O/H/L/C: {opens[i]:.2f} / {highs[i]:.2f} / {lows[i]:.2f} / {closes[i]:.2f}\n"
        f"Confluence: {aligned}/{total} concepts aligned\n"
    )
    if flags:
        message += "Flags: " + "; ".join(flags) + "\n"
    if pullback:
        message += (f"Pullback: bounced {pullback['bounce']:.1f} pts ({pullback['retrace'] * 100:.0f}% of the "
                    f"{pullback['leg_height']:.1f}-pt leg), rejected near {pullback['pb_extreme']:.2f}; "
                    f"SL sits just beyond that level\n")
    message += f"\nStructure: {structure_note}\n"
    if nearest_fvg:
        message += (f"Nearby unfilled FVG ({nearest_fvg['type']}): "
                    f"{nearest_fvg['low']:.2f} - {nearest_fvg['high']:.2f}\n")
    if sweep_note:
        message += f"Liquidity: {sweep_note}\n"
    if order_block:
        ob_aligned = (order_block["type"] == "bullish") == (signal == 1)
        ob_label = "aligned" if ob_aligned else "opposing - possible support/target"
        message += (f"Order block ({order_block['type']}, {ob_label}): "
                    f"{order_block['low']:.2f} - {order_block['high']:.2f}\n")
    if smc_range_ok:
        zone_label = ("premium (upper half of session range)" if close_now > seg_mid
                      else "discount (lower half of session range)")
        message += f"Price sits in {zone_label} (range {seg_low:.2f} - {seg_high:.2f})\n"
    if htf_trend:
        agreement = ("agrees with" if (htf_trend == "up" and signal == 1) or (htf_trend == "down" and signal == -1)
                     else "conflicts with")
        message += f"1H trend: {htf_trend} ({agreement} this signal)\n"
    if ltf_divergence_note:
        message += f"⚠️ Divergence: {ltf_divergence_note}\n"
    killzone = get_killzone(times[i])
    message += f"Session: {killzone}\n" if killzone else "Session: outside main London/New York killzones\n"
    if eq_high is not None:
        message += f"Equal highs (liquidity pool) near {eq_high:.2f}\n"
    if eq_low is not None:
        message += f"Equal lows (liquidity pool) near {eq_low:.2f}\n"
    if ote:
        message += f"OTE zone {ote['type']}: {ote['low']:.2f} - {ote['high']:.2f}\n"
    # NEW v5.4: nearest S/R level, shown as context
    nearest_lvl = nearest_sr_level(sr_levels, close_now, MAX_CONTEXT_DISTANCE_ATR, atr_now)
    if nearest_lvl:
        role = "resistance" if nearest_lvl["price"] > close_now else "support"
        message += f"Nearby S/R level ({role}, {nearest_lvl['touches']} touches): {nearest_lvl['price']:.2f}\n"
    if sr_reclaim:
        message += (f"Structure reclaim: price reclaimed {sr_reclaim['level']:.2f} "
                    f"({sr_reclaim['direction']}) recently\n")
    if news_flag:
        message += f"\n⚠️ CAUTION: high-impact USD news nearby ({news_title})\n"
    message += "\nData status: live (session, freshness, ATR, range and clean-candle checks passed)\n"
    message += (
        "\n(Guidance only — structure/FVG/order blocks/sweeps are added context, "
        "not a prediction. No trade placed automatically.)"
    )

    print(message)
    # CHANGED v5.5: every signal is sent now — nothing is withheld based on
    # setup strength. The WEAK/MEDIUM/STRONG label above is for your own
    # manual filtering, not the bot's.
    send_telegram(message)

    # ---- state, cooldown history, tracker, log ----
    state["last_alert_time"] = candle_time
    state["last_alert_direction"] = direction
    state.setdefault("sent", []).append({"t": candle_time, "d": direction})
    state.setdefault("open_signals", []).append({
        "candle_time": candle_time, "direction": direction, "trigger": trigger_type,
        "entry": close_now, "sl": sl, "tp1": tp1, "tp2": tp2,
        "confluence": f"{aligned}/{total}", "confidence_tier": confidence_tier,
        "stage": 0, "candles_seen": 0, "last_checked": candle_time,
    })
    append_log({
        "event": "signal", "time_utc": candle_time, "direction": direction, "trigger": trigger_type,
        "entry": f"{close_now:.2f}", "sl": f"{sl:.2f}", "tp1": f"{tp1:.2f}", "tp2": f"{tp2:.2f}",
        "confluence": f"{aligned}/{total}", "confidence_tier": confidence_tier,
        "latency_min": f"{max(latency_min, 0):.0f}", "flags": " | ".join(flags),
    })


def main():
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    if not is_market_open(now_utc):
        print(f"Market closed at {now_utc.isoformat()} UTC — skipping run.")
        return
    state = load_state()
    try:
        run(state, now_utc)
    finally:
        save_state(state)  # always persist tracker/cooldown state, even on early exits


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        raise
