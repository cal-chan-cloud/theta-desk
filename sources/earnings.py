"""Earnings dates from the NASDAQ calendar API.

    https://api.nasdaq.com/api/calendar/earnings?date=YYYY-MM-DD

Earnings are the single most important calendar item for an options desk: they
create the front-month IV bulge, they are why the term structure inverts, and
holding short vega through one is the fastest way to lose on an otherwise
correct trade.  The pipeline sweeps a forward window of sessions and keeps the
hits for the watchlist.
"""

import datetime as dt

import config
import marketcal
import net

URL = "https://api.nasdaq.com/api/calendar/earnings?date=%s"


def fetch_day(day, ttl=None):
    try:
        data = net.get_json(URL % day.isoformat(), tag="earnings", ttl=ttl,
                            headers={"Accept": "application/json"}, default={})
    except net.FetchError:
        return []
    rows = ((data or {}).get("data") or {}).get("rows") or []
    out = []
    for r in rows:
        sym = (r.get("symbol") or "").strip().upper()
        if not sym:
            continue
        out.append({
            "symbol": sym,
            "date": day.isoformat(),
            "time": (r.get("time") or "").replace("time-", "").replace("-", " ").strip(),
            "eps_forecast": r.get("epsForecast") or "",
            "fiscal_end": r.get("fiscalQuarterEnding") or "",
        })
    return out


def fetch_window(symbols, back_days=5, forward_days=45, ttl=None):
    """Earnings for `symbols` across a window of sessions around today."""
    want = {s.upper() for s in symbols}
    today = marketcal.session_date()
    found = {}
    d = today - dt.timedelta(days=back_days)
    end = today + dt.timedelta(days=forward_days)
    while d <= end:
        if marketcal.is_trading_day(d):
            for row in fetch_day(d, ttl=ttl):
                if row["symbol"] in want:
                    found.setdefault(row["symbol"], []).append(row)
        d += dt.timedelta(days=1)
    for sym in found:
        found[sym].sort(key=lambda r: r["date"])
    return found


def next_earnings(rows, today=None):
    """Nearest upcoming (or today's) earnings row for one symbol."""
    today = today or marketcal.session_date()
    upcoming = [r for r in rows if marketcal.parse_date(r["date"]) >= today]
    return upcoming[0] if upcoming else (rows[-1] if rows else None)


if __name__ == "__main__":
    res = fetch_window(config.WATCHLIST, forward_days=40, ttl=0)
    print("%d of %d watchlist names have earnings in the window"
          % (len(res), len(config.WATCHLIST)))
    for sym, rows in sorted(res.items(), key=lambda kv: kv[1][0]["date"]):
        r = rows[0]
        print("  %-6s %s  %-12s %s" % (sym, r["date"], r["time"], r["eps_forecast"]))
