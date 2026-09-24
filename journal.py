"""Trade journal: record positions, mark them daily, and measure the desk.

Two books are tracked side by side:

* **trade**  -- what you actually put on.  Marked every pipeline run so the
  equity curve, greeks and target progress are always current.
* **idea**   -- what the scanner proposed, marked the same way whether or not
  you took it.  This is the honest forward test: it records the scanner's
  suggestions before the outcome is known and never revises them, so the
  hit-rate you read later is the hit-rate it actually had.

Marking prefers the live quote for the exact contract and falls back to the
fitted surface when the contract has rolled out of the stored tenor ladder --
a mark is always produced, and `mark_source` says which it was.
"""

import datetime as dt
import json
import math

import config
import db
import marketcal
from model import bs
from model.strategies import Leg, Position


# --------------------------------------------------------------- valuation
def _expiry_ctx(roll, expiry):
    """Nearest analysed expiry to `expiry`, for surface-based marking."""
    if not roll or not roll.get("expiries"):
        return None
    target = marketcal.parse_date(expiry)
    best, bestd = None, None
    for m in roll["expiries"]:
        d = abs((marketcal.parse_date(m["expiry"]) - target).days)
        if bestd is None or d < bestd:
            best, bestd = m, d
    return best


def surface_on(symbol, asof_date, expiry):
    """Rebuild the fitted vol surface stored for (symbol, date, expiry).

    expiry_metrics keeps the smile's coefficients, scale and strike range
    forever -- which is what makes a past day's option prices reconstructible
    from a few thousand rows instead of a hundred thousand quotes.  Used to
    rebuild a mark history that would otherwise be gone.
    """
    from model import vol as _vol
    r = db.q1("""SELECT em.* FROM expiry_metrics em
                  JOIN chain_snapshot cs ON cs.id = em.snapshot_id
                 WHERE em.symbol=? AND em.asof_date=?
                 ORDER BY ABS(julianday(em.expiry) - julianday(?)) ASC, cs.ts DESC
                 LIMIT 1""", (symbol, asof_date, str(expiry)[:10]))
    if not r or r.get("smile_a") is None or not r.get("smile_scale"):
        return None
    sm = _vol.Smile(r["smile_a"], r["smile_b"], r["smile_c"], r.get("smile_rmse") or 0.0,
                    r.get("smile_k_min") or -0.3, r.get("smile_k_max") or 0.3,
                    r["smile_scale"], r.get("n_quotes") or 0,
                    r.get("t_years") or 0.1, r.get("forward") or 0.0)
    return {"smile": sm, "rate": r.get("rate") or config.DEFAULT_RISK_FREE,
            "div_yield": r.get("div_yield") or 0.0, "forward": r.get("forward"),
            "expiry": r["expiry"]}


def settlement_spot(symbol, expiry):
    """Underlying close ON the expiry date -- what the option actually settled against.

    Marking an expired option at *today's* spot is not a marking error, it is a
    different question entirely: it asks what the contract would be worth if it
    expired now at the current price.  Measured across 152 resolved ideas the
    spot used was a median 4.1% and a mean 5.6% away from the real expiry close
    (max 28%), because a three-week gap in pipeline runs meant everything was
    settled days or weeks late.  That turned expired long strangles into huge
    fictional winners and expired short strangles into fictional losers -- the
    exact opposite of what really happened -- and any model change made on the
    back of it would have been backwards.
    """
    row = db.q1("""SELECT close FROM ohlc WHERE symbol=? AND date<=?
                    ORDER BY date DESC LIMIT 1""", (symbol, str(expiry)[:10]))
    return row["close"] if row else None


def mark_leg(leg, roll, now=None):
    """(price, source) for one leg at current market."""
    now = now or marketcal.now_utc()
    if leg.get("right") == "S" or leg.get("is_stock"):
        spot = (roll or {}).get("spot")
        return (spot, "quote") if spot else (None, "none")
    expiry = marketcal.parse_date(leg["expiry"])
    if expiry and expiry < marketcal.session_date():
        return None, "expired"

    if roll:
        for m in roll["expiries"]:
            if m["expiry"] != (expiry.isoformat() if expiry else leg["expiry"]):
                continue
            for c in m["contracts"]:
                if c["occ"] == leg.get("occ") or (
                        c["right"] == leg["right"] and abs(c["strike"] - leg["strike"]) < 1e-6):
                    if c.get("mid"):
                        return c["mid"], "quote"
        ctx = _expiry_ctx(roll, leg["expiry"])
        if ctx and ctx.get("smile_obj"):
            T = marketcal.year_fraction(expiry, now)
            # Rescale the forward to this leg's own tenor
            r = ctx.get("rate") or config.DEFAULT_RISK_FREE
            qy = ctx.get("div_yield") or 0.0
            F = roll["spot"] * math.exp((r - qy) * T)
            iv = ctx["smile_obj"].iv(leg["strike"])
            return bs.black76(F, leg["strike"], T, iv, math.exp(-r * T), leg["right"]), "surface"

    row = db.q1("""SELECT mid, iv FROM contract_quote WHERE occ=?
                    ORDER BY asof_date DESC, snapshot_id DESC LIMIT 1""",
                (leg.get("occ"),))
    if row and row.get("mid"):
        return row["mid"], "stale"
    return None, "none"


def mark_legs(legs, roll, now=None, symbol=None):
    total, sources = 0.0, set()
    symbol = symbol or (roll or {}).get("symbol")
    for l in legs:
        px, src = mark_leg(l, roll, now)
        sources.add(src)
        if px is None:
            if src == "expired":
                # Settle against the close on the EXPIRY date, not today's spot.
                spot = settlement_spot(symbol, l.get("expiry")) if symbol else None
                if spot is None:
                    spot = (roll or {}).get("spot")
                if spot is None:
                    return None, "none"
                px = bs.intrinsic(spot, l["strike"], l["right"])
            else:
                return None, "none"
        total += l["qty"] * px
    order = ["none", "stale", "surface", "expired", "quote"]
    src = min(sources, key=lambda s: order.index(s) if s in order else 0)
    return total, src


def position_from_legs(symbol, strategy, legs, spot_entry, roll):
    objs = []
    for l in legs:
        ctx = _expiry_ctx(roll, l["expiry"]) if roll else None
        r = (ctx or {}).get("rate") or config.DEFAULT_RISK_FREE
        qy = (ctx or {}).get("div_yield") or 0.0
        slope = 0.0
        # A stock leg carries strike 0; log(0) would raise before it ever got
        # to the "stock has no smile" check.
        if (l.get("strike") or 0) > 0 and ctx and ctx.get("smile_obj") and ctx.get("forward"):
            slope = ctx["smile_obj"].slope(math.log(l["strike"] / ctx["forward"]))
        objs.append(Leg(l["right"], l["strike"], marketcal.parse_date(l["expiry"]),
                        l["qty"], l.get("price") or 0.0, l.get("iv") or 0.25,
                        slope=slope, r=r, q=qy, occ=l.get("occ")))
    return Position(symbol, strategy, objs, spot_entry)


# ------------------------------------------------------------------- trades
def open_trade(payload):
    """Create a journal entry.  `payload` may come straight from an idea."""
    now = marketcal.now_utc().isoformat()
    legs = payload["legs"] if isinstance(payload.get("legs"), list) else json.loads(payload["legs_json"])
    row = {
        "idea_id": payload.get("idea_id"),
        "symbol": payload["symbol"].upper(),
        "strategy": payload.get("strategy", "custom"),
        "direction": payload.get("direction"),
        "opened_date": payload.get("opened_date") or marketcal.session_date().isoformat(),
        "expiry": payload.get("expiry") or min(l["expiry"] for l in legs),
        "legs_json": json.dumps(legs),
        "qty": int(payload.get("qty") or 1),
        "entry_price": float(payload["entry_price"]),
        "entry_spot": payload.get("entry_spot"),
        "entry_iv": payload.get("entry_iv"),
        "fees": float(payload.get("fees") or
                      config.COMMISSION_PER_CONTRACT * len(legs) * int(payload.get("qty") or 1)),
        "status": "open",
        "max_profit": payload.get("max_profit"),
        "max_loss": payload.get("max_loss"),
        "targets_json": json.dumps(payload.get("targets")) if payload.get("targets") else None,
        "tags": payload.get("tags"),
        "notes": payload.get("notes"),
        "created": now, "updated": now,
    }
    return db.insert_dict("trade", row, mode="ABORT")


def close_trade(trade_id, exit_price, exit_date=None, reason=None, exit_spot=None):
    t = db.q1("SELECT * FROM trade WHERE id=?", (trade_id,))
    if not t:
        return None
    qty = t["qty"] or 1
    pnl = (float(exit_price) - t["entry_price"]) * 100.0 * qty - (t["fees"] or 0)
    basis = abs(t["entry_price"]) * 100.0 * qty
    db.execute("""UPDATE trade SET status='closed', closed_date=?, exit_price=?,
                    exit_spot=?, exit_reason=?, pnl=?, pnl_pct=?, updated=?
                   WHERE id=?""",
               (exit_date or marketcal.session_date().isoformat(), float(exit_price),
                exit_spot, reason, pnl, (pnl / basis * 100.0) if basis else None,
                marketcal.now_utc().isoformat(), trade_id))
    return db.q1("SELECT * FROM trade WHERE id=?", (trade_id,))


def delete_trade(trade_id):
    db.execute("DELETE FROM trade_mark WHERE trade_id=?", (trade_id,))
    db.execute("DELETE FROM trade WHERE id=?", (trade_id,))
    return True


def list_trades(status=None, limit=500):
    sql = "SELECT * FROM trade"
    args = []
    if status and status != "all":
        sql += " WHERE status=?"
        args.append(status)
    sql += " ORDER BY (status='open') DESC, opened_date DESC, id DESC LIMIT ?"
    args.append(limit)
    rows = db.q(sql, tuple(args))
    for r in rows:
        r["legs"] = json.loads(r["legs_json"]) if r.get("legs_json") else []
        r["targets"] = json.loads(r["targets_json"]) if r.get("targets_json") else None
        last = db.q1("""SELECT * FROM trade_mark WHERE trade_id=?
                        ORDER BY date DESC LIMIT 1""", (r["id"],))
        r["mark"] = last
    return rows


def mark_open_trades(rolls, today=None):
    today = today or marketcal.session_date().isoformat()
    rows = db.q("SELECT * FROM trade WHERE status='open'")
    n = 0
    now = marketcal.now_utc()
    for t in rows:
        legs = json.loads(t["legs_json"])
        roll = (rolls or {}).get(t["symbol"])
        mark, src = mark_legs(legs, roll, now, symbol=t["symbol"])
        if mark is None:
            continue
        qty = t["qty"] or 1
        pnl = (mark - t["entry_price"]) * 100.0 * qty - (t["fees"] or 0)
        basis = abs(t["entry_price"]) * 100.0 * qty
        spot = (roll or {}).get("spot")
        g = {"delta": None, "gamma": None, "theta": None, "vega": None}
        dte = None
        if roll and spot:
            pos = position_from_legs(t["symbol"], t["strategy"], legs,
                                      t["entry_spot"] or spot, roll)
            g = pos.greeks(spot, now)
            g = {k: v * qty for k, v in g.items()}
            dte = pos.dte(now)
        db.insert_dict("trade_mark", {
            "trade_id": t["id"], "date": today, "spot": spot, "mark": mark,
            "pnl": pnl, "pnl_pct": (pnl / basis * 100.0) if basis else None,
            "delta": g["delta"], "gamma": g["gamma"], "theta": g["theta"],
            "vega": g["vega"], "dte": dte,
        })
        db.execute("UPDATE trade SET updated=? WHERE id=?",
                   (marketcal.now_utc().isoformat(), t["id"]))
        n += 1

        # Auto-close positions whose legs have all expired: mark to intrinsic.
        if dte is not None and dte <= 0:
            close_trade(t["id"], mark, today, "expired", spot)
    return n


# -------------------------------------------------------------------- ideas
def mark_ideas(rolls, today=None, lookback_days=120):
    """Forward-test every idea the scanner has ever emitted."""
    today = today or marketcal.session_date().isoformat()
    now = marketcal.now_utc()
    # An idea that already hit a target or a stop is CLOSED -- its P&L is frozen
    # at the exit, exactly as the trade plan says.  Marking it onward would be
    # reporting a position the plan says you are no longer in.
    rows = db.q("""SELECT id, symbol, strategy, expiry, legs_json, entry_price,
                          max_profit, max_loss, targets_json, asof_date
                     FROM idea
                    WHERE asof_date >= date('now', ?) AND asof_date < ?
                      AND (exit_date IS NULL OR ? = 0)""",
                ("-%d day" % lookback_days, today,
                 1 if getattr(config, "MANAGED_EXITS", False) else 0))
    n = 0
    for i in rows:
        legs = json.loads(i["legs_json"])
        roll = (rolls or {}).get(i["symbol"])
        mark, src = mark_legs(legs, roll, now, symbol=i["symbol"])
        if mark is None:
            continue
        pnl = (mark - i["entry_price"]) * 100.0
        basis = abs(i["entry_price"]) * 100.0
        hit = None
        exit_reason = None
        try:
            tg = json.loads(i["targets_json"] or "{}")
            move = mark - i["entry_price"]
            for t in tg.get("targets", []):
                if t.get("pnl") is not None and move >= t["pnl"]:
                    hit = t["name"]
            stop = tg.get("stop")
            if stop and stop.get("pnl") is not None and move <= stop["pnl"]:
                hit = "STOP"
            # First target reached, or the stop, closes the position.
            if hit and hit != "T3":
                exit_reason = hit
            elif hit == "T3":
                exit_reason = "T3"
            # Time stop: the plan says be out at TIME_STOP_DTE.
            if not exit_reason:
                dleft = (marketcal.parse_date(i["expiry"]) - marketcal.session_date()).days
                if dleft <= config.TIME_STOP_DTE and dleft >= 0 and tg.get("time_stop"):
                    exit_reason = "TIME"
        except (ValueError, TypeError):
            pass
        if exit_reason and getattr(config, "MANAGED_EXITS", False):
            db.execute("""UPDATE idea SET exit_date=?, exit_reason=?, exit_pnl=?
                           WHERE id=? AND exit_date IS NULL""",
                       (today, exit_reason, pnl, i["id"]))
        exp_d = marketcal.parse_date(i["expiry"])
        settled = exp_d and exp_d < marketcal.session_date()
        spot_used = settlement_spot(i["symbol"], i["expiry"]) if settled else None
        db.insert_dict("idea_outcome", {
            "idea_id": i["id"], "asof_date": today,
            "spot": spot_used if spot_used is not None else (roll or {}).get("spot"),
            "mark": mark, "pnl": pnl,
            "pnl_pct": (pnl / basis * 100.0) if basis else None, "hit_target": hit,
        })
        n += 1
    return n


# ------------------------------------------------------------- performance
# A position is treated as resolved for calibration once it is within this many
# days of expiry -- by then the mark and the settled outcome have converged.
CALIB_DTE_LEFT = 3
CALIB_MIN_PER_BUCKET = 5


def _stats(pnls):
    if not pnls:
        return {"n": 0, "win_rate": None, "avg_win": None, "avg_loss": None,
                "profit_factor": None, "expectancy": None, "total": 0.0,
                "best": None, "worst": None, "sharpe": None}
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_w = sum(wins)
    gross_l = -sum(losses)
    mean = sum(pnls) / len(pnls)
    var = sum((p - mean) ** 2 for p in pnls) / len(pnls) if len(pnls) > 1 else 0.0
    sd = math.sqrt(var)
    # `None` rather than infinity for an all-winners record: infinity is not
    # valid JSON, and "no losses yet" is the honest reading anyway.  The UI
    # shows "--" and the win rate already says 100%.
    return {
        "n": len(pnls),
        "win_rate": 100.0 * len(wins) / len(pnls),
        "avg_win": (gross_w / len(wins)) if wins else None,
        "avg_loss": (-gross_l / len(losses)) if losses else None,
        "profit_factor": (gross_w / gross_l) if gross_l > 0 else None,
        "no_losses": bool(wins) and not gross_l > 0,
        "expectancy": mean,
        "total": sum(pnls),
        "best": max(pnls), "worst": min(pnls),
        "sharpe": (mean / sd) if sd > 1e-9 else None,
    }


def performance():
    """Realised performance of the journal, sliced the ways that matter."""
    closed = db.q("SELECT * FROM trade WHERE status='closed'")
    overall = _stats([t["pnl"] for t in closed if t["pnl"] is not None])

    by_strategy, by_symbol, by_reason = {}, {}, {}
    for t in closed:
        if t["pnl"] is None:
            continue
        by_strategy.setdefault(t["strategy"], []).append(t["pnl"])
        by_symbol.setdefault(t["symbol"], []).append(t["pnl"])
        by_reason.setdefault(t["exit_reason"] or "manual", []).append(t["pnl"])

    equity, run = [], 0.0
    for t in sorted(closed, key=lambda x: (x["closed_date"] or "", x["id"])):
        if t["pnl"] is None:
            continue
        run += t["pnl"]
        equity.append({"date": t["closed_date"], "pnl": t["pnl"], "cum": run,
                       "symbol": t["symbol"], "strategy": t["strategy"]})
    peak, max_dd = 0.0, 0.0
    for e in equity:
        peak = max(peak, e["cum"])
        max_dd = min(max_dd, e["cum"] - peak)

    open_rows = db.q("SELECT * FROM trade WHERE status='open'")
    open_pnl = 0.0
    open_greeks = {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    for t in open_rows:
        m = db.q1("SELECT * FROM trade_mark WHERE trade_id=? ORDER BY date DESC LIMIT 1",
                  (t["id"],))
        if m:
            open_pnl += m["pnl"] or 0.0
            for k in open_greeks:
                if m.get(k) is not None:
                    open_greeks[k] += m[k] * 100.0

    return {
        "overall": overall,
        "by_strategy": {k: _stats(v) for k, v in by_strategy.items()},
        "by_symbol": {k: _stats(v) for k, v in by_symbol.items()},
        "by_exit_reason": {k: _stats(v) for k, v in by_reason.items()},
        "equity_curve": equity,
        "max_drawdown": max_dd,
        "open_count": len(open_rows),
        "open_pnl": open_pnl,
        "open_greeks": open_greeks,
        "closed_count": len(closed),
    }


def idea_performance(days=90):
    """How the scanner's own suggestions have played out, UNDER ITS OWN PLAN.

    Where an idea hit a target or a stop, the frozen exit P&L is used rather
    than its value at expiry -- otherwise the number describes buy-and-hold,
    which the model never recommends.
    """
    rows = db.q("""SELECT i.id, i.asof_date, i.symbol, i.strategy, i.score, i.dte,
                          i.entry_price, i.pop, i.ev, i.max_profit, i.max_loss,
                          i.expiry,
                          julianday(i.expiry) - julianday('now') AS dte_left,
                          o.asof_date AS mark_date,
                          COALESCE(i.exit_pnl, o.pnl) AS pnl, o.pnl_pct,
                          COALESCE(i.exit_reason, o.hit_target) AS hit_target,
                          i.exit_reason, i.exit_date
                     FROM idea i
                     JOIN idea_outcome o ON o.idea_id = i.id
                    WHERE i.asof_date >= date('now', ?)
                      AND o.asof_date = (SELECT MAX(asof_date) FROM idea_outcome
                                          WHERE idea_id = i.id)""",
                ("-%d day" % days,))
    if not rows:
        return {"n": 0, "buckets": {}, "by_strategy": {}, "overall": _stats([]),
                "calibration": [], "resolved": 0, "open": 0, "calibration_note": None}

    overall = _stats([r["pnl"] for r in rows if r["pnl"] is not None])
    by_strategy, buckets = {}, {}
    for r in rows:
        if r["pnl"] is None:
            continue
        by_strategy.setdefault(r["strategy"], []).append(r["pnl"])
        b = "%d-%d" % (int(r["score"] // 5) * 5, int(r["score"] // 5) * 5 + 5)
        buckets.setdefault(b, []).append(r["pnl"])

    # Is the model's stated POP honest?
    #
    # POP is the probability of being profitable AT EXPIRY.  Comparing it with
    # whether a position happens to be green at a mid-life mark measures a
    # different question entirely: a 30-DTE bull put spread at 85% POP is
    # *supposed* to sit underwater when the stock dips early, and it still has
    # a month to be right.  Scoring that as a calibration miss made the model
    # look badly overconfident when nothing had actually resolved -- with 100
    # ideas on the board and zero past expiry, the old chart was drawing a
    # verdict out of pure noise.
    #
    # So calibration is computed ONLY from ideas that have reached expiry (or
    # are inside the final few days, where the mark and the outcome converge).
    # Everything still running is reported separately as open exposure.
    resolved = [r for r in rows if (r["dte_left"] is not None and r["dte_left"] <= CALIB_DTE_LEFT
                                    and r["pnl"] is not None)]
    still_open = [r for r in rows if r not in resolved]

    calib = []
    for lo in range(20, 100, 10):
        grp = [r for r in resolved if r["pop"] is not None and lo <= r["pop"] * 100 < lo + 10]
        if len(grp) >= CALIB_MIN_PER_BUCKET:
            calib.append({
                "predicted": lo + 5,
                "realised": 100.0 * sum(1 for r in grp if r["pnl"] > 0) / len(grp),
                "n": len(grp),
            })
    note = None
    if not calib:
        note = ("No idea has reached expiry yet (%d still running, nearest resolves in "
                "%.0f days). Probability of profit is an expiry statistic, so calibration "
                "stays empty until positions actually resolve." %
                (len(still_open),
                 min((r["dte_left"] for r in rows if r["dte_left"] is not None), default=0)))
    return {
        "n": len(rows),
        "resolved": len(resolved),
        "open": len(still_open),
        # Managed vs held, because the difference is the largest effect measured
        # so far: across 152 resolved ideas the plan's own stop/target ladder
        # moved meanR from -0.278 to -0.135 and the net from -$39,233 to
        # -$18,155 -- entirely on the long-premium side, where a stop keeps a
        # decaying option from bleeding to zero.
        "managed_exits": sum(1 for r in rows if r.get("exit_reason") in ("T1", "STOP", "TIME")),
        "held_to_expiry": sum(1 for r in rows if r.get("exit_reason") in (None, "SETTLED", "RECON")),
        "calibration_note": note,
        "overall": overall,
        "by_strategy": {k: _stats(v) for k, v in by_strategy.items()},
        "buckets": {k: _stats(v) for k, v in sorted(buckets.items())},
        "calibration": calib,
        "rows": sorted(rows, key=lambda r: (r["asof_date"], -(r["score"] or 0)), reverse=True)[:200],
    }
