"""CBOE delayed-quote option chains.

    https://cdn.cboe.com/api/global/delayed_quotes/options/AAPL.json

This is the best free options feed available without a key: every listed
contract with bid/ask/size, volume, open interest, vendor IV *and* vendor
greeks, plus the underlying's price and 30-day IV.  Quotes are delayed ~15
minutes, which is irrelevant for the swing-timeframe analysis this desk does.

Index products are served with a leading underscore (`_SPX.json`) and their
option roots carry a `W` suffix on weeklies (SPXW), which the OCC parser
handles generically.
"""

import datetime as dt
import re

import config
import marketcal
import net

BASE = "https://cdn.cboe.com/api/global/delayed_quotes/options/%s.json"

# Cash-settled index products CBOE serves under an underscore prefix
INDEX_SYMBOLS = {"SPX", "SPXW", "NDX", "RUT", "VIX", "XSP", "DJX", "OEX", "MXEA", "MXEF"}

_OCC = re.compile(r"^([A-Z0-9]{1,6}?)(\d{6})([CP])(\d{8})$")


def cboe_symbol(symbol):
    s = symbol.upper().lstrip("^")
    return "_" + s if s in INDEX_SYMBOLS else s


def parse_occ(occ):
    """'AAPL260814C00110000' -> (root, date(2026,8,14), 'C', 110.0)."""
    m = _OCC.match(occ.strip().upper())
    if not m:
        return None
    root, ymd, right, strike = m.groups()
    try:
        expiry = dt.date(2000 + int(ymd[0:2]), int(ymd[2:4]), int(ymd[4:6]))
    except ValueError:
        return None
    return root, expiry, right, int(strike) / 1000.0


def _f(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x and abs(x) != float("inf") else None


def fetch_chain(symbol, ttl=None):
    """Return a normalised chain snapshot, or None if the symbol has no data."""
    url = BASE % cboe_symbol(symbol)
    try:
        raw = net.get_json(url, tag="chain", ttl=ttl)
    except net.FetchError:
        return None
    if not raw or "data" not in raw:
        return None
    d = raw["data"]
    opts = d.get("options") or []
    if not opts:
        return None

    now = marketcal.now_utc()
    today = marketcal.session_date()
    spot = _f(d.get("current_price"))
    if not spot or spot <= 0:
        return None

    contracts = []
    call_v = put_v = call_oi = put_oi = 0.0
    dollar_volume = 0.0
    for o in opts:
        parsed = parse_occ(o.get("option", ""))
        if not parsed:
            continue
        root, expiry, right, strike = parsed
        if expiry < today or strike <= 0:
            continue
        bid, ask = _f(o.get("bid")), _f(o.get("ask"))
        last = _f(o.get("last_trade_price"))
        theo = _f(o.get("theo"))
        # Mid needs a genuine two-sided market.  A zero bid means "no market",
        # not "worth half the ask" -- treating it as a mid manufactures value
        # out of nothing and is how phantom edge gets into a scanner.
        mid = None
        if bid is not None and ask is not None and ask > 0 and bid >= 0 and ask >= bid:
            if bid > 0:
                mid = 0.5 * (bid + ask)
            elif ask <= 0.10:
                mid = 0.5 * ask          # penny options: half the ask is honest
        vol = _f(o.get("volume")) or 0.0
        oi = _f(o.get("open_interest")) or 0.0
        spread = (ask - bid) if (bid is not None and ask is not None) else None
        spread_pct = (spread / mid) if (spread is not None and mid and mid > 0) else None

        if right == "C":
            call_v += vol
            call_oi += oi
        else:
            put_v += vol
            put_oi += oi
        if mid:
            dollar_volume += vol * mid * 100.0

        contracts.append({
            "occ": o.get("option"),
            "root": root,
            "expiry": expiry,
            "right": right,
            "strike": strike,
            "bid": bid, "ask": ask, "mid": mid, "last": last, "theo": theo,
            "bid_size": _f(o.get("bid_size")), "ask_size": _f(o.get("ask_size")),
            "volume": vol, "open_interest": oi,
            "iv_src": _f(o.get("iv")),
            "delta_src": _f(o.get("delta")), "gamma_src": _f(o.get("gamma")),
            "theta_src": _f(o.get("theta")), "vega_src": _f(o.get("vega")),
            "rho_src": _f(o.get("rho")),
            "open": _f(o.get("open")), "high": _f(o.get("high")), "low": _f(o.get("low")),
            "prev_close": _f(o.get("prev_day_close")),
            "change": _f(o.get("change")),
            "last_trade_time": o.get("last_trade_time"),
            "spread": spread, "spread_pct": spread_pct,
        })

    if not contracts:
        return None

    expiries = sorted({c["expiry"] for c in contracts})
    return {
        "symbol": symbol.upper(),
        "ts": now.replace(microsecond=0).isoformat(),
        "asof_date": today.isoformat(),
        "source_ts": raw.get("timestamp"),
        "spot": spot,
        "prev_close": _f(d.get("prev_day_close")),
        "open": _f(d.get("open")), "high": _f(d.get("high")), "low": _f(d.get("low")),
        "close": _f(d.get("close")),
        "volume": _f(d.get("volume")),
        "change_pct": _f(d.get("price_change_percent")),
        "bid": _f(d.get("bid")), "ask": _f(d.get("ask")),
        "iv30_source": (_f(d.get("iv30")) or 0) / 100.0 or None,
        "security_type": d.get("security_type"),
        "contracts": contracts,
        "expiries": expiries,
        "call_volume": call_v, "put_volume": put_v,
        "call_oi": call_oi, "put_oi": put_oi,
        "total_volume": call_v + put_v,
        "total_oi": call_oi + put_oi,
        "dollar_volume": dollar_volume,
        "is_index": cboe_symbol(symbol).startswith("_"),
    }


def am_settled(symbol, root, expiry):
    """SPX/NDX/RUT *monthly* contracts settle on the open, not the close.

    That is a full session of time value -- getting it wrong makes every
    third-Friday index option look 1/252 of a year cheaper than it is.
    """
    base = symbol.upper().lstrip("^")
    if base not in INDEX_SYMBOLS:
        return False
    if root and root.endswith("W"):
        return False
    return marketcal.is_monthly_expiry(expiry)


if __name__ == "__main__":
    import sys
    sym = sys.argv[1] if len(sys.argv) > 1 else "AAPL"
    ch = fetch_chain(sym, ttl=0)
    if not ch:
        print("no chain for", sym)
        raise SystemExit(1)
    print("%s spot=%.2f contracts=%d expiries=%d vol=%s oi=%s iv30=%s"
          % (ch["symbol"], ch["spot"], len(ch["contracts"]), len(ch["expiries"]),
             ch["total_volume"], ch["total_oi"], ch["iv30_source"]))
    print("first expiries:", ch["expiries"][:8])
    c = ch["contracts"][len(ch["contracts"]) // 2]
    print("sample:", {k: c[k] for k in ("occ", "expiry", "right", "strike", "bid", "ask",
                                        "mid", "volume", "open_interest", "iv_src", "delta_src")})
