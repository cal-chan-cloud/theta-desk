"""Financial news via RSS (Yahoo Finance headline feed + Google News).

Two feeds rather than one because they fail differently: Yahoo's per-ticker feed
is precise but thin and sometimes empty for smaller names; Google News is deep
but noisy and needs the ticker disambiguated (searching "F" returns nothing
useful).  Items are deduplicated on a normalised title so the same Reuters story
syndicated four ways counts once.
"""

import datetime as dt
import hashlib
import html
import re
import urllib.parse
import xml.etree.ElementTree as ET

import config
import marketcal
import net

YAHOO_RSS = "https://feeds.finance.yahoo.com/rss/2.0/headline?s=%s&region=US&lang=en-US"
GOOGLE_RSS = "https://news.google.com/rss/search?q=%s&hl=en-US&gl=US&ceid=US:en"

# Names disambiguate Google News far better than tickers for common words.
COMPANY_NAMES = {
    "AAPL": "Apple", "MSFT": "Microsoft", "NVDA": "Nvidia", "AMZN": "Amazon",
    "GOOGL": "Alphabet Google", "META": "Meta Platforms", "TSLA": "Tesla",
    "AVGO": "Broadcom", "AMD": "AMD", "NFLX": "Netflix", "COIN": "Coinbase",
    "PLTR": "Palantir", "MSTR": "Strategy MicroStrategy", "SMCI": "Super Micro",
    "MU": "Micron", "CRWD": "CrowdStrike", "SHOP": "Shopify", "UBER": "Uber",
    "JPM": "JPMorgan", "GS": "Goldman Sachs", "XOM": "Exxon Mobil",
    "OXY": "Occidental Petroleum", "LLY": "Eli Lilly", "UNH": "UnitedHealth",
    "SPY": "S&P 500", "QQQ": "Nasdaq 100", "IWM": "Russell 2000",
    "DIA": "Dow Jones", "TLT": "Treasury bonds", "GLD": "gold price",
    "SLV": "silver price", "XLE": "energy sector stocks",
    "XLF": "bank stocks", "SMH": "semiconductor stocks", "ARKK": "ARK Innovation",
}

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_SRC_SUFFIX = re.compile(r"\s+-\s+([^-]{2,40})$")


def _strip(text):
    if not text:
        return ""
    return _WS_RE.sub(" ", html.unescape(_TAG_RE.sub(" ", text))).strip()


def _parse_rfc822(s):
    if not s:
        return None
    s = s.strip()
    for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z",
                "%d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M %z"):
        try:
            d = dt.datetime.strptime(s, fmt)
            if d.tzinfo is None:
                d = d.replace(tzinfo=dt.timezone.utc)
            return d.astimezone(dt.timezone.utc)
        except ValueError:
            continue
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(dt.timezone.utc)
    except ValueError:
        return None


def _norm_title(t):
    t = re.sub(r"[^a-z0-9 ]+", "", (t or "").lower())
    return _WS_RE.sub(" ", t).strip()[:120]


def _parse_feed(xml_text, feed_name):
    items = []
    if not xml_text:
        return items
    try:
        root = ET.fromstring(xml_text.encode("utf-8", "replace"))
    except ET.ParseError:
        return items
    for it in root.iter("item"):
        title = _strip(it.findtext("title"))
        if not title:
            continue
        link = (it.findtext("link") or "").strip()
        desc = _strip(it.findtext("description"))
        pub = _parse_rfc822(it.findtext("pubDate"))
        src_el = it.find("source")
        source = _strip(src_el.text) if src_el is not None else ""
        if not source:
            m = _SRC_SUFFIX.search(title)
            if m:
                source = m.group(1).strip()
                title = title[: m.start()].strip()
        items.append({
            "title": title,
            "summary": desc[:600],
            "link": link,
            "source": source or feed_name,
            "feed": feed_name,
            "published": pub,
        })
    return items


def fetch_ticker_news(symbol, ttl=None):
    """Merged, de-duplicated, recency-filtered news for one ticker."""
    out, seen = [], set()
    cutoff = marketcal.now_utc() - dt.timedelta(days=config.NEWS_MAX_AGE_DAYS)

    feeds = []
    try:
        feeds.append(("yahoo", net.get_text(YAHOO_RSS % urllib.parse.quote(symbol),
                                            tag="news", ttl=ttl, default="")))
    except net.FetchError:
        pass
    name = COMPANY_NAMES.get(symbol.upper(), symbol)
    query = urllib.parse.quote('%s stock OR "%s"' % (name, symbol))
    try:
        feeds.append(("google", net.get_text(GOOGLE_RSS % query,
                                             tag="news", ttl=ttl, default="")))
    except net.FetchError:
        pass

    for feed_name, body in feeds:
        for it in _parse_feed(body, feed_name):
            key = _norm_title(it["title"])
            if not key or key in seen:
                continue
            if it["published"] and it["published"] < cutoff:
                continue
            seen.add(key)
            it["symbol"] = symbol.upper()
            it["id"] = hashlib.sha1(("%s|%s" % (symbol.upper(), key)).encode()).hexdigest()[:24]
            out.append(it)

    out.sort(key=lambda x: x["published"] or dt.datetime.min.replace(tzinfo=dt.timezone.utc),
             reverse=True)
    return out[: config.NEWS_MAX_PER_TICKER]


def fetch_market_news(ttl=None):
    """Broad market headlines -- drives the macro banner, not per-ticker scores."""
    out, seen = [], set()
    for q in ("stock market today", "federal reserve interest rates",
              "S&P 500 outlook", "options market volatility"):
        try:
            body = net.get_text(GOOGLE_RSS % urllib.parse.quote(q), tag="news",
                                ttl=ttl, default="")
        except net.FetchError:
            continue
        for it in _parse_feed(body, "google"):
            key = _norm_title(it["title"])
            if key in seen:
                continue
            seen.add(key)
            it["symbol"] = "_MARKET"
            it["id"] = hashlib.sha1(("_MARKET|%s" % key).encode()).hexdigest()[:24]
            out.append(it)
    out.sort(key=lambda x: x["published"] or dt.datetime.min.replace(tzinfo=dt.timezone.utc),
             reverse=True)
    return out[:40]


if __name__ == "__main__":
    import sys
    sym = sys.argv[1] if len(sys.argv) > 1 else "AAPL"
    items = fetch_ticker_news(sym, ttl=0)
    print("%s: %d items" % (sym, len(items)))
    for it in items[:8]:
        print(" ", (it["published"] or "?"), "|", it["source"][:20], "|", it["title"][:90])
