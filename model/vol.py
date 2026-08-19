"""Volatility: smile fitting, skew metrics, realised estimators, IV rank.

The smile fit is the load-bearing piece.  Raw vendor IVs are noisy (wide
spreads, stale wings, integer-penny quoting), and every downstream metric --
25-delta skew, expected move, the risk-neutral density behind every EV
calculation -- inherits that noise unless it is smoothed first.

We fit  iv(z) = a + b*z + c*z^2  in *standardised* log-moneyness

    z = ln(K/F) / (sigma_atm * sqrt(T))

rather than in raw log-moneyness.  This matters more than it sounds.  A real
smile is roughly V-shaped in raw k, and its curvature scales like 1/(sigma^2 T);
fitting a parabola to raw k over a chain whose strikes run +/-25% therefore puts
the parabola's vertex well above the true at-the-money vol.  Measured on live
1-DTE AAPL, the raw-k fit returned 41.5% ATM against a 26.6% model-free vol --
a 15-point error that would have made every short-dated option look like a
screaming sell.  In z-space the strike range is ~+/-3 standard deviations
regardless of tenor, the quadratic is a good local approximation, and the same
chain fits to within a few tenths of a point of the model-free value.

A quadratic is deliberately chosen over SVI: it is closed-form (no optimiser to
diverge on a thin chain) and it reproduces 25-delta skew to within a few
hundredths of a vol point on liquid names.  Wings beyond the fitted range
extrapolate flat rather than following the parabola off to infinity.
"""

import math

import config
from . import bs

Z_FIT_LIMIT = 3.5          # widest |z| admitted to the fit
Z_KERNEL = 2.8             # Gaussian down-weighting scale in z
WING_DAMP = 0.55           # how much of the edge slope survives into the wings


# ============================================================== SMILE FITTING
class Smile:
    __slots__ = ("a", "b", "c", "rmse", "k_min", "k_max", "z_min", "z_max",
                 "scale", "n", "atm_iv", "T", "F")

    def __init__(self, a, b, c, rmse, k_min, k_max, scale, n, T, F):
        self.a, self.b, self.c = a, b, c
        self.rmse, self.k_min, self.k_max, self.n = rmse, k_min, k_max, n
        self.scale = max(scale, 1e-6)
        self.z_min, self.z_max = k_min / self.scale, k_max / self.scale
        self.T, self.F = T, F
        self.atm_iv = self.iv_k(0.0)

    def _eval(self, z):
        return self.a + self.b * z + self.c * z * z

    def _slope_z(self, z):
        return self.b + 2.0 * self.c * z

    def iv_k(self, k):
        """Quadratic inside the fitted range, damped-linear outside it.

        Flat wings were the first attempt and they are wrong in a way that
        matters: they cut deep out-of-the-money put vol off at the fit boundary,
        which flattens the left tail of the recovered density and makes CVaR
        look better than the market is actually pricing.  Continuing the
        parabola instead is worse still -- its curvature explodes.  A damped
        linear continuation, floored at the boundary value so a wing can never
        slope back *down* as it goes further out of the money, keeps the tails
        honest without inventing curvature the quotes never supported.
        """
        z = k / self.scale
        if z < self.z_min:
            edge = self._eval(self.z_min)
            v = edge + self._slope_z(self.z_min) * (z - self.z_min) * WING_DAMP
            v = max(v, edge)
        elif z > self.z_max:
            edge = self._eval(self.z_max)
            v = edge + self._slope_z(self.z_max) * (z - self.z_max) * WING_DAMP
            v = max(v, edge)
        else:
            v = self._eval(z)
        return min(max(v, config.MIN_IV), config.MAX_IV)

    def iv(self, K):
        if K <= 0 or self.F <= 0:
            return self.a
        return self.iv_k(math.log(K / self.F))

    def slope(self, k=0.0):
        """d(iv)/d(log K) -- reported in raw log-moneyness units."""
        z = min(max(k / self.scale, self.z_min), self.z_max)
        return self._slope_z(z) / self.scale

    def to_dict(self):
        return {"a": self.a, "b": self.b, "c": self.c, "rmse": self.rmse,
                "k_min": self.k_min, "k_max": self.k_max, "scale": self.scale,
                "n": self.n, "atm_iv": self.atm_iv}


def _flat_smile(rows, scale, T, F):
    sw = sum(r[2] for r in rows)
    flat = sum(r[1] * r[2] for r in rows) / sw
    ks = [r[0] for r in rows]
    return Smile(flat, 0.0, 0.0, 0.0, min(ks), max(ks), scale, len(rows), T, F)


def _seed_atm(rows):
    """Vol of the strikes nearest the forward -- the anchor for z-scaling."""
    near = sorted(rows, key=lambda r: abs(r[0]))[:5]
    if not near:
        return 0.30
    sw = sum(r[2] for r in near)
    if sw <= 0:
        return sum(r[1] for r in near) / len(near)
    return sum(r[1] * r[2] for r in near) / sw


def fit_smile(points, F, T):
    """points: list of (strike, iv, weight).  Returns a Smile or None."""
    rows = []
    for K, iv, w in points:
        if not K or not iv or K <= 0 or w <= 0:
            continue
        if not (config.MIN_IV <= iv <= config.MAX_IV):
            continue
        rows.append((math.log(K / F), iv, w))
    if not rows or F <= 0 or T <= 0:
        return None

    sigma_ref = _seed_atm(rows)
    scale = max(sigma_ref * math.sqrt(T), 1e-4)
    if len(rows) < config.FIT_MIN_QUOTES:
        return _flat_smile(rows, scale, T, F)

    def solve(rs):
        S = [[0.0] * 3 for _ in range(3)]
        rhs = [0.0] * 3
        for z, iv, w in rs:
            basis = (1.0, z, z * z)
            for i in range(3):
                rhs[i] += w * basis[i] * iv
                for j in range(3):
                    S[i][j] += w * basis[i] * basis[j]
        return _solve3(S, rhs)

    a = b = c = None
    zrows = []
    # Two passes: the z-scale depends on the ATM vol, which is what we are
    # solving for.  It converges in one iteration on every chain tested.
    for _pass in range(3):
        scale = max(sigma_ref * math.sqrt(T), 1e-4)
        zrows = []
        for limit in (Z_FIT_LIMIT, 5.0, 12.0):
            zrows = [(k / scale, iv, w * math.exp(-((k / scale) ** 2) / (2 * Z_KERNEL ** 2)))
                     for k, iv, w in rows if abs(k / scale) <= limit]
            if len(zrows) >= config.FIT_MIN_QUOTES:
                break
        if len(zrows) < config.FIT_MIN_QUOTES:
            return _flat_smile(rows, scale, T, F)
        coeffs = solve(zrows)
        if coeffs is None:
            return _flat_smile(rows, scale, T, F)
        a, b, c = coeffs
        new_ref = min(max(a, config.MIN_IV), config.MAX_IV)
        if abs(new_ref - sigma_ref) < 1e-4:
            sigma_ref = new_ref
            break
        sigma_ref = 0.5 * (sigma_ref + new_ref)      # damped, avoids oscillation

    # One robust re-fit: down-weight anything more than 3 MAD off the curve.
    resid = [abs(iv - (a + b * z + c * z * z)) for z, iv, _w in zrows]
    srt = sorted(resid)
    mad = srt[len(srt) // 2] if srt else 0.0
    if mad > 1e-9:
        rows2 = [(z, iv, w / (1.0 + (r / (3.0 * mad)) ** 2))
                 for (z, iv, w), r in zip(zrows, resid)]
        c2 = solve(rows2)
        if c2 is not None:
            a, b, c = c2

    scale = max(sigma_ref * math.sqrt(T), 1e-4)
    zs = [r[0] for r in zrows]
    sw = sum(r[2] for r in zrows)
    sse = sum(w * (iv - (a + b * z + c * z * z)) ** 2 for z, iv, w in zrows)
    rmse = math.sqrt(sse / sw) if sw > 0 else 0.0
    return Smile(a, b, c, rmse, min(zs) * scale, max(zs) * scale, scale,
                 len(zrows), T, F)


def _solve3(A, b):
    """Gaussian elimination with partial pivoting for a 3x3 system."""
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for col in range(3):
        piv = max(range(col, 3), key=lambda r: abs(M[r][col]))
        if abs(M[piv][col]) < 1e-14:
            return None
        M[col], M[piv] = M[piv], M[col]
        pv = M[col][col]
        for r in range(col + 1, 3):
            f = M[r][col] / pv
            for cc in range(col, 4):
                M[r][cc] -= f * M[col][cc]
    x = [0.0] * 3
    for r in range(2, -1, -1):
        s = M[r][3] - sum(M[r][cc] * x[cc] for cc in range(r + 1, 3))
        x[r] = s / M[r][r]
    return x


def strike_for_delta(smile, target_delta, F, T, df, S, right, iters=24):
    """Smile-consistent delta->strike inversion (fixed-point on sigma)."""
    if smile is None or T <= 0:
        return None
    sigma = smile.atm_iv
    K = F
    for _ in range(iters):
        K_new = bs.strike_from_delta(target_delta, F, T, sigma, df, S, right)
        if K_new is None or K_new <= 0:
            return None
        sigma_new = smile.iv(K_new)
        if abs(K_new - K) < 1e-6 * max(F, 1.0) and abs(sigma_new - sigma) < 1e-8:
            return K_new
        K, sigma = K_new, sigma_new
    return K


def skew_metrics(smile, F, T, df, S):
    """25-delta risk reversal / butterfly, plus a normalised skew slope."""
    out = {"iv_25p": None, "iv_25c": None, "iv_atm": None,
           "skew_25": None, "rr_25": None, "fly_25": None, "slope_atm": None,
           "k_25p": None, "k_25c": None}
    if smile is None:
        return out
    atm = smile.atm_iv
    out["iv_atm"] = atm
    out["slope_atm"] = smile.slope(0.0)
    kp = strike_for_delta(smile, -0.25, F, T, df, S, "P")
    kc = strike_for_delta(smile, 0.25, F, T, df, S, "C")
    if kp and kc:
        ivp, ivc = smile.iv(kp), smile.iv(kc)
        out.update({"iv_25p": ivp, "iv_25c": ivc, "k_25p": kp, "k_25c": kc,
                    "skew_25": ivp - ivc, "rr_25": ivc - ivp,
                    "fly_25": 0.5 * (ivp + ivc) - atm})
    return out


def butterfly_arbitrage_flags(smile, F, T, df, n=41, width=2.5):
    """Check the fitted smile implies a non-negative density.

    Breeden-Litzenberger: d2C/dK2 >= 0.  A quadratic smile can violate this in
    the wings; when it does we say so rather than quietly emitting a negative
    probability into every EV calculation downstream.
    """
    if smile is None or T <= 0 or F <= 0:
        return ["no-smile"]
    flags = []
    lo = F * math.exp(max(smile.k_min, -width * smile.atm_iv * math.sqrt(T) - 0.05))
    hi = F * math.exp(min(smile.k_max, width * smile.atm_iv * math.sqrt(T) + 0.05))
    if hi <= lo:
        return ["degenerate-range"]
    h = (hi - lo) / (n - 1)
    prices = []
    for i in range(n):
        K = lo + i * h
        prices.append(bs.black76(F, K, T, smile.iv(K), df, "C"))
    # Tolerances scale with the price level: on an 800-dollar underlying the
    # ncdf round-off alone is ~1e-7, so an absolute 1e-9 threshold flags every
    # perfectly healthy chain as arbitrageable.
    tol_slope = 1e-7 * max(F, 1.0)
    tol_conv = 1e-7 * max(F, 1.0) / (h * h)
    neg = 0
    for i in range(1, n - 1):
        d2 = (prices[i + 1] - 2 * prices[i] + prices[i - 1]) / (h * h)
        if d2 < -tol_conv:
            neg += 1
    if neg:
        flags.append("butterfly:%d/%d" % (neg, n - 2))
    # Calls must be non-increasing in strike
    if any(prices[i + 1] > prices[i] + tol_slope for i in range(n - 1)):
        flags.append("call-spread")
    return flags


# ================================================== MODEL-FREE IMPLIED VOL
def model_free_iv(otm_quotes, F, T, df, atm_hint=None, max_gap=0.09):
    """VIX-style variance-swap fair vol from the OTM strip.

    Independent of any pricing model, so it is the honest answer to "what vol is
    this expiry priced at" when the smile is steep -- ATM IV understates the
    cost of the whole distribution by several points on high-skew names.

    The integral is a Riemann sum over listed strikes, so it needs a reasonably
    dense grid.  Long-dated chains list strikes in huge steps, and the sum then
    over-weights whichever wing happens to be quoted: live NVDA 2027 expiries
    returned 51% against a 41% ATM.  Sparse grids and results that disagree
    violently with the ATM vol are therefore rejected rather than published --
    they would otherwise put a fake kink in the term structure.

    otm_quotes: [(strike, right, mid)] using calls above F and puts below.
    """
    if not otm_quotes or T <= 0 or F <= 0 or df <= 0:
        return None
    by_strike = {}
    for K, right, mid in otm_quotes:
        if K <= 0 or mid is None or mid <= 0:
            continue
        if (right.upper().startswith("C") and K < F) or (right.upper().startswith("P") and K > F):
            continue
        by_strike.setdefault(K, []).append(mid)
    strikes = sorted(by_strike)
    if len(strikes) < 8:
        return None
    gaps = sorted((strikes[i + 1] - strikes[i]) / F for i in range(len(strikes) - 1))
    if gaps[len(gaps) // 2] > max_gap:
        return None
    # K0 = highest strike at or below the forward
    below = [k for k in strikes if k <= F]
    if not below:
        return None
    K0 = max(below)

    total = 0.0
    for i, K in enumerate(strikes):
        if i == 0:
            dK = strikes[1] - strikes[0]
        elif i == len(strikes) - 1:
            dK = strikes[-1] - strikes[-2]
        else:
            dK = (strikes[i + 1] - strikes[i - 1]) / 2.0
        mids = by_strike[K]
        q = sum(mids) / len(mids)          # average both rights at K0
        total += (dK / (K * K)) * q / df    # undiscount
    var = (2.0 / T) * total - (1.0 / T) * ((F / K0 - 1.0) ** 2)
    if var <= 0:
        return None
    v = math.sqrt(var)
    if not (config.MIN_IV <= v <= config.MAX_IV):
        return None
    # A model-free vol that disagrees violently with the ATM vol is a
    # discretisation artefact, not a signal -- the true gap is the skew premium,
    # which is worth a few points, never a doubling.
    if atm_hint and atm_hint > 0 and not (0.65 <= v / atm_hint <= 1.75):
        return None
    return v


# ================================================== REALISED VOL ESTIMATORS
def _ann(var, n):
    if n <= 0 or var is None or var < 0:
        return None
    return math.sqrt(var * config.TRADING_DAYS_PER_YEAR)


def close_to_close(closes, window=None):
    c = closes[-(window + 1):] if window else closes
    if len(c) < 3:
        return None
    rets = [math.log(c[i] / c[i - 1]) for i in range(1, len(c)) if c[i - 1] > 0 and c[i] > 0]
    if len(rets) < 2:
        return None
    m = sum(rets) / len(rets)
    var = sum((r - m) ** 2 for r in rets) / (len(rets) - 1)
    return _ann(var, len(rets))


def parkinson(bars, window=20):
    b = bars[-window:]
    vals = [math.log(x["high"] / x["low"]) ** 2 for x in b
            if x.get("high") and x.get("low") and x["low"] > 0]
    if len(vals) < 3:
        return None
    return _ann(sum(vals) / (4.0 * math.log(2.0) * len(vals)), len(vals))


def garman_klass(bars, window=20):
    b = bars[-window:]
    vals = []
    for x in b:
        h, l, o, c = x.get("high"), x.get("low"), x.get("open"), x.get("close")
        if not all((h, l, o, c)) or l <= 0 or o <= 0:
            continue
        vals.append(0.5 * math.log(h / l) ** 2 - (2 * math.log(2) - 1) * math.log(c / o) ** 2)
    if len(vals) < 3:
        return None
    return _ann(max(sum(vals) / len(vals), 0.0), len(vals))


def rogers_satchell(bars, window=20):
    b = bars[-window:]
    vals = []
    for x in b:
        h, l, o, c = x.get("high"), x.get("low"), x.get("open"), x.get("close")
        if not all((h, l, o, c)) or min(h, l, o, c) <= 0:
            continue
        vals.append(math.log(h / c) * math.log(h / o) + math.log(l / c) * math.log(l / o))
    if len(vals) < 3:
        return None
    return _ann(max(sum(vals) / len(vals), 0.0), len(vals))


def yang_zhang(bars, window=20):
    """Lowest-variance realised estimator: handles both gaps and intraday drift."""
    b = bars[-(window + 1):]
    if len(b) < 5:
        return None
    o_r, c_r, rs = [], [], []
    for i in range(1, len(b)):
        p, x = b[i - 1], b[i]
        h, l, o, c, pc = x.get("high"), x.get("low"), x.get("open"), x.get("close"), p.get("close")
        if not all((h, l, o, c, pc)) or min(h, l, o, c, pc) <= 0:
            continue
        o_r.append(math.log(o / pc))
        c_r.append(math.log(c / o))
        rs.append(math.log(h / c) * math.log(h / o) + math.log(l / c) * math.log(l / o))
    n = len(o_r)
    if n < 4:
        return None
    mo = sum(o_r) / n
    mc = sum(c_r) / n
    vo = sum((x - mo) ** 2 for x in o_r) / (n - 1)
    vc = sum((x - mc) ** 2 for x in c_r) / (n - 1)
    vrs = sum(rs) / n
    k = 0.34 / (1.34 + (n + 1) / (n - 1))
    var = vo + k * vc + (1 - k) * vrs
    return _ann(max(var, 0.0), n)


def bipower(closes, window=20):
    """Jump-robust realised vol (bipower variation).

    Close-to-close vol cannot tell a 9% earnings gap apart from nine days of
    3% chop, so a single print keeps trailing HV elevated for a month and makes
    the *next* month's options look cheap.  Live AAPL showed exactly this: 30.3%
    trailing HV against 22.7% implied, which scored a long strangle at +55% EV
    on risk purely because of one gap already in the past.

    Bipower variation multiplies adjacent absolute returns, so an isolated jump
    contributes to only two terms and its effect vanishes asymptotically.  This
    is the *diffusive* vol; expected jumps are added back explicitly by the
    caller when an event actually falls inside the window.
    """
    c = closes[-(window + 1):]
    if len(c) < 6:
        return None
    rets = [math.log(c[i] / c[i - 1]) for i in range(1, len(c)) if c[i - 1] > 0 and c[i] > 0]
    if len(rets) < 5:
        return None
    bv = sum(abs(rets[i]) * abs(rets[i - 1]) for i in range(1, len(rets)))
    bv *= (math.pi / 2.0) * (len(rets) / max(len(rets) - 1.0, 1.0))
    daily_var = bv / max(len(rets) - 1, 1)
    return _ann(daily_var, len(rets))


def trimmed_cc(closes, window=20, k=3.0):
    """Close-to-close vol with returns winsorised at k median-absolute-deviations."""
    c = closes[-(window + 1):]
    if len(c) < 6:
        return None
    rets = [math.log(c[i] / c[i - 1]) for i in range(1, len(c)) if c[i - 1] > 0 and c[i] > 0]
    if len(rets) < 5:
        return None
    absr = sorted(abs(r) for r in rets)
    mad = absr[len(absr) // 2] or 1e-6
    cap = k * mad
    w = [max(-cap, min(cap, r)) for r in rets]
    m = sum(w) / len(w)
    var = sum((r - m) ** 2 for r in w) / (len(w) - 1)
    return _ann(var, len(w))


def jump_share(closes, window=20):
    """Fraction of recent variance attributable to jumps, in [0, 1).

    Reported so the UI can say *why* a vol forecast differs from naive HV.
    """
    cc = close_to_close(closes, window)
    bp = bipower(closes, window)
    if not cc or not bp or cc <= 0:
        return None
    return max(0.0, 1.0 - (bp * bp) / (cc * cc))


def ewma_vol(closes, lam=None, winsor=3.0):
    """Exponentially weighted vol, with returns winsorised by default.

    An un-winsorised EWMA is the *worst* estimator to have in a jump-robust
    blend: it squares the outlier and then weights it most heavily because it
    is recent.  On live AAPL the raw EWMA read 33.3% against a 22% intraday
    estimate purely on one earnings gap.
    """
    lam = lam or config.EWMA_LAMBDA
    if len(closes) < 10:
        return None
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))
            if closes[i - 1] > 0 and closes[i] > 0]
    if len(rets) < 10:
        return None
    if winsor:
        absr = sorted(abs(r) for r in rets)
        mad = absr[len(absr) // 2] or 1e-6
        cap = winsor * mad
        rets = [max(-cap, min(cap, r)) for r in rets]
    var = sum(r * r for r in rets[:20]) / 20.0
    for r in rets[20:]:
        var = lam * var + (1 - lam) * r * r
    return _ann(var, len(rets))


def all_realised(bars):
    closes = [b["close"] for b in bars if b.get("close")]
    out = {}
    for w in config.HV_WINDOWS:
        out["hv%d" % w] = close_to_close(closes, w) if len(closes) > w else None
    out["yz20"] = yang_zhang(bars, 20)
    out["yz60"] = yang_zhang(bars, 60)
    out["gk20"] = garman_klass(bars, 20)
    out["park20"] = parkinson(bars, 20)
    out["rs20"] = rogers_satchell(bars, 20)
    out["ewma"] = ewma_vol(closes)
    out["cc252"] = close_to_close(closes, 252)
    out["bp20"] = bipower(closes, 20)
    out["bp60"] = bipower(closes, 60)
    out["trim20"] = trimmed_cc(closes, 20)
    out["trim60"] = trimmed_cc(closes, 60)
    out["trim252"] = trimmed_cc(closes, 252)
    out["ewma_raw"] = ewma_vol(closes, winsor=0)
    out["jump_share"] = jump_share(closes, 60)
    return out


def forecast_vol(realised, horizon_days=30):
    """Diffusive vol forecast: jump-robust blend, mean-reverted to the 1y anchor.

    This is the vol expected in a *quiet* window.  Scheduled events (earnings)
    are added back as an explicit jump by the caller, using the market's own
    implied jump from the term-structure kink -- which is both more accurate
    and more honest than letting a stale historical gap stand in for a future
    one.

    The 1y anchor is deliberately the *jump-robust* long-run level too, so a
    single crash a year ago does not permanently inflate every forecast.
    """
    w = config.VOL_FORECAST_WEIGHTS
    num = den = 0.0
    for key, weight in w.items():
        v = realised.get(key)
        if v and v > 0:
            num += weight * v
            den += weight
    if den <= 0:
        v = realised.get("trim20") or realised.get("bp20") or realised.get("hv20")
        return v
    short = num / den
    # The long-run anchor is winsorised too: one crash a year ago should not
    # permanently inflate every forecast through the mean-reversion term.
    anchor = (realised.get("trim252") or realised.get("bp60")
              or realised.get("cc252") or realised.get("hv252") or short)
    if not anchor or anchor <= 0:
        return short
    pull = config.VOL_MEAN_REVERSION_30D * min(horizon_days / 30.0, 2.0)
    pull = min(max(pull, 0.0), 0.85)
    blended = (1 - pull) * short + pull * anchor
    return calibrate(blended)


def calibrate(fc):
    """Correct the blend's measured downward bias.  See config for the study.

    Applied at the single point every consumer goes through, so vol_edge, the
    P-density width, POP, CVaR and every target probability all move together
    rather than drifting out of agreement.
    """
    if not fc or fc <= 0 or not getattr(config, "VOL_CALIBRATION", False):
        return fc
    out = math.exp(config.VOL_CALIB_A + config.VOL_CALIB_B * math.log(fc))
    return min(max(out, config.MIN_IV), config.MAX_IV)


def add_event_jump(diffusive, jump_move, t_years):
    """Total vol over a window that contains one jump of size `jump_move`.

    Variance is additive: sigma_total^2 * T = sigma_diffusive^2 * T + jump^2.
    """
    if not diffusive or not t_years or t_years <= 0:
        return diffusive
    if not jump_move or jump_move <= 0:
        return diffusive
    var = diffusive * diffusive + (jump_move * jump_move) / t_years
    return math.sqrt(max(var, 1e-8))


def implied_event_move(per_expiry, event_date):
    """Recover the market's priced jump from the term-structure kink.

    With a diffusive rate v shared by both expiries,
        sigma_before^2 * T_before = v * T_before
        sigma_after^2  * T_after  = v * T_after + J^2
    so J = sqrt(sigma_after^2*T_after - sigma_before^2*T_before), where the
    'before' expiry sets v.  This is what the options market actually expects
    the print to be worth, expressed as a fraction of spot.
    """
    if not event_date or not per_expiry:
        return None
    before = [m for m in per_expiry
              if m.get("expiry") and m["expiry"] < event_date.isoformat()
              and (m.get("mfiv") or m.get("atm_iv")) and (m.get("dte") or 0) >= 2]
    after = [m for m in per_expiry
             if m.get("expiry") and m["expiry"] >= event_date.isoformat()
             and (m.get("mfiv") or m.get("atm_iv"))]
    if not before or not after:
        return None
    b = max(before, key=lambda m: m["dte"])
    a = min(after, key=lambda m: m["dte"])
    sb = b.get("mfiv") or b["atm_iv"]
    sa = a.get("mfiv") or a["atm_iv"]
    tb, ta = b["t_years"], a["t_years"]
    if ta <= tb:
        return None
    jump_var = sa * sa * ta - sb * sb * tb
    if jump_var <= 0:
        return 0.0
    j = math.sqrt(jump_var)
    return j if 0.0 <= j < 0.60 else None


# ============================================================ RANK / PERCENTILE
def rank_and_percentile(current, history, min_samples=3):
    """IV rank = position in the [min,max] range; IV percentile = % of days below.

    Both are reported because they disagree in exactly the situations that
    matter: after one vol spike, rank collapses toward 0 while percentile can
    still read 80.  Rank is the popular one; percentile is the robust one.

    `min_samples` guards against publishing a confident-looking rank off a
    handful of observations.  With four sessions on record, "rank 100" only
    means today was the highest of four -- roughly a coin flip -- yet it reads
    like a screaming vol extreme.  Callers with a long history (realised vol,
    which has two years) leave the default; IV rank passes the real threshold.
    """
    vals = [v for v in history if v is not None and v > 0]
    if current is None or current <= 0 or len(vals) < max(min_samples, 3):
        return {"rank": None, "pct": None, "n": len(vals),
                "min": min(vals) if vals else None, "max": max(vals) if vals else None}
    lo, hi = min(vals), max(vals)
    rank = 100.0 * (current - lo) / (hi - lo) if hi > lo else 50.0
    pct = 100.0 * sum(1 for v in vals if v < current) / len(vals)
    return {"rank": min(max(rank, 0.0), 100.0), "pct": pct, "n": len(vals),
            "min": lo, "max": hi}


def term_structure_slope(points):
    """Regression slope of IV on sqrt(T) across expiries.

    Negative slope = backwardation = an event is priced into the front.  Using
    sqrt(T) rather than T linearises the typical term shape so the number is
    comparable across names.
    """
    pts = [(math.sqrt(t), iv) for t, iv in points if t and t > 0 and iv and iv > 0]
    if len(pts) < 3:
        return None
    n = len(pts)
    mx = sum(p[0] for p in pts) / n
    my = sum(p[1] for p in pts) / n
    sxx = sum((p[0] - mx) ** 2 for p in pts)
    if sxx < 1e-12:
        return None
    return sum((p[0] - mx) * (p[1] - my) for p in pts) / sxx


def interpolate_iv_at_days(points, target_days):
    """Constant-total-variance interpolation to a fixed tenor (30/60/90d).

    Interpolating IV linearly in time is wrong -- variance is what is additive.
    """
    pts = sorted((t, iv) for t, iv in points if t and t > 0 and iv and iv > 0)
    if not pts:
        return None
    target_t = target_days / 365.0
    if len(pts) == 1 or target_t <= pts[0][0]:
        return pts[0][1]
    if target_t >= pts[-1][0]:
        return pts[-1][1]
    for i in range(1, len(pts)):
        t0, v0 = pts[i - 1]
        t1, v1 = pts[i]
        if target_t <= t1:
            w0, w1 = v0 * v0 * t0, v1 * v1 * t1
            w = (target_t - t0) / (t1 - t0)
            var = w0 + w * (w1 - w0)
            return math.sqrt(max(var, 1e-8) / target_t)
    return pts[-1][1]
