"""Daily OHLCV history from Yahoo's chart endpoint.

`/v8/finance/chart/` is the one Yahoo endpoint that still answers without a
crumb/cookie handshake.  It also serves the indices we need for the rate curve
(^IRX, ^FVX, ^TNX) and for the vol regime (^VIX), so the whole macro layer runs
off a single adapter.
"""

import datetime as dt
import urllib.parse

import marketcal
import net

CHART = "https://query1.finance.yahoo.com/v8/finance/chart/%s?range=%s&interval=%s"


def _clean(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x else None


def fetch_history(symbol, range_="2y", interval="1d", ttl=None):
    """Return [{date, open, high, low, close, volume}] oldest-first."""
    url = CHART % (urllib.parse.quote(symbol, safe=""), range_, interval)
    data = net.get_json(url, tag="prices", ttl=ttl, default={})
    try:
        res = data["chart"]["result"][0]
    except (KeyError, IndexError, TypeError):
        return []
    ts = res.get("timestamp") or []
    quote = (res.get("indicators", {}).get("quote") or [{}])[0]
    adj = (res.get("indicators", {}).get("adjclose") or [{}])
    adjclose = adj[0].get("adjclose") if adj and isinstance(adj[0], dict) else None

    o, h, l, c, v = (quote.get(k) or [] for k in ("open", "high", "low", "close", "volume"))
    bars = []
    for i, t in enumerate(ts):
        close = _clean(c[i]) if i < len(c) else None
        if close is None:
            continue
        # Yahoo timestamps daily bars at the session open in exchange tz;
        # converting through ET and taking the date gives the session date.
        d = marketcal.to_et(dt.datetime.fromtimestamp(t, dt.timezone.utc)).date()
        bar = {
            "date": d.isoformat(),
            "open": _clean(o[i]) if i < len(o) else None,
            "high": _clean(h[i]) if i < len(h) else None,
            "low": _clean(l[i]) if i < len(l) else None,
            "close": close,
            "volume": _clean(v[i]) if i < len(v) else None,
        }
        if adjclose and i < len(adjclose):
            bar["adjclose"] = _clean(adjclose[i])
        for k in ("open", "high", "low"):
            if bar[k] is None:
                bar[k] = close
        bars.append(bar)

    # The final bar of a live session is partial; keep it but mark it so the
    # realised-vol estimators can drop it (a half-formed range understates HV).
    if bars and bars[-1]["date"] == marketcal.session_date().isoformat():
        if marketcal.market_state() in ("open", "premarket"):
            bars[-1]["partial"] = True
    return bars


def last_close(symbol, ttl=None):
    """Latest regular-market price, used for the macro/rate quotes."""
    url = CHART % (urllib.parse.quote(symbol, safe=""), "5d", "1d")
    data = net.get_json(url, tag="macro", ttl=ttl, default={})
    try:
        meta = data["chart"]["result"][0]["meta"]
    except (KeyError, IndexError, TypeError):
        return None
    return _clean(meta.get("regularMarketPrice")) or _clean(meta.get("previousClose"))


def fetch_macro(symbols, ttl=None):
    out = {}
    for s in symbols:
        try:
            out[s] = last_close(s, ttl=ttl)
        except net.FetchError:
            out[s] = None
    return out


def intraday(symbol, range_="5d", interval="30m", ttl=900):
    return fetch_history(symbol, range_=range_, interval=interval, ttl=ttl)


if __name__ == "__main__":
    import sys
    sym = sys.argv[1] if len(sys.argv) > 1 else "AAPL"
    bars = fetch_history(sym, ttl=0)
    print("%s bars=%d  %s .. %s  last close=%.2f"
          % (sym, len(bars), bars[0]["date"], bars[-1]["date"], bars[-1]["close"]))
    print("macro:", fetch_macro(["^VIX", "^IRX", "^FVX", "^TNX"], ttl=0))
