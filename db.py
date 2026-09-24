"""SQLite persistence for Theta Desk.

Every snapshot the pipeline takes is written here and never overwritten, so the
database doubles as (a) the cache that makes the site fast and (b) the research
record that lets us measure whether our own ideas actually worked.

Schema notes
------------
* `contract_quote` is the big table -- one row per option per snapshot.  It is
  the only table that needs pruning; `prune()` keeps full detail for recent days
  and thins older days to one snapshot per day.
* Anything derived is stored alongside the inputs it was derived from, so a
  model change can be re-run against history without re-fetching.
"""

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager

import config

_local = threading.local()

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

-- ------------------------------------------------------------------ market
CREATE TABLE IF NOT EXISTS ohlc (
    symbol   TEXT NOT NULL,
    date     TEXT NOT NULL,          -- YYYY-MM-DD (ET session date)
    open     REAL, high REAL, low REAL, close REAL,
    volume   REAL,
    PRIMARY KEY (symbol, date)
);
CREATE INDEX IF NOT EXISTS ix_ohlc_sym_date ON ohlc(symbol, date DESC);

CREATE TABLE IF NOT EXISTS underlying_snapshot (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol        TEXT NOT NULL,
    asof_date     TEXT NOT NULL,
    ts            TEXT NOT NULL,      -- ISO-8601 UTC
    price         REAL,
    prev_close    REAL,
    open          REAL, high REAL, low REAL,
    volume        REAL,
    change_pct    REAL,
    iv30_source   REAL,               -- CBOE's own 30d IV
    UNIQUE (symbol, ts)
);
CREATE INDEX IF NOT EXISTS ix_und_sym_date ON underlying_snapshot(symbol, asof_date DESC);

-- ------------------------------------------------------------------- chains
CREATE TABLE IF NOT EXISTS chain_snapshot (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol        TEXT NOT NULL,
    asof_date     TEXT NOT NULL,
    ts            TEXT NOT NULL,
    source_ts     TEXT,
    spot          REAL,
    n_contracts   INTEGER,
    n_expiries    INTEGER,
    total_volume  REAL,
    total_oi      REAL,
    call_volume   REAL, put_volume REAL,
    call_oi       REAL, put_oi     REAL,
    dollar_volume REAL,
    risk_free     REAL,
    UNIQUE (symbol, ts)
);
CREATE INDEX IF NOT EXISTS ix_chain_sym_date ON chain_snapshot(symbol, asof_date DESC);

CREATE TABLE IF NOT EXISTS contract_quote (
    snapshot_id  INTEGER NOT NULL,
    symbol       TEXT NOT NULL,
    asof_date    TEXT NOT NULL,
    occ          TEXT NOT NULL,       -- OCC-style contract id
    expiry       TEXT NOT NULL,
    right        TEXT NOT NULL,       -- 'C' | 'P'
    strike       REAL NOT NULL,
    bid REAL, ask REAL, mid REAL, last REAL, theo REAL,
    bid_size REAL, ask_size REAL,
    volume REAL, open_interest REAL,
    iv_src REAL,                      -- vendor IV
    iv REAL,                          -- our IV, solved off the implied forward
    delta REAL, gamma REAL, theta REAL, vega REAL, rho REAL,
    vanna REAL, vomma REAL, charm REAL,
    intrinsic REAL, extrinsic REAL,
    moneyness REAL,                   -- log(K/F)
    spread_pct REAL,
    liquidity REAL,                   -- 0..100
    vol_oi REAL,
    dte REAL,
    PRIMARY KEY (snapshot_id, occ)
);
CREATE INDEX IF NOT EXISTS ix_cq_sym_exp ON contract_quote(symbol, asof_date, expiry);
CREATE INDEX IF NOT EXISTS ix_cq_occ     ON contract_quote(occ, asof_date);

CREATE TABLE IF NOT EXISTS expiry_metrics (
    snapshot_id   INTEGER NOT NULL,
    symbol        TEXT NOT NULL,
    asof_date     TEXT NOT NULL,
    expiry        TEXT NOT NULL,
    dte           REAL,
    t_years       REAL,
    t_vol         REAL,
    forward       REAL,
    discount      REAL,
    rate          REAL,
    div_yield     REAL,
    parity_r2     REAL,
    parity_method TEXT,
    n_quotes      INTEGER,
    atm_iv        REAL,
    mfiv          REAL,               -- model-free (VIX-style) implied vol
    -- iv(z) = a + b*z + c*z^2  where z = ln(K/F)/smile_scale, clamped to [k_min,k_max]/scale
    smile_a REAL, smile_b REAL, smile_c REAL,
    smile_scale REAL, smile_k_min REAL, smile_k_max REAL,
    smile_rmse    REAL,
    iv_25p REAL, iv_25c REAL,
    straddle_pct  REAL,
    em_low REAL, em_high REAL,
    skew_25       REAL,               -- iv(25d put) - iv(25d call)
    rr_25         REAL,               -- risk reversal
    fly_25        REAL,               -- butterfly
    slope_atm     REAL,               -- d(iv)/d(logK) at the money
    expected_move REAL,               -- +/- 1 sigma in price terms
    em_pct        REAL,
    straddle      REAL,
    max_pain      REAL,
    gex           REAL,               -- dealer gamma $ per 1% move
    gamma_flip    REAL,
    call_wall     REAL, put_wall REAL,
    pcr_vol REAL, pcr_oi REAL,
    call_volume REAL, put_volume REAL,
    call_oi REAL, put_oi REAL,
    total_volume REAL, total_oi REAL,
    arb_flags     TEXT,
    PRIMARY KEY (snapshot_id, expiry)
);
CREATE INDEX IF NOT EXISTS ix_em_sym ON expiry_metrics(symbol, asof_date DESC);

-- ------------------------------------------------------- per-symbol rollup
CREATE TABLE IF NOT EXISTS symbol_metrics (
    symbol        TEXT NOT NULL,
    asof_date     TEXT NOT NULL,
    ts            TEXT NOT NULL,
    spot          REAL,
    iv30          REAL,
    iv60          REAL,
    iv90          REAL,
    term_slope    REAL,
    hv10 REAL, hv20 REAL, hv30 REAL, hv60 REAL, hv252 REAL,
    hv_yz20 REAL, hv_gk20 REAL, hv_park20 REAL, hv_ewma REAL,
    vol_forecast  REAL,
    vrp           REAL,               -- iv30 - forecast
    vrp_ratio     REAL,
    iv_rank REAL, iv_pct REAL, iv_samples INTEGER,
    hv_rank REAL, hv_pct REAL,
    trend_score   REAL,
    trend_label   TEXT,
    rsi REAL, macd REAL, macd_hist REAL, adx REAL, atr REAL, atr_pct REAL,
    sma20 REAL, sma50 REAL, sma200 REAL, ema20 REAL,
    bb_pctb REAL, bb_width REAL,
    rel_strength REAL,
    ret_5 REAL, ret_20 REAL, ret_60 REAL,
    rvol          REAL,               -- underlying relative volume
    obv_slope     REAL,
    support       REAL, resistance REAL,
    news_score    REAL, news_count INTEGER, news_buzz REAL, news_confidence REAL,
    earnings_date TEXT, days_to_earnings REAL,
    implied_earnings_move REAL,
    jump_share    REAL,
    skew_25       REAL,
    regime        TEXT, vol_regime TEXT, trend_regime TEXT, vol_edge REAL,
    vol_edge_pctile REAL,
    opt_volume    REAL, opt_oi REAL, opt_dollar_volume REAL,
    opt_rvol      REAL,               -- options volume vs its own 20d average
    pcr_vol REAL, pcr_oi REAL,
    gex_total     REAL, gamma_flip REAL,
    max_pain      REAL,
    unusual_score REAL,
    detail_json   TEXT,
    PRIMARY KEY (symbol, ts)
);
CREATE INDEX IF NOT EXISTS ix_sm_date ON symbol_metrics(asof_date DESC, symbol);

-- --------------------------------------------------------------------- news
CREATE TABLE IF NOT EXISTS news (
    id          TEXT PRIMARY KEY,     -- hash of link
    symbol      TEXT NOT NULL,
    published   TEXT,
    fetched     TEXT,
    title       TEXT,
    summary     TEXT,
    link        TEXT,
    source      TEXT,
    feed        TEXT,
    sentiment   REAL,
    confidence  REAL,
    weight      REAL,
    tags        TEXT
);
CREATE INDEX IF NOT EXISTS ix_news_sym ON news(symbol, published DESC);

CREATE TABLE IF NOT EXISTS earnings (
    symbol      TEXT NOT NULL,
    date        TEXT NOT NULL,
    time        TEXT,
    eps_forecast TEXT,
    fiscal_end  TEXT,
    fetched     TEXT,
    PRIMARY KEY (symbol, date)
);

-- ------------------------------------------------------------------- ideas
CREATE TABLE IF NOT EXISTS idea (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    asof_date     TEXT NOT NULL,
    ts            TEXT NOT NULL,
    symbol        TEXT NOT NULL,
    strategy      TEXT NOT NULL,
    direction     TEXT,
    expiry        TEXT,
    dte           REAL,
    legs_json     TEXT,
    entry_price   REAL,               -- net debit(+) / credit(-)
    max_profit    REAL,
    max_loss      REAL,
    breakevens    TEXT,
    pop           REAL,
    ev            REAL,
    ev_per_risk   REAL,
    cvar5         REAL,
    score         REAL,
    rank          INTEGER,
    qty           INTEGER,
    targets_json  TEXT,
    rationale     TEXT,
    metrics_json  TEXT,
    -- Managed exit: once a target or stop is hit the idea is CLOSED and its
    -- P&L frozen.  Without this the forward test silently measures
    -- hold-to-expiry, which is not the strategy the model recommends.
    exit_date     TEXT,
    exit_reason   TEXT,
    exit_pnl      REAL,
    UNIQUE (asof_date, symbol, strategy, expiry, legs_json)
);
CREATE INDEX IF NOT EXISTS ix_idea_date ON idea(asof_date DESC, score DESC);

-- Forward-tested outcome of each idea (filled in by later pipeline runs)
CREATE TABLE IF NOT EXISTS idea_outcome (
    idea_id     INTEGER NOT NULL,
    asof_date   TEXT NOT NULL,
    spot        REAL,
    mark        REAL,
    pnl         REAL,
    pnl_pct     REAL,
    hit_target  TEXT,
    PRIMARY KEY (idea_id, asof_date)
);

-- ----------------------------------------------------------------- journal
CREATE TABLE IF NOT EXISTS trade (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    idea_id      INTEGER,
    symbol       TEXT NOT NULL,
    strategy     TEXT NOT NULL,
    direction    TEXT,
    opened_date  TEXT NOT NULL,
    expiry       TEXT,
    legs_json    TEXT NOT NULL,
    qty          INTEGER NOT NULL DEFAULT 1,
    entry_price  REAL NOT NULL,       -- per-spread, debit positive
    entry_spot   REAL,
    entry_iv     REAL,
    fees         REAL DEFAULT 0,
    status       TEXT NOT NULL DEFAULT 'open',
    closed_date  TEXT,
    exit_price   REAL,
    exit_spot    REAL,
    exit_reason  TEXT,
    pnl          REAL,
    pnl_pct      REAL,
    max_profit   REAL,
    max_loss     REAL,
    targets_json TEXT,
    tags         TEXT,
    notes        TEXT,
    created      TEXT,
    updated      TEXT
);
CREATE INDEX IF NOT EXISTS ix_trade_status ON trade(status, opened_date DESC);

CREATE TABLE IF NOT EXISTS trade_mark (
    trade_id  INTEGER NOT NULL,
    date      TEXT NOT NULL,
    spot      REAL,
    mark      REAL,
    pnl       REAL,
    pnl_pct   REAL,
    delta REAL, gamma REAL, theta REAL, vega REAL,
    dte       REAL,
    PRIMARY KEY (trade_id, date)
);

-- ------------------------------------------------------------------ system
CREATE TABLE IF NOT EXISTS pipeline_run (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    started   TEXT, finished TEXT,
    ok        INTEGER,
    stage     TEXT,
    stats_json TEXT,
    error     TEXT
);

CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY, value TEXT, updated TEXT
);
"""


def connect():
    """Thread-local connection (Flask serves requests on multiple threads)."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(config.DB_PATH, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        _local.conn = conn
    return conn


def init():
    conn = connect()
    conn.executescript(SCHEMA)
    conn.commit()
    _migrate(conn)
    return conn


def _migrate(conn):
    """Add columns the schema has gained since this DB was created.

    CREATE TABLE IF NOT EXISTS never alters an existing table, so a new metric
    would otherwise be silently dropped by insert_dict's column filter -- the
    worst kind of failure, because everything keeps working and the number just
    never appears.
    """
    wanted = {}
    table = None
    for line in SCHEMA.splitlines():
        s = line.strip()
        if s.upper().startswith("CREATE TABLE"):
            table = s.split()[-2] if s.endswith("(") else s.split("(")[0].split()[-1]
            wanted[table] = []
            continue
        if table and s.startswith(")"):
            table = None
            continue
        if table and s and not s.upper().startswith(("PRIMARY", "UNIQUE", "FOREIGN", "--")):
            # Split on commas FIRST: a line may declare several columns
            # ("smile_scale REAL, smile_k_min REAL"), and splitting on
            # whitespace first leaves the type token as "REAL," which makes
            # the generated ALTER a syntax error -- silently skipping the
            # column, which is exactly the failure this migration exists to
            # prevent.
            for chunk in s.split("--")[0].split(","):
                p = chunk.strip().split()
                if len(p) >= 2 and p[0].isidentifier() and p[1].isalpha():
                    wanted[table].append((p[0], p[1]))

    added = 0
    for tbl, cols in wanted.items():
        try:
            have = {r[1] for r in conn.execute("PRAGMA table_info(%s)" % tbl)}
        except sqlite3.Error:
            continue
        if not have:
            continue
        for name, ctype in cols:
            if name not in have:
                try:
                    conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (tbl, name, ctype))
                    added += 1
                except sqlite3.Error:
                    pass
    if added:
        conn.commit()
        _cols_cache.clear()
    return added


@contextmanager
def tx():
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def q(sql, args=()):
    return [dict(r) for r in connect().execute(sql, args).fetchall()]


def q1(sql, args=()):
    r = connect().execute(sql, args).fetchone()
    return dict(r) if r else None


def scalar(sql, args=(), default=None):
    r = connect().execute(sql, args).fetchone()
    if r is None or r[0] is None:
        return default
    return r[0]


def execute(sql, args=()):
    with tx() as c:
        cur = c.execute(sql, args)
        return cur.lastrowid


def executemany(sql, rows):
    rows = list(rows)
    if not rows:
        return 0
    with tx() as c:
        c.executemany(sql, rows)
    return len(rows)


_cols_cache = {}


def table_columns(table):
    if table not in _cols_cache:
        _cols_cache[table] = [r["name"] for r in
                              q("PRAGMA table_info(%s)" % table)]
    return _cols_cache[table]


def insert_dict(table, d, mode="REPLACE"):
    """Insert a dict, silently dropping keys that are not columns.

    Lets the model layer hand over rich dicts without every caller having to
    keep a column list in sync with the schema.
    """
    cols = [c for c in table_columns(table) if c in d]
    if not cols:
        return None
    ph = ",".join("?" * len(cols))
    sql = "INSERT OR %s INTO %s (%s) VALUES (%s)" % (mode, table, ",".join(cols), ph)
    return execute(sql, tuple(_coerce(d[c]) for c in cols))


def insert_dicts(table, rows, mode="REPLACE"):
    rows = list(rows)
    if not rows:
        return 0
    keys = set()
    for r in rows:
        keys |= set(r)
    cols = [c for c in table_columns(table) if c in keys]
    if not cols:
        return 0
    ph = ",".join("?" * len(cols))
    sql = "INSERT OR %s INTO %s (%s) VALUES (%s)" % (mode, table, ",".join(cols), ph)
    return executemany(sql, [tuple(_coerce(r.get(c)) for c in cols) for r in rows])


def _coerce(v):
    """SQLite takes only str/bytes/int/float/None -- normalise everything else."""
    if v is None or isinstance(v, (str, bytes, int, float)):
        return v
    if isinstance(v, bool):
        return int(v)
    if hasattr(v, "isoformat"):
        return v.isoformat()
    if isinstance(v, (list, dict, tuple)):
        return json.dumps(v, default=str)
    return str(v)


def upsert_rows(table, cols, rows, mode="REPLACE"):
    if not rows:
        return 0
    ph = ",".join("?" * len(cols))
    sql = "INSERT OR %s INTO %s (%s) VALUES (%s)" % (mode, table, ",".join(cols), ph)
    return executemany(sql, rows)


# ------------------------------------------------------------------ kv store
def kv_get(key, default=None):
    r = q1("SELECT value FROM kv WHERE key=?", (key,))
    if not r:
        return default
    try:
        return json.loads(r["value"])
    except Exception:
        return r["value"]


def kv_set(key, value):
    execute("INSERT OR REPLACE INTO kv (key,value,updated) VALUES (?,?,datetime('now'))",
            (key, json.dumps(value)))


# ----------------------------------------------------------------- retention
def prune(full_detail_days=None, archive_days=None):
    """Three-tier retention for the quote table.

    Tier 1 (last `full_detail_days`): every snapshot, every contract.
    Tier 2 (up to `archive_days`):    the last snapshot of each day, and only
                                      contracts that were near the money and
                                      actually had open interest.
    Tier 3 (beyond):                  no quotes at all.

    The per-expiry and per-symbol rollups are never pruned -- they are tiny
    (a few thousand rows) and they are what IV rank, the term structure history
    and the scanner's forward test actually read.  Keeping raw quotes forever
    would reach ~15 GB in a year and buy nothing the app queries.
    """
    full_detail_days = full_detail_days or config.QUOTE_FULL_DETAIL_DAYS
    archive_days = archive_days or config.QUOTE_ARCHIVE_DAYS
    conn = connect()
    cut_detail = "date('now', '-%d day')" % full_detail_days
    cut_archive = "date('now', '-%d day')" % archive_days
    before = scalar("SELECT COUNT(*) FROM contract_quote", default=0)

    with tx() as c:
        # Tier 2a: outside the detail window, keep only each day's last snapshot
        c.execute("""
            DELETE FROM contract_quote
             WHERE asof_date < %s
               AND snapshot_id NOT IN (
                   SELECT MAX(id) FROM chain_snapshot GROUP BY symbol, asof_date)
        """ % cut_detail)
        # Tier 2b: and within those, only liquid near-the-money contracts
        c.execute("""
            DELETE FROM contract_quote
             WHERE asof_date < %s
               AND (COALESCE(open_interest, 0) < ?
                    OR moneyness IS NULL
                    OR ABS(moneyness) > ?)
        """ % cut_detail, (config.ARCHIVE_MIN_OI, config.ARCHIVE_MAX_MONEYNESS))
        # Tier 2c: drop the second-order greeks from archived rows.  They are a
        # pure function of (spot, strike, T, iv) which are all still here, so
        # storing them is paying for something re-derivable in microseconds.
        c.execute("""
            UPDATE contract_quote
               SET vanna=NULL, vomma=NULL, charm=NULL, rho=NULL, theo=NULL,
                   bid_size=NULL, ask_size=NULL, last=NULL
             WHERE asof_date < %s AND vanna IS NOT NULL
        """ % cut_detail)
        # Tier 3
        c.execute("DELETE FROM contract_quote WHERE asof_date < %s" % cut_archive)
        c.execute("""DELETE FROM chain_snapshot WHERE asof_date < %s
                     AND id NOT IN (SELECT MAX(id) FROM chain_snapshot
                                    GROUP BY symbol, asof_date)""" % cut_detail)
        # underlying_snapshot is ~35 rows per run; keep it for the archive
        # window rather than the detail window -- pruning a tiny table hard
        # buys nothing and throws away the only intraday spot history there is.
        c.execute("DELETE FROM underlying_snapshot WHERE asof_date < %s" % cut_archive)
        c.execute("DELETE FROM news WHERE published < date('now','-%d day')"
                  % config.NEWS_RETENTION_DAYS)

    conn.execute("PRAGMA optimize")
    after = scalar("SELECT COUNT(*) FROM contract_quote", default=0)
    # Deleting rows in SQLite frees pages for reuse but never shrinks the file,
    # so a big prune has to be followed by a VACUUM or the size never drops.
    try:
        size = os.path.getsize(config.DB_PATH)
    except OSError:
        size = 0
    if before - after > 200000 or size > config.VACUUM_ABOVE_BYTES:
        vacuum()
    return {"before": before, "after": after, "removed": before - after}


def vacuum():
    conn = connect()
    conn.isolation_level = None
    conn.execute("VACUUM")
    conn.isolation_level = ""


def db_stats():
    tables = ["ohlc", "underlying_snapshot", "chain_snapshot", "contract_quote",
              "expiry_metrics", "symbol_metrics", "news", "earnings", "idea",
              "idea_outcome", "trade", "trade_mark"]
    out = {}
    for t in tables:
        try:
            out[t] = scalar("SELECT COUNT(*) FROM %s" % t, default=0)
        except sqlite3.Error:
            out[t] = 0
    try:
        out["_bytes"] = os.path.getsize(config.DB_PATH)
    except OSError:
        out["_bytes"] = 0
    return out


if __name__ == "__main__":
    t0 = time.time()
    init()
    print("initialised %s in %.2fs" % (config.DB_PATH, time.time() - t0))
    for k, v in db_stats().items():
        print("  %-20s %s" % (k, v))
