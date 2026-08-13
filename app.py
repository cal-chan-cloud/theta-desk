"""Theta Desk web server.

Reads only from SQLite, so pages load instantly and keep working when the
market data sources are down.  The pipeline is the only writer; the one
exception is `/api/refresh`, which runs the pipeline for a single symbol on
demand so you are never stuck looking at a stale chain.

    python app.py            -> http://127.0.0.1:5058
"""

import datetime as dt
import json
import math
import threading
import traceback

from flask import Flask, jsonify, request, send_from_directory, render_template
from flask.json.provider import DefaultJSONProvider

import config
import db
import journal
import marketcal
import net
from model import bs, chain as chainmod, parity, scanner, strategies, vol


def _finite(obj):
    """Recursively replace NaN/Infinity with None.

    Python's json module happily emits the bare tokens `Infinity` and `NaN`,
    which are NOT valid JSON: the browser's response.json() throws, and because
    that happens inside a fetch the whole view renders blank with no visible
    error.  A single all-winners profit factor did exactly that to the Journal
    tab.  Sanitising in the provider means no endpoint -- present or future --
    can reintroduce it.
    """
    if isinstance(obj, float):
        return obj if obj == obj and obj not in (float("inf"), float("-inf")) else None
    if isinstance(obj, dict):
        return {k: _finite(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_finite(v) for v in obj]
    return obj


class SafeJSON(DefaultJSONProvider):
    sort_keys = False

    def dumps(self, obj, **kwargs):
        kwargs.setdefault("allow_nan", False)
        return super().dumps(_finite(obj), **kwargs)


app = Flask(__name__, static_folder="static", template_folder="templates")
app.json = SafeJSON(app)

# Password gate.  A no-op until a password is configured, so a fresh clone
# still runs; `python set_password.py` turns it on.
import auth  # noqa: E402  (imported after `app` exists, by design)
auth.install(app)

_refresh_lock = threading.Lock()
_refreshing = set()


# --------------------------------------------------------------------- utils
def jnum(v, nd=None):
    """JSON-safe number: NaN/inf become None rather than invalid JSON."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return v
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return round(f, nd) if nd is not None else f


def loads(s, default=None):
    if not s:
        return default
    try:
        return json.loads(s)
    except (ValueError, TypeError):
        return default


def latest_date():
    return db.scalar("SELECT MAX(asof_date) FROM symbol_metrics") or \
        marketcal.session_date().isoformat()


def latest_metrics(symbol):
    return db.q1("""SELECT * FROM symbol_metrics WHERE symbol=?
                     ORDER BY ts DESC LIMIT 1""", (symbol.upper(),))


def roll_from_db(symbol):
    """Rebuild a chain-analysis context from the last stored snapshot.

    The fitted smile is reconstructed from its stored coefficients so the server
    can reprice a position at any spot/date without re-fetching -- that is what
    makes payoff curves and daily marks consistent with the model that produced
    the idea in the first place.
    """
    symbol = symbol.upper()
    snap = db.q1("SELECT * FROM chain_snapshot WHERE symbol=? ORDER BY ts DESC LIMIT 1",
                 (symbol,))
    if not snap:
        return None
    rows = db.q("SELECT * FROM expiry_metrics WHERE snapshot_id=? ORDER BY expiry",
                (snap["id"],))
    exps = []
    for r in rows:
        d = dict(r)
        if r.get("smile_a") is not None and r.get("smile_scale"):
            d["smile_obj"] = vol.Smile(
                r["smile_a"], r["smile_b"], r["smile_c"], r.get("smile_rmse") or 0.0,
                r.get("smile_k_min") or -0.3, r.get("smile_k_max") or 0.3,
                r["smile_scale"], r.get("n_quotes") or 0,
                r.get("t_years") or 0.1, r.get("forward") or snap["spot"])
        d["contracts"] = []
        exps.append(d)
    return {"symbol": symbol, "spot": snap["spot"], "expiries": exps,
            "asof": snap["ts"], "snapshot_id": snap["id"]}


# ---------------------------------------------------------------- static/app
@app.route("/")
def index():
    return render_template("index.html", port=config.PORT)


@app.route("/favicon.ico")
def favicon():
    return ("", 204)


@app.route("/static/<path:path>")
def static_files(path):
    return send_from_directory("static", path)


# ------------------------------------------------------------------- overview
@app.route("/api/overview")
def api_overview():
    """Everything the dashboard needs in one round trip."""
    date = latest_date()
    rows = db.q("""SELECT sm.* FROM symbol_metrics sm
                   JOIN (SELECT symbol, MAX(ts) AS ts FROM symbol_metrics
                          GROUP BY symbol) m
                     ON m.symbol = sm.symbol AND m.ts = sm.ts
                   ORDER BY sm.symbol""")

    # 30 sessions of closes per symbol for the watchlist sparklines.  One
    # windowed query beats 35 round trips and keeps the payload under ~40 KB.
    spark = {}
    for s in db.q("""SELECT symbol, date, close FROM (
                        SELECT symbol, date, close,
                               ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY date DESC) rn
                          FROM ohlc)
                      WHERE rn <= 30 ORDER BY symbol, date"""):
        spark.setdefault(s["symbol"], []).append(round(s["close"], 4))

    out = []
    for r in rows:
        d = loads(r.get("detail_json"), {}) or {}
        out.append({
            "symbol": r["symbol"], "asof": r["ts"], "spot": jnum(r["spot"], 2),
            "change_pct": jnum(d.get("change_pct"), 2),
            "iv30": jnum(r["iv30"], 4), "iv60": jnum(r["iv60"], 4), "iv90": jnum(r["iv90"], 4),
            "hv20": jnum(r["hv20"], 4), "vol_forecast": jnum(r["vol_forecast"], 4),
            "vrp": jnum(r["vrp"], 4), "vrp_ratio": jnum(r["vrp_ratio"], 3),
            "vol_edge": jnum(r["vol_edge"], 4),
            "iv_rank": jnum(r["iv_rank"], 1), "iv_pct": jnum(r["iv_pct"], 1),
            "iv_samples": r["iv_samples"],
            "hv_rank": jnum(r["hv_rank"], 1), "hv_pct": jnum(r["hv_pct"], 1),
            "term_slope": jnum(r["term_slope"], 4), "skew_25": jnum(r["skew_25"], 4),
            "trend_score": jnum(r["trend_score"], 3), "trend_label": r["trend_label"],
            "rsi": jnum(r["rsi"], 1), "adx": jnum(r["adx"], 1),
            "atr_pct": jnum(r["atr_pct"], 4),
            "ret_5": jnum(r["ret_5"], 4), "ret_20": jnum(r["ret_20"], 4),
            "rel_strength": jnum(r["rel_strength"], 4),
            "rvol": jnum(r["rvol"], 2), "opt_rvol": jnum(r["opt_rvol"], 2),
            "opt_volume": jnum(r["opt_volume"]), "opt_oi": jnum(r["opt_oi"]),
            "opt_dollar_volume": jnum(r["opt_dollar_volume"]),
            "call_volume": jnum(d.get("call_volume")), "put_volume": jnum(d.get("put_volume")),
            "pcr_vol": jnum(r["pcr_vol"], 3), "pcr_oi": jnum(r["pcr_oi"], 3),
            "gex_total": jnum(r["gex_total"]), "gamma_flip": jnum(r["gamma_flip"], 2),
            "max_pain": jnum(r["max_pain"], 2),
            "news_score": jnum(r["news_score"], 3), "news_count": r["news_count"],
            "news_buzz": jnum(r["news_buzz"], 2),
            "earnings_date": r["earnings_date"], "days_to_earnings": r["days_to_earnings"],
            "implied_earnings_move": jnum(r["implied_earnings_move"], 4),
            "regime": r["regime"], "vol_regime": r["vol_regime"],
            "trend_regime": r["trend_regime"],
            "unusual_score": jnum(r["unusual_score"], 2),
            "support": jnum(r["support"], 2), "resistance": jnum(r["resistance"], 2),
            "jump_share": jnum(r["jump_share"], 3),
            "atr_pct": jnum(r["atr_pct"], 4),
            "spark": spark.get(r["symbol"], []),
        })
    macro = db.kv_get("macro") or {}
    last_run = db.kv_get("last_run") or {}
    market_news = db.q("""SELECT title, link, source, published, sentiment
                            FROM news WHERE symbol='_MARKET'
                           ORDER BY published DESC LIMIT 12""")
    return jsonify({
        "asof_date": date,
        "market_state": marketcal.market_state(),
        "server_time_et": marketcal.now_et().strftime("%Y-%m-%d %H:%M:%S"),
        "symbols": out,
        "macro": macro,
        "last_run": last_run,
        "market_news": market_news,
        "watchlist": config.WATCHLIST,
        "totals": {
            "opt_volume": sum(x["opt_volume"] or 0 for x in out),
            "opt_dollar_volume": sum(x["opt_dollar_volume"] or 0 for x in out),
            "call_volume": sum(x["call_volume"] or 0 for x in out),
            "put_volume": sum(x["put_volume"] or 0 for x in out),
        },
    })


# -------------------------------------------------------------------- symbol
@app.route("/api/symbol/<symbol>")
def api_symbol(symbol):
    symbol = symbol.upper()
    m = latest_metrics(symbol)
    if not m:
        return jsonify({"error": "no data for %s -- run the pipeline" % symbol}), 404
    detail = loads(m.get("detail_json"), {}) or {}

    snap = db.q1("""SELECT * FROM chain_snapshot WHERE symbol=?
                     ORDER BY ts DESC LIMIT 1""", (symbol,))
    exps = db.q("""SELECT * FROM expiry_metrics WHERE snapshot_id=?
                    ORDER BY expiry""", (snap["id"],)) if snap else []

    bars = db.q("""SELECT date, open, high, low, close, volume FROM ohlc
                    WHERE symbol=? ORDER BY date DESC LIMIT 400""", (symbol,))
    bars = list(reversed(bars))

    news = db.q("""SELECT title, summary, link, source, published, sentiment,
                          confidence, weight, tags
                     FROM news WHERE symbol=? ORDER BY published DESC LIMIT 40""", (symbol,))

    iv_hist = db.q("""SELECT asof_date, MAX(ts) AS ts, iv30, hv20, vol_forecast
                        FROM symbol_metrics WHERE symbol=?
                       GROUP BY asof_date ORDER BY asof_date""", (symbol,))

    ideas = db.q("""SELECT * FROM idea WHERE symbol=? AND asof_date=?
                     ORDER BY score DESC""", (symbol, m["asof_date"]))

    return jsonify({
        "symbol": symbol,
        "metrics": {k: jnum(v) if isinstance(v, (int, float)) else v
                    for k, v in m.items() if k != "detail_json"},
        "detail": detail,
        "snapshot": snap,
        "expiries": [_clean_expiry(e) for e in exps],
        "bars": bars,
        "news": news,
        "iv_history": iv_hist,
        "ideas": [_clean_idea(i) for i in ideas],
    })


def _clean_expiry(e):
    keep = ("expiry", "dte", "t_years", "t_vol", "forward", "discount", "rate", "div_yield",
            "parity_r2", "parity_method", "n_quotes", "atm_iv", "mfiv",
            "smile_a", "smile_b", "smile_c", "smile_scale", "smile_rmse",
            "skew_25", "rr_25", "fly_25", "iv_25p", "iv_25c", "slope_atm",
            "expected_move", "em_pct", "em_low", "em_high", "straddle", "straddle_pct",
            "max_pain", "gex", "gamma_flip", "call_wall", "put_wall", "pcr_vol", "pcr_oi",
            "call_volume", "put_volume", "call_oi", "put_oi",
            "total_volume", "total_oi", "arb_flags")
    return {k: (jnum(e[k]) if isinstance(e.get(k), (int, float)) else e.get(k))
            for k in keep if k in e}


def _clean_idea(i):
    m = loads(i.get("metrics_json"), {}) or {}
    t = loads(i.get("targets_json"), {}) or {}
    return {
        "id": i["id"], "asof_date": i["asof_date"], "symbol": i["symbol"],
        "strategy": i["strategy"], "label": m.get("label", i["strategy"]),
        "direction": i["direction"], "expiry": i["expiry"], "dte": jnum(i["dte"], 1),
        "legs": loads(i["legs_json"], []),
        "entry_price": jnum(i["entry_price"], 3),
        "credit_debit": m.get("credit_debit"),
        "max_profit": jnum(i["max_profit"], 3), "max_loss": jnum(i["max_loss"], 3),
        "breakevens": loads(i["breakevens"], []),
        "pop": jnum(i["pop"], 4), "ev": jnum(i["ev"], 4),
        "ev_per_risk": jnum(i["ev_per_risk"], 2), "cvar5": jnum(i["cvar5"], 3),
        "score": jnum(i["score"], 1), "rank": i["rank"], "qty": i["qty"],
        "targets": t.get("targets", []), "stop": t.get("stop"),
        "time_stop": t.get("time_stop"),
        "rationale": i["rationale"],
        "components": m.get("components", {}), "greeks": m.get("greeks", {}),
        "regime": m.get("regime", {}), "liquidity": m.get("liquidity"),
        "atm_iv": m.get("atm_iv"), "mfiv": m.get("mfiv"),
        "forecast_vol": m.get("forecast_vol"), "sharpe": m.get("sharpe"),
        "p05": m.get("p05"), "p50": m.get("p50"), "p95": m.get("p95"),
        "ev_q": m.get("ev_q"), "warnings": m.get("warnings", []),
        "risk_dollars": m.get("risk_dollars"),
    }


# --------------------------------------------------------------------- chain
@app.route("/api/chain/<symbol>")
def api_chain(symbol):
    symbol = symbol.upper()
    expiry = request.args.get("expiry")
    snap = db.q1("SELECT * FROM chain_snapshot WHERE symbol=? ORDER BY ts DESC LIMIT 1",
                 (symbol,))
    if not snap:
        return jsonify({"error": "no chain for %s" % symbol}), 404
    if not expiry:
        expiry = db.scalar("""SELECT expiry FROM expiry_metrics WHERE snapshot_id=?
                               AND dte >= 7 ORDER BY dte LIMIT 1""", (snap["id"],)) \
            or db.scalar("""SELECT MIN(expiry) FROM expiry_metrics WHERE snapshot_id=?""",
                         (snap["id"],))
    rows = db.q("""SELECT * FROM contract_quote
                    WHERE snapshot_id=? AND expiry=? ORDER BY strike, right""",
                (snap["id"], expiry))
    em = db.q1("SELECT * FROM expiry_metrics WHERE snapshot_id=? AND expiry=?",
               (snap["id"], expiry))
    all_exp = db.q("""SELECT expiry, dte, atm_iv, mfiv, em_pct, total_volume, total_oi,
                             pcr_vol, pcr_oi, max_pain, gex, skew_25, call_wall, put_wall,
                             straddle, expected_move
                        FROM expiry_metrics WHERE snapshot_id=? ORDER BY expiry""",
                   (snap["id"],))

    strikes = {}
    for r in rows:
        s = strikes.setdefault(r["strike"], {"strike": r["strike"]})
        side = "call" if r["right"] == "C" else "put"
        s[side] = {k: jnum(r[k]) for k in
                   ("bid", "ask", "mid", "last", "volume", "open_interest", "iv", "iv_src",
                    "delta", "gamma", "theta", "vega", "rho", "vanna", "vomma", "charm",
                    "intrinsic", "extrinsic", "spread_pct", "liquidity", "vol_oi", "theo")}
        s[side]["occ"] = r["occ"]
    return jsonify({
        "symbol": symbol, "spot": jnum(snap["spot"], 2), "asof": snap["ts"],
        "expiry": expiry,
        "expiries": [_clean_expiry(e) for e in all_exp],
        "metrics": _clean_expiry(em) if em else {},
        "strikes": [strikes[k] for k in sorted(strikes)],
    })


# --------------------------------------------------------------------- ideas
@app.route("/api/ideas")
def api_ideas():
    date = request.args.get("date") or db.scalar("SELECT MAX(asof_date) FROM idea")
    rows = db.q("SELECT * FROM idea WHERE asof_date=? ORDER BY score DESC", (date,))
    dates = [r["asof_date"] for r in
             db.q("SELECT DISTINCT asof_date FROM idea ORDER BY asof_date DESC LIMIT 30")]
    return jsonify({"asof_date": date, "dates": dates,
                    "ideas": [_clean_idea(i) for i in rows]})


@app.route("/api/idea/<int:idea_id>/payoff")
def api_idea_payoff(idea_id):
    """Payoff diagram plus a mid-life value curve for one idea."""
    i = db.q1("SELECT * FROM idea WHERE id=?", (idea_id,))
    if not i:
        return jsonify({"error": "no such idea"}), 404
    legs = loads(i["legs_json"], [])
    roll = roll_from_db(i["symbol"])
    m = latest_metrics(i["symbol"])
    spot = (roll or {}).get("spot") or (m["spot"] if m else None)
    if not spot:
        return jsonify({"error": "no spot"}), 404

    pos = journal.position_from_legs(i["symbol"], i["strategy"], legs, spot, roll)
    now = marketcal.now_utc()
    near = pos.near_expiry
    half = marketcal.to_et(now).replace(tzinfo=None).date() + \
        dt.timedelta(days=max(int(marketcal.dte(near, now) * 0.5), 0))
    if half >= near:
        half = near - dt.timedelta(days=1)
    half_moment = min(marketcal.et_to_utc(dt.datetime.combine(half, dt.time(16, 0))),
                      marketcal.expiry_moment(near) - dt.timedelta(hours=1))

    lo, hi = spot * 0.75, spot * 1.25
    t = loads(i["targets_json"], {}) or {}
    pts = [x for tg in t.get("targets", []) for x in
           (tg.get("spot"), tg.get("spot_up"), tg.get("spot_down")) if x]
    if t.get("stop", {}).get("spot"):
        pts.append(t["stop"]["spot"])
    pts += [b for b in loads(i["breakevens"], [])]
    if pts:
        lo = min(lo, min(pts) * 0.97)
        hi = max(hi, max(pts) * 1.03)
    return jsonify({
        "symbol": i["symbol"], "spot": jnum(spot, 2),
        "expiry": pos.near_expiry.isoformat(),
        "at_expiry": [{"spot": jnum(p["spot"], 2), "pnl": jnum(p["pnl"], 4)}
                      for p in pos.payoff_curve(lo, hi, 161)],
        "now": [{"spot": jnum(p["spot"], 2), "pnl": jnum(p["pnl"], 4)}
                for p in pos.value_curve(now, lo, hi, 121)],
        "halfway": [{"spot": jnum(p["spot"], 2), "pnl": jnum(p["pnl"], 4)}
                    for p in pos.value_curve(half_moment, lo, hi, 121)],
        "halfway_date": half.isoformat(),
        "breakevens": loads(i["breakevens"], []),
        "targets": t.get("targets", []), "stop": t.get("stop"),
        "max_profit": jnum(i["max_profit"], 3), "max_loss": jnum(i["max_loss"], 3),
        "entry_price": jnum(i["entry_price"], 3),
    })


# ------------------------------------------------------------------- journal
@app.route("/api/trades", methods=["GET", "POST"])
def api_trades():
    if request.method == "POST":
        payload = request.get_json(force=True, silent=True) or {}
        try:
            if payload.get("from_idea"):
                i = db.q1("SELECT * FROM idea WHERE id=?", (payload["from_idea"],))
                if not i:
                    return jsonify({"error": "no such idea"}), 404
                m = latest_metrics(i["symbol"])
                t = loads(i["targets_json"], {})
                payload = {
                    "idea_id": i["id"], "symbol": i["symbol"], "strategy": i["strategy"],
                    "direction": i["direction"], "expiry": i["expiry"],
                    "legs": loads(i["legs_json"], []),
                    "qty": payload.get("qty") or i["qty"] or 1,
                    "entry_price": payload.get("entry_price", i["entry_price"]),
                    "entry_spot": (m or {}).get("spot"),
                    "entry_iv": (m or {}).get("iv30"),
                    "max_profit": i["max_profit"], "max_loss": i["max_loss"],
                    "targets": t, "notes": payload.get("notes"),
                    "tags": payload.get("tags"),
                }
            tid = journal.open_trade(payload)
            return jsonify({"ok": True, "id": tid})
        except Exception as e:                       # noqa: BLE001
            return jsonify({"error": str(e), "trace": traceback.format_exc()}), 400
    status = request.args.get("status", "all")
    return jsonify({"trades": journal.list_trades(status)})


@app.route("/api/trades/<int:trade_id>", methods=["PATCH", "DELETE"])
def api_trade(trade_id):
    if request.method == "DELETE":
        journal.delete_trade(trade_id)
        return jsonify({"ok": True})
    p = request.get_json(force=True, silent=True) or {}
    if p.get("action") == "close":
        t = journal.close_trade(trade_id, float(p["exit_price"]), p.get("exit_date"),
                                p.get("reason"), p.get("exit_spot"))
        return jsonify({"ok": True, "trade": t})
    fields = {k: p[k] for k in ("notes", "tags", "qty", "entry_price") if k in p}
    if fields:
        sets = ", ".join("%s=?" % k for k in fields)
        db.execute("UPDATE trade SET %s, updated=? WHERE id=?" % sets,
                   tuple(fields.values()) + (marketcal.now_utc().isoformat(), trade_id))
    return jsonify({"ok": True})


@app.route("/api/performance")
def api_performance():
    return jsonify({
        "journal": journal.performance(),
        "scanner": journal.idea_performance(days=int(request.args.get("days", 90))),
    })


@app.route("/api/trade/<int:trade_id>/marks")
def api_trade_marks(trade_id):
    return jsonify({"marks": db.q(
        "SELECT * FROM trade_mark WHERE trade_id=? ORDER BY date", (trade_id,))})


# ----------------------------------------------------------------- utilities
@app.route("/api/flow")
def api_flow():
    """Cross-market options flow: unusual activity and volume leaders."""
    rows = db.q("""SELECT sm.symbol, sm.spot, sm.opt_volume, sm.opt_oi,
                          sm.opt_dollar_volume, sm.opt_rvol, sm.pcr_vol, sm.pcr_oi,
                          sm.unusual_score, sm.iv30, sm.trend_score, sm.detail_json
                     FROM symbol_metrics sm
                     JOIN (SELECT symbol, MAX(ts) ts FROM symbol_metrics GROUP BY symbol) m
                       ON m.symbol=sm.symbol AND m.ts=sm.ts""")
    unusual = []
    for r in rows:
        d = loads(r["detail_json"], {}) or {}
        for u in (d.get("unusual") or []):
            u = dict(u)
            u["symbol"] = r["symbol"]
            u["spot"] = jnum(r["spot"], 2)
            unusual.append(u)
    unusual.sort(key=lambda u: -(u.get("dollar_volume") or 0))
    leaders = sorted(rows, key=lambda r: -(r["opt_dollar_volume"] or 0))
    return jsonify({
        "unusual": unusual[:60],
        "leaders": [{"symbol": r["symbol"], "spot": jnum(r["spot"], 2),
                     "volume": jnum(r["opt_volume"]), "oi": jnum(r["opt_oi"]),
                     "dollar_volume": jnum(r["opt_dollar_volume"]),
                     "rvol": jnum(r["opt_rvol"], 2),
                     "pcr_vol": jnum(r["pcr_vol"], 2),
                     "call_volume": jnum((loads(r["detail_json"], {}) or {}).get("call_volume")),
                     "put_volume": jnum((loads(r["detail_json"], {}) or {}).get("put_volume")),
                     "iv30": jnum(r["iv30"], 4)} for r in leaders],
    })


@app.route("/api/news")
def api_news():
    sym = (request.args.get("symbol") or "").upper()
    sql = """SELECT symbol, title, summary, link, source, published, sentiment,
                    confidence, weight, tags FROM news"""
    args = []
    if sym:
        sql += " WHERE symbol=?"
        args.append(sym)
    sql += " ORDER BY published DESC LIMIT 200"
    return jsonify({"news": db.q(sql, tuple(args))})


@app.route("/api/calculator", methods=["POST"])
def api_calculator():
    """Ad-hoc Black-Scholes calculator (the 'what if' panel)."""
    p = request.get_json(force=True, silent=True) or {}
    try:
        S = float(p["spot"]); K = float(p["strike"])
        T = max(float(p["days"]), 0.0) / 365.0
        r = float(p.get("rate", config.DEFAULT_RISK_FREE))
        qy = float(p.get("dividend", 0.0))
        right = (p.get("right") or "C").upper()[:1]
        F = S * math.exp((r - qy) * T)
        df = math.exp(-r * T)
        if p.get("price") is not None and p.get("solve_iv"):
            sigma = bs.implied_vol(float(p["price"]), F, K, T, df, right)
            if sigma is None:
                return jsonify({"error": "price outside no-arbitrage bounds"}), 400
        else:
            sigma = float(p.get("iv", 0.3))
        price = bs.black76(F, K, T, sigma, df, right)
        g = bs.greeks(S, F, K, T, sigma, df, right)
        lo, hi, em = bs.expected_move(S, sigma, T)
        return jsonify({
            "price": jnum(price, 4), "iv": jnum(sigma, 5), "forward": jnum(F, 4),
            "greeks": {k: jnum(v, 6) for k, v in g.items()},
            "prob_itm": jnum(bs.prob_itm(F, K, T, sigma, right), 4),
            "prob_touch": jnum(bs.prob_touch(S, K, sigma, T, r - qy), 4),
            "expected_move": {"low": jnum(lo, 2), "high": jnum(hi, 2), "move": jnum(em, 2)},
            "breakeven": jnum(K + price if right == "C" else K - price, 3),
        })
    except (KeyError, ValueError, TypeError) as e:
        return jsonify({"error": "bad input: %s" % e}), 400


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    """Re-run the pipeline for one symbol (or the whole list) on demand."""
    p = request.get_json(force=True, silent=True) or {}
    syms = p.get("symbols")
    if isinstance(syms, str):
        syms = [s.strip().upper() for s in syms.split(",") if s.strip()]
    syms = syms or config.WATCHLIST
    key = ",".join(sorted(syms))
    with _refresh_lock:
        if key in _refreshing:
            return jsonify({"ok": False, "error": "already refreshing"}), 409
        _refreshing.add(key)

    def work():
        try:
            import pipeline
            pipeline.run(symbols=syms, fresh=bool(p.get("fresh")),
                         do_news=p.get("news", True), quiet=True)
        except Exception:                            # noqa: BLE001
            traceback.print_exc()
        finally:
            with _refresh_lock:
                _refreshing.discard(key)

    threading.Thread(target=work, daemon=True).start()
    return jsonify({"ok": True, "symbols": syms, "started": True})


@app.route("/api/status")
def api_status():
    return jsonify({
        "last_run": db.kv_get("last_run"),
        "db": db.db_stats(),
        "http_cache": net.cache_size(),
        "market_state": marketcal.market_state(),
        "server_time_et": marketcal.now_et().strftime("%Y-%m-%d %H:%M:%S"),
        "refreshing": sorted(_refreshing),
        "auth_enabled": auth.is_enabled(),
        "config": {
            "account_size": config.ACCOUNT_SIZE,
            "risk_per_trade_pct": config.RISK_PER_TRADE_PCT,
            "watchlist_size": len(config.WATCHLIST),
            "scan_dte": [config.SCAN_MIN_DTE, config.SCAN_MAX_DTE],
        },
    })


@app.errorhandler(500)
def on_500(e):
    return jsonify({"error": "server error", "detail": str(e)}), 500


if __name__ == "__main__":
    db.init()
    print("Theta Desk -> http://%s:%d" % (config.HOST, config.PORT))
    if auth.is_enabled():
        print("Password protection is ON.")
    else:
        print("WARNING: no password set. Run  python set_password.py  to add one.")
        if config.HOST not in ("127.0.0.1", "localhost"):
            print("         HOST is %s, so this is reachable from the network." % config.HOST)
    app.run(host=config.HOST, port=config.PORT, debug=False, threaded=True)
