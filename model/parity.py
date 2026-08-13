"""Forward and discount-factor recovery from put-call parity.

Why bother
----------
Nearly every retail options tool prices with `q = 0` and a single hard-coded
risk-free rate.  On a dividend payer that pushes call IVs up and put IVs down by
several points, which is *exactly* the size of the skew signal we want to trade
off.  Recovering the forward from the market's own put-call parity relationship
removes the bias entirely and costs one weighted regression per expiry.

    C - P = df * (F - K)

Regressing (C-P) on K gives slope = -df and intercept = df*F.  The regression is
weighted by quote quality and re-fit once with outliers rejected, because a
single stale strike can otherwise drag the forward by 0.5%.
"""

import math

import config


class YieldCurve:
    """Piecewise log-linear zero curve from Treasury proxies (^IRX/^FVX/^TNX)."""

    def __init__(self, points=None):
        # (years, continuously compounded rate)
        self.points = sorted(points or [(0.25, config.DEFAULT_RISK_FREE)])

    @classmethod
    def from_quotes(cls, quotes):
        """quotes: {'^IRX': 3.69, '^FVX': 4.0, '^TNX': 4.3} in percent."""
        tenors = {"^IRX": 0.25, "^FVX": 5.0, "^TNX": 10.0, "^TYX": 30.0}
        pts = []
        for sym, t in tenors.items():
            v = quotes.get(sym)
            if v is None:
                continue
            try:
                y = float(v) / 100.0
            except (TypeError, ValueError):
                continue
            if -0.01 < y < 0.25:
                # bond-equivalent -> continuously compounded
                pts.append((t, math.log1p(y)))
        if not pts:
            return cls()
        return cls(pts)

    def rate(self, T):
        pts = self.points
        if not pts:
            return config.DEFAULT_RISK_FREE
        if T <= pts[0][0]:
            return pts[0][1]
        if T >= pts[-1][0]:
            return pts[-1][1]
        for i in range(1, len(pts)):
            t0, r0 = pts[i - 1]
            t1, r1 = pts[i]
            if T <= t1:
                w = (T - t0) / (t1 - t0)
                return r0 + w * (r1 - r0)
        return pts[-1][1]

    def df(self, T):
        return math.exp(-self.rate(T) * max(T, 0.0))

    def to_dict(self):
        return {"points": [[t, r] for t, r in self.points]}


def _wls(xs, ys, ws):
    """Weighted least squares y = a + b*x.  Returns (a, b, r2)."""
    sw = sum(ws)
    if sw <= 0 or len(xs) < 2:
        return None
    mx = sum(w * x for w, x in zip(ws, xs)) / sw
    my = sum(w * y for w, y in zip(ws, ys)) / sw
    sxx = sum(w * (x - mx) ** 2 for w, x in zip(ws, xs))
    sxy = sum(w * (x - mx) * (y - my) for w, x, y in zip(ws, xs, ys))
    if abs(sxx) < 1e-12:
        return None
    b = sxy / sxx
    a = my - b * mx
    syy = sum(w * (y - my) ** 2 for w, y in zip(ws, ys))
    r2 = 1.0 if syy <= 1e-15 else max(0.0, min(1.0, (sxy * sxy) / (sxx * syy)))
    return a, b, r2


def implied_forward(pairs, spot, T, curve, band=(0.80, 1.25)):
    """Recover (F, df, quality) for one expiry.

    `pairs` : list of dicts with keys strike, call_mid, put_mid, call_spread,
              put_spread, call_oi, put_oi  (mids must be two-sided quotes).
    Returns a dict; `method` records which estimator won so the UI can show it.
    """
    fallback_df = curve.df(T)
    fallback = {
        "forward": spot * math.exp(curve.rate(T) * T),
        "df": fallback_df,
        "rate": curve.rate(T),
        "div_yield": 0.0,
        "r2": 0.0,
        "n": 0,
        "method": "curve",
        "dispersion": None,
    }
    if spot <= 0 or T <= 0:
        return fallback

    usable = []
    for p in pairs:
        k = p.get("strike") or 0.0
        cm, pm = p.get("call_mid"), p.get("put_mid")
        if not k or cm is None or pm is None or cm <= 0 or pm <= 0:
            continue
        if not (band[0] * spot <= k <= band[1] * spot):
            continue
        # Weight by tightness and by how ATM the pair is: parity is exact
        # everywhere but only *measurable* where both legs have real quotes.
        spread = (p.get("call_spread") or 0.5) + (p.get("put_spread") or 0.5)
        moneyness = abs(math.log(k / spot))
        w = math.exp(-((moneyness / 0.10) ** 2)) / (1.0 + 20.0 * spread)
        oi = (p.get("call_oi") or 0) + (p.get("put_oi") or 0)
        w *= 1.0 + math.log1p(max(oi, 0.0)) / 12.0
        if w > 1e-9:
            usable.append((k, cm - pm, w))

    if len(usable) < 4:
        return fallback

    xs = [u[0] for u in usable]
    ys = [u[1] for u in usable]
    ws = [u[2] for u in usable]

    fit = _wls(xs, ys, ws)
    if fit is None:
        return fallback
    a, b, r2 = fit

    # Reject outliers once, then re-fit -- one stale strike moves F materially.
    resid = [abs(y - (a + b * x)) for x, y in zip(xs, ys)]
    order = sorted(resid)
    med = order[len(order) // 2]
    keep = [i for i, rr in enumerate(resid) if rr <= max(4.0 * med, 0.02 * spot)]
    if 4 <= len(keep) < len(xs):
        fit2 = _wls([xs[i] for i in keep], [ys[i] for i in keep], [ws[i] for i in keep])
        if fit2:
            a, b, r2 = fit2
            xs = [xs[i] for i in keep]
            ys = [ys[i] for i in keep]
            ws = [ws[i] for i in keep]

    df_fit = -b
    ok_regression = (
        len(xs) >= 8 and r2 > 0.9990
        and 0.70 < df_fit <= 1.0005
        and -0.02 < (-math.log(min(df_fit, 1.0)) / T if df_fit > 0 else -1) < 0.20
    )

    if ok_regression:
        df = min(df_fit, 1.0)
        F = a / df
        method = "parity"
    else:
        # Discount factors from Treasuries are reliable; only F is uncertain.
        df = fallback_df
        num = sum(w * (k + y / df) for k, y, w in zip(xs, ys, ws))
        den = sum(ws)
        F = num / den if den > 0 else fallback["forward"]
        method = "parity-fixed-df"

    if not (0.5 * spot < F < 2.0 * spot):
        return fallback

    fwds = [k + y / df for k, y in zip(xs, ys)]
    mean_f = sum(fwds) / len(fwds)
    dispersion = math.sqrt(sum((f - mean_f) ** 2 for f in fwds) / len(fwds)) / spot

    r = -math.log(df) / T
    q = r - math.log(F / spot) / T
    return {
        "forward": F,
        "df": df,
        "rate": r,
        "div_yield": q,
        "r2": r2,
        "n": len(xs),
        "method": method,
        "dispersion": dispersion,
    }
