"""Invariant harness for Theta Desk.  Run it after ANY model change.

    python tests.py            # model invariants (no network)
    python tests.py --live     # also exercise the live data adapters
    python tests.py --api      # also hit a running server on config.PORT

The point of these is not coverage, it is *anchoring*: each one pins a number
that can be derived two ways, so a model change that breaks the relationship
fails loudly instead of quietly shifting every result by a few per cent.
"""

import datetime as dt
import math
import os
import sys
import urllib.error

import config
import db
import marketcal
from model import bs, chain as chainmod, parity, scanner, sentiment, strategies, tech, vol

PASS, FAIL = 0, 0
FAILURES = []


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok   %s" % name)
    else:
        FAIL += 1
        FAILURES.append("%s %s" % (name, detail))
        print("  FAIL %s   %s" % (name, detail))


def close(a, b, tol=1e-6):
    if a is None or b is None:
        return False
    return abs(a - b) <= tol * max(1.0, abs(a), abs(b))


def section(t):
    print("\n== %s" % t)


# ===================================================================== normal
def test_normal():
    section("normal distribution")
    check("ncdf(0)=0.5", close(bs.ncdf(0), 0.5, 1e-12))
    check("ncdf(1.96)~0.975", close(bs.ncdf(1.959963985), 0.975, 1e-8))
    check("ncdf symmetric", close(bs.ncdf(-1.3) + bs.ncdf(1.3), 1.0, 1e-12))
    check("nppf inverts ncdf", all(close(bs.nppf(bs.ncdf(x)), x, 1e-6)
                                   for x in (-3.0, -1.0, 0.0, 0.5, 2.5)))
    check("npdf integrates ~1", close(sum(bs.npdf(-8 + i * 0.001) * 0.001
                                          for i in range(16001)), 1.0, 1e-6))


# ================================================================== pricing
def test_pricing():
    section("Black-76 pricing and parity")
    F, K, T, sig, df = 100.0, 100.0, 0.5, 0.25, math.exp(-0.04 * 0.5)
    c = bs.black76(F, K, T, sig, df, "C")
    p = bs.black76(F, K, T, sig, df, "P")
    check("ATM call = ATM put when F=K", close(c, p, 1e-10), "c=%.8f p=%.8f" % (c, p))
    # put-call parity: C - P = df*(F - K)
    for k in (80.0, 95.0, 100.0, 130.0):
        c = bs.black76(F, k, T, sig, df, "C")
        p = bs.black76(F, k, T, sig, df, "P")
        check("parity at K=%g" % k, close(c - p, df * (F - k), 1e-10),
              "%.10f vs %.10f" % (c - p, df * (F - k)))
    check("price rises with vol", bs.black76(F, K, T, 0.4, df, "C") > bs.black76(F, K, T, 0.2, df, "C"))
    check("price rises with T", bs.black76(F, K, 1.0, sig, df, "C") > bs.black76(F, K, 0.25, sig, df, "C"))
    lo, hi = bs.bounds(F, 80.0, T, df, "C")
    v = bs.black76(F, 80.0, T, sig, df, "C")
    check("call inside no-arb bounds", lo < v < hi)
    check("T=0 call is intrinsic", close(bs.black76(F, 90.0, 0.0, sig, 1.0, "C"), 10.0, 1e-12))


def test_iv():
    section("implied volatility inversion")
    F, T, df = 100.0, 0.4, math.exp(-0.045 * 0.4)
    worst = 0.0
    n = 0
    for K in (55, 70, 85, 95, 100, 105, 115, 130, 160):
        for sig in (0.08, 0.18, 0.35, 0.9, 2.0):
            for right in ("C", "P"):
                px = bs.black76(F, K, T, sig, df, right)
                if px < 1e-8:
                    continue
                back = bs.implied_vol(px, F, K, T, df, right)
                if back is None:
                    continue
                n += 1
                worst = max(worst, abs(back - sig))
    check("IV round-trips on %d cases" % n, worst < 1e-6, "worst error %.2e" % worst)
    check("IV rejects a price below intrinsic",
          bs.implied_vol(df * (F - 80.0) - 0.5, F, 80.0, T, df, "C") is None)
    check("IV rejects a price above the forward",
          bs.implied_vol(df * F * 1.01, F, 80.0, T, df, "C") is None)


def test_greeks():
    section("greeks against finite differences")
    S, K, T, sig, r, q = 100.0, 105.0, 0.6, 0.28, 0.045, 0.015
    F = S * math.exp((r - q) * T)
    df = math.exp(-r * T)
    for right in ("C", "P"):
        g = bs.greeks(S, F, K, T, sig, df, right)
        h = 1e-4
        def price(s=S, t=T, v=sig, rr=r):
            ff = s * math.exp((rr - q) * t)
            return bs.black76(ff, K, t, v, math.exp(-rr * t), right)
        fd_delta = (price(s=S + h) - price(s=S - h)) / (2 * h)
        fd_gamma = (price(s=S + h) - 2 * price() + price(s=S - h)) / (h * h)
        fd_vega = (price(v=sig + 1e-5) - price(v=sig - 1e-5)) / (2e-5) / 100.0
        fd_theta = -(price(t=T + 1e-5) - price(t=T - 1e-5)) / (2e-5) / 365.0
        fd_rho = (price(rr=r + 1e-6) - price(rr=r - 1e-6)) / (2e-6) / 100.0
        check("%s delta" % right, close(g["delta"], fd_delta, 1e-5), "%.8f vs %.8f" % (g["delta"], fd_delta))
        check("%s gamma" % right, close(g["gamma"], fd_gamma, 1e-3), "%.8f vs %.8f" % (g["gamma"], fd_gamma))
        check("%s vega" % right, close(g["vega"], fd_vega, 1e-5), "%.8f vs %.8f" % (g["vega"], fd_vega))
        check("%s theta" % right, close(g["theta"], fd_theta, 1e-4), "%.8f vs %.8f" % (g["theta"], fd_theta))
        check("%s rho" % right, close(g["rho"], fd_rho, 1e-4), "%.8f vs %.8f" % (g["rho"], fd_rho))
    gc = bs.greeks(S, F, K, T, sig, df, "C")
    gp = bs.greeks(S, F, K, T, sig, df, "P")
    check("call delta - put delta = e^-qT",
          close(gc["delta"] - gp["delta"], math.exp(-q * T), 1e-9))
    check("call and put share gamma", close(gc["gamma"], gp["gamma"], 1e-12))
    check("call and put share vega", close(gc["vega"], gp["vega"], 1e-12))
    check("greeks recover r and q", close(gc["r"], r, 1e-9) and close(gc["q"], q, 1e-9))


def test_probability():
    section("probability toolkit")
    S, sig, T = 100.0, 0.3, 0.25
    check("prob_itm at the money ~0.5 (less half-variance drift)",
          0.45 < bs.prob_itm(S, S, T, sig, "C") < 0.5)
    check("prob_itm call + put = 1", close(bs.prob_itm(S, 110.0, T, sig, "C") +
                                           bs.prob_itm(S, 110.0, T, sig, "P"), 1.0, 1e-12))
    check("prob_touch > prob_itm for the same barrier",
          bs.prob_touch(S, 110.0, sig, T) > bs.prob_itm(S, 110.0, T, sig, "C"))
    check("prob_touch monotone in barrier",
          bs.prob_touch(S, 105.0, sig, T) > bs.prob_touch(S, 115.0, sig, T) >
          bs.prob_touch(S, 140.0, sig, T))
    check("prob_touch at spot = 1", close(bs.prob_touch(S, S, sig, T), 1.0, 1e-9))

    # The lattice must reproduce the closed form when one barrier is far away.
    for hi in (105.0, 110.0, 120.0, 140.0):
        lat = bs.prob_no_touch_double(S, 1e-6, hi, sig, T, 0.0)
        ana = 1.0 - bs.prob_touch(S, hi, sig, T, 0.0)
        check("lattice matches analytic upper barrier %g" % hi, abs(lat - ana) < 0.004,
              "%.5f vs %.5f" % (lat, ana))
    for lo in (95.0, 90.0, 80.0):
        lat = bs.prob_no_touch_double(S, lo, 1e9, sig, T, 0.0)
        ana = 1.0 - bs.prob_touch(S, lo, sig, T, 0.0)
        check("lattice matches analytic lower barrier %g" % lo, abs(lat - ana) < 0.004,
              "%.5f vs %.5f" % (lat, ana))
    # Monotone in width -- this is the check the unstable explicit scheme failed.
    prev = 1.1
    ok = True
    for w in (0.02, 0.04, 0.06, 0.08, 0.10, 0.15, 0.25):
        p = bs.prob_touch_either(S, S * (1 - w), S * (1 + w), sig, T, 0.0)
        if p > prev + 1e-9:
            ok = False
        prev = p
    check("P(touch either) falls as barriers widen", ok)
    check("touch-either exceeds each one-sided probability",
          bs.prob_touch_either(S, 92.0, 108.0, sig, T) >
          max(bs.prob_touch(S, 108.0, sig, T), bs.prob_touch(S, 92.0, sig, T)))
    check("touch-either below the sum of the two",
          bs.prob_touch_either(S, 92.0, 108.0, sig, T) <
          bs.prob_touch(S, 108.0, sig, T) + bs.prob_touch(S, 92.0, sig, T))


# ================================================================ volatility
def test_vol_estimators():
    section("realised volatility estimators")
    import random
    random.seed(7)
    sig_true = 0.32
    daily = sig_true / math.sqrt(252)
    bars, px = [], 100.0
    for i in range(600):
        o = px
        r = random.gauss(0, daily)
        c = o * math.exp(r)
        hi = max(o, c) * math.exp(abs(random.gauss(0, daily * 0.5)))
        lo = min(o, c) * math.exp(-abs(random.gauss(0, daily * 0.5)))
        bars.append({"date": "d%03d" % i, "open": o, "high": hi, "low": lo,
                     "close": c, "volume": 1e6})
        px = c
    closes = [b["close"] for b in bars]
    for name, v in (("close-to-close", vol.close_to_close(closes, 252)),
                    ("yang-zhang", vol.yang_zhang(bars, 252)),
                    ("bipower", vol.bipower(closes, 252)),
                    ("winsorised", vol.trimmed_cc(closes, 252)),
                    ("ewma", vol.ewma_vol(closes))):
        check("%s recovers 32%% vol" % name, v is not None and abs(v - sig_true) < 0.06,
              "got %s" % (round(v, 4) if v else None))

    # Inject ONE controlled gap and confirm the jump-robust estimators resist it.
    # The shock is computed so the return at the splice is exactly ln(0.88),
    # whatever the random series already did that day -- naively scaling the tail
    # by 0.90 silently *shrinks* the move when the underlying day was already up
    # (index 590 here was +6.1%, so a 0.9x scale made it -4.4% and lowered vol).
    i0 = len(bars) - 10
    shock = 0.88 / (bars[i0]["close"] / bars[i0 - 1]["close"])
    jumped = [dict(b) for b in bars]
    for i in range(i0, len(jumped)):
        for k in ("open", "high", "low", "close"):
            jumped[i][k] *= shock
    jc = [b["close"] for b in jumped]
    check("the injected gap is exactly -12.8%",
          close(math.log(jc[i0] / jc[i0 - 1]), math.log(0.88), 1e-12))
    cc_before, cc_after = vol.close_to_close(closes, 30), vol.close_to_close(jc, 30)
    tr_before, tr_after = vol.trimmed_cc(closes, 30), vol.trimmed_cc(jc, 30)
    check("a single 10% gap inflates close-to-close", cc_after > cc_before * 1.25,
          "%.4f -> %.4f" % (cc_before, cc_after))
    check("the winsorised estimator resists it", tr_after < tr_before * 1.15,
          "%.4f -> %.4f" % (tr_before, tr_after))
    js = vol.jump_share(jc, 30)
    check("jump share flags the gap", js is not None and js > 0.15, "share=%s" % js)


def test_smile():
    section("smile fitting")
    F, T, df = 400.0, 0.25, 0.99
    # A textbook skewed smile: iv falls with strike, curving up in the wings.
    def true_iv(K):
        k = math.log(K / F)
        return 0.28 - 0.45 * k + 1.6 * k * k
    pts = []
    for i in range(-14, 15):
        K = F * math.exp(i * 0.02)
        pts.append((K, true_iv(K), 1.0))
    sm = vol.fit_smile(pts, F, T)
    check("smile fits ATM to within 0.2 vol points", abs(sm.atm_iv - true_iv(F)) < 0.002,
          "%.5f vs %.5f" % (sm.atm_iv, true_iv(F)))
    err = max(abs(sm.iv(F * math.exp(k)) - true_iv(F * math.exp(k)))
              for k in (-0.10, -0.05, 0.0, 0.05, 0.10))
    check("smile reproduces the body", err < 0.01, "max err %.4f" % err)
    check("skew sign is right (puts over calls)",
          sm.iv(F * 0.93) > sm.atm_iv > sm.iv(F * 1.07))
    check("wings extrapolate flat, never negative",
          sm.iv(F * 0.01) > 0 and sm.iv(F * 20) > 0)
    sk = vol.skew_metrics(sm, F, T, df, F)
    check("25-delta skew is positive here", sk["skew_25"] > 0, "skew=%s" % sk["skew_25"])
    flags = vol.butterfly_arbitrage_flags(sm, F, T, df)
    check("a well-formed smile raises no arbitrage flags", not flags, "flags=%s" % flags)

    # Flat surface must reproduce flat vol and a model-free vol equal to it.
    flat = vol.fit_smile([(F * math.exp(i * 0.03), 0.25, 1.0) for i in range(-12, 13)], F, T)
    check("flat smile stays flat", close(flat.atm_iv, 0.25, 1e-6) and abs(flat.b) < 1e-6)
    quotes = []
    for i in range(-40, 41):
        K = round(F * math.exp(i * 0.01), 2)
        right = "C" if K >= F else "P"
        quotes.append((K, right, bs.black76(F, K, T, 0.25, df, right)))
    mf = vol.model_free_iv(quotes, F, T, df)
    check("model-free vol recovers a flat 25% surface", mf is not None and abs(mf - 0.25) < 0.005,
          "got %s" % (round(mf, 5) if mf else None))


def test_term_structure():
    section("term structure and rank")
    pts = [(30 / 365.0, 0.20), (60 / 365.0, 0.24), (90 / 365.0, 0.26)]
    check("term slope positive in contango", vol.term_structure_slope(pts) > 0)
    check("interp at a listed tenor is exact",
          close(vol.interpolate_iv_at_days(pts, 30), 0.20, 1e-9))
    mid = vol.interpolate_iv_at_days(pts, 45)
    check("interp between tenors lands between them", 0.20 < mid < 0.24, "got %.4f" % mid)
    # Variance, not vol, is what interpolates linearly
    t0, v0 = pts[0]
    t1, v1 = pts[1]
    tt = 45 / 365.0
    w = (tt - t0) / (t1 - t0)
    expect = math.sqrt((v0 * v0 * t0 + w * (v1 * v1 * t1 - v0 * v0 * t0)) / tt)
    check("interp is linear in total variance", close(mid, expect, 1e-9))

    r = vol.rank_and_percentile(0.30, [0.10, 0.20, 0.30, 0.40, 0.50])
    check("IV rank at the top of the range = 50", close(r["rank"], 50.0, 1e-9))
    check("IV percentile counts days below", close(r["pct"], 40.0, 1e-9))
    check("rank needs samples", vol.rank_and_percentile(0.3, [0.2])["rank"] is None)

    j = vol.add_event_jump(0.20, 0.06, 30 / 365.0)
    check("an event jump raises total vol", j > 0.20, "0.20 -> %.4f" % j)
    check("jump maths is additive in variance",
          close(j * j * (30 / 365.0), 0.20 * 0.20 * (30 / 365.0) + 0.06 ** 2, 1e-12))
    per_exp = [{"expiry": "2026-09-01", "dte": 10, "t_years": 10 / 365.0, "mfiv": 0.20, "atm_iv": 0.20},
               {"expiry": "2026-09-20", "dte": 30, "t_years": 30 / 365.0, "mfiv": None, "atm_iv": None}]
    # after-expiry vol chosen so the implied jump is exactly 6%
    t_a = 30 / 365.0
    var_a = 0.20 * 0.20 * (10 / 365.0) + 0.06 ** 2
    per_exp[1]["mfiv"] = math.sqrt(var_a / t_a)
    got = vol.implied_event_move(per_exp, dt.date(2026, 9, 10))
    check("implied event move recovered from the term kink", close(got, 0.06, 1e-6),
          "got %s" % (round(got, 6) if got else None))


# =============================================================== parity/chain
def test_parity():
    section("forward recovery from put-call parity")
    curve = parity.YieldCurve([(0.25, 0.04), (5.0, 0.045)])
    check("curve interpolates", 0.04 <= curve.rate(1.0) <= 0.045)
    check("discount factor consistent with rate",
          close(curve.df(0.5), math.exp(-curve.rate(0.5) * 0.5), 1e-12))

    S, T, r, q, sig = 250.0, 0.35, 0.04, 0.018, 0.30
    F_true = S * math.exp((r - q) * T)
    df_true = math.exp(-r * T)
    pairs = []
    for i in range(-12, 13):
        K = round(S * math.exp(i * 0.02), 2)
        pairs.append({
            "strike": K,
            "call_mid": bs.black76(F_true, K, T, sig, df_true, "C"),
            "put_mid": bs.black76(F_true, K, T, sig, df_true, "P"),
            "call_spread": 0.01, "put_spread": 0.01, "call_oi": 500, "put_oi": 500,
        })
    got = parity.implied_forward(pairs, S, T, curve)
    check("forward recovered to 1bp", abs(got["forward"] / F_true - 1) < 1e-4,
          "%.6f vs %.6f (%s)" % (got["forward"], F_true, got["method"]))
    check("dividend yield recovered", abs(got["div_yield"] - q) < 5e-3,
          "%.5f vs %.5f" % (got["div_yield"], q))
    check("parity fit is near-perfect on clean data", got["r2"] > 0.9999)

    # One stale strike must not move the forward.
    dirty = [dict(p) for p in pairs]
    dirty[3]["call_mid"] *= 1.9
    got2 = parity.implied_forward(dirty, S, T, curve)
    check("one bad strike is rejected", abs(got2["forward"] / F_true - 1) < 2e-3,
          "%.4f vs %.4f" % (got2["forward"], F_true))
    check("too few pairs falls back to the curve",
          parity.implied_forward(pairs[:2], S, T, curve)["method"] == "curve")


def test_chain_metrics():
    section("chain metrics")
    check("liquidity rewards tight spreads",
          chainmod.liquidity_score(1.00, 1.02, 1.01, 500, 5000) >
          chainmod.liquidity_score(1.00, 1.40, 1.20, 500, 5000))
    check("liquidity rewards open interest",
          chainmod.liquidity_score(1.00, 1.02, 1.01, 500, 5000) >
          chainmod.liquidity_score(1.00, 1.02, 1.01, 500, 5))
    check("no quote scores zero", chainmod.liquidity_score(None, None, None, 0, 0) == 0)

    # Max pain: all OI at one strike must pin there.
    by_strike = {}
    for k in (90.0, 100.0, 110.0):
        by_strike[k] = {"C": {"open_interest": 0}, "P": {"open_interest": 0}}
    by_strike[100.0]["C"]["open_interest"] = 10000
    by_strike[100.0]["P"]["open_interest"] = 10000
    mp = chainmod._max_pain(by_strike, sorted(by_strike))
    check("max pain sits where the open interest is", close(mp, 100.0, 1e-9), "got %s" % mp)

    contracts = [{"right": "C", "strike": 105.0, "open_interest": 900},
                 {"right": "C", "strike": 110.0, "open_interest": 300},
                 {"right": "P", "strike": 95.0, "open_interest": 800}]
    check("call wall found", close(chainmod._wall(contracts, "C", 100.0), 105.0, 1e-9))
    check("put wall found", close(chainmod._wall(contracts, "P", 100.0), 95.0, 1e-9))

    exps = sorted({dt.date(2026, 8, 14) + dt.timedelta(days=i) for i in range(0, 400, 3)})
    sel = chainmod.select_expiries(exps, now=dt.datetime(2026, 8, 13, 14, tzinfo=dt.timezone.utc))
    dtes = sorted(marketcal.dte(e) for e in sel)
    check("expiry ladder spans the curve", len(sel) >= 8 and max(dtes) > 180,
          "n=%d max dte=%.0f" % (len(sel), max(dtes) if dtes else 0))
    check("expiry ladder keeps the front", min(dtes) < 5)

    # Symbol-level positioning must not be quoted off an expiring 0-DTE strip.
    per_exp = [{"expiry": "2026-08-13", "dte": 0.2, "max_pain": 999.0, "call_wall": 999.0},
               {"expiry": "2026-08-21", "dte": 8.0, "max_pain": 500.0, "call_wall": 505.0},
               {"expiry": "2026-09-18", "dte": 36.0, "max_pain": 480.0, "call_wall": 490.0},
               {"expiry": "2026-12-18", "dte": 127.0, "max_pain": 450.0, "call_wall": 460.0}]
    ref = chainmod.reference_expiry(per_exp)
    check("reference expiry skips the 0-DTE strip", ref["expiry"] == "2026-09-18",
          "picked %s" % ref["expiry"])
    check("reference expiry falls back when nothing is far enough out",
          chainmod.reference_expiry(per_exp[:1])["expiry"] == "2026-08-13")

    # A last print from a previous session must not be read as today's flow.
    today = marketcal.session_date()
    fresh = {"bid": 1.00, "ask": 1.20, "last": 1.19,
             "last_trade_time": today.isoformat() + "T14:31:00"}
    stale = dict(fresh, last_trade_time=(today - dt.timedelta(days=4)).isoformat() + "T14:31:00")
    check("fresh print yields a side hint", chainmod._side_hint(fresh, today) == "at ask")
    check("stale print yields no side hint", chainmod._side_hint(stale, today) is None)
    check("missing timestamp yields no side hint",
          chainmod._side_hint({"bid": 1.0, "ask": 1.2, "last": 1.19}, today) is None)


# ================================================================= strategies
def _mk_position(strategy="bull_put_spread"):
    exp = marketcal.session_date() + dt.timedelta(days=35)
    legs = {
        "bull_put_spread": [
            strategies.Leg("P", 95.0, exp, -1, 2.50, 0.30, r=0.04),
            strategies.Leg("P", 90.0, exp, 1, 1.30, 0.32, r=0.04)],
        "long_call": [strategies.Leg("C", 105.0, exp, 1, 3.00, 0.28, r=0.04)],
        "long_straddle": [strategies.Leg("C", 100.0, exp, 1, 4.00, 0.28, r=0.04),
                          strategies.Leg("P", 100.0, exp, 1, 3.80, 0.28, r=0.04)],
        "iron_condor": [
            strategies.Leg("P", 92.0, exp, -1, 1.50, 0.32, r=0.04),
            strategies.Leg("P", 87.0, exp, 1, 0.80, 0.34, r=0.04),
            strategies.Leg("C", 108.0, exp, -1, 1.40, 0.27, r=0.04),
            strategies.Leg("C", 113.0, exp, 1, 0.70, 0.27, r=0.04)],
    }[strategy]
    return strategies.Position("TEST", strategy, legs, 100.0)


def test_positions():
    section("position payoff algebra")
    p = _mk_position("bull_put_spread")
    check("credit spread nets a credit", p.net_price < 0 and close(p.net_price, -1.20, 1e-9))
    mx, mn, ub_p, ub_l = p.extremes()
    check("credit spread max profit = credit", close(mx, 1.20, 1e-9), "got %s" % mx)
    check("credit spread max loss = width - credit", close(mn, -3.80, 1e-9), "got %s" % mn)
    check("credit spread risk is defined", not ub_l and not ub_p)
    be = p.breakevens()
    check("credit spread breakeven = short strike - credit", close(be[0], 93.80, 1e-6), "got %s" % be)
    check("payoff above both strikes = credit", close(p.payoff(120.0), 1.20, 1e-9))
    check("payoff below both strikes = max loss", close(p.payoff(50.0), -3.80, 1e-9))

    lc = _mk_position("long_call")
    mx, mn, ub_p, ub_l = lc.extremes()
    check("long call profit is unbounded", ub_p and mx is None)
    check("long call loss is the premium", close(mn, -3.00, 1e-9))
    check("long call risk is NOT flagged unbounded", not ub_l)

    st = _mk_position("long_straddle")
    mx, mn, ub_p, ub_l = st.extremes()
    check("straddle profit unbounded, loss defined", ub_p and not ub_l and close(mn, -7.80, 1e-9))
    check("straddle has two breakevens", len(st.breakevens()) == 2, "%s" % st.breakevens())

    ic = _mk_position("iron_condor")
    mx, mn, _, ubl = ic.extremes()
    check("condor max profit = net credit", close(mx, 1.40, 1e-9), "got %s" % mx)
    check("condor max loss = wing width - credit", close(mn, -3.60, 1e-9), "got %s" % mn)
    check("condor risk defined", not ubl)
    check("condor has two breakevens", len(ic.breakevens()) == 2, "%s" % ic.breakevens())

    # A covered call is long stock + short call: capped upside, defined
    # downside.  Modelled as a bare short call it reads as unbounded upside
    # risk, which is the opposite of the truth.
    exp = marketcal.session_date() + dt.timedelta(days=35)
    cc = strategies.Position("TEST", "covered_call", [
        strategies.Leg("S", 0.0, exp, 1, 100.0, 0.0, r=0.04),
        strategies.Leg("C", 105.0, exp, -1, 3.00, 0.28, r=0.04)], 100.0)
    mx, mn, ubp, ubl = cc.extremes()
    check("covered call risk is NOT unbounded", not ubl)
    check("covered call upside is capped", not ubp)
    check("covered call max profit = strike - cost + credit", close(mx, 8.00, 1e-9), "got %s" % mx)
    check("covered call max loss = -(cost - credit)", close(mn, -97.00, 1e-9), "got %s" % mn)
    check("covered call breakeven = spot - credit",
          cc.breakevens() and close(cc.breakevens()[0], 97.0, 1e-6), "%s" % cc.breakevens())
    check("covered call is delta-positive but under 1",
          0.3 < cc.greeks(100.0)["delta"] < 1.0, "%s" % cc.greeks(100.0)["delta"])
    check("stock leg is worth spot at any date",
          close(cc.legs[0].value(123.45, 100.0, marketcal.now_utc()), 123.45, 1e-12))
    check("covered call payoff flat above the strike",
          close(cc.payoff(200.0), cc.payoff(150.0), 1e-9))

    # Value must converge to payoff as time runs out.
    moment = marketcal.expiry_moment(p.near_expiry) - dt.timedelta(seconds=30)
    worst = max(abs(p.pnl(s, moment) - p.payoff(s)) for s in (85, 92, 96, 105))
    check("value converges to payoff at expiry", worst < 0.02, "worst %.4f" % worst)


def test_density_and_ev():
    section("risk-neutral density and expectancy")
    F, T, df = 100.0, 0.25, math.exp(-0.04 * 0.25)
    sm = vol.fit_smile([(F * math.exp(i * 0.03), 0.30, 1.0) for i in range(-14, 15)], F, T)
    dens = strategies.risk_neutral_density(sm, F, T, df)
    check("density is produced", len(dens) > 100)
    tot = sum(p for _s, p in dens)
    check("density sums to 1", close(tot, 1.0, 1e-9), "got %.8f" % tot)
    mean = sum(s * p for s, p in dens)
    check("density mean equals the forward", abs(mean / F - 1) < 0.005,
          "%.4f vs %.4f" % (mean, F))
    var = sum((math.log(s / F)) ** 2 * p for s, p in dens)
    check("density variance equals sigma^2 T", abs(math.sqrt(var) / (0.30 * math.sqrt(T)) - 1) < 0.02,
          "%.5f vs %.5f" % (math.sqrt(var), 0.30 * math.sqrt(T)))
    # No arbitrage: a fairly-priced option has ~zero EV under Q.
    exp0 = marketcal.session_date() + dt.timedelta(days=int(T * 365))
    px = bs.black76(F, 105.0, T, 0.30, df, "C")
    pos = strategies.Position("T", "long_call",
                              [strategies.Leg("C", 105.0, exp0, 1, px / df, 0.30, r=0.04)], F)
    ev = strategies.evaluate(pos, dens)
    check("fair-value option has ~zero Q-expectancy", abs(ev["ev"]) < 0.02 * px,
          "ev=%.5f on a %.3f option" % (ev["ev"], px))
    check("POP is between 0 and 1", 0 <= ev["pop"] <= 1)
    check("CVaR is at or below the 5th percentile", ev["cvar5"] <= ev["p05"] + 1e-9)
    check("percentiles are ordered", ev["p05"] <= ev["p25"] <= ev["p50"] <= ev["p75"] <= ev["p95"])

    p2 = strategies.transform_density(dens, F, T, 0.45, 0.04)
    sd2 = math.sqrt(sum((math.log(s / F) - sum(math.log(x / F) * q for x, q in p2)) ** 2 * p
                        for s, p in p2))
    check("P-density is rescaled to the forecast vol",
          abs(sd2 / (0.45 * math.sqrt(T)) - 1) < 0.02, "%.5f vs %.5f" % (sd2, 0.45 * math.sqrt(T)))
    check("P-density still sums to 1", close(sum(p for _s, p in p2), 1.0, 1e-9))
    # Long vol under a higher forecast vol must be positive EV.
    ev2 = strategies.evaluate(pos, p2)
    check("long option gains EV when forecast vol exceeds implied", ev2["ev"] > ev["ev"])


def test_horizon_evaluation():
    section("horizon vs expiry expectancy")
    F, T, df = 100.0, 0.25, math.exp(-0.04 * 0.25)
    sm = vol.fit_smile([(F * math.exp(i * 0.03), 0.30, 1.0) for i in range(-14, 15)], F, T)
    dens = strategies.risk_neutral_density(sm, F, T, df)
    exp0 = marketcal.session_date() + dt.timedelta(days=int(T * 365))
    lc = strategies.Position("T", "long_call",
                             [strategies.Leg("C", 105.0, exp0, 1, 3.0, 0.30, r=0.04)], F)

    now = marketcal.now_utc()
    at_expiry = strategies.evaluate(lc, dens)
    hm, t_h = strategies.horizon_for(lc, T, now)
    at_horizon = strategies.evaluate(lc, dens, moment=hm)
    check("horizon lands strictly before expiry",
          hm < marketcal.expiry_moment(lc.near_expiry) and hm > now)
    check("horizon vol-time is shorter than to expiry", 0 < t_h <= T, "%.5f vs %.5f" % (t_h, T))

    # THE bug this pins: evaluate() ignored `moment` and always used intrinsic,
    # so a long option showed identical expectancy at every horizon and time
    # decay never entered the ranking at all.
    check("a long option is worth MORE before expiry than at it",
          at_horizon["ev"] > at_expiry["ev"],
          "horizon %.4f vs expiry %.4f" % (at_horizon["ev"], at_expiry["ev"]))
    check("the two horizons actually differ", abs(at_horizon["ev"] - at_expiry["ev"]) > 1e-6)

    # A short option is the mirror image: it has NOT yet collected all its theta.
    sp = strategies.Position("T", "short_put",
                             [strategies.Leg("P", 95.0, exp0, -1, 2.0, 0.30, r=0.04)], F)
    hm2, _ = strategies.horizon_for(sp, T, now)
    check("a short option has collected less by the horizon than by expiry",
          strategies.evaluate(sp, dens, moment=hm2)["ev"] < strategies.evaluate(sp, dens)["ev"])

    check("horizon respects the time stop",
          config.EVAL_HORIZON_FRAC > 0 and config.EVAL_AT_HORIZON)
    # A near-dated position must still produce a usable horizon.
    soon = marketcal.session_date() + dt.timedelta(days=3)
    near = strategies.Position("T", "long_call",
                               [strategies.Leg("C", 105.0, soon, 1, 1.0, 0.30, r=0.04)], F)
    hm3, t3 = strategies.horizon_for(near, 3 / 365.0, now)
    check("short-dated horizon stays before its own expiry",
          hm3 < marketcal.expiry_moment(soon) and t3 > 0)


def test_targets():
    section("take-profit ladder")
    p = _mk_position("bull_put_spread")
    t = strategies.build_targets(p, 100.0, 0.30, 0.04)
    names = [r["name"] for r in t["targets"]]
    check("three rungs plus a stop", names == ["T1", "T2", "T3"] and t["stop"] is not None)
    check("rungs increase in profit", t["targets"][0]["pnl"] < t["targets"][1]["pnl"] <= t["targets"][2]["pnl"])
    check("T1 is half the credit", close(t["targets"][0]["pnl"], 0.60, 1e-9))
    check("stop is 2x the credit", close(t["stop"]["pnl"], -2.40, 1e-9))
    check("expire-worthless is a time outcome, never a price one",
          t["targets"][2].get("path") == "expiry" and t["targets"][2].get("spot") is None)
    probs = [r.get("prob") for r in t["targets"] if r.get("prob") is not None]
    check("all probabilities are valid", all(0 <= x <= 1 for x in probs), "%s" % probs)
    check("exit price matches the P&L rung",
          all(close(r["spread_price"], p.net_price + r["pnl"], 1e-9) for r in t["targets"]))

    st = _mk_position("long_straddle")
    ts = strategies.build_targets(st, 100.0, 0.30, 0.0)
    two = [r for r in ts["targets"] if r.get("spot_up") is not None and r.get("spot_down") is not None]
    check("a straddle gets two-sided price targets", len(two) >= 1,
          "paths=%s" % [r.get("path") for r in ts["targets"]])
    if two:
        r = two[0]
        check("two-sided target straddles spot", r["spot_down"] < 100.0 < r["spot_up"])
        one = max(bs.prob_touch(100.0, r["spot_up"], 0.30, 0.05),
                  bs.prob_touch(100.0, r["spot_down"], 0.30, 0.05))
        check("two-sided probability beats either side alone", r["prob"] >= one - 1e-6)
    mono = [r["prob"] for r in ts["targets"] if r.get("prob") is not None]
    check("target probabilities never increase with distance",
          all(mono[i] >= mono[i + 1] - 1e-9 for i in range(len(mono) - 1)), "%s" % mono)

    check("sizing respects the risk budget",
          strategies.suggested_qty(3.80) ==
          max(1, int((config.ACCOUNT_SIZE * config.RISK_PER_TRADE_PCT) // 380.0)))
    check("sizing never returns zero", strategies.suggested_qty(1e6) == 1)


def test_fills():
    section("fill assumptions")
    c = {"bid": 1.00, "ask": 1.20, "mid": 1.10}
    buy = strategies.fill_price(c, 1)
    sell = strategies.fill_price(c, -1)
    check("buying pays above mid", buy > 1.10 and close(buy, 1.15, 1e-9))
    check("selling receives below mid", sell < 1.10 and close(sell, 1.05, 1e-9))
    check("round trip costs half the spread", close(buy - sell, 0.10, 1e-9))
    check("no mid means no fill", strategies.fill_price({"bid": 0, "ask": 0.5, "mid": None}, 1) is None)


# ================================================================== calendar
def test_calendar():
    section("market calendar")
    check("Christmas 2026 is a holiday", not marketcal.is_trading_day(dt.date(2026, 12, 25)))
    check("July 4 2026 (Saturday) observed on the 3rd",
          not marketcal.is_trading_day(dt.date(2026, 7, 3)))
    check("Good Friday 2026 is closed", not marketcal.is_trading_day(dt.date(2026, 4, 3)))
    check("Easter 2026 computed correctly", marketcal.easter(2026) == dt.date(2026, 4, 5))
    check("a normal Wednesday is open", marketcal.is_trading_day(dt.date(2026, 8, 12)))
    check("weekends are closed", not marketcal.is_trading_day(dt.date(2026, 8, 15)))
    check("third-Friday detection", marketcal.is_monthly_expiry(dt.date(2026, 9, 18)))
    check("not every Friday is a monthly", not marketcal.is_monthly_expiry(dt.date(2026, 9, 11)))
    n = marketcal.trading_days_between(dt.date(2026, 8, 13), dt.date(2026, 8, 20))
    check("5 sessions in a plain week", n == 5, "got %d" % n)
    check("DST offsets", marketcal.et_offset(dt.datetime(2026, 7, 1, tzinfo=dt.timezone.utc)) == -4
          and marketcal.et_offset(dt.datetime(2026, 1, 15, tzinfo=dt.timezone.utc)) == -5)
    exp = dt.date(2026, 9, 18)
    now = dt.datetime(2026, 8, 13, 20, 0, tzinfo=dt.timezone.utc)
    tc = marketcal.year_fraction(exp, now)
    tv = marketcal.vol_year_fraction(exp, now)
    check("calendar T is positive and sane", 0.09 < tc < 0.11, "%.5f" % tc)
    check("business time is close to calendar time over a month", abs(tv - tc) < 0.01,
          "cal %.5f vs vol %.5f" % (tc, tv))
    check("AM settlement is earlier than PM",
          marketcal.expiry_moment(exp, True) < marketcal.expiry_moment(exp, False))


# ================================================================= technicals
def test_technicals():
    section("technical indicators")
    closes = [float(x) for x in range(1, 60)]
    check("SMA of a ramp", close(tech.sma(closes, 5), (55 + 56 + 57 + 58 + 59) / 5.0, 1e-12))
    check("RSI of a pure uptrend is 100", close(tech.rsi(closes), 100.0, 1e-9))
    check("RSI of a pure downtrend is 0", close(tech.rsi(list(reversed(closes))), 0.0, 1e-9))
    flat = [50.0] * 60
    check("EMA of a constant is that constant", close(tech.ema(flat, 20), 50.0, 1e-9))
    bb = tech.bollinger(flat)
    check("Bollinger width is zero on a flat series", close(bb["width"], 0.0, 1e-12))
    bars = [{"date": "d%d" % i, "open": 100, "high": 102, "low": 98, "close": 100, "volume": 1}
            for i in range(60)]
    check("ATR of a constant 4-point range is 4", close(tech.atr(bars), 4.0, 1e-9))
    up = [{"date": "d%d" % i, "open": 100 + i, "high": 101 + i, "low": 99 + i,
           "close": 100.5 + i, "volume": 1000} for i in range(120)]
    a, pdi, mdi = tech.adx(up)
    check("ADX is high in a clean trend", a > 40, "adx=%.1f" % a)
    check("+DI dominates in an uptrend", pdi > mdi)
    res = tech.analyze(up)
    check("trend score is strongly positive", res["trend_score"] > 0.4, "%.3f" % res["trend_score"])
    check("trend label agrees", res["trend_label"] in ("up", "strong up"))
    down = [{"date": "d%d" % i, "open": 200 - i, "high": 201 - i, "low": 199 - i,
             "close": 199.5 - i, "volume": 1000} for i in range(120)]
    res2 = tech.analyze(down)
    check("downtrend scores negative", res2["trend_score"] < -0.4, "%.3f" % res2["trend_score"])
    check("trend score stays inside [-1,1]", -1 <= res["trend_score"] <= 1 and -1 <= res2["trend_score"] <= 1)


# ================================================================== sentiment
def test_sentiment():
    section("news sentiment")
    pos, _, _ = sentiment.score_text("Apple beats estimates and raises guidance")
    neg, _, _ = sentiment.score_text("Apple misses estimates and cuts guidance")
    check("beat + raise is positive", pos > 0.4, "%.3f" % pos)
    check("miss + cut is negative", neg < -0.4, "%.3f" % neg)
    check("negation flips a positive", sentiment.score_text("did not beat estimates")[0] < pos)
    neutral, hits, _ = sentiment.score_text("Apple announces its annual shareholder meeting date")
    check("bland text scores near zero", abs(neutral) < 0.25, "%.3f (%d hits)" % (neutral, hits))
    check("scores stay in [-1,1]",
          all(-1 <= sentiment.score_text(t)[0] <= 1 for t in
              ("surge soar rally beat raise upgrade record breakthrough",
               "crash plunge fraud bankruptcy miss cut downgrade halt recall")))
    item = {"title": "Nvidia beats estimates", "summary": "", "published": None, "source": "Reuters"}
    check("ticker in the title is fully relevant",
          close(sentiment.relevance(item, "NVDA", "Nvidia"), 1.0, 1e-9) or
          sentiment.relevance(item, "NVDA", "Nvidia") > 0.9)
    roundup = {"title": "10 stocks to watch on Monday", "summary": "", "published": None}
    check("a round-up is heavily discounted",
          sentiment.relevance(roundup, "NVDA", "Nvidia") < 0.4)
    check("credible sources weigh more",
          sentiment.source_weight("Reuters") > sentiment.source_weight("Insider Monkey"))
    agg = sentiment.aggregate([sentiment.score_item(
        {"title": "Nvidia beats estimates and raises guidance", "summary": "",
         "published": marketcal.now_utc(), "source": "Reuters"}, "NVDA", "Nvidia")])
    check("aggregate is positive", agg["score"] > 0.3, "%.3f" % agg["score"])
    check("aggregate reports a count", agg["count"] == 1)


# ==================================================================== scanner
def test_vol_calibration():
    section("vol-forecast calibration")
    check("calibration is on by default", config.VOL_CALIBRATION)
    # It must RAISE the forecast: the measured bias was 12% low.
    raised = [vol.calibrate(f) > f for f in (0.10, 0.20, 0.35, 0.60, 1.00)]
    check("calibration raises the forecast at every level", all(raised))
    check("correction is close to a flat 1.135x (fitted B ~ 1)",
          all(abs(vol.calibrate(f) / f - 1.135) < 0.02 for f in (0.15, 0.30, 0.60)),
          "%s" % [round(vol.calibrate(f) / f, 4) for f in (0.15, 0.30, 0.60)])
    check("calibration is monotone", vol.calibrate(0.2) < vol.calibrate(0.4) < vol.calibrate(0.8))
    check("calibration stays inside the IV bounds",
          config.MIN_IV <= vol.calibrate(0.01) and vol.calibrate(4.9) <= config.MAX_IV)
    check("calibration is a no-op on junk input",
          vol.calibrate(0) in (0, None) and vol.calibrate(None) is None)
    # forecast_vol must route through it
    bars = [{"date": "d%03d" % i, "open": 100, "high": 101, "low": 99,
             "close": 100 * (1.004 if i % 2 else 0.996), "volume": 1e6} for i in range(300)]
    rv = vol.all_realised(bars)
    fc = vol.forecast_vol(rv)
    raw_blend = sum(config.VOL_FORECAST_WEIGHTS[k] * rv[k]
                    for k in config.VOL_FORECAST_WEIGHTS if rv.get(k)) /         sum(config.VOL_FORECAST_WEIGHTS[k] for k in config.VOL_FORECAST_WEIGHTS if rv.get(k))
    check("forecast_vol applies the calibration", fc > raw_blend * 1.05,
          "fc=%.4f vs raw blend %.4f" % (fc, raw_blend))


def test_board_construction():
    section("portfolio construction")
    mk = lambda s, d, sc: {"symbol": s, "direction": d, "score": sc, "strategy": "x"}
    ideas = ([mk("SPY", "bullish", 99 - i) for i in range(6)] +
             [mk("NVDA", "bullish", 90 - i) for i in range(6)] +
             [mk("TLT", "bearish", 80), mk("GLD", "neutral", 79),
              mk("XOM", "neutral", 78), mk("UBER", "volatility", 77)])
    out = scanner.rank_all(ideas, top_n=12)
    bull = sum(1 for i in out if i["direction"] == "bullish")
    check("bullish share is capped", bull <= int(12 * config.BOARD_MAX_BULLISH_PCT),
          "%d bullish of %d kept" % (bull, len(out)))
    per_sym = {}
    for i in out:
        per_sym[i["symbol"]] = per_sym.get(i["symbol"], 0) + 1
    check("per-symbol cap holds", max(per_sym.values()) <= config.BOARD_MAX_PER_SYMBOL,
          "%s" % per_sym)
    per_grp = {}
    for i in out:
        g = scanner.GROUP_OF.get(i["symbol"], i["symbol"])
        per_grp[g] = per_grp.get(g, 0) + 1
    check("correlation-group cap holds", max(per_grp.values()) <= config.BOARD_MAX_PER_GROUP,
          "%s" % per_grp)
    check("ranks are contiguous from 1",
          [i["rank"] for i in out] == list(range(1, len(out) + 1)))
    check("the top idea always survives", out and out[0]["score"] == 99)
    check("dropped ideas record why",
          all(i.get("excluded_reason") for i in ideas if i not in out))
    check("SPY and QQQ share a correlation group",
          scanner.GROUP_OF.get("SPY") == scanner.GROUP_OF.get("QQQ") == "index")

    check("drift tilt is shrunk from the original 0.35",
          config.DRIFT_TILT_SHARPE <= 0.15, "%.2f" % config.DRIFT_TILT_SHARPE)


def test_settlement_and_pop_calibration():
    section("settlement and POP calibration")
    import journal
    db.init()
    # Settlement must use the close ON the expiry date, not the latest close.
    sym = db.scalar("SELECT symbol FROM ohlc GROUP BY symbol ORDER BY COUNT(*) DESC LIMIT 1")
    rows = db.q("SELECT date, close FROM ohlc WHERE symbol=? ORDER BY date DESC LIMIT 40", (sym,))
    if len(rows) > 25:
        past, latest = rows[20], rows[0]
        got = journal.settlement_spot(sym, past["date"])
        check("settlement_spot returns the close on that date",
              close(got, past["close"], 1e-9), "%s vs %s" % (got, past["close"]))
        check("settlement_spot is NOT the latest close when the date is older",
              abs(got - latest["close"]) > 1e-9 or abs(past["close"] - latest["close"]) < 1e-9)
    check("settlement_spot tolerates a non-trading date",
          journal.settlement_spot(sym, "2026-01-03") is not None)
    check("settlement_spot returns None for an unknown symbol",
          journal.settlement_spot("__NOPE__", "2026-06-01") is None)

    # An expired leg must settle against its own expiry, not today.
    exp = rows[20]["date"] if len(rows) > 25 else "2026-06-01"
    legs = [{"right": "C", "strike": 1.0, "expiry": exp, "qty": 1}]
    mk, src = journal.mark_legs(legs, {"symbol": sym, "spot": 999999.0, "expiries": []},
                                symbol=sym)
    check("a deep-ITM expired call settles at its expiry close, not today's spot",
          mk is not None and abs(mk - (journal.settlement_spot(sym, exp) - 1.0)) < 1e-6,
          "got %s" % mk)

    # POP calibration: debit cut hard, credit barely moved, bounded and monotone.
    for raw in (0.10, 0.30, 0.50, 0.77, 0.95):
        d = scanner.calibrate_pop(raw, False)
        c = scanner.calibrate_pop(raw, True)
        check("POP %.0f%%: debit shrinks more than credit" % (raw * 100), d < c <= raw + 1e-9,
              "debit %.3f credit %.3f" % (d, c))
        check("  stays a probability", 0.0 <= d <= 1.0 and 0.0 <= c <= 1.0)
    check("POP calibration is monotone",
          scanner.calibrate_pop(0.2, False) < scanner.calibrate_pop(0.6, False)
          < scanner.calibrate_pop(0.9, False))
    check("credit calibration is nearly a no-op (it was already honest)",
          abs(scanner.calibrate_pop(0.77, True) / 0.77 - 1) < 0.06)
    check("debit calibration is a real cut (38% stated -> 14% realised)",
          scanner.calibrate_pop(0.38, False) < 0.30,
          "%.3f" % scanner.calibrate_pop(0.38, False))
    check("calibration passes None through", scanner.calibrate_pop(None, True) is None)
    check("POP is now a scored component", "pop" in config.IDEA_WEIGHTS)
    check("weights still sum to 1", abs(sum(config.IDEA_WEIGHTS.values()) - 1.0) < 1e-9)
    check("debit POP floor was raised to the measured level",
          config.MIN_POP_DEBIT >= 0.45, "%.2f" % config.MIN_POP_DEBIT)


def test_scanner():
    section("scanner regimes")
    check("high IV vs forecast reads rich",
          scanner.vol_regime(0.40, 0.28, 80)["label"] == "rich")
    check("low IV vs forecast reads cheap",
          scanner.vol_regime(0.20, 0.30, 10)["label"] == "cheap")
    check("matching IV and forecast reads fair",
          scanner.vol_regime(0.30, 0.30, 50)["label"] == "fair")
    check("trend regimes map correctly",
          scanner.trend_regime(0.6) == "bullish" and scanner.trend_regime(-0.6) == "bearish"
          and scanner.trend_regime(0.0) == "neutral")
    for trend in ("bullish", "bearish", "neutral"):
        for v in ("rich", "fair", "cheap"):
            strats = scanner.STRATEGY_MATRIX.get((trend, v))
            check("matrix covers %s/%s" % (trend, v), bool(strats))
            for s in strats:
                check("  %s is a known structure" % s, s in strategies.STRATEGY_LABELS)
    check("bullish+rich sells put premium",
          "bull_put_spread" in scanner.STRATEGY_MATRIX[("bullish", "rich")])
    check("neutral+cheap buys volatility",
          any(s in scanner.EVENT_STRATEGIES for s in scanner.STRATEGY_MATRIX[("neutral", "cheap")]))
    exps = [{"expiry": "2026-09-18", "dte": 36}, {"expiry": "2026-08-21", "dte": 8},
            {"expiry": "2026-12-18", "dte": 127}]
    picked = scanner.pick_expiries(exps)
    check("expiry picker prefers the 25-50 day band",
          picked and abs(picked[0]["dte"] - 36) < 1e-9, "%s" % [p["dte"] for p in picked])


# ======================================================================== db
def test_db():
    section("database")
    db.init()
    cols = db.table_columns("symbol_metrics")
    for c in ("iv30", "vol_forecast", "iv_rank", "trend_score", "implied_earnings_move",
              "jump_share", "vol_regime", "news_confidence", "skew_25"):
        check("symbol_metrics has %s" % c, c in cols)
    ec = db.table_columns("expiry_metrics")
    for c in ("smile_a", "smile_scale", "smile_k_min", "rate", "div_yield", "iv_25p"):
        check("expiry_metrics has %s" % c, c in ec)
    db.kv_set("_test", {"a": 1})
    check("kv round-trips", db.kv_get("_test") == {"a": 1})
    db.execute("DELETE FROM kv WHERE key='_test'")
    check("insert_dict drops unknown keys",
          db.insert_dict("kv", {"key": "_t2", "value": "1", "updated": "x",
                                "not_a_column": 5}) is not None)
    db.execute("DELETE FROM kv WHERE key='_t2'")


# ====================================================================== live
def test_live():
    section("live data adapters")
    from sources import cboe, prices, news as news_src, earnings as earn_src
    ch = cboe.fetch_chain("SPY", ttl=0)
    check("CBOE returns a chain", ch and len(ch["contracts"]) > 500,
          "n=%s" % (len(ch["contracts"]) if ch else 0))
    if ch:
        check("spot is plausible", 50 < ch["spot"] < 5000, "%s" % ch["spot"])
        check("chain carries volume and OI", ch["total_volume"] > 0 and ch["total_oi"] > 0)
        c = ch["contracts"][0]
        check("contracts parse to a date", isinstance(c["expiry"], dt.date))
        check("no expired contracts survive", all(x["expiry"] >= marketcal.session_date()
                                                  for x in ch["contracts"]))
    occ = cboe.parse_occ("AAPL260814C00110000")
    check("OCC parser", occ == ("AAPL", dt.date(2026, 8, 14), "C", 110.0), "%s" % (occ,))
    occ2 = cboe.parse_occ("SPXW261218P04500000")
    check("OCC parser handles weekly index roots",
          occ2 == ("SPXW", dt.date(2026, 12, 18), "P", 4500.0), "%s" % (occ2,))

    bars = prices.fetch_history("SPY", range_="6mo", ttl=0)
    check("price history returned", len(bars) > 80, "n=%d" % len(bars))
    check("bars are chronological", all(bars[i]["date"] <= bars[i + 1]["date"]
                                        for i in range(len(bars) - 1)))
    check("bars have OHLC", all(b["low"] <= b["close"] <= b["high"] for b in bars[:50]))
    macro = prices.fetch_macro(["^VIX", "^IRX"], ttl=0)
    check("VIX fetched", macro.get("^VIX") and 5 < macro["^VIX"] < 100, "%s" % macro.get("^VIX"))
    check("T-bill yield fetched", macro.get("^IRX") is not None)

    items = news_src.fetch_ticker_news("AAPL", ttl=0)
    check("news returned", len(items) > 3, "n=%d" % len(items))
    check("news items have titles", all(i.get("title") for i in items))

    curve = parity.YieldCurve.from_quotes(macro)
    check("curve built from live quotes", 0 < curve.rate(0.25) < 0.15, "%s" % curve.rate(0.25))

    if ch:
        roll = chainmod.rollup("SPY", ch, curve)
        check("rollup produced expiries", roll and len(roll["expiries"]) >= 5)
        check("IV30 is plausible", 0.03 < (roll["iv30"] or 0) < 1.5, "%s" % roll["iv30"])
        check("term structure differentiates tenors",
              roll["iv30"] != roll["iv90"], "%s vs %s" % (roll["iv30"], roll["iv90"]))
        clean = 0
        for m in roll["expiries"]:
            if not m.get("arb_flags"):
                clean += 1
            check_atm = m["atm_iv"]
            if m["mfiv"] and check_atm:
                check("%s ATM and model-free vol agree within 6 pts" % m["expiry"],
                      abs(m["mfiv"] - check_atm) < 0.06,
                      "atm %.4f mfiv %.4f" % (check_atm, m["mfiv"]))
        check("most expiries are arbitrage-free", clean >= len(roll["expiries"]) * 0.7,
              "%d/%d clean" % (clean, len(roll["expiries"])))
        # Pick a tenor where implied vol is actually well determined.  Index 3
        # can be a 5-DTE contract; at that maturity vega is near zero, so a
        # normal 6% quote spread becomes ~2 vol points and the comparison
        # measures quote granularity rather than whether our solver agrees with
        # the vendor.  (Observed: SPY 5-DTE at 10.7% vol gave a 1.9-point median
        # gap while the smile's own fit RMSE was 0.0034.)
        usable = [m for m in roll["expiries"] if (m.get("dte") or 0) >= 20]
        front = usable[0] if usable else roll["expiries"][-1]
        check("forward is near spot for a short tenor",
              abs(front["forward"] / roll["spot"] - 1) < 0.05,
              "F=%.2f S=%.2f" % (front["forward"], roll["spot"]))
        # Our IV should track the vendor's on liquid near-the-money quotes.
        diffs = []
        for c in front["contracts"]:
            if (c.get("iv_quote") and c.get("iv_src") and (c.get("liquidity") or 0) > 55
                    and abs(c["strike"] / roll["spot"] - 1) < 0.06):
                diffs.append(abs(c["iv_quote"] - c["iv_src"]))
        if diffs:
            diffs.sort()
            med = diffs[len(diffs) // 2]
            check("our IV matches the vendor's near the money (median < 1 pt)", med < 0.01,
                  "median %.4f over %d quotes" % (med, len(diffs)))


def test_api():
    section("HTTP API")
    import http.cookiejar
    import json as _json
    import urllib.parse
    import urllib.request
    base = "http://%s:%d" % (config.HOST, config.PORT)

    # A cookie jar so the session survives across calls, exactly as a browser
    # would hold it.  If the gate is on, log in first -- that exercises the
    # real authentication path rather than bypassing it for the tests.
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    urllib.request.install_opener(opener)

    import auth
    if auth.is_enabled():
        pw = os.environ.get("THETA_DESK_TEST_PASSWORD") or os.environ.get("THETA_DESK_PASSWORD")
        if not pw:
            path = os.path.join(config.DATA_DIR, "INITIAL_PASSWORD.txt")
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    pw = f.read().strip()
        if not pw:
            check("API suite can authenticate", False,
                  "a password is set but none available to the tests; export "
                  "THETA_DESK_TEST_PASSWORD to run the API suite")
            return
        body = urllib.parse.urlencode({"password": pw, "next": "/"}).encode()
        try:
            with opener.open(base + "/login", data=body, timeout=20) as r:
                check("login succeeds with the right password", r.status in (200, 302))
        except Exception as e:                            # noqa: BLE001
            check("login succeeds with the right password", False, str(e))
            return
        # And that a wrong one does not hand out a session.
        jar2 = http.cookiejar.CookieJar()
        op2 = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar2))
        bad = urllib.parse.urlencode({"password": "definitely-not-it"}).encode()
        try:
            op2.open(base + "/login", data=bad, timeout=20)
            check("wrong password is rejected", False, "server accepted a bad password")
        except urllib.error.HTTPError as e:
            check("wrong password is rejected", e.code in (401, 429), "got HTTP %s" % e.code)
        try:
            op2.open(base + "/api/overview", timeout=20)
            check("unauthenticated API access is blocked", False, "server served data")
        except urllib.error.HTTPError as e:
            check("unauthenticated API access is blocked", e.code == 401, "got HTTP %s" % e.code)

    def get(path):
        with urllib.request.urlopen(base + path, timeout=20) as r:
            raw = r.read().decode()
        # STRICT parse: Python happily emits the bare tokens Infinity/NaN, which
        # are not valid JSON.  A browser's response.json() throws on them, and
        # because that happens inside a fetch the view renders blank with no
        # visible error -- one all-winners profit factor wiped the entire
        # Journal tab that way.  parse_constant makes any such token an error.
        def _bad(tok):
            raise ValueError("invalid JSON token %r in %s" % (tok, path))
        return r.status, _json.loads(raw, parse_constant=_bad)

    try:
        st, ov = get("/api/overview")
    except Exception as e:                                # noqa: BLE001
        check("server reachable on %s" % base, False, str(e))
        return
    check("GET /api/overview", st == 200 and "symbols" in ov)
    check("overview carries symbols", len(ov["symbols"]) > 0, "%d" % len(ov["symbols"]))
    sym = ov["symbols"][0]["symbol"]
    for r in ov["symbols"]:
        check("%s has a spot" % r["symbol"], r["spot"] and r["spot"] > 0)
        check("%s IV30 is sane" % r["symbol"], r["iv30"] is None or 0.01 < r["iv30"] < 4)
        check("%s trend score in range" % r["symbol"],
              r["trend_score"] is None or -1 <= r["trend_score"] <= 1)
    st, d = get("/api/symbol/" + sym)
    check("GET /api/symbol", st == 200 and d["symbol"] == sym)
    check("symbol view has expiries", len(d["expiries"]) > 0)
    check("symbol view has price bars", len(d["bars"]) > 100)
    st, cdata = get("/api/chain/" + sym)
    check("GET /api/chain", st == 200 and len(cdata["strikes"]) > 5)
    check("chain rows carry greeks",
          any((s.get("call") or {}).get("delta") is not None for s in cdata["strikes"]))
    st, ideas = get("/api/ideas")
    check("GET /api/ideas", st == 200)
    for i in ideas.get("ideas", [])[:8]:
        check("idea %s/%s has legs" % (i["symbol"], i["strategy"]), len(i["legs"]) > 0)
        check("  POP in [0,1]", 0 <= i["pop"] <= 1)
        check("  targets present", len(i.get("targets") or []) > 0)
        check("  score in [0,100]", 0 <= i["score"] <= 100)
        if i["max_loss"] is not None:
            check("  max loss is negative", i["max_loss"] < 0)
        st2, po = get("/api/idea/%d/payoff" % i["id"])
        check("  payoff endpoint", st2 == 200 and len(po["at_expiry"]) > 50)
    st, fl = get("/api/flow")
    check("GET /api/flow", st == 200 and "leaders" in fl)
    st, pf = get("/api/performance")
    check("GET /api/performance", st == 200 and "journal" in pf)
    st, stt = get("/api/status")
    check("GET /api/status", st == 200 and "db" in stt)
    st, nw = get("/api/news")
    check("GET /api/news", st == 200 and "news" in nw)

    # Every endpoint must survive a strict parse even with a live journal in a
    # degenerate state (all winners, no losers, one sample).
    tmp = None
    try:
        ideas_list = ideas.get("ideas") or []
        if ideas_list:
            i0 = ideas_list[0]
            req = urllib.request.Request(
                base + "/api/trades", method="POST",
                data=_json.dumps({"from_idea": i0["id"], "qty": 1}).encode(),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=20) as r:
                tmp = _json.loads(r.read().decode()).get("id")
            req = urllib.request.Request(
                base + "/api/trades/%d" % tmp, method="PATCH",
                data=_json.dumps({"action": "close",
                                  "exit_price": abs(i0["entry_price"]) * 2.0,
                                  "reason": "test"}).encode(),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=20):
                pass
            for path in ("/api/performance", "/api/overview", "/api/flow"):
                stx, _body = get(path)
                check("strict JSON from %s with a degenerate journal" % path, stx == 200)
    except (urllib.error.URLError, ValueError) as e:
        check("degenerate-journal JSON round trip", False, str(e))
    finally:
        if tmp:
            req = urllib.request.Request(base + "/api/trades/%d" % tmp, method="DELETE")
            try:
                with urllib.request.urlopen(req, timeout=20):
                    pass
            except urllib.error.URLError:
                pass


# ======================================================================= main
def main():
    live = "--live" in sys.argv
    api = "--api" in sys.argv
    t0 = dt.datetime.now()
    test_normal()
    test_pricing()
    test_iv()
    test_greeks()
    test_probability()
    test_vol_estimators()
    test_smile()
    test_term_structure()
    test_parity()
    test_chain_metrics()
    test_positions()
    test_density_and_ev()
    test_horizon_evaluation()
    test_targets()
    test_fills()
    test_calendar()
    test_technicals()
    test_sentiment()
    test_vol_calibration()
    test_board_construction()
    test_settlement_and_pop_calibration()
    test_scanner()
    test_db()
    if live:
        test_live()
    if api:
        test_api()
    dur = (dt.datetime.now() - t0).total_seconds()
    print("\n%s  %d passed, %d failed  (%.1fs)" %
          ("ALL GREEN" if not FAIL else "FAILURES", PASS, FAIL, dur))
    if FAIL:
        print("\nfailed checks:")
        for f in FAILURES:
            print("  - " + f)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
