"""Daily pipeline: fetch -> analyse -> persist -> generate ideas -> mark book.

Run it as often as you like.  Everything is snapshot-stamped and additive, so
intraday runs build a finer-grained history without corrupting the daily series.
The site reads only from SQLite, so it stays fast and works offline once a run
has completed.

    python pipeline.py                 # full run over the watchlist
    python pipeline.py --symbols NVDA,SPY
    python pipeline.py --fresh         # ignore the HTTP cache
    python pipeline.py --no-news       # skip the slowest stage
"""

import argparse
import datetime as dt
import json
import sys
import time
import traceback

import config
import db
import marketcal
import net
from model import chain as chainmod
from model import scanner, sentiment, strategies, tech, vol
from sources import cboe, earnings as earn_src, news as news_src, prices


def log(msg, *args):
    stamp = dt.datetime.now().strftime("%H:%M:%S")
    print("[%s] %s" % (stamp, (msg % args) if args else msg), flush=True)


# ------------------------------------------------------------------ history
def store_bars(symbol, bars):
    rows = [(symbol, b["date"], b.get("open"), b.get("high"), b.get("low"),
             b["close"], b.get("volume")) for b in bars]
    db.upsert_rows("ohlc", ["symbol", "date", "open", "high", "low", "close", "volume"], rows)
    return len(rows)


def load_bars(symbol, limit=520):
    rows = db.q("""SELECT date, open, high, low, close, volume FROM ohlc
                    WHERE symbol=? ORDER BY date DESC LIMIT ?""", (symbol, limit))
    return list(reversed(rows))


def hv_series(bars, window=20, lookback=260):
    """Trailing realised vol at each of the last `lookback` sessions.

    Gives an HV rank from day one, instead of waiting a year for our own IV
    history to accumulate.  The two are reported separately because they are
    not interchangeable -- but an HV rank of 8 tells you the same "vol is
    unusually quiet" story on the first run that IV rank will confirm later.
    """
    out = []
    n = len(bars)
    start = max(window + 2, n - lookback)
    for i in range(start, n + 1):
        v = vol.close_to_close([b["close"] for b in bars[:i]], window)
        if v:
            out.append(v)
    return out


# -------------------------------------------------------------------- stages
def macro_stage(fresh=False):
    ttl = 0 if fresh else None
    quotes = prices.fetch_macro(config.MACRO_SYMBOLS, ttl=ttl)
    from model.parity import YieldCurve
    curve = YieldCurve.from_quotes(quotes)
    vix_bars = prices.fetch_history("^VIX", range_="1y", ttl=ttl)
    vix = quotes.get("^VIX")
    vix_rank = None
    if vix and vix_bars:
        closes = [b["close"] for b in vix_bars]
        rk = vol.rank_and_percentile(vix, closes)
        vix_rank = rk["rank"]
    state = {
        "quotes": quotes, "curve": curve.to_dict(),
        "vix": vix, "vix_rank": vix_rank,
        "spx": quotes.get("^GSPC"),
        "risk_free_3m": curve.rate(0.25),
        "market_state": marketcal.market_state(),
        "asof": marketcal.now_utc().replace(microsecond=0).isoformat(),
    }
    db.kv_set("macro", state)
    log("macro: VIX=%s (rank %s)  r3m=%.2f%%  market=%s",
        vix, round(vix_rank) if vix_rank is not None else "?",
        state["risk_free_3m"] * 100, state["market_state"])
    return curve, state


def news_stage(symbols, fresh=False):
    ttl = 0 if fresh else None
    out = {}
    rows = []
    for sym in symbols:
        try:
            items = news_src.fetch_ticker_news(sym, ttl=ttl)
        except Exception as e:                          # noqa: BLE001
            log("  news %s failed: %s", sym, e)
            out[sym] = sentiment.aggregate([])
            continue
        company = news_src.COMPANY_NAMES.get(sym)
        scored = [sentiment.score_item(i, sym, company) for i in items]
        out[sym] = sentiment.aggregate(scored)
        for s in scored:
            rows.append({
                "id": s["id"], "symbol": sym,
                "published": s["published"].isoformat() if s.get("published") else None,
                "fetched": marketcal.now_utc().isoformat(),
                "title": s["title"], "summary": s.get("summary"),
                "link": s.get("link"), "source": s.get("source"), "feed": s.get("feed"),
                "sentiment": s["sentiment"], "confidence": s["confidence"],
                "weight": s["weight"], "tags": ",".join(s.get("tags", [])),
            })
    try:
        market = news_src.fetch_market_news(ttl=ttl)
        for i in market:
            s = sentiment.score_item(i, "_MARKET")
            rows.append({
                "id": s["id"], "symbol": "_MARKET",
                "published": s["published"].isoformat() if s.get("published") else None,
                "fetched": marketcal.now_utc().isoformat(),
                "title": s["title"], "summary": s.get("summary"), "link": s.get("link"),
                "source": s.get("source"), "feed": s.get("feed"),
                "sentiment": s["sentiment"], "confidence": s["confidence"],
                "weight": s["weight"], "tags": ",".join(s.get("tags", [])),
            })
    except Exception as e:                              # noqa: BLE001
        log("  market news failed: %s", e)
    db.insert_dicts("news", rows)
    log("news: %d articles across %d symbols", len(rows), len(out))
    return out


def iv_history(symbol, days=None):
    days = days or config.IV_RANK_LOOKBACK_DAYS
    rows = db.q("""SELECT asof_date, MAX(ts) AS ts, iv30 FROM symbol_metrics
                    WHERE symbol=? AND iv30 IS NOT NULL
                      AND asof_date >= date('now', ?)
                    GROUP BY asof_date ORDER BY asof_date""",
                (symbol, "-%d day" % days))
    return [r["iv30"] for r in rows if r["iv30"]]


def opt_volume_history(symbol, days=30):
    rows = db.q("""SELECT asof_date, MAX(total_volume) AS v FROM chain_snapshot
                    WHERE symbol=? AND asof_date >= date('now', ?)
                    GROUP BY asof_date ORDER BY asof_date""",
                (symbol, "-%d day" % days))
    return [r["v"] for r in rows if r["v"]]


def process_symbol(symbol, curve, macro, bench_bars, news_agg, earnings_rows,
                   fresh=False, make_ideas=True):
    """Fetch, analyse and persist one symbol.  Returns (metrics, ideas)."""
    ttl = 0 if fresh else None
    now = marketcal.now_utc()
    today = marketcal.session_date().isoformat()

    ch = cboe.fetch_chain(symbol, ttl=ttl)
    if not ch:
        raise RuntimeError("no option chain")

    bars_live = prices.fetch_history(symbol, range_="2y", ttl=ttl)
    if bars_live:
        store_bars(symbol, bars_live)
    bars = load_bars(symbol)
    if len(bars) < 60:
        raise RuntimeError("only %d price bars" % len(bars))

    # --- options -------------------------------------------------------
    roll = chainmod.rollup(symbol, ch, curve, now=now)
    if not roll:
        raise RuntimeError("chain produced no usable expiries")

    # --- price / trend --------------------------------------------------
    t = tech.analyze(bars, bench_bars)
    if not t:
        raise RuntimeError("technical analysis failed")

    # Realised vol drops the live partial bar: a half-formed daily range
    # understates the true range and biases every estimator low.
    complete = [b for b in bars if not b.get("partial")]
    if bars_live and bars_live[-1].get("partial") and complete and complete[-1]["date"] == bars[-1]["date"]:
        complete = complete[:-1]
    rv = vol.all_realised(complete)
    forecast = vol.forecast_vol(rv, horizon_days=30)

    iv30 = roll["iv30"] or ch.get("iv30_source")
    ivh = iv_history(symbol)
    # IV rank needs a real sample before it means anything; HV rank is built
    # from two years of price history and is meaningful from day one.
    iv_rk = vol.rank_and_percentile(iv30, ivh, min_samples=config.IV_RANK_MIN_SAMPLES)
    hvh = hv_series(complete)
    hv_rk = vol.rank_and_percentile(rv.get("hv20"), hvh)

    vrp = (iv30 - forecast) if (iv30 and forecast) else None
    vrp_ratio = (iv30 / forecast) if (iv30 and forecast and forecast > 0) else None

    # --- events ---------------------------------------------------------
    e_row = earn_src.next_earnings(earnings_rows or [])
    e_date = marketcal.parse_date(e_row["date"]) if e_row else None
    days_to_earn = None
    if e_date:
        days_to_earn = (e_date - marketcal.session_date()).days
    # What the options market itself thinks the print is worth, from the kink
    # between the last expiry before it and the first one after.
    implied_jump = None
    if e_date and days_to_earn is not None and days_to_earn >= 0:
        implied_jump = vol.implied_event_move(roll["expiries"], e_date)

    # --- options volume context ----------------------------------------
    volh = opt_volume_history(symbol)
    prior = [v for v in volh[:-1]] if len(volh) > 1 else []
    opt_rvol = None
    if prior:
        avg = sum(prior) / len(prior)
        if avg > 0:
            opt_rvol = roll["opt_volume"] / avg

    na = news_agg or {}
    unusual = roll["unusual"]
    unusual_score = sum(u["score"] for u in unusual[:5]) / 5.0 if unusual else 0.0

    metrics = {
        "symbol": symbol, "asof_date": today, "ts": ch["ts"], "spot": roll["spot"],
        "iv30": iv30, "iv60": roll["iv60"], "iv90": roll["iv90"],
        "term_slope": roll["term_slope"],
        "hv10": rv.get("hv10"), "hv20": rv.get("hv20"), "hv30": rv.get("hv30"),
        "hv60": rv.get("hv60"), "hv252": rv.get("hv252"),
        "hv_yz20": rv.get("yz20"), "hv_gk20": rv.get("gk20"),
        "hv_park20": rv.get("park20"), "hv_ewma": rv.get("ewma"),
        "vol_forecast": forecast, "vrp": vrp, "vrp_ratio": vrp_ratio,
        "iv_rank": iv_rk["rank"], "iv_pct": iv_rk["pct"], "iv_samples": iv_rk["n"],
        "hv_rank": hv_rk["rank"], "hv_pct": hv_rk["pct"],
        "trend_score": t["trend_score"], "trend_label": t["trend_label"],
        "rsi": t["rsi"], "macd": t["macd"], "macd_hist": t["macd_hist"],
        "adx": t["adx"], "atr": t["atr"], "atr_pct": t["atr_pct"],
        "sma20": t["sma20"], "sma50": t["sma50"], "sma200": t["sma200"], "ema20": t["ema20"],
        "bb_pctb": t["bb_pctb"], "bb_width": t["bb_width"],
        "rel_strength": t["rel_strength"],
        "ret_5": t["ret_5"], "ret_20": t["ret_20"], "ret_60": t["ret_60"],
        "rvol": t["rvol"], "obv_slope": t["obv_slope"],
        "support": t["support"], "resistance": t["resistance"],
        "news_score": na.get("score", 0.0), "news_count": na.get("count", 0),
        "news_buzz": na.get("buzz", 0.0),
        "news_confidence": na.get("confidence", 0.0),
        "earnings_date": e_date.isoformat() if e_date else None,
        "days_to_earnings": days_to_earn,
        "implied_earnings_move": implied_jump,
        "jump_share": rv.get("jump_share"),
        "opt_volume": roll["opt_volume"], "opt_oi": roll["opt_oi"],
        "opt_dollar_volume": roll["opt_dollar_volume"], "opt_rvol": opt_rvol,
        "pcr_vol": roll["pcr_vol"], "pcr_oi": roll["pcr_oi"],
        "gex_total": roll["gex_total"], "gamma_flip": roll["gamma_flip"],
        "max_pain": roll["max_pain"],
        "skew_25": roll["skew_25"],
        "unusual_score": unusual_score,
    }
    vr = scanner.vol_regime(iv30, forecast, iv_rk["rank"])
    tr = scanner.trend_regime(t["trend_score"])
    metrics["regime"] = "%s / IV %s" % (tr, vr["label"])
    metrics["vol_regime"] = vr["label"]
    metrics["trend_regime"] = tr
    metrics["vol_edge"] = vr["edge"]

    detail = {
        "trend_components": t["trend_components"],
        "levels": t["levels"],
        "realised": rv,
        "iv_rank_detail": iv_rk, "hv_rank_detail": hv_rk,
        "news": na,
        "high_52w": t["high_52w"], "low_52w": t["low_52w"], "pos_52w": t["pos_52w"],
        "plus_di": t["plus_di"], "minus_di": t["minus_di"],
        "bb_upper": t["bb_upper"], "bb_lower": t["bb_lower"],
        "unusual": unusual,
        "gamma_profile": roll["gamma_profile"]["curve"],
        "avg_volume": t["avg_volume"],
        "call_volume": roll["call_volume"], "put_volume": roll["put_volume"],
        "call_oi": roll["call_oi"], "put_oi": roll["put_oi"],
        "underlying_volume": ch.get("volume"),
        "change_pct": ch.get("change_pct"),
        "prev_close": ch.get("prev_close"),
        "day_open": ch.get("open"), "day_high": ch.get("high"), "day_low": ch.get("low"),
        "n_contracts": roll["n_contracts"],
        "earnings_time": e_row.get("time") if e_row else None,
        "earnings_eps": e_row.get("eps_forecast") if e_row else None,
    }
    metrics["detail_json"] = json.dumps(detail, default=str)

    persist_symbol(symbol, ch, roll, metrics)

    ideas = []
    if make_ideas:
        try:
            ideas = scanner.build_ideas(symbol, roll, metrics, now=now)
        except Exception as e:                          # noqa: BLE001
            log("  ideas %s failed: %s", symbol, e)
            if "--debug" in sys.argv:
                traceback.print_exc()
    return metrics, ideas, roll


def persist_symbol(symbol, ch, roll, metrics):
    today = metrics["asof_date"]
    db.insert_dict("underlying_snapshot", {
        "symbol": symbol, "asof_date": today, "ts": ch["ts"], "price": ch["spot"],
        "prev_close": ch.get("prev_close"), "open": ch.get("open"),
        "high": ch.get("high"), "low": ch.get("low"), "volume": ch.get("volume"),
        "change_pct": ch.get("change_pct"), "iv30_source": ch.get("iv30_source"),
    })
    snap_id = db.insert_dict("chain_snapshot", {
        "symbol": symbol, "asof_date": today, "ts": ch["ts"],
        "source_ts": ch.get("source_ts"), "spot": ch["spot"],
        "n_contracts": roll["n_contracts"], "n_expiries": len(roll["expiries"]),
        "total_volume": roll["opt_volume"], "total_oi": roll["opt_oi"],
        "call_volume": roll["call_volume"], "put_volume": roll["put_volume"],
        "call_oi": roll["call_oi"], "put_oi": roll["put_oi"],
        "dollar_volume": roll["opt_dollar_volume"],
        "risk_free": metrics.get("risk_free"),
    })
    if snap_id is None:
        snap_id = db.scalar("SELECT id FROM chain_snapshot WHERE symbol=? AND ts=?",
                            (symbol, ch["ts"]))

    quotes, exp_rows = [], []
    for m in roll["expiries"]:
        for c in m["contracts"]:
            quotes.append({
                "snapshot_id": snap_id, "symbol": symbol, "asof_date": today,
                "occ": c["occ"], "expiry": m["expiry"], "right": c["right"],
                "strike": c["strike"], "bid": c.get("bid"), "ask": c.get("ask"),
                "mid": c.get("mid"), "last": c.get("last"), "theo": c.get("theo"),
                "bid_size": c.get("bid_size"), "ask_size": c.get("ask_size"),
                "volume": c.get("volume"), "open_interest": c.get("open_interest"),
                "iv_src": c.get("iv_src"), "iv": c.get("iv"),
                "delta": c.get("delta"), "gamma": c.get("gamma"), "theta": c.get("theta"),
                "vega": c.get("vega"), "rho": c.get("rho"), "vanna": c.get("vanna"),
                "vomma": c.get("vomma"), "charm": c.get("charm"),
                "intrinsic": c.get("intrinsic"), "extrinsic": c.get("extrinsic"),
                "moneyness": c.get("moneyness"), "spread_pct": c.get("spread_pct"),
                "liquidity": c.get("liquidity"), "vol_oi": c.get("vol_oi"),
                "dte": c.get("dte"),
            })
        row = {k: v for k, v in m.items()
               if k not in ("contracts", "smile_obj", "smile", "strikes")}
        row.update({"snapshot_id": snap_id, "symbol": symbol, "asof_date": today})
        if m.get("smile"):
            s = m["smile"]
            # Store every coefficient the smile needs to be *rebuilt*, not just
            # displayed: the server reconstructs it to reprice payoff curves and
            # to mark positions whose expiry has rolled out of the quote table.
            row.update({"smile_a": s["a"], "smile_b": s["b"], "smile_c": s["c"],
                        "smile_scale": s["scale"], "smile_k_min": s["k_min"],
                        "smile_k_max": s["k_max"], "smile_rmse": s["rmse"]})
        exp_rows.append(row)

    db.insert_dicts("contract_quote", quotes)
    db.insert_dicts("expiry_metrics", exp_rows)
    db.insert_dict("symbol_metrics", metrics)
    return snap_id


def persist_ideas(ideas):
    today = marketcal.session_date().isoformat()
    ts = marketcal.now_utc().replace(microsecond=0).isoformat()
    # Today's run replaces today's board.  Leg greeks shift between intraday
    # runs, so the UNIQUE key never matches and re-running would otherwise stack
    # near-identical copies of every idea.  Prior days are never touched -- that
    # history is the scanner's forward test.
    db.execute("""DELETE FROM idea_outcome WHERE idea_id IN
                  (SELECT id FROM idea WHERE asof_date=?)""", (today,))
    db.execute("DELETE FROM idea WHERE asof_date=?", (today,))
    rows = []
    for i in ideas:
        rows.append({
            "asof_date": today, "ts": ts, "symbol": i["symbol"],
            "strategy": i["strategy"], "direction": i["direction"],
            "expiry": i["expiry"], "dte": i["dte"],
            "legs_json": json.dumps(i["legs"]),
            "entry_price": i["net_price"], "max_profit": i["max_profit"],
            "max_loss": i["max_loss"], "breakevens": json.dumps(i["breakevens"]),
            "pop": i["pop"], "ev": i["ev"], "ev_per_risk": i["ev_pct_of_risk"],
            "cvar5": i["cvar5"], "score": i["score"], "rank": i.get("rank"),
            "qty": i["qty"],
            "targets_json": json.dumps({"targets": i["targets"], "stop": i["stop"],
                                        "time_stop": i["time_stop"]}),
            "rationale": i["rationale"],
            "metrics_json": json.dumps({
                "components": i["components"], "greeks": i["greeks"],
                "regime": i["regime"], "liquidity": i["liquidity"],
                "atm_iv": i["atm_iv"], "mfiv": i["mfiv"],
                "forecast_vol": i["forecast_vol"], "sharpe": i["sharpe"],
                "p05": i["p05"], "p50": i["p50"], "p95": i["p95"],
                "ev_q": i["ev_q"], "warnings": i["warnings"],
                "risk_dollars": i["risk_dollars"], "credit_debit": i["credit_debit"],
                "label": i["label"],
                # Horizon vs expiry expectancy: the whole point of the horizon
                # change is being able to SEE the difference, so both are stored.
                "ev_expiry": i.get("ev_expiry"), "pop_expiry": i.get("pop_expiry"),
                "eval_days": i.get("eval_days"), "pop_raw": i.get("pop_raw"),
            }, default=str),
        })
    db.insert_dicts("idea", rows)
    return len(rows)


# ------------------------------------------------------------------ marking
def mark_book(rolls):
    """Mark open journal trades and yesterday's ideas against today's chain."""
    import journal
    today = marketcal.session_date().isoformat()
    n_tr = journal.mark_open_trades(rolls, today)
    n_id = journal.mark_ideas(rolls, today)
    log("marked %d open trades, %d prior ideas", n_tr, n_id)
    return n_tr, n_id


# --------------------------------------------------------------------- main
def run(symbols=None, fresh=False, do_news=True, do_ideas=True, quiet=False):
    t0 = time.time()
    db.init()
    net.reset_stats()
    symbols = symbols or config.WATCHLIST
    run_id = db.execute("INSERT INTO pipeline_run (started, stage) VALUES (?,?)",
                        (marketcal.now_utc().isoformat(), "start"))

    stats = {"symbols": 0, "failed": [], "ideas": 0, "contracts": 0}
    try:
        curve, macro = macro_stage(fresh=fresh)

        log("earnings calendar...")
        try:
            earnings_map = earn_src.fetch_window(symbols, ttl=0 if fresh else None)
        except Exception as e:                          # noqa: BLE001
            log("  earnings failed: %s", e)
            earnings_map = {}
        rows = []
        for sym, ers in earnings_map.items():
            for r in ers:
                r = dict(r)
                r["fetched"] = marketcal.now_utc().isoformat()
                rows.append(r)
        db.insert_dicts("earnings", rows)
        log("earnings: %d dates for %d symbols", len(rows), len(earnings_map))

        news_agg = news_stage(symbols, fresh=fresh) if do_news else {}

        bench = prices.fetch_history(config.BENCHMARK, range_="1y",
                                     ttl=0 if fresh else None)
        store_bars(config.BENCHMARK, bench)

        # PASS 1 -- metrics only.  Ideas are deferred until every symbol has been
        # measured, because the vol regime is now decided cross-sectionally and
        # a name's percentile cannot be known before its peers exist.
        all_ideas, rolls, metrics_by_sym = [], {}, {}
        for i, sym in enumerate(symbols, 1):
            t1 = time.time()
            try:
                m, ideas, roll = process_symbol(
                    sym, curve, macro, bench, news_agg.get(sym), earnings_map.get(sym),
                    fresh=fresh, make_ideas=False)
                rolls[sym] = roll
                metrics_by_sym[sym] = (m, roll)
                stats["symbols"] += 1
                stats["contracts"] += roll["n_contracts"]
                if not quiet:
                    log("  %2d/%d %-6s spot=%8.2f iv30=%5.1f%% fc=%5.1f%% ivR=%s "
                        "trend=%+.2f %-12s ideas=%d (%.1fs)",
                        i, len(symbols), sym, m["spot"], (m["iv30"] or 0) * 100,
                        (m["vol_forecast"] or 0) * 100,
                        ("%3.0f" % m["iv_rank"]) if m["iv_rank"] is not None else " --",
                        m["trend_score"], m["regime"], len(ideas), time.time() - t1)
            except Exception as e:                      # noqa: BLE001
                stats["failed"].append("%s: %s" % (sym, e))
                log("  %2d/%d %-6s FAILED: %s", i, len(symbols), sym, e)
                if "--debug" in sys.argv:
                    traceback.print_exc()

        # PASS 2 -- rank each name's vol edge against its peers, then build.
        # How often has each name already been proposed on recent boards?  A
        # name that reappears every day accumulates into a concentrated bet
        # nobody decided to make.
        recent = {}
        for r in db.q("""SELECT symbol, COUNT(*) n FROM idea
                          WHERE asof_date >= date('now', ?) AND asof_date < ?
                          GROUP BY symbol""",
                      ("-%d day" % config.CONCENTRATION_LOOKBACK_DAYS,
                       marketcal.session_date().isoformat())):
            recent[r["symbol"]] = r["n"]

        edges = sorted((m.get("vol_edge") for m, _r in metrics_by_sym.values()
                        if m.get("vol_edge") is not None))
        if do_ideas and edges:
            for sym, (m, roll) in metrics_by_sym.items():
                e = m.get("vol_edge")
                if e is not None and len(edges) > 4:
                    below = sum(1 for x in edges if x < e)
                    m["vol_edge_pctile"] = 100.0 * below / len(edges)
                    # Re-classify with the peer group now known, and WRITE IT
                    # BACK.  Pass 1 had no cross-section, so its label came from
                    # the absolute fallback; leaving that stored would show the
                    # dashboard one regime while the scanner traded another.
                    vr2 = scanner.vol_regime(m.get("iv30"), m.get("vol_forecast"),
                                             m.get("iv_rank"), pctile=m["vol_edge_pctile"])
                    m["vol_regime"] = vr2["label"]
                    m["regime"] = "%s / IV %s" % (m.get("trend_regime"), vr2["label"])
                    db.execute("""UPDATE symbol_metrics
                                     SET vol_edge_pctile=?, vol_regime=?, regime=?
                                   WHERE symbol=? AND ts=?""",
                               (m["vol_edge_pctile"], m["vol_regime"], m["regime"],
                                sym, m["ts"]))
                m["recent_appearances"] = recent.get(sym, 0)
                try:
                    all_ideas.extend(scanner.build_ideas(sym, roll, m, now=marketcal.now_utc()))
                except Exception as e:                     # noqa: BLE001
                    log("  ideas %s failed: %s", sym, e)
            log("cross-section: vol edge from %.0f%% to %.0f%% across %d names",
                edges[0] * 100, edges[-1] * 100, len(edges))

        ranked = scanner.rank_all(all_ideas)
        stats["ideas"] = persist_ideas(ranked)
        log("ideas: %d generated, %d stored after ranking", len(all_ideas), stats["ideas"])

        try:
            mark_book(rolls)
        except Exception as e:                          # noqa: BLE001
            log("marking failed: %s", e)

        pruned = db.prune()
        if pruned and pruned.get("removed"):
            log("pruned %s quote rows (%s -> %s)", pruned["removed"],
                pruned["before"], pruned["after"])
        stats["prune"] = pruned
        stats["http"] = net.stats()
        stats["elapsed"] = round(time.time() - t0, 1)
        stats["db"] = db.db_stats()
        db.kv_set("last_run", {
            "finished": marketcal.now_utc().isoformat(),
            "asof_date": marketcal.session_date().isoformat(),
            "stats": stats,
        })
        db.execute("""UPDATE pipeline_run SET finished=?, ok=1, stage='done', stats_json=?
                       WHERE id=?""",
                   (marketcal.now_utc().isoformat(), json.dumps(stats, default=str), run_id))
        log("done in %.1fs -- %d symbols, %d contracts, %d ideas, http=%s",
            time.time() - t0, stats["symbols"], stats["contracts"], stats["ideas"],
            stats["http"])
        if stats["failed"]:
            log("failures: %s", "; ".join(stats["failed"]))
        return stats
    except Exception as e:                              # noqa: BLE001
        db.execute("""UPDATE pipeline_run SET finished=?, ok=0, error=? WHERE id=?""",
                   (marketcal.now_utc().isoformat(), traceback.format_exc(), run_id))
        log("PIPELINE FAILED: %s", e)
        raise


def assert_fresh(max_age_hours=30):
    """Verify the DB actually holds a usable, recent dataset.

    The scheduled task's exit code is only trustworthy if something re-reads
    the database and fails when the data is not there -- a fetch that returns
    empty JSON exits 0 just as happily as a good one.
    """
    problems = []
    last = db.kv_get("last_run") or {}
    if not last:
        problems.append("no completed pipeline run recorded")
    n_sym = db.scalar("""SELECT COUNT(DISTINCT symbol) FROM symbol_metrics
                          WHERE ts >= datetime('now', ?)""",
                      ("-%d hour" % max_age_hours,), default=0)
    if n_sym < max(5, len(config.WATCHLIST) // 2):
        problems.append("only %d symbols updated in the last %dh" % (n_sym, max_age_hours))
    n_q = db.scalar("""SELECT COUNT(*) FROM contract_quote
                        WHERE asof_date >= date('now','-3 day')""", default=0)
    if n_q < 1000:
        problems.append("only %d recent contract quotes" % n_q)
    bad_iv = db.scalar("""SELECT COUNT(*) FROM symbol_metrics
                           WHERE ts >= datetime('now','-30 hour')
                             AND (iv30 IS NULL OR iv30 <= 0 OR iv30 > 4)""", default=0)
    if bad_iv:
        problems.append("%d symbols with implausible IV30" % bad_iv)
    n_ideas = db.scalar("""SELECT COUNT(*) FROM idea WHERE asof_date >= date('now','-3 day')""",
                        default=0)
    if n_ideas == 0:
        problems.append("no ideas generated in the last 3 days")
    if problems:
        for p in problems:
            log("STALE: %s", p)
        return False
    log("fresh: %d symbols, %d quotes, %d ideas", n_sym, n_q, n_ideas)
    return True


def main():
    ap = argparse.ArgumentParser(description="Theta Desk data pipeline")
    ap.add_argument("--symbols", help="comma-separated override of the watchlist")
    ap.add_argument("--fresh", action="store_true", help="bypass the HTTP cache")
    ap.add_argument("--no-news", action="store_true")
    ap.add_argument("--no-ideas", action="store_true")
    ap.add_argument("--verify", action="store_true", help="only run the freshness check")
    ap.add_argument("--vacuum", action="store_true")
    ap.add_argument("--debug", action="store_true")
    args, _unknown = ap.parse_known_args()

    db.init()
    if args.verify:
        sys.exit(0 if assert_fresh() else 1)
    syms = [s.strip().upper() for s in args.symbols.split(",")] if args.symbols else None
    run(symbols=syms, fresh=args.fresh, do_news=not args.no_news,
        do_ideas=not args.no_ideas)
    if args.vacuum:
        db.vacuum()
    sys.exit(0 if assert_fresh() else 1)


if __name__ == "__main__":
    main()
