"""Black-Scholes / Black-76 pricing, greeks, and implied-volatility inversion.

Design choices that matter for accuracy
---------------------------------------
1. Everything is priced off the **forward** F and the **discount factor** df,
   not off spot + a guessed dividend yield.  F and df are recovered per expiry
   from put-call parity (see model.parity), so the model is automatically
   consistent with whatever the market is actually pricing for dividends,
   borrow cost and rates.  A hard-coded q of 0 systematically biases call IVs
   up and put IVs down on dividend payers, which then poisons skew metrics.

2. IV inversion is Newton-on-log-vol with a Brent-style bracketed fallback.
   Log-vol keeps the iterate positive; the bracket guarantees convergence for
   deep wings where vega is ~0 and pure Newton diverges.

3. Greeks are analytic (no bumping) and returned in trader units:
   vega per 1 vol point, theta per calendar day, rho per 1% rate move.
"""

import math

SQRT_2PI = math.sqrt(2.0 * math.pi)
SQRT_2 = math.sqrt(2.0)
EPS = 1e-12


# --------------------------------------------------------------- normal dist
def npdf(x):
    if x < -40.0 or x > 40.0:
        return 0.0
    return math.exp(-0.5 * x * x) / SQRT_2PI


def ncdf(x):
    """Standard normal CDF via erfc -- accurate to ~1e-16 in both tails."""
    return 0.5 * math.erfc(-x / SQRT_2)


def nppf(p):
    """Inverse normal CDF (Acklam), refined once with Halley's method."""
    if p <= 0.0:
        return -40.0
    if p >= 1.0:
        return 40.0
    a = (-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00)
    b = (-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01)
    c = (-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00)
    d = (7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00)
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        x = (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    elif p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        x = -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    else:
        q = p - 0.5
        r = q * q
        x = (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)
    e = ncdf(x) - p
    u = e * SQRT_2PI * math.exp(x * x / 2.0)
    return x - u / (1.0 + x * u / 2.0)


# ------------------------------------------------------------------ Black-76
def d1d2(F, K, T, sigma):
    if F <= 0 or K <= 0 or T <= 0 or sigma <= 0:
        return None, None
    v = sigma * math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * v * v) / v
    return d1, d1 - v


def black76(F, K, T, sigma, df, right):
    """Undiscounted-forward Black price, discounted by df."""
    call = right.upper().startswith("C")
    if T <= 0 or sigma <= 0:
        return df * max(F - K, 0.0) if call else df * max(K - F, 0.0)
    d1, d2 = d1d2(F, K, T, sigma)
    if call:
        return df * (F * ncdf(d1) - K * ncdf(d2))
    return df * (K * ncdf(-d2) - F * ncdf(-d1))


def intrinsic(S, K, right):
    r = right.upper()[:1]
    if r == "S":          # a share of the underlying: worth spot at any date
        return S
    return max(S - K, 0.0) if r == "C" else max(K - S, 0.0)


def bounds(F, K, T, df, right):
    """No-arbitrage price bounds; quotes outside are unusable for IV."""
    if right.upper().startswith("C"):
        return df * max(F - K, 0.0), df * F
    return df * max(K - F, 0.0), df * K


# -------------------------------------------------------------------- greeks
def greeks(S, F, K, T, sigma, df, right, per_day=True):
    """Full analytic greek set in trader units.

    r and q are *implied* from (df, F, S) so the greeks stay consistent with the
    same forward the IV was solved against.
    """
    call = right.upper().startswith("C")
    out = {"delta": 0.0, "gamma": 0.0, "vega": 0.0, "theta": 0.0, "rho": 0.0,
           "vanna": 0.0, "vomma": 0.0, "charm": 0.0, "d1": None, "d2": None}
    if S <= 0 or K <= 0 or T <= EPS or sigma <= EPS or F <= 0 or df <= 0:
        if T <= EPS:
            out["delta"] = (1.0 if S > K else 0.0) if call else (-1.0 if S < K else 0.0)
        return out

    r = -math.log(df) / T
    q = r - math.log(F / S) / T
    sqT = math.sqrt(T)
    d1, d2 = d1d2(F, K, T, sigma)
    pdf1 = npdf(d1)
    disc_q = math.exp(-q * T)

    if call:
        delta = disc_q * ncdf(d1)
        rho = K * T * df * ncdf(d2) / 100.0
        theta = (-(S * disc_q * pdf1 * sigma) / (2 * sqT)
                 - r * K * df * ncdf(d2) + q * S * disc_q * ncdf(d1))
        charm = -disc_q * (q * ncdf(d1) - pdf1 * (2 * (r - q) * T - d2 * sigma * sqT)
                           / (2 * T * sigma * sqT))
    else:
        delta = -disc_q * ncdf(-d1)
        rho = -K * T * df * ncdf(-d2) / 100.0
        theta = (-(S * disc_q * pdf1 * sigma) / (2 * sqT)
                 + r * K * df * ncdf(-d2) - q * S * disc_q * ncdf(-d1))
        charm = disc_q * (q * ncdf(-d1) + pdf1 * (2 * (r - q) * T - d2 * sigma * sqT)
                          / (2 * T * sigma * sqT))

    gamma = disc_q * pdf1 / (S * sigma * sqT)
    vega = S * disc_q * pdf1 * sqT            # per 1.00 of vol
    vanna = -disc_q * pdf1 * d2 / sigma       # dVega/dSpot
    vomma = vega * d1 * d2 / sigma            # dVega/dVol

    out.update({
        "delta": delta,
        "gamma": gamma,
        "vega": vega / 100.0,                 # per 1 vol point
        "theta": theta / 365.0 if per_day else theta,
        "rho": rho,
        "vanna": vanna / 100.0,
        "vomma": vomma / 10000.0,
        "charm": charm / 365.0 if per_day else charm,
        "d1": d1, "d2": d2,
        "r": r, "q": q,
    })
    return out


def vega_raw(F, K, T, sigma, df):
    """dPrice/dSigma for 1.00 of vol, in forward measure (used by the solver)."""
    d1, _ = d1d2(F, K, T, sigma)
    if d1 is None:
        return 0.0
    return df * F * npdf(d1) * math.sqrt(T)


# -------------------------------------------------------------- IV inversion
def _initial_guess(F, K, T, df, price, call):
    """Brenner-Subrahmanyam ATM seed, widened for OTM strikes."""
    fwd_price = price / max(df, EPS)
    atm = SQRT_2PI * fwd_price / (F * math.sqrt(T)) if F > 0 and T > 0 else 0.3
    k = abs(math.log(F / K)) if F > 0 and K > 0 else 0.0
    seed = math.sqrt(max(atm * atm + 2.0 * k / max(T, EPS) * 0.25, 1e-4))
    return min(max(seed, 0.02), 4.0)


def implied_vol(price, F, K, T, df, right, lo=1e-4, hi=6.0, tol=1e-8, max_iter=64):
    """Solve Black-76 for sigma.  Returns None if the quote is unusable."""
    if price is None or price <= 0 or T <= EPS or F <= 0 or K <= 0 or df <= 0:
        return None
    call = right.upper().startswith("C")
    lo_b, hi_b = bounds(F, K, T, df, right)
    if price <= lo_b + 1e-10 or price >= hi_b - 1e-10:
        return None

    sigma = _initial_guess(F, K, T, df, price, call)
    x = math.log(sigma)                      # solve in log space: sigma > 0 always
    for _ in range(32):
        s = math.exp(x)
        diff = black76(F, K, T, s, df, right) - price
        v = vega_raw(F, K, T, s, df) * s     # d(price)/d(log sigma)
        if v < 1e-10:
            break
        step = diff / v
        step = max(min(step, 1.0), -1.0)     # damp: wings can overshoot wildly
        x -= step
        if x < math.log(lo) or x > math.log(hi):
            break
        # Converge on the *volatility* step, not the price residual.  In the
        # deep wings vega is tiny, so a price residual of 1e-8 can still be
        # 4e-4 of vol -- enough to shift a wing IV by 0.04 points and visibly
        # tilt the fitted skew.
        if abs(step) < 1e-12:
            s = math.exp(x)
            return s if lo <= s <= hi else None

    # Bracketed bisection fallback -- monotone in sigma, so this always works
    a, b = lo, hi
    fa = black76(F, K, T, a, df, right) - price
    fb = black76(F, K, T, b, df, right) - price
    if fa * fb > 0:
        return None
    for _ in range(max_iter):
        m = 0.5 * (a + b)
        # Bracket width, not price residual: a deep wing option's price is
        # nearly flat in vol, so |price error| < 1e-8 is satisfied across a
        # band of vol thousands of times wider than that.
        if (b - a) < 1e-11:
            return m
        fm = black76(F, K, T, m, df, right) - price
        if fa * fm < 0:
            b, fb = m, fm
        else:
            a, fa = m, fm
    return 0.5 * (a + b)


def strike_from_delta(target_delta, F, T, sigma, df, S, right):
    """Invert delta -> strike at a flat sigma (smile-aware version in vol.py)."""
    call = right.upper().startswith("C")
    if T <= 0 or sigma <= 0 or F <= 0:
        return None
    r = -math.log(df) / T if df > 0 else 0.0
    q = r - math.log(F / S) / T if S > 0 else 0.0
    disc_q = math.exp(-q * T)
    p = abs(target_delta) / disc_q
    p = min(max(p, 1e-6), 1 - 1e-6)
    d1 = nppf(p) if call else -nppf(p)
    v = sigma * math.sqrt(T)
    return F * math.exp(-d1 * v + 0.5 * v * v)


# ------------------------------------------------------- probability toolkit
def prob_itm(F, K, T, sigma, right):
    """Risk-neutral P(finish ITM) = N(d2) for a call."""
    d1, d2 = d1d2(F, K, T, sigma)
    if d2 is None:
        return 1.0 if intrinsic(F, K, right) > 0 else 0.0
    return ncdf(d2) if right.upper().startswith("C") else ncdf(-d2)


def prob_touch(S, barrier, sigma, T, drift=0.0):
    """P(the underlying trades through `barrier` before T) under GBM.

    First-passage probability -- the correct version of the "2x P(ITM)" rule of
    thumb, which is only exact for zero drift and at-the-money barriers.
    """
    if S <= 0 or barrier <= 0 or sigma <= 0 or T <= 0:
        return 0.0
    b = math.log(barrier / S)
    nu = drift - 0.5 * sigma * sigma
    v = sigma * math.sqrt(T)
    if abs(b) < 1e-12:
        return 1.0
    try:
        if barrier > S:
            p = ncdf((-b + nu * T) / v) + math.exp(2 * nu * b / (sigma * sigma)) * ncdf((-b - nu * T) / v)
        else:
            p = ncdf((b - nu * T) / v) + math.exp(2 * nu * b / (sigma * sigma)) * ncdf((b + nu * T) / v)
    except OverflowError:
        return 1.0
    return min(max(p, 0.0), 1.0)


def _thomas(a, b, c, d):
    """Tridiagonal solve (Thomas algorithm).  a=sub, b=diag, c=super, d=rhs."""
    n = len(b)
    cp = [0.0] * n
    dp = [0.0] * n
    cp[0] = c[0] / b[0]
    dp[0] = d[0] / b[0]
    for i in range(1, n):
        m = b[i] - a[i] * cp[i - 1]
        cp[i] = c[i] / m if i < n - 1 else 0.0
        dp[i] = (d[i] - a[i] * dp[i - 1]) / m
    x = [0.0] * n
    x[-1] = dp[-1]
    for i in range(n - 2, -1, -1):
        x[i] = dp[i] - cp[i] * x[i + 1]
    return x


def prob_no_touch_double(S, lo, hi, sigma, T, drift=0.0, nodes=641, steps=150):
    """P(spot stays strictly inside (lo, hi) for the whole period).

    Solves the backward Kolmogorov equation with absorbing boundaries

        du/dtau = nu*du/dx + (sigma^2/2)*d2u/dx2,   u(boundary)=0,  u(x,0)=1

    using **backward Euler** with a tridiagonal solve.  An explicit scheme was
    tried first and was a trap: keeping it stable needs dt <= 0.4*dx^2/sigma^2,
    which for realistic barriers is tens of thousands of steps.  Capping the
    steps to stay fast silently violated the stability condition and returned
    garbage -- P(touch) came out as 20%, 60%, 20%, 99% for monotonically
    widening barriers.  Backward Euler is unconditionally stable, so the result
    is monotone by construction.

    Convergence is dominated by the *space* step, not the time step: measured
    against the single-barrier analytic, error falls from 4.1pp (161 nodes) to
    0.5pp (321) to 0.15pp (641) while raising the step count from 180 to 600
    changes nothing.  Hence a wide grid and few steps.  tests.py asserts the
    degenerate single-barrier case matches the closed form.

    Needed because straddles and condors have two-sided targets, where
    P(touch either) is emphatically not the sum of the one-sided probabilities.
    """
    if sigma <= 0 or T <= 0 or S <= 0:
        return 1.0 if lo < S < hi else 0.0
    if not (lo < S < hi):
        return 0.0
    lo = max(lo, 1e-12)
    x0, xl, xh = math.log(S), math.log(lo), math.log(hi)
    if xh - xl < 1e-9:
        return 0.0
    dx = (xh - xl) / (nodes - 1)
    nu = drift - 0.5 * sigma * sigma
    dt = T / steps
    alpha = 0.5 * sigma * sigma * dt / (dx * dx)
    beta = 0.5 * nu * dt / dx

    n = nodes - 2                                   # interior unknowns only
    if n < 1:
        return 0.0
    sub = [-(alpha - beta)] * n
    diag = [1.0 + 2.0 * alpha] * n
    sup = [-(alpha + beta)] * n
    sub[0] = 0.0
    sup[-1] = 0.0

    u = [1.0] * n
    for _ in range(steps):
        u = _thomas(sub, diag, sup, u)              # boundaries contribute 0

    pos = (x0 - xl) / dx - 1.0                      # index into the interior
    if pos <= 0:
        return max(0.0, min(1.0, u[0] * max(pos + 1.0, 0.0)))
    if pos >= n - 1:
        return max(0.0, min(1.0, u[-1] * max(n - pos, 0.0)))
    i = int(pos)
    w = pos - i
    return max(0.0, min(1.0, u[i] * (1 - w) + u[i + 1] * w))


def prob_touch_either(S, lo, hi, sigma, T, drift=0.0):
    return 1.0 - prob_no_touch_double(S, lo, hi, sigma, T, drift)


def prob_between(S, lo, hi, sigma, T, drift=0.0):
    """P(S_T lands inside [lo, hi]) -- terminal, not path-dependent."""
    if sigma <= 0 or T <= 0:
        return 1.0 if lo <= S <= hi else 0.0
    v = sigma * math.sqrt(T)
    mu = math.log(S) + (drift - 0.5 * sigma * sigma) * T
    z_hi = (math.log(hi) - mu) / v if hi > 0 else 40.0
    z_lo = (math.log(lo) - mu) / v if lo > 0 else -40.0
    return max(ncdf(z_hi) - ncdf(z_lo), 0.0)


def lognormal_quantile(S, sigma, T, drift, p):
    """Price at percentile p of the terminal distribution."""
    if sigma <= 0 or T <= 0:
        return S
    return S * math.exp((drift - 0.5 * sigma * sigma) * T + sigma * math.sqrt(T) * nppf(p))


def expected_move(S, sigma, T, n_sigma=1.0):
    """+/- n-sigma range in price terms (lognormal, not the linear shortcut)."""
    if sigma <= 0 or T <= 0:
        return S, S, 0.0
    v = sigma * math.sqrt(T)
    up = S * math.exp(n_sigma * v - 0.5 * v * v)
    dn = S * math.exp(-n_sigma * v - 0.5 * v * v)
    return dn, up, (up - dn) / 2.0


# -------------------------------------------------------------- convenience
def price_from_iv(S, K, T, sigma, r, q, right):
    """Classic spot-parameterised BSM, for when F/df are not available."""
    if T <= 0 or sigma <= 0:
        return intrinsic(S, K, right)
    F = S * math.exp((r - q) * T)
    return black76(F, K, T, sigma, math.exp(-r * T), right)
