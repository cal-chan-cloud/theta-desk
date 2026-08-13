"""Chain analytics: per-contract enrichment, per-expiry metrics, symbol rollup.

Pipeline for one symbol
-----------------------
    raw chain
      -> group by expiry
      -> recover forward F and discount df from put-call parity   (parity.py)
      -> solve our own IV per contract off (F, df)                (bs.py)
      -> fit the smile, weighted by vega x liquidity              (vol.py)
      -> analytic greeks at the contract's own IV                 (bs.py)
      -> expiry metrics: max pain, GEX, walls, expected move, PCR
      -> symbol rollup: constant-maturity IV, term slope, gamma profile

Deliberately *not* using the vendor's greeks for anything but a cross-check:
they are computed against an unknown forward, so mixing them with ours would
make the book's aggregate delta internally inconsistent.
"""

import math

import config
import marketcal
from . import bs, parity, vol


# ============================================================ per-contract
def liquidity_score(bid, ask, mid, volume, oi):
    """0-100.  Weighted toward spread, because spread is what you actually pay.

    Open interest tells you a market exists; volume tells you it traded today;
    the spread tells you the cost of being wrong about either.
    """
    if mid is None or mid <= 0 or bid is None or ask is None:
        return 0.0
    spread_pct = (ask - bid) / mid
    if spread_pct <= config.LIQ_GOOD_SPREAD_PCT:
        s_score = 100.0
    elif spread_pct >= config.LIQ_BAD_SPREAD_PCT:
        s_score = 0.0
    else:
        span = config.LIQ_BAD_SPREAD_PCT - config.LIQ_GOOD_SPREAD_PCT
        s_score = 100.0 * (config.LIQ_BAD_SPREAD_PCT - spread_pct) / span
    oi_score = 100.0 * min(math.log1p(max(oi, 0)) / math.log1p(config.LIQ_GOOD_OI), 1.0)
    v_score = 100.0 * min(math.log1p(max(volume, 0)) / math.log1p(config.LIQ_GOOD_VOLUME), 1.0)
    return round(0.55 * s_score + 0.28 * oi_score + 0.17 * v_score, 1)


def _usable_for_fit(c, F, T, df):
    """Is this quote clean enough to inform the volatility surface?"""
    mid = c.get("mid")
    if mid is None or mid <= 0 or T <= 0:
        return False
    if (c.get("bid") or 0) < config.QUOTE_MIN_BID:
        return False
    spread = (c.get("ask") or 0) - (c.get("bid") or 0)
    if spread > config.QUOTE_MAX_SPREAD_ABS:
        return False
    if mid > 0 and spread / mid > config.QUOTE_MAX_SPREAD_PCT:
        return False
    lo, hi = bs.bounds(F, c["strike"], T, df, c["right"])
    return lo + 1e-9 < mid < hi - 1e-9


def analyze_expiry(symbol, expiry, contracts, spot, curve, now=None, is_index=False):
    """Everything derivable from one expiry's strip."""
    now = now or marketcal.now_utc()
    root = contracts[0].get("root") if contracts else None
    am = is_index and root and not root.endswith("W") and marketcal.is_monthly_expiry(expiry)
    T = marketcal.year_fraction(expiry, now, am_settled=am)
    T_vol = marketcal.vol_year_fraction(expiry, now)
    dte = marketcal.dte(expiry, now)

    # ---- forward / discount from parity ---------------------------------
    by_strike = {}
    for c in contracts:
        d = by_strike.setdefault(c["strike"], {})
        d[c["right"]] = c
    pairs = []
    for k, d in by_strike.items():
        cc, pp = d.get("C"), d.get("P")
        if not cc or not pp:
            continue
        pairs.append({
            "strike": k, "call_mid": cc.get("mid"), "put_mid": pp.get("mid"),
            "call_spread": cc.get("spread_pct"), "put_spread": pp.get("spread_pct"),
            "call_oi": cc.get("open_interest"), "put_oi": pp.get("open_interest"),
        })
    fwd = parity.implied_forward(pairs, spot, T, curve)
    F, df = fwd["forward"], fwd["df"]

    # ---- per-contract IV -------------------------------------------------
    # Only OTM/ATM quotes inform the surface.  A deep-ITM option is almost all
    # intrinsic: its extrinsic value is smaller than its own bid-ask spread, so
    # inverting its price for vol amplifies quote noise enormously.  Live AAPL
    # showed a 110-strike call (spot 303) solving to 136% vol.  Market makers
    # quote those off their OTM twin via parity anyway, so the OTM strip carries
    # all the information and none of the noise.
    fit_points = []
    for c in contracts:
        c["dte"] = dte
        c["t_years"] = T
        c["moneyness"] = math.log(c["strike"] / F) if F > 0 and c["strike"] > 0 else None
        c["intrinsic"] = bs.intrinsic(spot, c["strike"], c["right"])
        c["extrinsic"] = (c["mid"] - c["intrinsic"]) if c.get("mid") is not None else None
        c["liquidity"] = liquidity_score(c.get("bid"), c.get("ask"), c.get("mid"),
                                         c.get("volume") or 0, c.get("open_interest") or 0)
        c["vol_oi"] = ((c.get("volume") or 0) / c["open_interest"]) if c.get("open_interest") else None
        c["iv_quote"] = None
        if _usable_for_fit(c, F, T, df):
            iv = bs.implied_vol(c["mid"], F, c["strike"], T, df, c["right"])
            if iv and config.MIN_IV <= iv <= config.MAX_IV:
                c["iv_quote"] = iv
                is_otm = (c["strike"] >= F * 0.98) if c["right"] == "C" else (c["strike"] <= F * 1.02)
                if is_otm:
                    # Weight by vega (information content) x quote quality.
                    v = bs.vega_raw(F, c["strike"], T, iv, df)
                    w = v * (0.2 + c["liquidity"] / 100.0)
                    if w > 1e-9:
                        fit_points.append((c["strike"], iv, w))

    smile = vol.fit_smile(fit_points, F, T)

    # Every contract gets the *surface* vol, so the strip's greeks are mutually
    # consistent (a book's aggregate delta means nothing if each leg is marked
    # on its own noisy vol).  The contract's own solved vol is kept alongside.
    for c in contracts:
        if smile is not None:
            c["iv"] = smile.iv(c["strike"])
            if c["iv_quote"] is not None and smile.rmse > 0:
                if abs(c["iv_quote"] - c["iv"]) > max(6 * smile.rmse, 0.12):
                    c["iv_outlier"] = True
        else:
            c["iv"] = c["iv_quote"] or c.get("iv_src")
        g = bs.greeks(spot, F, c["strike"], T, c["iv"] or 0.0, df, c["right"])
        for k in ("delta", "gamma", "theta", "vega", "rho", "vanna", "vomma", "charm"):
            c[k] = g[k]

    # ---- ATM / expected move --------------------------------------------
    atm_iv = smile.atm_iv if smile else None
    otm = []
    for c in contracts:
        if c.get("mid") and c["mid"] > 0 and _usable_for_fit(c, F, T, df):
            otm.append((c["strike"], c["right"], c["mid"]))
    mfiv = vol.model_free_iv(otm, F, T, df, atm_hint=atm_iv)

    sigma_em = mfiv or atm_iv
    em_lo = em_hi = em = None
    if sigma_em:
        em_lo, em_hi, em = bs.expected_move(spot, sigma_em, T_vol, 1.0)

    straddle = None
    if atm_iv:
        straddle = (bs.black76(F, F, T, atm_iv, df, "C") + bs.black76(F, F, T, atm_iv, df, "P"))

    skew = vol.skew_metrics(smile, F, T, df, spot)
    arb = vol.butterfly_arbitrage_flags(smile, F, T, df) if smile else ["no-smile"]

    # ---- flow / positioning ---------------------------------------------
    call_v = sum(c.get("volume") or 0 for c in contracts if c["right"] == "C")
    put_v = sum(c.get("volume") or 0 for c in contracts if c["right"] == "P")
    call_oi = sum(c.get("open_interest") or 0 for c in contracts if c["right"] == "C")
    put_oi = sum(c.get("open_interest") or 0 for c in contracts if c["right"] == "P")

    gex = 0.0
    for c in contracts:
        oi = c.get("open_interest") or 0
        if oi <= 0 or not c.get("gamma"):
            continue
        sign = 1.0 if c["right"] == "C" else -1.0
        gex += sign * c["gamma"] * oi * 100.0 * spot * spot * 0.01

    strikes = sorted(by_strike)
    max_pain = _max_pain(by_strike, strikes)
    call_wall = _wall(contracts, "C", spot)
    put_wall = _wall(contracts, "P", spot)

    return {
        "expiry": expiry.isoformat(),
        "dte": dte, "t_years": T, "t_vol": T_vol,
        "am_settled": bool(am),
        "forward": F, "discount": df, "rate": fwd["rate"], "div_yield": fwd["div_yield"],
        "parity_r2": fwd["r2"], "parity_n": fwd["n"], "parity_method": fwd["method"],
        "n_quotes": smile.n if smile else 0,
        "n_contracts": len(contracts),
        "atm_iv": atm_iv, "mfiv": mfiv,
        "smile": smile.to_dict() if smile else None,
        "smile_obj": smile,
        "smile_rmse": smile.rmse if smile else None,
        "skew_25": skew["skew_25"], "rr_25": skew["rr_25"], "fly_25": skew["fly_25"],
        "iv_25p": skew["iv_25p"], "iv_25c": skew["iv_25c"],
        "slope_atm": skew["slope_atm"],
        "expected_move": em, "em_pct": (em / spot if em and spot else None),
        "em_low": em_lo, "em_high": em_hi,
        "straddle": straddle,
        "straddle_pct": (straddle / spot if straddle and spot else None),
        "max_pain": max_pain,
        "gex": gex,
        "call_wall": call_wall, "put_wall": put_wall,
        "pcr_vol": (put_v / call_v) if call_v > 0 else None,
        "pcr_oi": (put_oi / call_oi) if call_oi > 0 else None,
        "call_volume": call_v, "put_volume": put_v,
        "call_oi": call_oi, "put_oi": put_oi,
        "total_volume": call_v + put_v, "total_oi": call_oi + put_oi,
        "arb_flags": ",".join(arb) if arb else "",
        "strikes": strikes,
    }


def _max_pain(by_strike, strikes):
    """Strike that minimises total intrinsic value paid out to option holders.

    Interpreted carefully: it is a description of where open interest is
    concentrated, not a prediction.  It only matters when OI is large relative
    to the underlying's float turnover, which is why we also surface OI walls.
    """
    if not strikes:
        return None
    best, best_pain = None, None
    for settle in strikes:
        pain = 0.0
        for k, d in by_strike.items():
            cc, pp = d.get("C"), d.get("P")
            if cc and settle > k:
                pain += (settle - k) * (cc.get("open_interest") or 0)
            if pp and settle < k:
                pain += (k - settle) * (pp.get("open_interest") or 0)
        if best_pain is None or pain < best_pain:
            best, best_pain = settle, pain
    return best


def _wall(contracts, right, spot, band=0.25):
    """Largest open-interest strike on one side, within +/-band of spot."""
    best, best_oi = None, 0.0
    for c in contracts:
        if c["right"] != right:
            continue
        if not (spot * (1 - band) <= c["strike"] <= spot * (1 + band)):
            continue
        oi = c.get("open_interest") or 0
        if oi > best_oi:
            best, best_oi = c["strike"], oi
    return best


# ============================================================ symbol rollup
def gamma_profile(contracts_by_expiry, spot, curve, now=None, span=0.12, points=41,
                  max_dte=120):
    """Net dealer gamma as a function of spot -- and the zero-gamma flip level.

    Re-prices gamma at each candidate spot rather than shifting a single number,
    because gamma itself moves fast with spot; the flip level computed the lazy
    way can be off by several percent.
    """
    now = now or marketcal.now_utc()
    lo, hi = spot * (1 - span), spot * (1 + span)
    step = (hi - lo) / (points - 1)
    grid, curve_pts = [lo + i * step for i in range(points)], []

    live = []
    for exp_iso, info in contracts_by_expiry.items():
        if info["dte"] > max_dte or info["dte"] <= 0:
            continue
        F0, df, T = info["forward"], info["discount"], info["t_years"]
        for c in info["contracts"]:
            oi = c.get("open_interest") or 0
            if oi < 10 or not c.get("iv"):
                continue
            if not (lo * 0.75 <= c["strike"] <= hi * 1.25):
                continue
            live.append((c["strike"], c["right"], c["iv"], oi, F0, df, T))
    # SPY-scale chains can put 10k contracts in this loop; the tail carries a
    # negligible share of gamma, so keep the largest positions and move on.
    if len(live) > 3000:
        live.sort(key=lambda x: x[3], reverse=True)
        live = live[:3000]

    for S in grid:
        total = 0.0
        for K, right, iv, oi, F0, df, T in live:
            F = F0 * (S / spot)                    # forward moves with spot
            g = bs.greeks(S, F, K, T, iv, df, right)
            sign = 1.0 if right == "C" else -1.0
            total += sign * g["gamma"] * oi * 100.0 * S * S * 0.01
        curve_pts.append({"spot": S, "gex": total})

    flip = None
    for i in range(1, len(curve_pts)):
        a, b = curve_pts[i - 1], curve_pts[i]
        if a["gex"] == 0:
            flip = a["spot"]
            break
        if a["gex"] * b["gex"] < 0:
            w = abs(a["gex"]) / (abs(a["gex"]) + abs(b["gex"]))
            flip = a["spot"] + w * (b["spot"] - a["spot"])
            break
    return {"curve": curve_pts, "flip": flip,
            "gex_at_spot": _interp(curve_pts, spot), "n_contracts": len(live)}


def _interp(pts, x):
    if not pts:
        return None
    if x <= pts[0]["spot"]:
        return pts[0]["gex"]
    if x >= pts[-1]["spot"]:
        return pts[-1]["gex"]
    for i in range(1, len(pts)):
        if x <= pts[i]["spot"]:
            a, b = pts[i - 1], pts[i]
            w = (x - a["spot"]) / (b["spot"] - a["spot"])
            return a["gex"] + w * (b["gex"] - a["gex"])
    return pts[-1]["gex"]


def reference_expiry(per_expiry, target_days=30, min_dte=5):
    """The expiry whose positioning metrics are worth quoting at symbol level.

    Max pain and open-interest walls off the *front* expiry are close to
    meaningless on names that list a contract every day: at 0 DTE the strip is
    a handful of expiring strikes, so the dashboard would headline a "max pain"
    that describes nothing.  Prefer the expiry nearest one month out, which is
    where open interest actually concentrates.
    """
    if not per_expiry:
        return None
    live = [m for m in per_expiry if (m.get("dte") or 0) >= min_dte]
    pool = live or per_expiry
    return min(pool, key=lambda m: abs((m.get("dte") or 0) - target_days))


def unusual_contracts(contracts, spot, top=15, min_volume=100):
    """Contracts trading far above their own open interest.

    volume > open interest means most of today's prints are *opening* trades:
    someone is putting on a position, not closing one.  Filtered by dollar
    volume so a 500-lot in a 5-cent option does not outrank a real position.
    """
    rows = []
    session = marketcal.session_date()
    for c in contracts:
        v = c.get("volume") or 0
        oi = c.get("open_interest") or 0
        mid = c.get("mid")
        if v < min_volume or not mid or mid <= 0:
            continue
        ratio = v / oi if oi > 0 else (v / 10.0)
        dollars = v * mid * 100.0
        if dollars < 25000:
            continue
        score = math.log1p(dollars / 1000.0) * min(ratio, 12.0) ** 0.5
        rows.append({
            "occ": c["occ"], "expiry": c["expiry"].isoformat() if hasattr(c["expiry"], "isoformat") else c["expiry"],
            "right": c["right"], "strike": c["strike"], "dte": round(c.get("dte") or 0, 1),
            "volume": v, "open_interest": oi, "vol_oi": round(ratio, 2),
            "mid": mid, "dollar_volume": dollars,
            "iv": c.get("iv"), "delta": c.get("delta"),
            "moneyness_pct": (c["strike"] / spot - 1.0) * 100.0 if spot else None,
            "score": score,
            "side_hint": _side_hint(c, session),
        })
    rows.sort(key=lambda r: r["score"], reverse=True)
    return rows[:top]


def aggregate_greeks(per_expiry):
    """Open-interest-weighted greeks for the whole listed book, per expiry.

    Answers "how much delta/gamma/vega is actually outstanding here", which is
    the number that tells you whether the positioning metrics above are worth
    anything on this name at all.
    """
    out = []
    for m in per_expiry:
        agg = {"expiry": m["expiry"], "dte": m.get("dte"),
               "delta": 0.0, "gamma": 0.0, "vega": 0.0, "theta": 0.0,
               "call_delta": 0.0, "put_delta": 0.0}
        for c in m.get("contracts") or []:
            oi = c.get("open_interest") or 0
            if oi <= 0:
                continue
            for k in ("delta", "gamma", "vega", "theta"):
                if c.get(k) is not None:
                    agg[k] += c[k] * oi * 100.0
            if c.get("delta") is not None:
                agg["call_delta" if c["right"] == "C" else "put_delta"] += c["delta"] * oi * 100.0
        out.append(agg)
    return out


def _side_hint(c, session=None):
    """Where did the last print land in the spread? Crude but informative.

    Only meaningful if the print happened *today*.  An illiquid contract can
    carry a last price from days ago, and reading it against today's bid/ask
    produces a confident "at ask" on a trade that never happened in this
    session -- precisely the row an unusual-activity screen must not invent.
    """
    bid, ask, last = c.get("bid"), c.get("ask"), c.get("last")
    if not all(v is not None for v in (bid, ask, last)) or ask <= bid:
        return None
    session = session or marketcal.session_date()
    lt = c.get("last_trade_time")
    if not lt or str(lt)[:10] != session.isoformat():
        return None
    pos = (last - bid) / (ask - bid)
    if pos >= 0.85:
        return "at ask"
    if pos >= 0.60:
        return "above mid"
    if pos <= 0.15:
        return "at bid"
    if pos <= 0.40:
        return "below mid"
    return "mid"


TENOR_LADDER = (14, 21, 30, 45, 60, 90, 120, 180, 270, 365, 545, 730)


def select_expiries(all_expiries, now=None, near_days=10, max_near=8, max_total=18):
    """Pick a tenor ladder rather than the first N expiries.

    SPY lists an expiry almost every trading day, so "first 14" covers barely
    two weeks -- the 30/60/90-day constant-maturity IVs then all interpolate to
    the same number and the term-structure slope reads flat.  Taking the near
    expiries plus the closest listed expiry to each rung of a tenor ladder keeps
    the curve honest for both a 0-DTE name and a quarterly-only name.
    """
    now = now or marketcal.now_utc()
    dted = [(e, marketcal.dte(e, now)) for e in all_expiries]
    dted = [(e, d) for e, d in dted if d > -0.5]
    if not dted:
        return []
    keep = {e for e, d in dted[:max_near] if d <= near_days}
    for target in TENOR_LADDER:
        best = min(dted, key=lambda x: abs(x[1] - target))
        if abs(best[1] - target) <= max(target * 0.7, 10):
            keep.add(best[0])
    out = sorted(keep)
    if len(out) > max_total:                      # keep the near ones + spread
        near = [e for e in out if marketcal.dte(e, now) <= near_days][:max_near]
        far = [e for e in out if e not in near]
        stride = max(1, len(far) // max(max_total - len(near), 1))
        out = sorted(set(near) | set(far[::stride]))[:max_total]
    return out


def rollup(symbol, chain, curve, now=None, max_expiries=18):
    """Analyse a tenor ladder of expiries and aggregate to symbol level."""
    now = now or marketcal.now_utc()
    spot = chain["spot"]
    groups = {}
    for c in chain["contracts"]:
        groups.setdefault(c["expiry"], []).append(c)

    expiries = select_expiries(sorted(groups), now=now, max_total=max_expiries)
    per_expiry, by_iso = [], {}
    for e in expiries:
        cs = groups[e]
        if len(cs) < 4:
            continue
        m = analyze_expiry(symbol, e, cs, spot, curve, now=now, is_index=chain.get("is_index"))
        m["contracts"] = cs
        per_expiry.append(m)
        by_iso[m["expiry"]] = m

    if not per_expiry:
        return None

    iv_points = [(m["t_years"], m["mfiv"] or m["atm_iv"]) for m in per_expiry]
    iv30 = vol.interpolate_iv_at_days(iv_points, 30)
    iv60 = vol.interpolate_iv_at_days(iv_points, 60)
    iv90 = vol.interpolate_iv_at_days(iv_points, 90)
    term_slope = vol.term_structure_slope(iv_points)

    profile = gamma_profile(by_iso, spot, curve, now=now)

    all_contracts = [c for m in per_expiry for c in m["contracts"]]
    tot_v = sum(c.get("volume") or 0 for c in all_contracts)
    tot_oi = sum(c.get("open_interest") or 0 for c in all_contracts)
    call_v = sum(c.get("volume") or 0 for c in all_contracts if c["right"] == "C")
    put_v = sum(c.get("volume") or 0 for c in all_contracts if c["right"] == "P")
    call_oi = sum(c.get("open_interest") or 0 for c in all_contracts if c["right"] == "C")
    put_oi = sum(c.get("open_interest") or 0 for c in all_contracts if c["right"] == "P")
    dollar_v = sum((c.get("volume") or 0) * (c.get("mid") or 0) * 100.0 for c in all_contracts)

    # Weight skew toward the 30-day tenor: that is where the trades live.
    skew30 = _tenor_weighted(per_expiry, "skew_25", 30)
    ref = reference_expiry(per_expiry) or per_expiry[0]

    return {
        "reference_expiry": ref["expiry"],
        "call_wall": ref.get("call_wall"), "put_wall": ref.get("put_wall"),
        "symbol": symbol,
        "spot": spot,
        "expiries": per_expiry,
        "by_expiry": by_iso,
        "iv30": iv30, "iv60": iv60, "iv90": iv90,
        "term_slope": term_slope,
        "skew_25": skew30,
        "gamma_profile": profile,
        "gex_total": profile["gex_at_spot"],
        "gamma_flip": profile["flip"],
        "max_pain": ref["max_pain"],
        "opt_volume": tot_v, "opt_oi": tot_oi,
        "call_volume": call_v, "put_volume": put_v,
        "call_oi": call_oi, "put_oi": put_oi,
        "opt_dollar_volume": dollar_v,
        "pcr_vol": (put_v / call_v) if call_v > 0 else None,
        "pcr_oi": (put_oi / call_oi) if call_oi > 0 else None,
        "unusual": unusual_contracts(all_contracts, spot),
        "oi_greeks": aggregate_greeks(per_expiry),
        "n_contracts": len(all_contracts),
    }


def _tenor_weighted(per_expiry, key, target_days):
    """Gaussian-weighted average of a per-expiry metric around a target tenor."""
    num = den = 0.0
    for m in per_expiry:
        v = m.get(key)
        if v is None or not m.get("dte"):
            continue
        w = math.exp(-((math.log(max(m["dte"], 1) / target_days)) ** 2) / (2 * 0.55 ** 2))
        num += w * v
        den += w
    return num / den if den > 0 else None
