"""Local overrides for Theta Desk.  Copy to `config_local.py` and edit.

`config.py` imports this file last, so anything defined here wins. It is
gitignored, which is the point: personal settings — how much money you run,
how much of it you are willing to lose on one idea, what you actually watch —
stay on your machine while the rest of the project is public.

Only define what you want to change. Everything omitted keeps the default.
"""

# --------------------------------------------------------------------------
# Money.  These drive position sizing AND the affordability component of the
# idea score, so they change which trades reach the top of the board -- a
# structure risking more than the per-trade budget is pushed down rather than
# hidden, so a larger account still sees it.
# --------------------------------------------------------------------------
# ACCOUNT_SIZE = 25000.0
# RISK_PER_TRADE_PCT = 0.02          # 2% of the account at risk per idea

# --------------------------------------------------------------------------
# Your universe.  Every name here gets a full chain snapshot each run
# (~3k contracts and roughly 1-2 seconds apiece), so this is the main lever on
# how long the pipeline takes.
# --------------------------------------------------------------------------
# WATCHLIST = [
#     "SPY", "QQQ", "IWM",
#     "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA",
# ]

# --------------------------------------------------------------------------
# Trade construction
# --------------------------------------------------------------------------
# SCAN_MIN_DTE = 7                   # ignore anything expiring sooner
# SCAN_MAX_DTE = 75
# SCAN_TOP_N = 25                    # ideas kept on the daily board
# SCAN_MAX_PER_TICKER = 3
# COMMISSION_PER_CONTRACT = 0.65     # set to 0 if your broker is free
# SLIPPAGE_FRAC_OF_SPREAD = 0.25     # fraction of the bid-ask you assume to pay

# --------------------------------------------------------------------------
# Exit policy -- this is what the take-profit ladder is built from
# --------------------------------------------------------------------------
# CREDIT_TP1_FRAC = 0.50             # buy a credit spread back at 50% of credit
# CREDIT_STOP_MULT = 2.00            # stop out when the loss reaches 2x credit
# DEBIT_TP1_FRAC = 0.50              # +50% on premium paid
# DEBIT_STOP_FRAC = 0.50             # -50% on premium paid
# TIME_STOP_DTE = 21                 # close here; gamma risk outruns theta

# --------------------------------------------------------------------------
# Server
# --------------------------------------------------------------------------
# HOST = "127.0.0.1"                 # 0.0.0.0 exposes it to your network --
#                                    # set a password first (set_password.py)
# PORT = 5058
