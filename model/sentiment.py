"""Headline sentiment for equities, from a finance-specific lexicon.

General-purpose sentiment models are wrong on financial text in a specific and
costly way: "shares plunge on strong guidance cut" is negative, but so is
"cheap", "low", and "falling" in a general lexicon while "beat", "raise" and
"outperform" carry no special weight.  The lexicon here is finance-first
(Loughran-McDonald in spirit), phrase-aware, negation-aware, and -- most
importantly -- *relevance-weighted*: Yahoo's per-ticker RSS regularly returns
generic market stories, and scoring those against a single name manufactures
signal out of noise.
"""

import datetime as dt
import math
import re

import config

# --------------------------------------------------------------- the lexicon
# Multi-word phrases are matched first and consume their span, so "price target
# raised" never also scores the bare word "raised".
PHRASES = {
    # earnings / guidance
    "beats estimates": 0.75, "beat estimates": 0.75, "tops estimates": 0.75,
    "earnings beat": 0.7, "exceeds expectations": 0.7, "better than expected": 0.65,
    "misses estimates": -0.8, "missed estimates": -0.8, "earnings miss": -0.75,
    "falls short": -0.6, "worse than expected": -0.7, "disappointing results": -0.75,
    "raises guidance": 0.85, "raised guidance": 0.85, "boosts outlook": 0.8,
    "lifts forecast": 0.8, "raises outlook": 0.8, "guidance above": 0.7,
    "cuts guidance": -0.9, "cut guidance": -0.9, "lowers outlook": -0.85,
    "slashes forecast": -0.9, "withdraws guidance": -0.95, "guidance below": -0.75,
    "record revenue": 0.7, "record profit": 0.75, "record quarter": 0.7,
    # analyst actions
    "price target raised": 0.6, "raises price target": 0.6, "boosts price target": 0.6,
    "price target cut": -0.6, "cuts price target": -0.6, "lowers price target": -0.6,
    "upgraded to buy": 0.8, "upgrades to buy": 0.8, "double upgrade": 0.9,
    "downgraded to sell": -0.85, "downgrades to sell": -0.85, "double downgrade": -0.9,
    "initiated with buy": 0.6, "top pick": 0.6, "conviction buy": 0.7,
    "underperform rating": -0.6, "outperform rating": 0.6,
    # corporate actions
    "share buyback": 0.6, "stock buyback": 0.6, "repurchase program": 0.55,
    "dividend increase": 0.6, "raises dividend": 0.65, "special dividend": 0.5,
    "dividend cut": -0.85, "suspends dividend": -0.9,
    "stock split": 0.35, "acquisition of": 0.3, "to acquire": 0.35,
    "takeover bid": 0.7, "buyout offer": 0.7, "merger agreement": 0.4,
    "goes private": 0.5, "taken private": 0.5, "spin off": 0.2,
    "secondary offering": -0.45, "dilutive offering": -0.6, "convertible notes": -0.3,
    "files for bankruptcy": -1.0, "chapter 11": -1.0, "going concern": -0.9,
    # legal / regulatory
    "class action": -0.5, "securities fraud": -0.85, "sec investigation": -0.8,
    "sec probe": -0.8, "doj investigation": -0.8, "antitrust suit": -0.6,
    "antitrust lawsuit": -0.6, "regulatory approval": 0.65, "fda approval": 0.85,
    "fda rejects": -0.9, "complete response letter": -0.85, "clinical hold": -0.85,
    "phase 3 success": 0.85, "trial failure": -0.9, "product recall": -0.7,
    "data breach": -0.6, "cyberattack": -0.55,
    # operations
    "job cuts": -0.35, "layoffs": -0.35, "restructuring charge": -0.4,
    "cost cutting": 0.15, "margin expansion": 0.6, "margin compression": -0.6,
    "supply chain issues": -0.5, "production halt": -0.7, "capacity expansion": 0.45,
    "market share gains": 0.55, "losing market share": -0.6,
    "short seller": -0.6, "short report": -0.7, "activist investor": 0.3,
    "insider buying": 0.5, "insider selling": -0.3,
    # price action
    "all time high": 0.6, "record high": 0.55, "52 week high": 0.5,
    "52 week low": -0.5, "bear market": -0.5, "death cross": -0.4,
    "golden cross": 0.4, "short squeeze": 0.5, "sell off": -0.5, "selloff": -0.5,
}

POSITIVE = {
    "beat": 0.55, "beats": 0.55, "surge": 0.65, "surges": 0.65, "soar": 0.7,
    "soars": 0.7, "jump": 0.5, "jumps": 0.5, "rally": 0.5, "rallies": 0.5,
    "climb": 0.4, "climbs": 0.4, "gain": 0.35, "gains": 0.35, "rise": 0.3,
    "rises": 0.3, "upgrade": 0.7, "upgrades": 0.7, "upgraded": 0.7,
    "outperform": 0.6, "outperforms": 0.6, "bullish": 0.65, "optimistic": 0.45,
    "strong": 0.4, "stronger": 0.45, "robust": 0.45, "solid": 0.35,
    "growth": 0.3, "expansion": 0.3, "profit": 0.3, "profitable": 0.45,
    "record": 0.45, "boost": 0.5, "boosts": 0.5, "raise": 0.45, "raises": 0.45,
    "approve": 0.5, "approved": 0.55, "approval": 0.55, "win": 0.45, "wins": 0.45,
    "breakthrough": 0.7, "milestone": 0.4, "partnership": 0.35, "deal": 0.25,
    "demand": 0.3, "momentum": 0.4, "accelerating": 0.45, "efficiency": 0.3,
    "undervalued": 0.5, "buy": 0.4, "overweight": 0.55, "accumulate": 0.45,
    "rebound": 0.45, "recovery": 0.4, "expands": 0.35, "launches": 0.3,
    "innovative": 0.3, "leading": 0.3, "dominant": 0.4, "surpass": 0.6,
}

NEGATIVE = {
    "miss": -0.6, "misses": -0.6, "missed": -0.6, "plunge": -0.75, "plunges": -0.75,
    "plummet": -0.8, "plummets": -0.8, "tumble": -0.6, "tumbles": -0.6,
    "slump": -0.55, "slumps": -0.55, "sink": -0.55, "sinks": -0.55,
    "fall": -0.35, "falls": -0.35, "drop": -0.4, "drops": -0.4, "decline": -0.4,
    "declines": -0.4, "slide": -0.45, "slides": -0.45, "crash": -0.85,
    "downgrade": -0.7, "downgrades": -0.7, "downgraded": -0.7,
    "underperform": -0.6, "bearish": -0.65, "pessimistic": -0.45,
    "weak": -0.5, "weaker": -0.55, "weakness": -0.5, "soft": -0.35,
    "sluggish": -0.5, "disappointing": -0.7, "disappoints": -0.7,
    "loss": -0.45, "losses": -0.45, "lawsuit": -0.5, "sue": -0.45, "sued": -0.5,
    "probe": -0.55, "investigation": -0.55, "fraud": -0.85, "scandal": -0.8,
    "warning": -0.55, "warns": -0.6, "cut": -0.45, "cuts": -0.45, "slash": -0.65,
    "slashes": -0.65, "halt": -0.6, "halted": -0.6, "recall": -0.6,
    "delay": -0.45, "delays": -0.45, "delayed": -0.45, "resign": -0.4,
    "resigns": -0.4, "steps down": -0.35, "overvalued": -0.5, "sell": -0.4,
    "underweight": -0.55, "avoid": -0.5, "risk": -0.25, "risks": -0.25,
    "concern": -0.4, "concerns": -0.4, "headwind": -0.5, "headwinds": -0.5,
    "pressure": -0.35, "struggling": -0.6, "struggles": -0.55, "bankruptcy": -0.95,
    "default": -0.8, "downturn": -0.55, "recession": -0.5, "slowdown": -0.5,
    "shortfall": -0.6, "writedown": -0.65, "impairment": -0.6, "dilution": -0.5,
}

NEGATORS = {"not", "no", "never", "without", "fails", "fail", "failed", "unable",
            "denies", "denied", "lacks", "lack", "isnt", "arent", "wont", "cannot",
            "despite", "however", "but"}
INTENSIFIERS = {"very": 1.35, "highly": 1.3, "sharply": 1.4, "significantly": 1.3,
                "massively": 1.5, "slightly": 0.6, "modestly": 0.65, "marginally": 0.55,
                "record": 1.25, "huge": 1.4, "major": 1.2, "sharp": 1.35}

TAG_PATTERNS = [
    ("earnings", r"\b(earnings|quarterly results|q[1-4]\b|eps|revenue|guidance)\b"),
    ("analyst", r"\b(upgrade|downgrade|price target|initiat\w+ coverage|rating)\b"),
    ("m&a", r"\b(acquisit\w+|merger|takeover|buyout|acquire\w*)\b"),
    ("legal", r"\b(lawsuit|sue[sd]?|probe|investigation|settlement|antitrust|sec\b|doj\b)\b"),
    ("product", r"\b(launch\w*|unveil\w*|release[sd]?|new product|fda|trial|approval)\b"),
    ("macro", r"\b(fed\b|inflation|rate cut|rate hike|tariff|cpi\b|jobs report|recession)\b"),
    ("insider", r"\b(insider|stake|13f|buyback|repurchase|dividend)\b"),
    ("technical", r"\b(resistance|support|breakout|moving average|oversold|overbought)\b"),
]

_WORD = re.compile(r"[a-z0-9'&]+")
_PHRASE_RE = {p: re.compile(r"\b" + re.escape(p).replace(r"\ ", r"\s+") + r"\b")
              for p in PHRASES}


def score_text(text, headline_weight=1.0):
    """Return (score in [-1,1], hit_count, matched terms)."""
    if not text:
        return 0.0, 0, []
    t = text.lower()
    matched, total, hits = [], 0.0, 0

    consumed = []
    for phrase, val in PHRASES.items():
        for m in _PHRASE_RE[phrase].finditer(t):
            if any(s <= m.start() < e for s, e in consumed):
                continue
            consumed.append((m.start(), m.end()))
            total += val * 1.6            # phrases are far more reliable than words
            hits += 1
            matched.append(phrase)

    words = _WORD.findall(t)
    spans = []
    pos = 0
    for w in words:
        i = t.find(w, pos)
        spans.append(i)
        pos = i + len(w)

    for idx, w in enumerate(words):
        if any(s <= spans[idx] < e for s, e in consumed):
            continue
        val = POSITIVE.get(w) or NEGATIVE.get(w)
        if val is None:
            continue
        mult = 1.0
        for j in range(max(0, idx - 3), idx):
            if words[j] in NEGATORS:
                mult *= -0.85
                break
        for j in range(max(0, idx - 2), idx):
            if words[j] in INTENSIFIERS:
                mult *= INTENSIFIERS[words[j]]
        total += val * mult
        hits += 1
        matched.append(w)

    if hits == 0:
        return 0.0, 0, []
    # Saturating rather than averaging: three negative words is more negative
    # than one, but not three times more.
    raw = total / (1.0 + 0.55 * (hits - 1))
    return math.tanh(raw * headline_weight), hits, matched[:8]


def tags_for(text):
    t = (text or "").lower()
    return [name for name, pat in TAG_PATTERNS if re.search(pat, t)]


def relevance(item, symbol, company=None):
    """How much this headline is actually about `symbol`.

    Yahoo's per-ticker feed mixes in general market wraps; scoring those as
    if they were company news is the single largest source of fake sentiment.
    """
    title = (item.get("title") or "").lower()
    summary = (item.get("summary") or "").lower()
    sym = symbol.lower()
    name_words = [w for w in re.split(r"\W+", (company or "").lower()) if len(w) > 3]

    score = 0.25                                    # baseline: it was in the feed
    if re.search(r"\b%s\b" % re.escape(sym), title):
        score = 1.0
    elif name_words and any(w in title for w in name_words):
        score = 0.95
    elif re.search(r"\b%s\b" % re.escape(sym), summary):
        score = 0.6
    elif name_words and any(w in summary for w in name_words):
        score = 0.55
    # Multi-ticker round-ups dilute: "10 stocks to watch" is not company news
    if re.search(r"\b(\d+\s+(stocks|names|picks)|stocks to watch|market wrap|"
                 r"midday|premarket|movers)\b", title):
        score *= 0.35
    return score


def source_weight(source):
    s = (source or "").lower()
    for key, w in config.NEWS_SOURCE_WEIGHTS.items():
        if key in s:
            return w
    return 1.0


def score_item(item, symbol, company=None, now=None):
    """Attach sentiment, confidence, weight and tags to one news item."""
    now = now or dt.datetime.now(dt.timezone.utc)
    title = item.get("title") or ""
    summary = item.get("summary") or ""

    s_title, n_title, terms = score_text(title, headline_weight=1.0)
    s_sum, n_sum, _ = score_text(summary, headline_weight=0.6)
    if n_title and n_sum:
        sent = 0.75 * s_title + 0.25 * s_sum
    else:
        sent = s_title if n_title else s_sum

    rel = relevance(item, symbol, company)
    src = source_weight(item.get("source"))

    pub = item.get("published")
    age_h = 0.0
    if isinstance(pub, dt.datetime):
        age_h = max((now - pub).total_seconds() / 3600.0, 0.0)
    decay = 0.5 ** (age_h / config.NEWS_HALF_LIFE_HOURS)

    hits = n_title + n_sum
    confidence = min(1.0, hits / 4.0) * rel
    weight = rel * src * decay

    item = dict(item)
    item.update({
        "sentiment": round(sent, 4),
        "confidence": round(confidence, 4),
        "weight": round(weight, 4),
        "relevance": round(rel, 3),
        "age_hours": round(age_h, 1),
        "tags": tags_for(title + " " + summary),
        "terms": terms,
    })
    return item


def aggregate(items, baseline_count=8.0):
    """Symbol-level news score plus a 'buzz' measure of attention.

    Buzz matters independently of direction: an unusual *volume* of coverage
    precedes realised volatility whether the stories are good or bad, which is
    a long-vol signal even when the sentiment nets to zero.
    """
    scored = [i for i in items if i.get("weight", 0) > 0.02]
    if not scored:
        return {"score": 0.0, "count": 0, "buzz": 0.0, "confidence": 0.0,
                "positive": 0, "negative": 0, "top_tags": []}
    wsum = sum(i["weight"] for i in scored)
    score = sum(i["sentiment"] * i["weight"] for i in scored) / wsum if wsum else 0.0
    conf = sum(i["confidence"] * i["weight"] for i in scored) / wsum if wsum else 0.0

    fresh = [i for i in scored if i.get("age_hours", 999) <= 48]
    buzz = math.log1p(len(fresh)) / math.log1p(baseline_count) - 1.0

    tag_counts = {}
    for i in scored:
        for t in i.get("tags", []):
            tag_counts[t] = tag_counts.get(t, 0) + 1
    top = sorted(tag_counts.items(), key=lambda kv: -kv[1])[:4]

    return {
        "score": round(score, 4),
        "count": len(scored),
        "fresh_count": len(fresh),
        "buzz": round(buzz, 3),
        "confidence": round(conf, 3),
        "positive": sum(1 for i in scored if i["sentiment"] > 0.15),
        "negative": sum(1 for i in scored if i["sentiment"] < -0.15),
        "top_tags": [t for t, _ in top],
    }
