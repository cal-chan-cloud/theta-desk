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
def vol_regime(iv30, forecast, iv_rank):
    """Rich / fair / cheap, from the implied-vs-forecast gap and IV rank."""
    edge = None
    if iv30 and forecast and forecast > 0:
        edge = (iv30 - forecast) / forecast
    votes = 0
    if edge is not None:
        if edge > 0.18:
            votes += 2
        elif edge > 0.06:
            votes += 1
        elif edge < -0.10:
            votes -= 2
        elif edge < -0.02:
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
    return {"label": label, "edge": edge, "votes": votes}


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
    vr = vol_regime(iv30, forecast, metrics.get("iv_rank"))
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
    # A deliberately modest tilt: trend and news together move the drift by at
    # most a ~0.3 Sharpe.  Anything larger and the EV ranking becomes a
    # momentum bet wearing an options costume.
    tilt = 0.25 * trend_score + 0.08 * news_score
    tilt = max(min(tilt, 0.35), -0.35)

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

    ev_q = strategies.evaluate(pos, dens_q, fees_per_spread=fees)
    ev_p = strategies.evaluate(pos, dens_p, fees_per_spread=fees)
    if not ev_p:
        return None

    # Where max loss is undefined, size and score against the 5% tail instead.
    risk = abs(max_l) if max_l is not None else abs(min(ev_p["p05"], -0.01))
    ev_per_risk = ev_p["ev"] / risk if risk > 0 else 0.0
    g = pos.greeks(spot, now)

    # ---- component scores, each in [-1, 1] -------------------------------
    comp = {}
    comp["edge"] = _norm(ev_per_risk, 0.12)

    # Does the structure's vega sign agree with the vol read?
    vega_sign = 1.0 if g["vega"] > 0 else (-1.0 if g["vega"] < 0 else 0.0)
    edge_pct = vr["edge"] if vr["edge"] is not None else 0.0
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
    if pos.is_credit and max_l is not None:
        # A credit smaller than a tenth of the width is not worth the tail
        width = risk + abs(entry)
        if width > 0 and abs(entry) / width < 0.08:
            return None
    if ev_p["pop"] < 0.20:
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
        "pop": ev_p["pop"], "cvar5": ev_p["cvar5"], "sharpe": ev_p["sharpe"],
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


def rank_all(all_ideas, top_n=None):
    top_n = top_n or config.SCAN_TOP_N
    ideas = sorted(all_ideas, key=lambda i: -i["score"])
    for i, idea in enumerate(ideas, 1):
        idea["rank"] = i
    return ideas[:top_n]
