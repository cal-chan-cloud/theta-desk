# Theta Desk

A local options-research terminal: full chain analytics, volatility surface,
stock trends, news, a daily trade board with take-profit ladders, and a journal
that measures both your trades and the scanner's own suggestions.

**Site:** http://127.0.0.1:5058 — `run_site.bat`
**Daily refresh:** `run_daily.bat` (register with `register_task.ps1`)

Everything runs locally. No API keys, no paid data, no accounts.

---

## Quick start

```bat
cd "%USERPROFILE%\theta_desk"
run_daily.bat        :: fetch + analyse + build the board   (~2.5 min cold)
run_site.bat         :: serve the site on 127.0.0.1:5058
```

Then register the daily job once:

```powershell
powershell -ExecutionPolicy Bypass -File register_task.ps1
```

On a fresh clone, set a password before starting the server:

```bat
python set_password.py              :: prompts twice, nothing echoed
python set_password.py --generate   :: or generate a strong one
python set_password.py --status     :: check whether one is set
```

---

## Access control

The site sits behind a password gate.

* The password is never stored — only a **PBKDF2-HMAC-SHA256** hash with a
  per-install random salt at 600k iterations, compared with
  `hmac.compare_digest` so verification does not leak through timing.
* Credentials live in `data/auth.json`, which is **gitignored**. Nothing secret
  ever enters the repository. `THETA_DESK_PASSWORD` in the environment
  overrides the file if you ever deploy this behind a real host.
* The session is a Flask signed cookie over a persisted random key, so
  restarting the server does not log you out and a forged cookie fails the HMAC.
* Failed logins back off exponentially per client address (2s, 4s, 8s … capped
  at five minutes).
* `/static/*` and `/healthz` stay open — the first so the login page can render,
  the second so a monitor can poll without credentials. **Every `/api/*` route
  returns 401 without a session**, which is what actually matters, since the API
  is where the data is.

Two things worth being clear about:

* **`HOST` defaults to `127.0.0.1`**, so the site is already unreachable from
  outside this machine. The password matters the moment you change that, put it
  behind a tunnel, or share a screen.
* **It is plain HTTP.** On localhost that is fine. If you ever expose it, put it
  behind a TLS terminator and set `THETA_DESK_HTTPS=1` so the session cookie is
  marked `Secure`.

To remove the gate: `python set_password.py --clear`.

**The repository is public; your instance is not.** Nothing secret is in here —
no API keys (there are none to have), no database, no credentials. The password
hash, the session signing key and your personal settings all live in gitignored
files on your own machine.

## Personal settings

`config.py` holds published **defaults**. Your own numbers go in
`config_local.py`, which is gitignored and imported last so it wins:

```bat
copy config_local.example.py config_local.py
```

Account size and risk-per-trade are the ones worth setting first — they drive
position sizing *and* the affordability component of the idea score, so they
change which trades reach the top of the board.

Verify the model at any time:

```bat
python tests.py            :: 234 model invariants, no network
python tests.py --live     :: + the live data adapters
python tests.py --api      :: + every HTTP endpoint (server must be running)
```

`tests.py --live --api` runs 441 checks in about five seconds. Run it after any
model change: several of the bugs documented below were found by a number coming
out visibly wrong, and each now has an invariant pinning it.

---

## Where the data comes from

| Source | Gives us | Notes |
|---|---|---|
| **CBOE delayed quotes** (`cdn.cboe.com`) | Every listed contract: bid/ask/size, volume, open interest, vendor IV and greeks; underlying price and 30-day IV | ~15 min delayed, no key. ~100k contracts per run across the watchlist. |
| **Yahoo chart API** (`/v8/finance/chart`) | Daily OHLCV, plus `^VIX`, `^IRX`, `^FVX`, `^TNX` | The one Yahoo endpoint that still answers without a crumb handshake. `/v7/quote` and `/v10/quoteSummary` both return 401 now. |
| **Yahoo Finance RSS + Google News RSS** | Per-ticker and market headlines | Two feeds because they fail differently — Yahoo is precise but thin, Google is deep but noisy. |
| **NASDAQ calendar API** | Earnings dates, times and EPS estimates | Swept over a forward window of sessions. |

**SSL note:** `cdn.cboe.com` presents a chain containing a root that has expired
in the Windows certificate store, so Python's default context rejects it while
every browser accepts it. `net.py` prefers `certifi`'s bundle. Verification is
never disabled.

---

## What it computes

### Per contract
Our own implied vol (solved off the recovered forward, not off spot), delta,
gamma, theta, vega, rho, **vanna, vomma, charm**, intrinsic/extrinsic, spread %,
volume/OI ratio, and a 0–100 liquidity score weighted toward spread — because
spread is what you actually pay.

### Per expiry
Implied forward and discount factor from put-call parity, implied dividend
yield, a fitted volatility smile, **model-free (VIX-style) implied vol**,
25-delta skew / risk reversal / butterfly, ATM skew slope, expected move,
straddle price, max pain, open-interest walls, dealer gamma exposure, put/call
ratios by volume and by open interest, and arbitrage flags on the fitted
surface.

### Per symbol
Constant-maturity IV at 30/60/90 days (interpolated in **total variance**, not
in vol), term-structure slope, nine realised-vol estimators, a jump-robust vol
forecast, variance risk premium, IV rank/percentile *and* HV rank/percentile,
a composite trend score from seven normalised sub-signals, support/resistance
pivot clusters, news sentiment and buzz, earnings date **and the market's own
implied earnings move**, net dealer gamma with its zero-gamma flip level, and
unusual options activity.

---

## Model notes — the decisions that matter

These are the places where the obvious implementation is wrong, and what is
done instead. Each was found by a number coming out visibly wrong on live data.

**1. Price off the recovered forward, not off spot + a guessed dividend.**
`C − P = df·(F − K)` is regressed per expiry (weighted by quote quality, with
one outlier-rejection pass) to recover `F` and `df`. Assuming `q = 0` biases
call IVs up and put IVs down on any dividend payer — by exactly the size of the
skew signal we want to measure.

**2. Fit the smile in standardised moneyness `z = ln(K/F)/(σ√T)`, not raw `k`.**
A real smile is roughly V-shaped in raw log-moneyness and its curvature scales
like `1/(σ²T)`, so a parabola fitted over a ±25% strike range puts its vertex
well above the true at-the-money vol. Measured on live 1-DTE AAPL, the raw-`k`
fit returned **41.5% ATM against a 26.6% model-free vol**. In `z`-space the
strike range is ~±3 standard deviations at every tenor and the same chain fits
to within tenths of a point.

**3. Only out-of-the-money quotes inform the surface.** A deep ITM option is
almost all intrinsic — its extrinsic value is smaller than its own bid-ask
spread, so inverting its price for vol amplifies quote noise enormously. Live
AAPL had a 110-strike call (spot 303) solving to **136% vol**. Market makers
quote those off their OTM twin via parity anyway.

**4. Wings extrapolate damped-linear, not flat.** Flat wings cut deep OTM put
vol off at the fit boundary, which flattens the left tail of the recovered
density and makes CVaR look better than the market is pricing. Continuing the
parabola is worse — its curvature explodes. A damped linear continuation,
floored so a wing can never slope back down as it goes further OTM, keeps the
tails honest.

**5. Volatility forecasting is jump-robust, and expected jumps are added back
explicitly.** This is the single largest source of fake edge. Two weeks after a
−7.6% earnings print, live AAPL read: close-to-close 34.5%, **Yang-Zhang 40.1%**
(it models the overnight component, so it is the *most* contaminated),
raw EWMA 33.3% — while intraday-only estimators said 21.7–22.2% and winsorised
close-to-close said 24.1%. Options were priced at 23.2%. The naive blend called
that a 25% discount and scored a long strangle at **+55% EV on risk** — the
entire "edge" was one gap that had already happened.

So the forecast blends winsorised, intraday-range (Rogers-Satchell,
Garman-Klass) and bipower estimators, mean-reverts toward a winsorised long-run
anchor, and then — if a scheduled event actually falls inside the expiry — adds
back the jump the **market itself is pricing**, recovered from the kink between
the last expiry before the print and the first one after:

```
J = sqrt( σ_after²·T_after − σ_before²·T_before )
```

**6. Expectancy comes from the chain's own density, not a lognormal guess.**
The fitted smile is inverted via Breeden-Litzenberger into the market's terminal
distribution. Under that measure every fair trade has EV = 0 — so it is used
only to measure *execution cost*. The real edge comes from a P-measure density:
the same distribution re-centred on our drift and re-scaled from implied vol to
the forecast, **keeping the market's skew and kurtosis** (the parts we have no
better estimate of) and substituting only the mean and variance (the parts we
do).

**7. Fills assume you give up a quarter of the bid-ask on every leg.**
Mid-price fills are a fiction that makes every backtest profitable. On a
four-leg condor this is the difference between a good idea and a bad one.

**8. Two-sided targets use a real double-barrier probability.** A straddle's
50%-profit target can be hit up *or* down, and `P(touch either)` is not the sum
of the one-sided probabilities. It is solved on the backward Kolmogorov equation
with absorbing boundaries. The first implementation used an explicit scheme —
which needs `dt ≤ 0.4·dx²/σ²` for stability, tens of thousands of steps for
realistic barriers. Capping the steps to stay fast silently violated that and
returned **20%, 60%, 20%, 99% for monotonically widening barriers**. Backward
Euler with a tridiagonal solve is unconditionally stable; convergence is
dominated by the *space* step (4.1pp error at 161 nodes → 0.15pp at 641, while
raising 180 steps to 600 changes nothing). `tests.py` asserts the degenerate
single-barrier case matches the closed form.

**9. Model-free vol is rejected when the strike grid is too sparse.** The
VIX-style integral is a Riemann sum over listed strikes. Long-dated chains list
strikes in huge steps and the sum over-weights whichever wing is quoted — live
NVDA 2027 expiries returned **51% against a 41% ATM**, putting a fake kink in
the term structure. Sparse grids and results wildly at odds with ATM are
dropped rather than published.

**10. A trade you cannot size is not a trade.** On a $968 underlying a single
long call risked $4,280 against a $500 per-trade budget and still ranked **#1**
on expectancy alone. Affordability is now a scored component, not just a
warning, so it drops down the board (to #12) without disappearing for anyone
running a bigger account.

**11. Volatility accrues on trading days.** Expected moves use business-time
`√(sessions/252)`; pricing uses calendar ACT/365 to the actual close (or the
*open*, for AM-settled index monthlies — a full session of time value).

**12. A covered call is long stock plus a short call, and must be modelled as
both.** Priced as a bare short call it reports *unbounded upside risk* — the
exact opposite of the truth, since the stock caps the upside and the real risk
is the stock falling. `Position` therefore supports stock legs, and the
far-side payoff slope counts them.

**13. Symbol-level positioning is quoted off a ~30-day expiry, not the front
one.** On any name that lists a contract every day, the front expiry at 0 DTE is
a handful of dying strikes: its "max pain" and open-interest walls describe
nothing. The scanner likewise scores a structure's short strikes against the
walls in *the expiry being traded*, not the front.

**14. A side-of-spread hint is only shown for prints from the current
session.** An illiquid contract can carry a last price from days ago; reading it
against today's bid/ask produces a confident "at ask" on a trade that never
happened today — precisely the row an unusual-activity screen must not invent.

---

## The daily trade board

Regime is read three ways — trend, vol, and event — and the structure follows:

| | IV cheap | IV fair | IV rich |
|---|---|---|---|
| **Bullish** | long call, bull call spread | bull call spread, bull put spread | bull put spread, cash-secured put |
| **Neutral** | long strangle, long straddle | iron condor | iron condor, iron butterfly, short strangle |
| **Bearish** | long put, bear put spread | bear put spread, bear call spread | bear call spread |

An imminent print with cheap front-month vol also injects the long-gamma
structures.

Each idea is scored on **expectancy (27%), vol edge (18%), trend agreement
(16%), affordability (12%), liquidity (12%), dealer positioning (8%) and news
(7%)**, and carries a full trade plan:

* every leg with its fill price, IV, delta, open interest and liquidity score
* max profit / max loss / breakevens / net greeks / suggested contract count
* **a take-profit ladder** — each rung priced as a spread value *and* translated
  into either the underlying price that gets you there or the date decay gets
  you there, with a first-passage probability attached
* a stop, a 21-DTE time stop, and explicit cautions (earnings inside the window,
  short vega through a print, thin quotes, size above budget, noisy surface)

Tuning lives entirely in `config.py` — watchlist, account size, risk per trade,
DTE band, exit fractions, scoring weights.

---

## The journal

Two books are tracked side by side:

* **`trade`** — what you actually put on. Marked every pipeline run, so the
  equity curve, book greeks and target progress stay current.
* **`idea`** — everything the scanner has ever proposed, marked the same way
  whether or not you took it. It is written before the outcome is known and
  never revised, so the hit rate you read later is the hit rate it actually had.

The **calibration chart** compares stated probability of profit against how
often those ideas were really in profit. If the model's confidence is not
earned, that chart is where it shows.

Marking prefers the live quote for the exact contract and falls back to the
*rebuilt* fitted surface when a contract has rolled out of the stored tenor
ladder — a mark is always produced, and `mark_source` records which it was.

---

## The interface

Dark-first, keyboard-driven, no build step and no CDN — `charts.js` is a
dependency-free SVG library written for this app.

* **Keys** — `1`–`7` switch tabs, `/` or `⌘K`/`Ctrl+K` opens a symbol
  palette, `Esc` closes it.
* **Charts** all carry a crosshair and tooltip, a legend for two or more series,
  and a **Table** toggle — every value is reachable without reading colour.
* **Both themes are selected, not flipped.** The dark categorical steps are the
  same eight hues re-stepped for the dark surface, and each set was checked
  against its own surface for lightness band, chroma floor, colour-vision
  separation and contrast. `?theme=light` overrides the stored preference so a
  themed view can be linked.
* Up/down and profit/loss use the **reserved status pair** — never a series hue
  — and always ship with a sign and a number, so meaning never rests on colour.
* Refreshes hold the previous render at reduced opacity instead of flashing a
  skeleton, and a render exception surfaces where the content should have been
  rather than leaving a blank tab.

## Layout

```
config.py         watchlist, tunables, scoring weights -- all knobs live here
auth.py           password gate: PBKDF2, signed sessions, login backoff
set_password.py   CLI to set / generate / clear the password
db.py             SQLite schema, auto-migration, retention
net.py            throttled + retried + disk-cached HTTP (certifi CA bundle)
marketcal.py      US market calendar, DST, holidays, time-to-expiry
pipeline.py       fetch -> analyse -> persist -> build board -> mark book
journal.py        trade journal, marking, performance, scanner forward test
app.py            Flask server + JSON API
tests.py          220 model invariants (+ live and API suites)

sources/          cboe.py  prices.py  news.py  earnings.py
model/            bs.py     Black-76, greeks, IV inversion, barrier probabilities
                  parity.py forward/discount recovery, yield curve
                  vol.py    smile fit, skew, realised estimators, ranks, jumps
                  chain.py  per-contract enrichment, expiry metrics, gamma profile
                  tech.py   indicators, levels, composite trend score
                  sentiment.py finance lexicon, relevance weighting
                  strategies.py positions, payoff, density, EV, target ladder
                  scanner.py regime -> candidates -> scored ideas

static/           app.js  charts.js  styles.css   (no CDN, no build step)
templates/        index.html
data/             theta_desk.db, http_cache/, logs/
```

**Dependencies: `flask` and `requests` only.** Every calculation is stdlib
`math` — no numpy, pandas or scipy anywhere, because the interpreter Task
Scheduler runs (the python.org build) does not have them while the Microsoft
Store build on PATH does. Designing that out is why the scheduled job works.

---

## Retention

`contract_quote` is the only table that grows fast — about **100k rows per full
run**, ~375 bytes each. Nothing in the app reads it historically: the chain view
and the mark fallback both hit the latest snapshot, and IV rank, the term
structure history and the scanner's forward test all read the small rollups.
Left unbounded it would pass **15 GB inside a year**.

`db.prune()` therefore keeps three tiers:

| Tier | Window | Kept |
|---|---|---|
| Detail | last 3 days | every snapshot, every contract |
| Archive | to 60 days | one snapshot per day, open interest ≥ 250 and within ±12% of spot, second-order greeks dropped |
| — | beyond | no quotes |

The archive can be this aggressive because **`expiry_metrics` stores the fitted
smile's coefficients, scale and strike range** — the entire volatility surface
for any past day is reconstructible from a few thousand rows. The quote archive
exists to preserve what actually *traded*, not the surface. Rollups are never
pruned.

Verified on a real 492k-row database: the archive pass takes it to 10,920 rows
and 184 MB → 9.9 MB with `PRAGMA integrity_check` clean and every rollup intact.
Steady state lands around **350–450 MB**. SQLite frees pages but never shrinks
the file, so a large prune triggers a `VACUUM` automatically.

A full run stores ~100k contract quotes and takes about **2.5 minutes cold**, or
**~25 seconds warm** off the HTTP cache.

---

## Things to know

* **IV rank reads `--` at first.** It is built from this desk's own snapshots
  and needs ~40 sessions. **HV rank is meaningful from day one** (two years of
  price history) and tells the same "vol is unusually quiet/loud" story
  meanwhile.
* **Relative options volume also needs history** — it compares against this
  desk's own trailing average for that name.
* Quotes are ~15 minutes delayed. This is a swing-timeframe research tool, not
  an execution system.
* Sentiment is lexicon-based and relevance-weighted. Yahoo's per-ticker feed
  mixes in general market wraps; scoring those as company news is the single
  largest source of fake sentiment, so off-topic headlines are heavily
  discounted.
* Nothing here is investment advice. Every number is a model output with the
  assumptions listed above.
