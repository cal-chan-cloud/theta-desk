"""Price-trend analytics: indicators, levels, and a single composite score.

The composite trend score is the number the strategy selector actually reads,
so its sub-scores are all normalised to [-1, +1] *by the stock's own
volatility* rather than by fixed thresholds.  A 5% move in TLT and a 5% move in
MSTR are not the same signal, and a scanner that treats them alike will
systematically over-rank low-vol names as "breakouts".
"""

import math

import config


# ------------------------------------------------------------------- helpers
def sma(vals, n):
    if len(vals) < n or n <= 0:
        return None
    return sum(vals[-n:]) / n


def sma_series(vals, n):
    out, run = [], 0.0
    for i, v in enumerate(vals):
        run += v
        if i >= n:
            run -= vals[i - n]
        out.append(run / n if i >= n - 1 else None)
    return out


def ema_series(vals, n):
    if not vals:
        return []
    k = 2.0 / (n + 1)
    out = [None] * len(vals)
    if len(vals) < n:
        return out
    seed = sum(vals[:n]) / n
    out[n - 1] = seed
    prev = seed
    for i in range(n, len(vals)):
        prev = vals[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def ema(vals, n):
    s = ema_series(vals, n)
    return s[-1] if s else None


def rsi(closes, period=None):
    period = period or config.RSI_PERIOD
    if len(closes) < period + 1:
        return None
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    ag, al = gains / period, losses / period
    for i in range(period + 1, len(closes)):          # Wilder smoothing
        d = closes[i] - closes[i - 1]
        ag = (ag * (period - 1) + max(d, 0.0)) / period
        al = (al * (period - 1) + max(-d, 0.0)) / period
    if al == 0:
        return 100.0
    rs = ag / al
    return 100.0 - 100.0 / (1.0 + rs)


def macd(closes, fast=None, slow=None, signal=None):
    fast = fast or config.MACD_FAST
    slow = slow or config.MACD_SLOW
    signal = signal or config.MACD_SIGNAL
    if len(closes) < slow + signal:
        return {"macd": None, "signal": None, "hist": None}
    ef, es = ema_series(closes, fast), ema_series(closes, slow)
    line = [(a - b) if (a is not None and b is not None) else None for a, b in zip(ef, es)]
    valid = [v for v in line if v is not None]
    sig = ema_series(valid, signal)
    m = line[-1]
    s = sig[-1] if sig else None
    return {"macd": m, "signal": s, "hist": (m - s) if (m is not None and s is not None) else None}


def true_range(bars):
    trs = []
    for i in range(1, len(bars)):
        h, l = bars[i]["high"], bars[i]["low"]
        pc = bars[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return trs


def atr(bars, period=None):
    period = period or config.ATR_PERIOD
    trs = true_range(bars)
    if len(trs) < period:
        return None
    a = sum(trs[:period]) / period
    for t in trs[period:]:
        a = (a * (period - 1) + t) / period            # Wilder
    return a


def adx(bars, period=None):
    """Wilder ADX with +DI/-DI.  Returns (adx, plus_di, minus_di)."""
    period = period or config.ADX_PERIOD
    if len(bars) < period * 2 + 2:
        return None, None, None
    plus_dm, minus_dm, trs = [], [], []
    for i in range(1, len(bars)):
        up = bars[i]["high"] - bars[i - 1]["high"]
        dn = bars[i - 1]["low"] - bars[i]["low"]
        plus_dm.append(up if (up > dn and up > 0) else 0.0)
        minus_dm.append(dn if (dn > up and dn > 0) else 0.0)
        h, l, pc = bars[i]["high"], bars[i]["low"], bars[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))

    def wilder(seq):
        s = sum(seq[:period])
        out = [s]
        for v in seq[period:]:
            s = s - s / period + v
            out.append(s)
        return out

    tr_s, p_s, m_s = wilder(trs), wilder(plus_dm), wilder(minus_dm)
    dxs = []
    for tr, p, m in zip(tr_s, p_s, m_s):
        if tr <= 0:
            continue
        pdi, mdi = 100.0 * p / tr, 100.0 * m / tr
        denom = pdi + mdi
        dxs.append(100.0 * abs(pdi - mdi) / denom if denom > 0 else 0.0)
    if len(dxs) < period:
        return None, None, None
    a = sum(dxs[:period]) / period
    for d in dxs[period:]:
        a = (a * (period - 1) + d) / period
    tr, p, m = tr_s[-1], p_s[-1], m_s[-1]
    pdi = 100.0 * p / tr if tr > 0 else None
    mdi = 100.0 * m / tr if tr > 0 else None
    return a, pdi, mdi


def bollinger(closes, period=None, nstd=None):
    period = period or config.BB_PERIOD
    nstd = nstd or config.BB_STD
    if len(closes) < period:
        return {"mid": None, "upper": None, "lower": None, "pctb": None, "width": None}
    win = closes[-period:]
    m = sum(win) / period
    var = sum((x - m) ** 2 for x in win) / period
    sd = math.sqrt(var)
    up, lo = m + nstd * sd, m - nstd * sd
    px = closes[-1]
    pctb = (px - lo) / (up - lo) if up > lo else 0.5
    return {"mid": m, "upper": up, "lower": lo, "pctb": pctb,
            "width": (up - lo) / m if m else None}


def obv(bars):
    out, run = [], 0.0
    for i in range(1, len(bars)):
        v = bars[i].get("volume") or 0
        if bars[i]["close"] > bars[i - 1]["close"]:
            run += v
        elif bars[i]["close"] < bars[i - 1]["close"]:
            run -= v
        out.append(run)
    return out


def slope_norm(series, n=20):
    """Least-squares slope over n points, normalised by the series' own scale."""
    s = [v for v in series[-n:] if v is not None]
    if len(s) < 5:
        return None
    m = len(s)
    mx = (m - 1) / 2.0
    my = sum(s) / m
    sxx = sum((i - mx) ** 2 for i in range(m))
    sxy = sum((i - mx) * (v - my) for i, v in enumerate(s))
    if sxx == 0:
        return None
    b = sxy / sxx
    scale = max(abs(my), 1e-9)
    return b * m / scale


def returns(closes, n):
    if len(closes) <= n or closes[-n - 1] <= 0:
        return None
    return closes[-1] / closes[-n - 1] - 1.0


# ------------------------------------------------------------ support/resist
def swing_levels(bars, lookback=180, fractal=3, max_levels=6):
    """Pivot highs/lows clustered into levels, scored by touches and recency."""
    b = bars[-lookback:]
    if len(b) < fractal * 2 + 5:
        return {"support": [], "resistance": []}
    a = atr(b) or (b[-1]["close"] * 0.02)
    pivots_h, pivots_l = [], []
    for i in range(fractal, len(b) - fractal):
        win = b[i - fractal:i + fractal + 1]
        if b[i]["high"] >= max(x["high"] for x in win):
            pivots_h.append((b[i]["high"], i))
        if b[i]["low"] <= min(x["low"] for x in win):
            pivots_l.append((b[i]["low"], i))

    def cluster(pivots):
        out = []
        for price, idx in sorted(pivots):
            placed = False
            for c in out:
                if abs(c["price"] - price) <= a * 0.75:
                    n = c["touches"]
                    c["price"] = (c["price"] * n + price) / (n + 1)
                    c["touches"] = n + 1
                    c["last"] = max(c["last"], idx)
                    placed = True
                    break
            if not placed:
                out.append({"price": price, "touches": 1, "last": idx})
        for c in out:
            recency = math.exp(-(len(b) - 1 - c["last"]) / 60.0)
            c["strength"] = round(c["touches"] * (0.4 + 0.6 * recency), 2)
        return sorted(out, key=lambda c: -c["strength"])[:max_levels]

    px = b[-1]["close"]
    res = [c for c in cluster(pivots_h) if c["price"] > px * 1.001]
    sup = [c for c in cluster(pivots_l) if c["price"] < px * 0.999]
    res.sort(key=lambda c: c["price"])
    sup.sort(key=lambda c: -c["price"])
    return {"support": sup, "resistance": res}


# ------------------------------------------------------------ composite score
def _clip(x, lo=-1.0, hi=1.0):
    return max(lo, min(hi, x))


def analyze(bars, bench_bars=None):
    """Full technical picture for one symbol."""
    bars = [b for b in bars if b.get("close")]
    if len(bars) < 60:
        return None
    closes = [b["close"] for b in bars]
    px = closes[-1]

    s20, s50, s200 = sma(closes, 20), sma(closes, 50), sma(closes, 200)
    e20 = ema(closes, 20)
    r = rsi(closes)
    mac = macd(closes)
    a = atr(bars)
    atr_pct = (a / px) if (a and px) else None
    adx_v, pdi, mdi = adx(bars)
    bb = bollinger(closes)
    ob = obv(bars)
    obv_sl = slope_norm(ob, 20)
    vols = [b.get("volume") or 0 for b in bars]
    avg_vol = sma(vols, 20)
    rvol = (vols[-1] / avg_vol) if (avg_vol and avg_vol > 0) else None

    ret5, ret20, ret60 = returns(closes, 5), returns(closes, 20), returns(closes, 60)
    hi52 = max(closes[-252:]) if len(closes) >= 60 else max(closes)
    lo52 = min(closes[-252:]) if len(closes) >= 60 else min(closes)
    pos52 = (px - lo52) / (hi52 - lo52) if hi52 > lo52 else 0.5

    rel = None
    if bench_bars and len(bench_bars) > 25:
        bc = [b["close"] for b in bench_bars if b.get("close")]
        br20 = returns(bc, 20)
        if ret20 is not None and br20 is not None:
            rel = ret20 - br20

    # ---- sub-scores, each in [-1, 1] ------------------------------------
    sub = {}
    if s20 and s50 and s200:
        stack = 0.0
        stack += 0.34 if px > s20 else -0.34
        stack += 0.33 if s20 > s50 else -0.33
        stack += 0.33 if s50 > s200 else -0.33
        sub["ma_stack"] = _clip(stack)
    if s200:
        sub["price_vs_200"] = _clip(math.tanh((px / s200 - 1.0) / 0.12))
    if mac["hist"] is not None and px:
        # Normalise MACD by the stock's own daily range, not by dollars.
        scale = max((atr_pct or 0.02) * px * 0.6, 1e-6)
        sub["macd"] = _clip(math.tanh(mac["hist"] / scale))
    if r is not None:
        sub["rsi"] = _clip((r - 50.0) / 22.0)
    if adx_v is not None and pdi is not None and mdi is not None:
        direction = 1.0 if pdi > mdi else -1.0
        sub["adx_dir"] = _clip(direction * min(adx_v / 35.0, 1.0))
    if ret20 is not None and atr_pct:
        # 20-day return in units of its own 20-day sigma
        sigma20 = atr_pct * math.sqrt(20.0)
        sub["roc"] = _clip(math.tanh(ret20 / max(sigma20, 1e-6)))
    if rel is not None:
        sub["rel_strength"] = _clip(math.tanh(rel / 0.06))

    num = den = 0.0
    for k, w in config.TREND_WEIGHTS.items():
        if k in sub:
            num += w * sub[k]
            den += w
    score = (num / den) if den > 0 else 0.0
    coverage = den / sum(config.TREND_WEIGHTS.values())

    if score >= 0.55:
        label = "strong up"
    elif score >= 0.20:
        label = "up"
    elif score > -0.20:
        label = "neutral"
    elif score > -0.55:
        label = "down"
    else:
        label = "strong down"

    levels = swing_levels(bars)
    support = levels["support"][0]["price"] if levels["support"] else None
    resistance = levels["resistance"][0]["price"] if levels["resistance"] else None

    return {
        "price": px,
        "sma20": s20, "sma50": s50, "sma200": s200, "ema20": e20,
        "rsi": r, "macd": mac["macd"], "macd_signal": mac["signal"], "macd_hist": mac["hist"],
        "atr": a, "atr_pct": atr_pct,
        "adx": adx_v, "plus_di": pdi, "minus_di": mdi,
        "bb_upper": bb["upper"], "bb_lower": bb["lower"], "bb_mid": bb["mid"],
        "bb_pctb": bb["pctb"], "bb_width": bb["width"],
        "obv_slope": obv_sl, "rvol": rvol, "avg_volume": avg_vol,
        "ret_5": ret5, "ret_20": ret20, "ret_60": ret60,
        "high_52w": hi52, "low_52w": lo52, "pos_52w": pos52,
        "rel_strength": rel,
        "trend_score": score, "trend_label": label, "trend_coverage": coverage,
        "trend_components": sub,
        "levels": levels,
        "support": support, "resistance": resistance,
    }
