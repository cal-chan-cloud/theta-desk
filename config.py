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
# Vol-forecast recalibration.  MEASURED, not assumed.
#
# A walk-forward backtest over 2 years x 35 names (2,660 forecast/outcome
# pairs, forecast computed from strictly prior data) found the blend
# systematically UNDER-forecasts realised vol: median predicted/actual 0.876.
# That is not harmless.  sigma_p sets the width of the P-measure density, so a
# forecast that is 12% too low makes every short-premium structure look safer
# than it is -- inflating POP, understating CVaR, and flattering exactly the
# iron condors and short strangles the scanner likes to rank first.
#
# The irony is that the bias comes from the jump-robust machinery added to fix
# the AAPL earnings-gap problem: winsorising and bipower strip out real
# volatility that does recur, not just the stale gap.  Plain Yang-Zhang is
# nearly unbiased (1.012) but has 3% worse RMSE; the blend wins on variance and
# loses on bias, so the fix is to keep the blend and correct the level.
#
# Mincer-Zarnowitz calibration  log(actual) = A + B*log(forecast), fitted on the
# FIRST half of the sample and validated on the second half it never saw:
#     median predicted/actual   0.869 -> 0.994
#     mean bias                -0.0765 -> -0.0289
#     RMSE                      0.1609 -> 0.1394   (13% better)
# B is 0.994, i.e. essentially 1, so this is close to a flat 1.135x scale --
# the apparent regime slope in the raw diagnostics is mostly an artefact of
# conditioning on the realised outcome.  Set VOL_CALIBRATION = False to disable.
VOL_CALIBRATION = True
VOL_CALIB_A = 0.1268
VOL_CALIB_B = 0.9942

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

# --------------------------------------------------------------------------
# Portfolio construction.  Individual scores are blind to the shape of the
# whole board: nothing stopped 2026-08-13..19 running 15-20 bullish of 25 at
# once, and most of that week's loss was that single aggregate bet rather than
# bad individual trades.  These caps are applied AFTER ranking, so the best
# idea in a crowded bucket still survives -- the marginal fifth copy does not.
BOARD_MAX_BULLISH_PCT = 0.55     # share of the board allowed to be long delta
BOARD_MAX_BEARISH_PCT = 0.55
BOARD_MAX_PER_GROUP = 5          # ideas per correlated group (see GROUPS)
BOARD_MAX_PER_SYMBOL = 2         # down from 3: one name, one thesis
BOARD_ENFORCE = True

# --------------------------------------------------------------------------
# Hard limits, as opposed to score penalties.  MEASURED over the first 75 ideas
# (2026-08-13..19, marked daily):
#
#   long_call          6 ideas   0% green   -$5,787   median return on risk -0.51
#   bull_call_spread  23 ideas  35% green   -$3,171   median -0.16
#   bull_put_spread   25 ideas  52% green     -$361   median  0.00
#   debit  36 ideas  net -$9,495  mean risk $1,277
#   credit 39 ideas  net   -$741  mean risk $  593
#
# and MU alone was -$7,083 of a -$10,236 total across 5 ideas on 4 boards.
#
# Two structural problems, both fixable without tuning anything to this sample:
#
# 1. An idea risking several times the per-trade budget was DEMOTED by the
#    sizing score but never removed, so it still reached the board and still
#    got taken at one contract.  A trade you cannot size is not a trade.
# 2. A debit structure with a 27% chance of profit is a lottery ticket however
#    good its modelled expectancy looks, because that expectancy leans entirely
#    on the vol forecast being right about the future.
MAX_RISK_MULTIPLE = 2.0          # drop ideas risking > this x the per-trade budget
MIN_POP_DEBIT = 0.45             # measured, see POP calibration below
MIN_POP_CREDIT = 0.20
# Same name proposed day after day becomes a concentrated bet by accumulation:
# MU reached 5 live ideas that way.  Counted across recent boards, not just today.
CONCENTRATION_LOOKBACK_DAYS = 5
CONCENTRATION_MAX_RECENT = 4     # appearances on recent boards before we stop adding

# --------------------------------------------------------------------------
# PROBABILITY-OF-PROFIT CALIBRATION.  MEASURED on 152 ideas that reached expiry
# and were settled at the close ON their expiry date.
#
#   family   n    stated POP   realised   gap
#   credit   68      77%          72%      -5     <- honest
#   debit    84      38%          14%     -24     <- badly overstated
#
# A 14% win rate where 38% was predicted, on 84 samples, is not noise.
#
# WHY the model is wrong only for long premium: the density's width comes from
# a forecast of the MEAN realised vol, and realised vol is strongly right-
# skewed.  Over the same window the forecast was almost perfect on the mean
# (42.9% forecast vs 43.2% realised) yet it EXCEEDED the realised figure in 71%
# of individual cases -- the mean is carried by a few violent names while the
# typical one goes quiet.  Buying premium is a bet that YOUR name is one of the
# movers, so a mean-calibrated density systematically overstates its chance.
# Short premium is unaffected: it wins in the quiet majority, which is exactly
# the case the mean over-weights.
#
# Calibration is applied as a shrink toward the measured realised rate, damped
# by CALIB_WEIGHT so 152 observations in one six-week regime cannot fully
# dictate the model.  Raise CALIB_WEIGHT as the sample grows.
#
# Refit 2026-09-25 on 191 settled ideas (RAW model POP vs held-to-expiry win):
#   credit  n=80   raw 76.7%  realised 70.0%   ratio 0.91
#   debit   n=111  raw 38.7%  realised 17.1%   ratio 0.44
# Same verdict, slightly less extreme for debit.  Fit on RAW POP only -- boards
# since 09-24 store the calibrated number in `pop` and the raw one in
# metrics.pop_raw; fitting on `pop` would calibrate twice.
POP_CALIBRATION = True
POP_CALIB_WEIGHT = 0.6           # 0 = trust the model, 1 = trust the sample
POP_CALIB_FACTOR = {"credit": 0.91, "debit": 0.44}

# The floors above were set by counterfactual, not taste.  Replaying the 152
# resolved ideas with different debit POP floors:
#     floor 0.00 -> net -$39,233   meanR -0.278   84 of 84 debits kept
#     floor 0.40 -> net -$14,550   meanR -0.185   32 kept
#     floor 0.45 -> net  -$6,666   meanR -0.068    8 kept
#     floor 0.50 -> net  -$1,254   meanR +0.010    0 kept  (credit-only book)
# 0.45 is chosen deliberately over 0.50: no debit in the sample cleared 0.50, so
# that floor bans long premium outright, which would be fitting to a single
# quiet regime rather than correcting a model error.  Long premium is supposed
# to lose in quiet markets and pay in violent ones.

# Names that rise and fall together.  Four "independent" ideas on SPY, QQQ, IWM
# and DIA are one index bet with four tickets; the same is true across the
# semis.  Anything unlisted is its own group.
CORRELATION_GROUPS = {
    "index":  ["SPY", "QQQ", "IWM", "DIA"],
    "semis":  ["NVDA", "AMD", "SMH", "MU", "AVGO", "SMCI"],
    "megacap": ["AAPL", "MSFT", "AMZN", "GOOGL", "META"],
    "crypto": ["COIN", "MSTR"],
    "metals": ["GLD", "SLV"],
    "energy": ["XOM", "OXY", "XLE"],
    "banks":  ["JPM", "GS", "XLF"],
    "rates":  ["TLT"],
}
SCAN_MAX_PER_TICKER = 3
COMMISSION_PER_CONTRACT = 0.65   # used in EV; set to 0 if your broker is free
SLIPPAGE_FRAC_OF_SPREAD = 0.25   # assume you pay a quarter of the spread

# Account sizing for the suggested contract count.
# These are DEFAULTS. Put your real numbers in config_local.py (gitignored) --
# see config_local.example.py. Position sizing and the affordability component
# of the idea score both read these, so they change what the board recommends.
ACCOUNT_SIZE = 10000.0
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

# Never open a trade the time stop will close almost at once.  On 2026-09-24
# fourteen of the 25 ideas used the Oct-16 expiry at 22 calendar days: the
# scanner scored them on a 0.7-day horizon (DTE - 21) and the time stop closed
# them the next session -- a round-trip bid-ask paid for one day of theta.  An
# expiry is tradable only if it is inside the time stop already (held to
# expiry, no time stop) or leaves at least this many calendar days before it.
# DTE is counted in calendar days from the session date everywhere, which is
# how the time stop has always been enforced.
MIN_DAYS_BEFORE_TIME_STOP = 7

# A stop on a position that spans an earnings print is not armed until the
# second session after the print (see replay.stop_armed_from).  Measured over
# the 21 credit ideas that spanned one: held +$2,181, managed with a live stop
# -$2,868 -- the stop fired on the gap-day mark every time.  Positions that
# span a print must be defined-risk (EARNINGS_NAKED_OK below), so switching
# the stop off for two sessions cannot cost more than the width.
EARNINGS_STOP_HOLD = True
EARNINGS_NAKED_OK = False

# --------------------------------------------------------------------------
# Does the forward test obey the model's OWN exit rules?
#
# It did not, and that quietly mis-stated every result: ideas were marked daily
# until expiry, so the recorded performance was buy-and-hold-to-expiry -- a
# strategy the model never recommends and nobody would run.  Replaying the
# ladder over the 152 resolved ideas:
#
#                       meanR      net
#   hold to expiry     -0.278   -$39,233
#   managed T1/stop    -0.175   -$21,576     <- the strategy actually proposed
#
# and the entire benefit is on the long-premium side, where a stop prevents a
# decaying option bleeding to zero:
#   debit   -0.511 -> -0.297      credit  +0.010 -> -0.025
#
# Credit is marginally WORSE managed, because taking 50% of the credit caps the
# win while leaving the tail intact -- the standard critique of that rule.  That
# result is regime-dependent (this was a quiet stretch where short premium
# mostly expired worthless) so the policy is left alone; what is fixed is the
# measurement.
#
# 2026-09-25: those figures came from three measurements with three rule sets.
# replay.py is now the one exit engine (all three rules, stops and time exits
# filled at the mark less exit cost, gap days priced from stored surfaces) and
# sync_exits writes its answer into the idea table each run.  On 191 settled
# ideas:                         meanR      net
#   hold to expiry              -0.284   -$42,144
#   managed, stop live on print -0.087   -$19,816
#   managed, stop held on print -0.047   -$13,567   <- current rules
#     of which credit           +0.025    +$1,142
#     of which debit            -0.099   -$14,709
MANAGED_EXITS = True

# --------------------------------------------------------------------------
# Evaluation horizon.  THE MOST IMPORTANT CONSTANT IN THE MODEL.
#
# Expectancy used to be computed at EXPIRY while positions are held for about
# ten days and judged on the daily mark.  For a long option almost all of the
# expiry expectancy lives in tail paths that need the full time to develop; over
# ten days you simply pay theta.  A P&L attribution over 247 idea-marks made the
# size of this plain:
#
#   theta   debit structures  -$19,737     credit structures  +$12,209
#   delta   only -$1,155 across everything -- 4% of the loss
#
# and per day, as a share of the money at risk:
#
#   long_strangle -3.05%/day   long_call -2.35%/day   bull_call_spread -0.57%
#   iron_condor   +1.19%/day   short_strangle +14.46%/day
#
# A long call therefore surrendered roughly a quarter of its risk to decay over
# a ten-day hold before direction or volatility did anything at all -- and none
# of that appeared in the expectancy the ranking was built on.
#
# Evaluating at the horizon actually traded puts carry back into EV, POP and
# CVaR where it belongs, instead of bolting a separate "carry" term onto the
# score.  The horizon is the earlier of half the time to expiry or the documented
# 21-DTE time stop, which is when the exit policy says to be out.
EVAL_AT_HORIZON = True
EVAL_HORIZON_FRAC = 0.5
EARNINGS_BLACKOUT_DAYS = 2       # avoid holding short vega through a print

# Ranking weights for the daily idea score.
# `sizing` exists because a beautiful trade you cannot put on at a sane size is
# not a trade: on a $968 underlying a single long call risked $4,280 against a
# $500 per-trade budget and still ranked first on expectancy alone.
# How hard the real-world drift is allowed to lean on the trend score.
#
# Was 0.35 (a ~0.35 Sharpe tilt).  On a 40%-vol name that is r + 14% annualised
# drift, which materially inflates the expectancy of every long-delta structure
# -- and the boards from 2026-08-13..19 ran 15-20 bullish of 25 into a falling
# tape and lost most of their money to that skew.  Trend predicting 30-day
# returns is a weak effect at best (short-term reversal competes with it), and
# the scanner's own forward test has not yet shown the trend score predicts
# anything (r = +0.05 on 75 marks).  Shrunk hard until that evidence exists;
# the directional view still reaches the score through IDEA_WEIGHTS["trend"].
DRIFT_TILT_SHARPE = 0.10
DRIFT_NEWS_WEIGHT = 0.04

IDEA_WEIGHTS = {
    "edge": 0.20,        # model EV per dollar risked (was 0.27)
    "pop": 0.20,         # calibrated probability of profit -- NEW
    "vol_edge": 0.14,    # implied vs forecast vol mismatch
    "trend": 0.12,       # directional agreement
    "liquidity": 0.10,
    "news": 0.06,
    "structure": 0.08,   # GEX / OI walls / max pain support
    "sizing": 0.10,      # does one contract fit the risk budget?
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

# --------------------------------------------------------------------------
# Local overrides -- MUST stay last so it wins over everything above.
#
# config_local.py is gitignored. It is where personal settings live (account
# size, risk appetite, your own watchlist) so this file can be published
# without publishing them. Copy config_local.example.py to get started.
# --------------------------------------------------------------------------
try:
    from config_local import *          # noqa: F401,F403
except ImportError:
    pass
