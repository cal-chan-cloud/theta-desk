"""Theta Desk configuration.

Everything tunable lives here so the model can be re-calibrated without
touching logic.  Stdlib-only by design: the interpreter Task Scheduler runs
(the python.org build) has flask + requests but NOT numpy/pandas/scipy, while
the MS Store build on PATH has all of them.  Anything imported by the pipeline
must therefore work on the smaller of the two.
"""

import os

# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------
ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "data")
CACHE_DIR = os.path.join(DATA_DIR, "http_cache")
DB_PATH = os.path.join(DATA_DIR, "theta_desk.db")
LOG_DIR = os.path.join(DATA_DIR, "logs")

for _d in (DATA_DIR, CACHE_DIR, LOG_DIR):
    os.makedirs(_d, exist_ok=True)

# --------------------------------------------------------------------------
# Server
# --------------------------------------------------------------------------
HOST = "127.0.0.1"
PORT = 5058                      # 5057 is Gridiron Edge

# --------------------------------------------------------------------------
# Universe
# --------------------------------------------------------------------------
# Tickers scanned by the daily pipeline.  CBOE serves indices with a leading
# underscore (_SPX); `sources.cboe` handles that translation.
WATCHLIST = [
    # Index / broad ETFs -- the liquidity backbone
    "SPY", "QQQ", "IWM", "DIA",
    # Mega-cap tech
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO", "AMD",
    # High-IV single names traders actually trade
    "NFLX", "COIN", "PLTR", "MSTR", "SMCI", "MU", "CRWD", "SHOP", "UBER",
    # Financials / energy / healthcare for sector spread
    "JPM", "GS", "XOM", "OXY", "LLY", "UNH",
    # Vol / rates instruments
    "TLT", "GLD", "SLV", "XLE", "XLF", "SMH", "ARKK",
]

BENCHMARK = "SPY"
MACRO_SYMBOLS = ["^VIX", "^GSPC", "^IRX", "^FVX", "^TNX"]

# --------------------------------------------------------------------------
# Market conventions
# --------------------------------------------------------------------------
MARKET_TZ_OFFSET = -4            # ET vs UTC during DST; resolved dynamically
EQUITY_EXPIRY_HOUR_ET = 16.0     # PM-settled equity/ETF options
INDEX_AM_EXPIRY_HOUR_ET = 9.5    # AM-settled (SPX monthly)
DAYS_PER_YEAR = 365.0
TRADING_DAYS_PER_YEAR = 252.0

# Fallback risk-free if the Treasury proxies cannot be fetched
DEFAULT_RISK_FREE = 0.0425

# --------------------------------------------------------------------------
# Chain hygiene -- quotes failing these are excluded from *model fitting*
# (they are still stored, and still counted for volume/OI statistics)
# --------------------------------------------------------------------------
QUOTE_MIN_BID = 0.01
QUOTE_MAX_SPREAD_PCT = 0.75      # (ask-bid)/mid
QUOTE_MAX_SPREAD_ABS = 25.0
MIN_IV = 0.01
MAX_IV = 5.00
FIT_MIN_QUOTES = 6               # per-expiry quotes needed to fit a smile
FIT_DELTA_BAND = (0.02, 0.98)    # only fit where the option has real vega

# --------------------------------------------------------------------------
# Liquidity scoring
# --------------------------------------------------------------------------
LIQ_GOOD_SPREAD_PCT = 0.04       # <=4% of mid scores full marks
LIQ_BAD_SPREAD_PCT = 0.25
LIQ_GOOD_OI = 1000
LIQ_GOOD_VOLUME = 250
MIN_TRADEABLE_OI = 25
MIN_TRADEABLE_VOLUME = 5

# --------------------------------------------------------------------------
# Volatility model
# --------------------------------------------------------------------------
HV_WINDOWS = [5, 10, 20, 30, 60, 90, 120, 252]
IV_RANK_MIN_SAMPLES = 40         # below this, IV rank is flagged "building"
IV_RANK_LOOKBACK_DAYS = 365
# Blend weights for the DIFFUSIVE forecast vol -- the vol expected in a window
# with no scheduled event.  Expected jumps are added back explicitly by
# vol.add_event_jump using the market's own implied earnings move.
#
# Every estimator here is chosen for immunity to a *past* overnight gap, which
# is the dominant source of error.  Measured on live AAPL two weeks after a
# -7.6% earnings print: close-to-close said 34.5%, Yang-Zhang 40.1% (it models
# the overnight component explicitly, so it is the most contaminated of all),
# raw EWMA 33.3% -- while the intraday-only estimators said 21.7-22.2% and the
# winsorised close-to-close 24.1%.  Options were priced at 23.2%.  The naive
# blend called that a 25% discount and scored a long strangle at +55% EV on
# risk; the whole "edge" was one gap that had already happened.
#
# Winsorised and intraday estimators therefore carry the weight; bipower gets a
# modest share (it is only asymptotically jump-free, and one large jump in a
# 20-day window still leaks through); Yang-Zhang is excluded outright.
VOL_FORECAST_WEIGHTS = {
    "trim20": 0.24,   # winsorised close-to-close, 20d
    "trim60": 0.18,   # winsorised close-to-close, 60d
    "rs20": 0.14,     # Rogers-Satchell: intraday only, blind to overnight gaps
    "gk20": 0.10,     # Garman-Klass
    "bp60": 0.12,     # bipower over a long enough window to dilute a jump
    "ewma": 0.14,     # winsorised EWMA -- the fast-reacting term
    "trim252": 0.08,  # long-run level
}
EWMA_LAMBDA = 0.94
# Realised vol mean-reverts; forecast pulls toward the 1y mean by this much
# over a 30d horizon.  Fitted from the typical half-life of equity vol (~40d).
VOL_MEAN_REVERSION_30D = 0.35
# Volatility risk premium: implied has historically exceeded subsequent
# realised by roughly this ratio for liquid US equity underlyings.
TYPICAL_VRP_RATIO = 1.12

# --------------------------------------------------------------------------
# Trend model -- weights of the composite trend score (sum of |w| = 1.0)
# --------------------------------------------------------------------------
TREND_WEIGHTS = {
    "ma_stack": 0.22,      # 20/50/200 alignment
    "price_vs_200": 0.14,
    "macd": 0.14,
    "rsi": 0.10,
    "adx_dir": 0.14,
    "roc": 0.14,
    "rel_strength": 0.12,  # vs SPY
}
RSI_PERIOD = 14
ATR_PERIOD = 14
ADX_PERIOD = 14
BB_PERIOD, BB_STD = 20, 2.0
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9

# --------------------------------------------------------------------------
# News
# --------------------------------------------------------------------------
NEWS_MAX_AGE_DAYS = 14
NEWS_HALF_LIFE_HOURS = 36.0      # sentiment decays with this half-life
NEWS_MAX_PER_TICKER = 60
NEWS_SOURCE_WEIGHTS = {          # crude credibility prior
    "reuters": 1.25, "bloomberg": 1.25, "wall street journal": 1.2,
    "cnbc": 1.1, "barron": 1.1, "financial times": 1.2, "associated press": 1.15,
    "seeking alpha": 0.8, "zacks": 0.7, "motley fool": 0.6, "benzinga": 0.85,
    "investorplace": 0.6, "simply wall st": 0.6, "insider monkey": 0.5,
}

# --------------------------------------------------------------------------
# Strategy / scanner
# --------------------------------------------------------------------------
SCAN_MIN_DTE = 7
SCAN_MAX_DTE = 75
SCAN_PREFERRED_DTE = (25, 50)
SCAN_TOP_N = 25                  # ideas surfaced per day
SCAN_MAX_PER_TICKER = 3
COMMISSION_PER_CONTRACT = 0.65   # used in EV; set to 0 if your broker is free
SLIPPAGE_FRAC_OF_SPREAD = 0.25   # assume you pay a quarter of the spread

# Account sizing for the suggested contract count
ACCOUNT_SIZE = 25000.0
RISK_PER_TRADE_PCT = 0.02        # 2% of account at risk per idea

# Exit policy (drives the take-profit ladder)
CREDIT_TP1_FRAC = 0.50           # buy back at 50% of credit
CREDIT_TP2_FRAC = 0.75
CREDIT_STOP_MULT = 2.00          # stop when loss = 2x credit
DEBIT_TP1_FRAC = 0.50            # +50% on premium paid
DEBIT_TP2_FRAC = 1.00            # +100%
DEBIT_TP3_FRAC = 2.00
DEBIT_STOP_FRAC = 0.50           # -50% on premium paid
TIME_STOP_DTE = 21               # gamma risk ramps below this
EARNINGS_BLACKOUT_DAYS = 2       # avoid holding short vega through a print

# Ranking weights for the daily idea score.
# `sizing` exists because a beautiful trade you cannot put on at a sane size is
# not a trade: on a $968 underlying a single long call risked $4,280 against a
# $500 per-trade budget and still ranked first on expectancy alone.
IDEA_WEIGHTS = {
    "edge": 0.27,        # model EV per dollar risked
    "vol_edge": 0.18,    # implied vs forecast vol mismatch
    "trend": 0.16,       # directional agreement
    "liquidity": 0.12,
    "news": 0.07,
    "structure": 0.08,   # GEX / OI walls / max pain support
    "sizing": 0.12,      # does one contract fit the risk budget?
}

# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
HTTP_TIMEOUT = 30
HTTP_RETRIES = 3
HTTP_BACKOFF = 1.6
THROTTLE_SECONDS = {             # min gap between requests to a host
    "cdn.cboe.com": 0.35,
    "query1.finance.yahoo.com": 0.6,
    "query2.finance.yahoo.com": 0.6,
    "feeds.finance.yahoo.com": 0.5,
    "news.google.com": 0.8,
    "api.nasdaq.com": 1.0,
}
CACHE_TTL = {                    # seconds a cached HTTP body stays valid
    "chain": 300,
    "prices": 1800,
    "news": 900,
    "earnings": 21600,
    "macro": 1800,
}

# --------------------------------------------------------------------------
# Retention
# --------------------------------------------------------------------------
# `contract_quote` is the only table that grows fast: ~100k rows per full run.
# Nothing in the app *reads* it historically -- the chain view and the mark
# fallback both hit the latest snapshot, and IV rank / the forward test read the
# small rollups -- so old quotes are a research archive, not a dependency.
# Unbounded, one run a day would reach roughly 15 GB inside a year.
#
# The archive can be aggressive because `expiry_metrics` stores the fitted
# smile's coefficients, scale and strike range -- so the *whole* volatility
# surface for any past day is reconstructible from a few thousand rows without
# keeping a hundred thousand individual quotes.  The quote archive exists to
# preserve what actually traded, not to preserve the surface.
#
# Measured on a live 35-name run (102k quotes/day, ~375 bytes/row):
#   detail 3d + archive 60d @ OI>=250, |k|<=0.12  ->  ~930k rows, ~350 MB
QUOTE_FULL_DETAIL_DAYS = 3       # every snapshot, every contract
QUOTE_ARCHIVE_DAYS = 60          # one snapshot/day, near-the-money + liquid only
ARCHIVE_MIN_OI = 250             # archived contracts must be genuinely traded
ARCHIVE_MAX_MONEYNESS = 0.12     # |ln(K/spot)| -- the wings are not worth keeping
NEWS_RETENTION_DAYS = 120
VACUUM_ABOVE_BYTES = 500 * 1024 * 1024
