"""Replay every idea against its own trade plan, one session at a time.

This is the canonical way to measure the scanner.  It exists because three
separate measurements over three sessions each used slightly different rules,
and the differences mattered more than the thing being measured:

* the forward test marked ideas until expiry, i.e. measured buy-and-hold,
  which the plan never recommends;
* the first managed-exit backfill applied the take-profit and the stop but not
  the 21-DTE time stop, and filled stops AT the stop price -- optimistic
  whenever a price gaps through, which on sparse marks is most of the time;
* production then applied all three rules but filled at the day's mark, and
  recorded exits up to 20 days late wherever the pipeline had not run.

One engine, one rule set, one fill model:

  marks    stored daily marks, with every missing session priced from that
           day's closing spot and the nearest earlier stored vol surface
           (sticky-moneyness).  The 2026-09-03..23 gap is filled this way
           rather than skipped, so an exit lands on the day it happened.
  cost     every managed exit pays SLIPPAGE_FRAC_OF_SPREAD of each leg's
           bid-ask (the spread recorded at entry), exactly as the entry did.
           Marks are mids; closing at a mid for free flattered every early
           exit, and most of all a trade closed the day after it was opened.
  T1       end-of-day check on the mark NET of exit cost; filled at the
           threshold (a resting limit order), optionally at the mark.
  STOP     end-of-day check; filled at the MARK less exit cost.  A stop is a
           market order on trigger, and a gap through it fills worse, not at
           the stop.  Not armed until the second session after an earnings
           date inside the trade (EARNINGS_STOP_HOLD, strategies.stop_armed_from).
  TIME     closed at the first session at or inside TIME_STOP_DTE.
  EXPIRY   intrinsic at the close ON the expiry date, no cost.

    python replay.py            # summary over every resolved idea
"""

import datetime as dt
import json
import math
import statistics as st

import config
import db
import marketcal
from model import bs, strategies, vol as volmod

_surface_cache = {}
_spot_cache = {}


# ----------------------------------------------------------------- inputs
def _spot(symbol, day):
    key = (symbol, day)
    if key not in _spot_cache:
        r = db.q1("SELECT close FROM ohlc WHERE symbol=? AND date<=? ORDER BY date DESC LIMIT 1",
                  (symbol, day))
        _spot_cache[key] = r["close"] if r else None
    return _spot_cache[key]


def _surface(symbol, day, expiry):
    """Nearest stored surface ON or BEFORE `day` for the expiry closest to `expiry`."""
    key = (symbol, day, expiry)
    if key in _surface_cache:
        return _surface_cache[key]
    r = db.q1("""SELECT em.* FROM expiry_metrics em
                  WHERE em.symbol=? AND em.asof_date=(
                        SELECT MAX(asof_date) FROM expiry_metrics
                         WHERE symbol=? AND asof_date<=?)
                  ORDER BY ABS(julianday(em.expiry) - julianday(?)) ASC, em.snapshot_id DESC
                  LIMIT 1""", (symbol, symbol, day, expiry))
    out = None
    if r and r.get("smile_a") is not None and r.get("smile_scale"):
        out = {
            "smile": volmod.Smile(r["smile_a"], r["smile_b"], r["smile_c"],
                                  r.get("smile_rmse") or 0.0,
                                  r.get("smile_k_min") or -0.3, r.get("smile_k_max") or 0.3,
                                  r["smile_scale"], r.get("n_quotes") or 0,
                                  r.get("t_years") or 0.1, r.get("forward") or 1.0),
            "rate": r.get("rate") or config.DEFAULT_RISK_FREE,
            "q": r.get("div_yield") or 0.0,
            "asof": r["asof_date"],
        }
    _surface_cache[key] = out
    return out


def _price_legs(legs, symbol, day):
    """Value of the position at the close of `day`, or None if it cannot be priced."""
    S = _spot(symbol, day)
    if not S:
        return None
    moment = marketcal.et_to_utc(dt.datetime.combine(marketcal.parse_date(day), dt.time(16, 0)))
    total = 0.0
    for l in legs:
        if l.get("right") == "S":
            total += l["qty"] * S
            continue
        exp = marketcal.parse_date(l["expiry"])
        if exp <= marketcal.parse_date(day):
            total += l["qty"] * bs.intrinsic(_spot(symbol, l["expiry"]) or S, l["strike"], l["right"])
            continue
        ctx = _surface(symbol, day, l["expiry"])
        if not ctx:
            return None
        T = marketcal.year_fraction(exp, moment, floor_hours=0.0)
        F = S * math.exp((ctx["rate"] - ctx["q"]) * T)
        # Sticky-moneyness: read the smile at this strike's moneyness against
        # TODAY's forward, so a stale surface is shifted with spot rather than
        # pinned to the strikes it was fitted on.
        iv = ctx["smile"].iv_k(math.log(l["strike"] / F))
        total += l["qty"] * bs.black76(F, l["strike"], T, iv, math.exp(-ctx["rate"] * T), l["right"])
    return total


def sessions_between(start, end):
    d, out = marketcal.parse_date(start), []
    end = marketcal.parse_date(end)
    while d <= end:
        if marketcal.is_trading_day(d):
            out.append(d.isoformat())
        d += dt.timedelta(days=1)
    return out


def mark_series(idea, today=None):
    """[(date, pnl_dollars, source)] for every session the idea was alive."""
    today = today or marketcal.session_date().isoformat()
    legs = json.loads(idea["legs_json"] or "[]")
    stored = {r["asof_date"]: r["pnl"] for r in
              db.q("SELECT asof_date, pnl FROM idea_outcome WHERE idea_id=?", (idea["id"],))}
    last = min(idea["expiry"], today)
    out = []
    for day in sessions_between(idea["asof_date"], last):
        if day == idea["asof_date"]:
            continue                       # opened today; nothing to mark yet
        if day == idea["expiry"] and not (day == today and
                                          marketcal.market_state() in ("premarket", "open")):
            # Settlement is intrinsic at the close on expiry -- never a stored
            # intraday mark, which may have been taken hours before the close.
            # (During the expiry session itself there is no close yet; the
            # stored intraday mark stands in until a later run settles it.)
            S = _spot(idea["symbol"], day)
            if S is None:
                continue
            v = sum(l["qty"] * (S if l.get("right") == "S" else bs.intrinsic(S, l["strike"], l["right"]))
                    for l in legs)
            out.append((day, (v - idea["entry_price"]) * 100.0, "settle"))
            continue
        if day in stored and stored[day] is not None:
            out.append((day, stored[day], "stored"))
            continue
        v = _price_legs(legs, idea["symbol"], day)
        if v is not None:
            out.append((day, (v - idea["entry_price"]) * 100.0, "gapfill"))
    return out


# ------------------------------------------------------------------ rules
DEFAULT_POLICY = {
    "t1": True, "t1_fill": "threshold",
    "stop": True,
    "time_stop_dte": None,     # None -> config.TIME_STOP_DTE
    "exit_cost": True,
    "earnings_stop_hold": None,  # None -> config.EARNINGS_STOP_HOLD
}


def exit_cost(legs):
    """Dollars paid to close at market: the same share of the spread as the entry."""
    frac = config.SLIPPAGE_FRAC_OF_SPREAD
    return sum(abs(l["qty"]) * frac * max((l.get("ask") or 0) - (l.get("bid") or 0), 0.0)
               for l in legs if l.get("right") != "S") * 100.0


def earnings_in(idea):
    """Earnings date the scanner knew about on the day it opened the idea, if it
    falls strictly after the open and on or before expiry."""
    r = db.q1("""SELECT earnings_date FROM symbol_metrics WHERE symbol=? AND asof_date=?
                 ORDER BY rowid DESC LIMIT 1""", (idea["symbol"], idea["asof_date"]))
    ed = (r or {}).get("earnings_date")
    ed = str(ed)[:10] if ed else None
    return ed if ed and idea["asof_date"] <= ed <= idea["expiry"] else None


def apply_plan(idea, series, policy=None, earnings_date=None):
    """Walk the series and return the managed exit and the held-to-end value."""
    p = dict(DEFAULT_POLICY, **(policy or {}))
    tsd = config.TIME_STOP_DTE if p["time_stop_dte"] is None else p["time_stop_dte"]
    hold = (config.EARNINGS_STOP_HOLD if p["earnings_stop_hold"] is None
            else p["earnings_stop_hold"])
    tg = json.loads(idea["targets_json"] or "{}")
    t1 = next((x["pnl"] * 100.0 for x in (tg.get("targets") or [])
               if x.get("name") == "T1" and x.get("pnl") is not None), None)
    stop = (tg.get("stop") or {}).get("pnl")
    stop = stop * 100.0 if stop is not None else None
    exp = marketcal.parse_date(idea["expiry"])
    open_dte = (exp - marketcal.parse_date(idea["asof_date"])).days
    cost = exit_cost(json.loads(idea["legs_json"] or "[]")) if p["exit_cost"] else 0.0
    armed = None
    if hold:
        # The plan's own date when it recorded one; older ideas predate the rule.
        armed = (tg.get("stop") or {}).get("armed_from") or (
            strategies.stop_armed_from(earnings_date) if earnings_date else None)

    held = series[-1][1] if series else None
    for day, pnl, src in series:
        if src == "settle":
            break
        net = pnl - cost
        if p["stop"] and stop is not None and pnl <= stop and (armed is None or day >= armed):
            return {"exit_date": day, "reason": "STOP", "pnl": net, "held": held}
        if p["t1"] and t1 is not None and net >= t1:
            fill = t1 if p["t1_fill"] == "threshold" else net
            return {"exit_date": day, "reason": "T1", "pnl": fill, "held": held}
        # A time stop only applies to a trade that was opened OUTSIDE the zone;
        # one opened inside it has nothing to be stopped out of.
        if tsd and open_dte > tsd and (exp - marketcal.parse_date(day)).days <= tsd:
            return {"exit_date": day, "reason": "TIME", "pnl": net, "held": held}
    last = series[-1] if series else (None, None, None)
    return {"exit_date": last[0], "reason": "EXPIRY" if last[2] == "settle" else "OPEN",
            "pnl": held, "held": held}


# ----------------------------------------------------------------- summary
def resolved_ideas(today=None):
    today = today or marketcal.session_date().isoformat()
    return db.q("""SELECT id, asof_date, symbol, strategy, direction, expiry, dte, pop,
                          score, entry_price, legs_json, targets_json, metrics_json
                     FROM idea WHERE expiry <= ? ORDER BY asof_date""", (today,))


def run(policy=None, ideas=None, today=None):
    ideas = ideas if ideas is not None else resolved_ideas(today)
    out = []
    for i in ideas:
        s = mark_series(i, today)
        if not s:
            continue
        m = json.loads(i["metrics_json"] or "{}")
        ed = earnings_in(i)
        res = apply_plan(i, s, policy, earnings_date=ed)
        res.update({"id": i["id"], "symbol": i["symbol"], "strategy": i["strategy"],
                    "earnings_date": ed,
                    "family": m.get("credit_debit"), "risk": m.get("risk_dollars") or 1.0,
                    "open_dte": (marketcal.parse_date(i["expiry"]) -
                                 marketcal.parse_date(i["asof_date"])).days,
                    "pop": i["pop"], "pop_raw": m.get("pop_raw"), "score": i["score"],
                    "asof_date": i["asof_date"], "n_marks": len(s),
                    "gapfilled": sum(1 for x in s if x[2] == "gapfill")})
        out.append(res)
    return out


def sync_exits(today=None, lookback_days=120):
    """Write every idea's managed exit and held-to-expiry value from the replay.

    The ONLY place idea.exit_* is written.  Production used to decide exits
    inside mark_ideas on whatever day the pipeline happened to run, so a
    three-week gap recorded stops and time exits up to 20 days late, at the
    wrong price, under slightly different rules from every backtest.  Re-deriving
    them here each run means a late run corrects itself, and the site, the
    tests and any backfill all read the same answer.
    """
    today = today or marketcal.session_date().isoformat()
    _surface_cache.clear()
    _spot_cache.clear()
    ideas = db.q("""SELECT id, asof_date, symbol, strategy, direction, expiry, dte, pop,
                           score, entry_price, legs_json, targets_json, metrics_json
                      FROM idea WHERE asof_date >= date(?, ?) AND asof_date < ?""",
                 (today, "-%d day" % lookback_days, today))
    n = {"exits": 0, "open": 0, "settled": 0}
    for i in ideas:
        s = mark_series(i, today)
        if not s:
            continue
        res = apply_plan(i, s, earnings_date=earnings_in(i))
        settled = s[-1][2] == "settle"
        held = s[-1][1] if settled else None
        if res["reason"] in ("T1", "STOP", "TIME"):
            vals, n["exits"] = (res["exit_date"], res["reason"], res["pnl"]), n["exits"] + 1
        elif settled:
            vals, n["settled"] = (res["exit_date"], "EXPIRY", res["pnl"]), n["settled"] + 1
        else:
            vals, n["open"] = (None, None, None), n["open"] + 1
        db.execute("UPDATE idea SET exit_date=?, exit_reason=?, exit_pnl=?, held_pnl=? WHERE id=?",
                   vals + (held, i["id"]))
    return n


def summarize(rows, key="pnl"):
    if not rows:
        return {"n": 0}
    R = [r[key] / max(r["risk"], 1.0) for r in rows]
    return {"n": len(rows), "meanR": st.mean(R), "medianR": st.median(R),
            "net": sum(r[key] for r in rows),
            "win": sum(1 for r in rows if r[key] > 0) / len(rows)}


if __name__ == "__main__":
    db.init()
    rows = run()
    h, m = summarize(rows, "held"), summarize(rows, "pnl")
    print("resolved ideas: %d   (gap-filled marks: %d)"
          % (len(rows), sum(r["gapfilled"] for r in rows)))
    for lab, s in (("held to expiry", h), ("managed to plan", m)):
        print("  %-16s meanR %+.3f  median %+.3f  net %+10.0f  win %.0f%%"
              % (lab, s["meanR"], s["medianR"], s["net"], 100 * s["win"]))
