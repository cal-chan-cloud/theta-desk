"""Daily trade generation: regime -> candidate structures -> ranked ideas.

The scanner is deliberately opinionated about *why* a structure is proposed.
Every idea carries the three-way regime read (trend / vol / event) that produced
it, and its score is the weighted agreement between what the structure needs to
work and what the data actually says.  A bull put spread proposed on a name with
cheap IV is a worse idea than the same spread on a name with rich IV even if the
raw expectancy looks similar, because the expectancy estimate itself is less
trustworthy when it depends on a vol edge that is not there.
"""

import math

import config
import marketcal
from . import bs, strategies, vol as volmod


# ------------------------------------------------------------------ regimes
def vol_regime(iv30, forecast, iv_rank, pctile=None):
    """Rich / fair / cheap, measured against the NORMAL variance risk premium.

    Implied vol exceeding forecast realised vol is not an edge -- it is the
    resting state of the options market.  Sellers are paid a premium for
    carrying variance risk, historically around 12% for liquid US equity
    underlyings (config.TYPICAL_VRP_RATIO).  A name where IV sits exactly that
    far above forecast is *fairly* priced, not rich.

    This matters more since the forecast was recalibrated.  The old thresholds
    were tuned against a forecast biased 12% low, so they had the typical
    premium silently baked in; correcting the forecast without re-centring the
    thresholds flipped the whole board from 68% credit to 68% debit on one
    constant -- top ideas became 27%-POP long calls overnight.  Comparing the
    excess over the normal premium makes the classification invariant to the
    level of the forecast, which is what it should always have been.
    """
    edge = None
    if iv30 and forecast and forecast > 0:
        edge = (iv30 - forecast) / forecast
    votes = 0
    if edge is not None:
        excess = edge - (config.TYPICAL_VRP_RATIO - 1.0)
        # CROSS-SECTIONAL first, absolute only as a fallback.
        #
        # Absolute thresholds are fragile to anything that moves the whole
        # cross-section at once.  Recalibrating the vol forecast upward by 13.5%
        # -- a change that is right on its own terms and improved forecast RMSE
        # by 13% -- shifted every name's edge down together and flipped the board
        # from 68% credit to 88% debit, with 27%-POP long calls at the top.  That
        # is a threshold artefact, not a view.
        #
        # Percentile within today's watchlist is immune to it: a name is rich
        # because its premium is rich *relative to what else is on offer today*,
        # which is how relative value actually works, and it guarantees the board
        # cannot become a single one-way bet just because a constant moved.
        if pctile is not None:
            if pctile >= 80:
                votes += 2
            elif pctile >= 62:
                votes += 1
            elif pctile <= 20:
                votes -= 2
            elif pctile <= 38:
                votes -= 1
        else:
            if excess > 0.12:
                votes += 2
            elif excess > 0.04:
                votes += 1
            elif excess < -0.14:
                votes -= 2
            elif excess < -0.05:
                votes -= 1
    if iv_rank is not None:
        if iv_rank > 70:
            votes += 1
        elif iv_rank < 25:
            votes -= 1
    if votes >= 2:
        label = "rich"
    elif votes <= -2:
        label = "cheap"
    else:
        label = "fair"
    return {"label": label, "edge": edge, "votes": votes, "pctile": pctile,
            "excess": (edge - (config.TYPICAL_VRP_RATIO - 1.0)) if edge is not None else None}


def trend_regime(trend_score):
    if trend_score is None:
        return "neutral"
    if trend_score >= 0.30:
        return "bullish"
    if trend_score <= -0.30:
        return "bearish"
    return "neutral"


STRATEGY_MATRIX = {
    ("bullish", "rich"): ["bull_put_spread", "cash_secured_put", "covered_call",
                          "bull_call_spread"],
    ("bullish", "fair"): ["bull_call_spread", "bull_put_spread"],
    ("bullish", "cheap"): ["long_call", "bull_call_spread"],
    ("bearish", "rich"): ["bear_call_spread", "bear_put_spread"],
    ("bearish", "fair"): ["bear_put_spread", "bear_call_spread"],
    ("bearish", "cheap"): ["long_put", "bear_put_spread"],
    ("neutral", "rich"): ["iron_condor", "iron_butterfly", "short_strangle"],
    ("neutral", "fair"): ["iron_condor"],
    ("neutral", "cheap"): ["long_strangle", "long_straddle"],
}

# Structures whose thesis is the event itself
EVENT_STRATEGIES = ["long_straddle", "long_strangle"]


def _norm(x, scale):
    """Map an unbounded quantity to [-1, 1] smoothly."""
    if x is None:
        return 0.0
    return math.tanh(x / scale)


def pick_expiries(expiries, earnings_date=None, min_dte=None, max_dte=None,
                  preferred=None, limit=3):
    """Choose expiries to trade: the preferred band, plus the earnings expiry."""
    min_dte = min_dte if min_dte is not None else config.SCAN_MIN_DTE
    max_dte = max_dte if max_dte is not None else config.SCAN_MAX_DTE
    preferred = preferred or config.SCAN_PREFERRED_DTE
    cands = [e for e in expiries if min_dte <= (e.get("dte") or 0) <= max_dte]
    if not cands:
        return []
    mid = 0.5 * (preferred[0] + preferred[1])
    cands.sort(key=lambda e: abs(e["dte"] - mid))
    chosen = cands[:limit]
    if earnings_date:
        after = [e for e in expiries
                 if marketcal.parse_date(e["expiry"]) > earnings_date
                 and (e.get("dte") or 0) <= max_dte]
        if after:
            first = min(after, key=lambda e: e["dte"])
            if first not in chosen:
                chosen.append(first)
    return chosen


def build_ideas(symbol, roll, metrics, now=None, max_per_symbol=None):
    """Generate scored ideas for one symbol.

    `roll`    : model.chain.rollup output
    `metrics` : the symbol-level dict assembled by the pipeline (trend, vol,
                news, earnings, structure)
    """
    now = now or marketcal.now_utc()
    max_per_symbol = max_per_symbol or config.SCAN_MAX_PER_TICKER
    spot = roll["spot"]

    forecast = metrics.get("vol_forecast")
    iv30 = metrics.get("iv30") or roll.get("iv30")
    vr = vol_regime(iv30, forecast, metrics.get("iv_rank"),
                    pctile=metrics.get("vol_edge_pctile"))
    tr = trend_regime(metrics.get("trend_score"))
    earnings_date = marketcal.parse_date(metrics["earnings_date"]) if metrics.get("earnings_date") else None
    dte_earn = metrics.get("days_to_earnings")

    strat_list = list(STRATEGY_MATRIX.get((tr, vr["label"]), ["iron_condor"]))
    # An imminent print with cheap front-month vol is the classic long-gamma
    # setup; an imminent print with rich vol is the classic premium sale that
    # you must be out of, or deliberately in, before the crush.
    if dte_earn is not None and 0 <= dte_earn <= 21:
        if vr["label"] == "cheap":
            for s in EVENT_STRATEGIES:
                if s not in strat_list:
                    strat_list.insert(0, s)

    # ---- P-measure parameters -------------------------------------------
    sigma_base = forecast or iv30 or 0.3
    jump = metrics.get("implied_earnings_move")
    r = 0.0
    for m in roll["expiries"]:
        if m.get("rate"):
            r = m["rate"]
            break
    trend_score = metrics.get("trend_score") or 0.0
    news_score = metrics.get("news_score") or 0.0
    # The drift tilt is an ALPHA CLAIM: any deviation from the risk-neutral
    # r - q says we know which way the stock goes.  It also used to be counted
    # twice -- once here (tilting the density, which raises EV, which feeds
    # comp["edge"]) and again as comp["trend"].  A bullish name therefore got
    # paid for its trend in two separate score components, which is a large
    # part of why boards ran 15-20 bullish of 25.  The tilt is now shrunk hard
    # and the directional view is carried by comp["trend"] alone.
    tilt = config.DRIFT_TILT_SHARPE * trend_score + config.DRIFT_NEWS_WEIGHT * news_score
    lim = config.DRIFT_TILT_SHARPE + config.DRIFT_NEWS_WEIGHT
    tilt = max(min(tilt, lim), -lim)

    today = marketcal.session_date()
    expiries = pick_expiries(roll["expiries"], earnings_date)
    ideas = []
    for exp in expiries:
        if not exp.get("smile_obj"):
            continue
        contracts = exp.get("contracts") or []
        tradeable = [c for c in contracts
                     if c.get("mid") and (c.get("open_interest") or 0) >= config.MIN_TRADEABLE_OI
                     and (c.get("liquidity") or 0) >= 25]
        if len(tradeable) < 8:
            continue
        dens_q = strategies.risk_neutral_density(
            exp["smile_obj"], exp["forward"], exp["t_years"], exp["discount"])
        if not dens_q:
            continue

        # The forecast is diffusive vol.  If a print lands inside this expiry,
        # add the jump the market itself is pricing -- otherwise every
        # earnings-month option looks rich and every calendar looks free.
        exp_date = marketcal.parse_date(exp["expiry"])
        has_event = bool(earnings_date and today <= earnings_date <= exp_date)
        sigma_exp = volmod.add_event_jump(sigma_base, jump, exp["t_vol"]) if has_event else sigma_base
        drift_p = r + tilt * sigma_exp

        dens_p = strategies.transform_density(dens_q, spot, exp["t_vol"], sigma_exp, drift_p)

        for strat in strat_list:
            pos = strategies.build(strat, symbol, exp, tradeable, spot)
            if pos is None:
                continue
            idea = score_idea(pos, exp, roll, metrics, dens_q, dens_p,
                              vr, tr, sigma_exp, drift_p, now)
            if idea:
                idea["has_event"] = has_event
                idea["event_jump"] = jump if has_event else None
                ideas.append(idea)

    ideas.sort(key=lambda i: -i["score"])
    # Keep one instance of each structure per symbol -- three variants of the
    # same condor is not diversification, it is one idea listed three times.
    seen, out = set(), []
    for i in ideas:
        if i["strategy"] in seen:
            continue
        seen.add(i["strategy"])
        out.append(i)
        if len(out) >= max_per_symbol:
            break
    return out


NAKED_OK = {"short_strangle", "cash_secured_put", "covered_call"}


def calibrate_pop(pop, is_credit):
    """Shrink the modelled POP toward what that family actually delivered.

    See config.POP_CALIBRATION for the measurement.  Credit barely moves (it was
    already honest at -5 points); debit is cut hard, because a stated 38% turned
    into a realised 14% across 84 resolved ideas.
    """
    if pop is None or not getattr(config, "POP_CALIBRATION", False):
        return pop
    f = config.POP_CALIB_FACTOR["credit" if is_credit else "debit"]
    w = config.POP_CALIB_WEIGHT
    out = pop * ((1.0 - w) + w * f)
    return max(0.0, min(1.0, out))


def score_idea(pos, exp, roll, metrics, dens_q, dens_p, vr, tr, sigma_p, drift_p, now):
    spot = roll["spot"]
    max_p, max_l, ub_profit, ub_loss = pos.extremes()
    # Unbounded *profit* is a feature (long calls, straddles); unbounded *loss*
    # is what needs permission.
    if ub_loss and pos.strategy not in NAKED_OK:
        return None
    if max_l is not None and max_l >= 0:
        return None

    entry = pos.net_price
    if abs(entry) < 0.02:
        return None
    fees = config.COMMISSION_PER_CONTRACT * len(pos.legs) / 100.0   # per share

    # Expectancy at the horizon we actually trade, not at expiry.  See
    # config.EVAL_AT_HORIZON for the attribution that motivated this.
    horizon_moment, t_h = strategies.horizon_for(pos, exp["t_vol"], now)
    dens_ph = strategies.transform_density(dens_q, spot, t_h, sigma_p, drift_p)         if config.EVAL_AT_HORIZON else dens_p
    ev_q = strategies.evaluate(pos, dens_q, fees_per_spread=fees)
    ev_p = strategies.evaluate(pos, dens_ph, moment=horizon_moment, fees_per_spread=fees)
    # Expiry expectancy is kept alongside so the two horizons stay comparable.
    ev_expiry = strategies.evaluate(pos, dens_p, fees_per_spread=fees)
    if not ev_p:
        return None

    # POP is an EXPIRY statistic, deliberately, even though expectancy is now
    # measured at the horizon actually traded.  The two answer different
    # questions and must not be mixed: EV@horizon captures the carry you really
    # pay, while "probability of profit" is what was calibrated against 152
    # trades that were settled AT their expiry close.  Applying a calibration
    # fitted on expiry outcomes to a 10-day-horizon probability would be
    # comparing two different quantities.
    pop_for_rule = (ev_expiry.get("pop") if ev_expiry else None) or ev_p["pop"]
    pop_cal = calibrate_pop(pop_for_rule, pos.is_credit)

    # Where max loss is undefined, size and score against the 5% tail instead.
    risk = abs(max_l) if max_l is not None else abs(min(ev_p["p05"], -0.01))
    ev_per_risk = ev_p["ev"] / risk if risk > 0 else 0.0
    g = pos.greeks(spot, now)

    # ---- component scores, each in [-1, 1] -------------------------------
    comp = {}
    # Expectancy per dollar of risk.  With the drift tilt shrunk, this is now
    # close to a pure *volatility* edge -- how much the structure is worth when
    # the market's own density is re-scaled to our forecast of realised vol --
    # rather than a directional bet wearing an expectancy costume.
    # Expectancy per dollar of risk, blended with RISK-ADJUSTED expectancy.
    #
    # Raw EV/risk treats a lottery ticket and a grinder as equivalent when their
    # means match, and the long calls that lost 100% of the time scored well on
    # exactly that basis.  ev/sd penalises the dispersion the mean hides, which
    # is ordinary portfolio theory rather than a fit to last week.
    sharpe = ev_p.get("sharpe")
    comp["edge"] = 0.6 * _norm(ev_per_risk, 0.12) + 0.4 * _norm(sharpe, 0.30)

    # Does the structure's vega sign agree with the vol read?
    vega_sign = 1.0 if g["vega"] > 0 else (-1.0 if g["vega"] < 0 else 0.0)
    # Excess over the normal variance premium, for the same reason: being paid
    # the going rate for variance is not an edge in either direction.
    edge_pct = vr.get("excess") if vr.get("excess") is not None else 0.0
    comp["vol_edge"] = _norm(-vega_sign * edge_pct, 0.20)

    # Does the structure's delta sign agree with the trend?
    delta_sign = 1.0 if g["delta"] > 0.02 else (-1.0 if g["delta"] < -0.02 else 0.0)
    ts = metrics.get("trend_score") or 0.0
    comp["trend"] = delta_sign * ts if delta_sign else (0.35 * (1.0 - min(abs(ts) / 0.5, 1.0)))

    liq = sum(l.liquidity or 0 for l in pos.legs) / len(pos.legs)
    comp["liquidity"] = (liq - 45.0) / 45.0

    ns = metrics.get("news_score") or 0.0
    conf = metrics.get("news_confidence") or 0.0
    comp["news"] = delta_sign * ns * conf if delta_sign else 0.0

    comp["structure"] = _structure_score(pos, roll, metrics, spot, exp)

    # Calibrated probability of profit, centred so a coin flip scores zero.
    # POP was previously used only as a pass/fail floor and never as a ranking
    # signal, which is a large part of why the score came out ANTI-predictive
    # (corr -0.137 with return on risk across 152 resolved ideas; the 82+ bucket
    # won 0 of 8).  The score rewarded modelled expectancy, and modelled
    # expectancy was highest exactly where the probability was most overstated.
    comp["pop"] = max(-1.0, min(1.0, (pop_cal - 0.5) / 0.35)) if pop_cal is not None else 0.0

    # Affordability: one contract must fit the per-trade risk budget, or the
    # idea is academic.  Scored rather than filtered, so a bigger account still
    # sees it -- just not at the top of the board.
    budget = config.ACCOUNT_SIZE * config.RISK_PER_TRADE_PCT
    risk_dollars = risk * 100.0
    if risk_dollars <= budget:
        comp["sizing"] = 1.0
    else:
        comp["sizing"] = -min(1.0, math.log(risk_dollars / budget, 2) / 2.5)

    total = 0.0
    for k, w in config.IDEA_WEIGHTS.items():
        total += w * max(min(comp.get(k, 0.0), 1.0), -1.0)
    score = 50.0 * (1.0 + total)                       # 0..100

    # ---- hard filters ----------------------------------------------------
    if liq < 30:
        return None

    # ---- hard limits (see config for the measurements behind these) --------
    budget = config.ACCOUNT_SIZE * config.RISK_PER_TRADE_PCT
    if risk_dollars > budget * config.MAX_RISK_MULTIPLE:
        return None
    # The floor is applied to the RAW model POP so the counterfactual that set it
    # (which replayed raw values) still means what it measured; the calibrated
    # number is what gets displayed and scored.
    floor = config.MIN_POP_CREDIT if pos.is_credit else config.MIN_POP_DEBIT
    if pop_for_rule < floor:
        return None
    recent = metrics.get("recent_appearances") or 0
    if recent >= config.CONCENTRATION_MAX_RECENT:
        return None
    if pos.is_credit and max_l is not None:
        # A credit smaller than a tenth of the width is not worth the tail
        width = risk + abs(entry)
        if width > 0 and abs(entry) / width < 0.08:
            return None
    targets = strategies.build_targets(pos, spot, sigma_p, drift_p, now)
    qty = strategies.suggested_qty(risk)

    d = pos.to_dict()
    d.update({
        "dte": exp["dte"],
        "expiry": exp["expiry"],
        "score": round(score, 2),
        "components": {k: round(v, 3) for k, v in comp.items()},
        "ev": ev_p["ev"], "ev_pct_of_risk": ev_per_risk * 100.0,
        "ev_q": ev_q.get("ev"),
        "pop": pop_cal, "pop_raw": pop_for_rule,
        "cvar5": ev_p["cvar5"], "sharpe": ev_p["sharpe"],
        "ev_expiry": ev_expiry.get("ev"), "pop_expiry": ev_expiry.get("pop"),
        "eval_days": round((horizon_moment - now).total_seconds() / 86400.0, 1),
        "p05": ev_p["p05"], "p50": ev_p["p50"], "p95": ev_p["p95"],
        "greeks": {k: round(v, 5) for k, v in g.items()},
        "liquidity": round(liq, 1),
        "qty": qty,
        "risk_dollars": round(risk * 100 * qty, 2),
        "credit_debit": "credit" if pos.is_credit else "debit",
        "targets": targets["targets"],
        "stop": targets["stop"],
        "time_stop": targets["time_stop"],
        "regime": {"trend": tr, "vol": vr["label"], "vol_edge_pct": (vr["edge"] or 0) * 100.0},
        "atm_iv": exp["atm_iv"], "mfiv": exp["mfiv"],
        "forecast_vol": sigma_p,
        "rationale": _rationale(pos, exp, roll, metrics, vr, tr, ev_p, g),
        "warnings": _warnings(pos, exp, metrics),
    })
    return d


def _structure_score(pos, roll, metrics, spot, exp=None):
    """Does dealer positioning support the structure's short strikes?"""
    score = 0.0
    n = 0
    flip = roll.get("gamma_flip")
    gex = roll.get("gex_total")
    if gex is not None:
        # Positive gamma = dealers dampen moves = good for premium sellers.
        seller = pos.is_credit
        score += (0.5 if (gex > 0) == seller else -0.5)
        n += 1
    if flip and spot:
        dist = (spot - flip) / spot
        if pos.direction == "bullish":
            score += _norm(dist, 0.05) * 0.5
        elif pos.direction == "bearish":
            score += -_norm(dist, 0.05) * 0.5
        n += 1
    # Short strikes sitting behind an open-interest wall are better defended --
    # but the wall that matters is the one in the expiry being *traded*.  Using
    # the front expiry compared a 45-day short strike against a 0-DTE strip on
    # any name that lists daily expiries.
    ctx = exp if exp else (roll["expiries"][0] if roll.get("expiries") else {})
    cw, pw = ctx.get("call_wall"), ctx.get("put_wall")
    for l in pos.legs:
        if l.qty >= 0 or l.right == "S":
            continue
        if l.right == "C" and cw and l.strike >= cw:
            score += 0.35
            n += 1
        if l.right == "P" and pw and l.strike <= pw:
            score += 0.35
            n += 1
    return max(min(score / max(n, 1), 1.0), -1.0) if n else 0.0


def _rationale(pos, exp, roll, metrics, vr, tr, ev, g):
    bits = []
    iv = exp.get("atm_iv")
    fc = metrics.get("vol_forecast")
    if iv and fc:
        bits.append("%d-day IV %.1f%% vs %.1f%% forecast realised (%s%.0f%%)"
                    % (round(exp["dte"]), iv * 100, fc * 100,
                       "+" if iv >= fc else "", (iv / fc - 1) * 100))
    if metrics.get("iv_rank") is not None:
        bits.append("IV rank %.0f" % metrics["iv_rank"])
    bits.append("trend %s (%.2f)" % (metrics.get("trend_label", "?"),
                                     metrics.get("trend_score") or 0))
    if metrics.get("days_to_earnings") is not None and metrics["days_to_earnings"] <= 30:
        bits.append("earnings in %d days" % metrics["days_to_earnings"])
    if abs(metrics.get("news_score") or 0) > 0.15:
        bits.append("news %s (%.2f)" % ("positive" if metrics["news_score"] > 0 else "negative",
                                        metrics["news_score"]))
    bits.append("POP %.0f%%, EV %+.2f per spread" % (ev["pop"] * 100, ev["ev"] * 100))
    bits.append("net delta %+.2f, theta %+.2f/day, vega %+.2f"
                % (g["delta"], g["theta"] * 100, g["vega"] * 100))
    return "; ".join(bits)


def _warnings(pos, exp, metrics):
    out = []
    dte_e = metrics.get("days_to_earnings")
    if dte_e is not None and 0 <= dte_e <= exp["dte"]:
        g = pos.greeks()
        if g["vega"] < 0:
            out.append("Short vega through an earnings print on day %d" % dte_e)
        else:
            out.append("Earnings inside the trade window (day %d) -- IV crush risk after" % dte_e)
    if exp.get("arb_flags"):
        out.append("Surface quality: %s" % exp["arb_flags"])
    if exp["dte"] < config.TIME_STOP_DTE:
        out.append("Under %d DTE: gamma risk is already elevated" % config.TIME_STOP_DTE)
    liq = sum(l.liquidity or 0 for l in pos.legs) / len(pos.legs)
    if liq < 45:
        out.append("Thin quotes (liquidity %.0f/100) -- use limit orders, expect partial fills" % liq)
    max_p, max_l, _up, ub_loss = pos.extremes()
    budget = config.ACCOUNT_SIZE * config.RISK_PER_TRADE_PCT
    if max_l is not None and abs(max_l) * 100.0 > budget * 1.05:
        out.append("One contract risks $%.0f, above the $%.0f per-trade budget (%.0f%% of a $%s account)"
                   % (abs(max_l) * 100.0, budget, abs(max_l) * 100.0 / config.ACCOUNT_SIZE * 100,
                      format(int(config.ACCOUNT_SIZE), ",")))
    if ub_loss:
        out.append("Undefined risk to the upside -- sized against the 5%% tail, not a max loss")
    if (exp.get("smile_rmse") or 0) > 0.03:
        out.append("Noisy vol surface (fit RMSE %.1f pts) -- IV-based numbers are less reliable"
                   % ((exp.get("smile_rmse") or 0) * 100))
    return out


GROUP_OF = {}
for _g, _syms in getattr(config, "CORRELATION_GROUPS", {}).items():
    for _s in _syms:
        GROUP_OF[_s] = _g


def rank_all(all_ideas, top_n=None):
    """Rank, then shape the BOARD -- not just the individual ideas.

    Per-idea scoring cannot see that it has produced twenty bullish trades, or
    five versions of the same index bet.  This pass walks the ranked list and
    skips an idea once its bucket is full, so the best idea in a crowded bucket
    always survives and only the marginal duplicate is dropped.  Every skip is
    recorded on the idea so the reason is visible rather than mysterious.
    """
    top_n = top_n or config.SCAN_TOP_N
    ideas = sorted(all_ideas, key=lambda i: -i["score"])
    if not getattr(config, "BOARD_ENFORCE", False):
        for n, idea in enumerate(ideas, 1):
            idea["rank"] = n
        return ideas[:top_n]

    max_bull = int(top_n * config.BOARD_MAX_BULLISH_PCT)
    max_bear = int(top_n * config.BOARD_MAX_BEARISH_PCT)
    kept, dropped = [], []
    n_bull = n_bear = 0
    per_group, per_sym = {}, {}

    for idea in ideas:
        if len(kept) >= top_n:
            break
        sym = idea["symbol"]
        grp = GROUP_OF.get(sym, sym)
        d = idea.get("direction")
        reason = None
        if d == "bullish" and n_bull >= max_bull:
            reason = "board already %d%% long delta" % int(config.BOARD_MAX_BULLISH_PCT * 100)
        elif d == "bearish" and n_bear >= max_bear:
            reason = "board already %d%% short delta" % int(config.BOARD_MAX_BEARISH_PCT * 100)
        elif per_sym.get(sym, 0) >= config.BOARD_MAX_PER_SYMBOL:
            reason = "%d ideas already on %s" % (config.BOARD_MAX_PER_SYMBOL, sym)
        elif per_group.get(grp, 0) >= config.BOARD_MAX_PER_GROUP:
            reason = "%d ideas already in the '%s' group" % (config.BOARD_MAX_PER_GROUP, grp)
        if reason:
            idea["excluded_reason"] = reason
            dropped.append(idea)
            continue
        per_sym[sym] = per_sym.get(sym, 0) + 1
        per_group[grp] = per_group.get(grp, 0) + 1
        if d == "bullish":
            n_bull += 1
        elif d == "bearish":
            n_bear += 1
        idea["group"] = grp
        kept.append(idea)

    for n, idea in enumerate(kept, 1):
        idea["rank"] = n
    if dropped:
        kept_syms = {i["symbol"] for i in kept}
        for i in kept:
            i["board_note"] = ("%d higher-scoring ideas were held back by board limits"
                               % len(dropped)) if len(dropped) else None
    return kept
