"""Strategy construction, valuation, expectancy, and the take-profit ladder.

How expectancy is computed (and why it is not just P(ITM))
----------------------------------------------------------
1. The fitted smile is inverted into the market's own terminal distribution via
   Breeden-Litzenberger:  f_Q(K) = e^{rT} d2C/dK2.  That density already carries
   the market's skew and fat tails -- far better than assuming lognormal.

2. Under Q every fairly-priced structure has EV = 0.  So Q-expectancy is only
   used to measure *execution cost* (how much the bid-ask and our fill
   assumption take out of a theoretically fair trade).

3. The real edge comes from a P-measure density: the Q density is re-centred on
   our own drift and re-scaled from implied vol to our *forecast* vol, while
   keeping the market's skew/kurtosis shape.  If implied is above forecast,
   short-premium structures come out with positive EV -- which is the documented
   equity variance risk premium, not a modelling artefact.  CVaR is reported
   alongside so the tail that pays for that premium stays visible.

4. Every take-profit level is converted into *both* an underlying price and an
   option price, and a probability of getting there (first-passage, not
   terminal), so a target is actionable rather than decorative.
"""

import datetime as dt
import math

import config
import marketcal
from . import bs

STICKY_DELTA_WEIGHT = 0.5    # observed equity behaviour sits between the two regimes


# ================================================================== the legs
class Leg:
    """One option leg, carrying the market parameters it was priced against."""

    __slots__ = ("right", "strike", "expiry", "qty", "price", "iv", "slope",
                 "r", "q", "occ", "bid", "ask", "delta", "gamma", "theta", "vega",
                 "oi", "volume", "liquidity")

    def __init__(self, right, strike, expiry, qty, price, iv, slope=0.0,
                 r=0.04, q=0.0, occ=None, bid=None, ask=None, greeks=None,
                 oi=None, volume=None, liquidity=None):
        self.right = right.upper()[:1]
        self.strike = float(strike)
        self.expiry = expiry
        self.qty = int(qty)
        self.price = float(price)
        self.iv = float(iv)
        self.slope = float(slope or 0.0)
        self.r, self.q = float(r), float(q)
        self.occ, self.bid, self.ask = occ, bid, ask
        self.oi, self.volume, self.liquidity = oi, volume, liquidity
        g = greeks or {}
        self.delta = g.get("delta")
        self.gamma = g.get("gamma")
        self.theta = g.get("theta")
        self.vega = g.get("vega")

    def to_dict(self):
        return {
            "right": self.right, "strike": self.strike,
            "expiry": self.expiry.isoformat() if hasattr(self.expiry, "isoformat") else self.expiry,
            "qty": self.qty, "price": round(self.price, 4), "iv": round(self.iv, 4),
            "occ": self.occ, "bid": self.bid, "ask": self.ask,
            "delta": 1.0 if self.is_stock else self.delta,
            "gamma": self.gamma, "theta": self.theta,
            "vega": self.vega, "oi": self.oi, "volume": self.volume,
            "liquidity": self.liquidity,
            "is_stock": self.is_stock,
            "action": ("BUY" if self.qty > 0 else "SELL"),
        }

    @property
    def is_stock(self):
        return self.right == "S"

    def value(self, S, S_entry, moment, vol_shift=0.0):
        """Per-share value of one contract of this leg at (S, moment)."""
        if self.is_stock:
            return S
        T = marketcal.year_fraction(self.expiry, moment, floor_hours=0.0)
        if T <= 1e-9:
            return bs.intrinsic(S, self.strike, self.right)
        F = S * math.exp((self.r - self.q) * T)
        df = math.exp(-self.r * T)
        iv = self.iv + vol_shift
        if S_entry and S > 0 and S_entry > 0 and self.slope:
            # Blend sticky-strike and sticky-delta: a pure sticky-strike model
            # under-states the P&L of vertical spreads when the underlying moves,
            # because the skew rolls along with spot in reality.
            iv += STICKY_DELTA_WEIGHT * (-self.slope) * math.log(S / S_entry)
        iv = min(max(iv, config.MIN_IV), config.MAX_IV)
        return bs.black76(F, self.strike, T, iv, df, self.right)

    def greeks_at(self, S, moment, vol_shift=0.0):
        if self.is_stock:
            return {"delta": 1.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
        T = marketcal.year_fraction(self.expiry, moment, floor_hours=0.0)
        if T <= 1e-9:
            return {"delta": 1.0 if (self.right == "C" and S > self.strike) else
                    (-1.0 if (self.right == "P" and S < self.strike) else 0.0),
                    "gamma": 0.0, "theta": 0.0, "vega": 0.0}
        F = S * math.exp((self.r - self.q) * T)
        df = math.exp(-self.r * T)
        return bs.greeks(S, F, self.strike, T, self.iv + vol_shift, df, self.right)


class Position:
    """A multi-leg options structure, quoted per spread and per share."""

    def __init__(self, symbol, strategy, legs, spot_entry, entry_date=None,
                 label=None, direction=None):
        self.symbol = symbol
        self.strategy = strategy
        self.legs = legs
        self.spot_entry = float(spot_entry)
        self.entry_date = entry_date or marketcal.session_date()
        self.label = label or strategy
        self.direction = direction or "neutral"

    # -------------------------------------------------------------- basics
    @property
    def net_price(self):
        """Positive = net debit paid, negative = net credit received."""
        return sum(l.qty * l.price for l in self.legs)

    @property
    def expiries(self):
        return sorted({l.expiry for l in self.legs})

    @property
    def near_expiry(self):
        return self.expiries[0]

    @property
    def far_expiry(self):
        return self.expiries[-1]

    @property
    def is_credit(self):
        return self.net_price < 0

    def dte(self, now=None):
        return marketcal.dte(self.near_expiry, now)

    def payoff(self, S):
        """P&L per share at the near expiry, treating longer legs at intrinsic."""
        val = sum(l.qty * bs.intrinsic(S, l.strike, l.right) for l in self.legs)
        return val - self.net_price

    def value(self, S, moment, vol_shift=0.0):
        return sum(l.qty * l.value(S, self.spot_entry, moment, vol_shift) for l in self.legs)

    def pnl(self, S, moment, vol_shift=0.0):
        return self.value(S, moment, vol_shift) - self.net_price

    def greeks(self, S=None, moment=None, vol_shift=0.0):
        S = S or self.spot_entry
        moment = moment or marketcal.now_utc()
        out = {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
        for l in self.legs:
            g = l.greeks_at(S, moment, vol_shift)
            for k in out:
                out[k] += l.qty * (g.get(k) or 0.0)
        return out

    # ---------------------------------------------------- payoff structure
    def breakpoints(self):
        return sorted({l.strike for l in self.legs if not l.is_stock and l.strike > 0})

    def extremes(self):
        """Exact max profit / max loss from the piecewise-linear payoff.

        Evaluating at 0, every strike, and a far point is exact: the payoff has
        no curvature between strikes, so an extreme must sit on a breakpoint or
        run off to a limit.

        Only the *upside* can be unbounded.  Spot is floored at zero, so the
        downside extreme is always a finite number that `payoff(0)` already
        gives us -- a short put's worst case is large but perfectly defined.
        Returns (max_profit, max_loss, unbounded_profit, unbounded_loss) where
        an unbounded side is reported as None.

        Stock legs count toward the far-side slope.  Without that, a covered
        call reads as a naked short call and gets reported as *unbounded upside
        risk* -- the exact opposite of the truth, since the long stock caps it.
        """
        ks = self.breakpoints()
        if not ks:
            return None, None, False, False
        hi = max(ks) * 3.0 + 10.0
        pts = [0.0] + ks + [hi]
        vals = [self.payoff(s) for s in pts]
        slope_hi = sum(l.qty * (1.0 if l.right in ("C", "S") else 0.0) for l in self.legs)
        unbounded_profit = slope_hi > 1e-9
        unbounded_loss = slope_hi < -1e-9
        max_p = None if unbounded_profit else max(vals)
        max_l = None if unbounded_loss else min(vals)
        return max_p, max_l, unbounded_profit, unbounded_loss

    def breakevens(self):
        ks = self.breakpoints()
        if not ks:
            return []
        lo, hi = 0.0, max(ks) * 3.0 + 10.0
        pts = [lo] + ks + [hi]
        outs = []
        for i in range(len(pts) - 1):
            a, b = pts[i], pts[i + 1]
            fa, fb = self.payoff(a), self.payoff(b)
            if fa == 0:
                outs.append(a)
            if fa * fb < 0:
                # linear segment -> exact root
                outs.append(a + (b - a) * (-fa) / (fb - fa))
        return sorted({round(x, 4) for x in outs if x > 0})

    def payoff_curve(self, lo=None, hi=None, n=121):
        s0 = self.spot_entry
        lo = lo if lo is not None else s0 * 0.6
        hi = hi if hi is not None else s0 * 1.4
        step = (hi - lo) / (n - 1)
        return [{"spot": lo + i * step, "pnl": self.payoff(lo + i * step)} for i in range(n)]

    def value_curve(self, moment, lo=None, hi=None, n=81, vol_shift=0.0):
        s0 = self.spot_entry
        lo = lo if lo is not None else s0 * 0.7
        hi = hi if hi is not None else s0 * 1.3
        step = (hi - lo) / (n - 1)
        return [{"spot": lo + i * step, "pnl": self.pnl(lo + i * step, moment, vol_shift)}
                for i in range(n)]

    def to_dict(self):
        max_p, max_l, ub_up, ub_loss = self.extremes()
        return {
            "symbol": self.symbol, "strategy": self.strategy, "label": self.label,
            "direction": self.direction,
            "legs": [l.to_dict() for l in self.legs],
            "net_price": round(self.net_price, 4),
            "is_credit": self.is_credit,
            "expiry": self.near_expiry.isoformat(),
            "expiries": [e.isoformat() for e in self.expiries],
            "max_profit": max_p, "max_loss": max_l,
            "unbounded_profit": ub_up, "unbounded_loss": ub_loss,
            "breakevens": self.breakevens(),
            "spot_entry": self.spot_entry,
        }


# ============================================================== fill pricing
def fill_price(contract, side):
    """What you realistically pay/receive.  side=+1 buy, -1 sell.

    Mid-price fills are a fiction that makes every backtest look profitable.
    We assume you give up a configurable fraction of the spread on each leg,
    which for a 4-leg condor is the difference between a good idea and a bad one.
    """
    bid, ask, mid = contract.get("bid"), contract.get("ask"), contract.get("mid")
    if mid is None or mid <= 0:
        return None
    if bid is None or ask is None or ask <= bid:
        return mid
    give = config.SLIPPAGE_FRAC_OF_SPREAD * (ask - bid)
    px = mid + side * give
    return max(px, 0.01)


def _mk_leg(contract, qty, expiry, slope, r, q):
    px = fill_price(contract, 1 if qty > 0 else -1)
    if px is None:
        return None
    return Leg(contract["right"], contract["strike"], expiry, qty, px,
               contract.get("iv") or 0.2, slope=slope, r=r, q=q,
               occ=contract.get("occ"), bid=contract.get("bid"), ask=contract.get("ask"),
               greeks={k: contract.get(k) for k in ("delta", "gamma", "theta", "vega")},
               oi=contract.get("open_interest"), volume=contract.get("volume"),
               liquidity=contract.get("liquidity"))


# ========================================================= density machinery
def risk_neutral_density(smile, F, T, df, n=201, span=6.0):
    """Breeden-Litzenberger density on a log-spaced strike grid.

    Returns [(S, prob_mass)] normalised to 1.  Log spacing keeps resolution in
    the body where it matters while still reaching the tails.
    """
    if smile is None or T <= 0 or F <= 0:
        return []
    sig = max(smile.atm_iv, 1e-3)
    w = sig * math.sqrt(T)
    lo_k, hi_k = -span * w - 0.5 * w * w, span * w
    ks = [lo_k + (hi_k - lo_k) * i / (n - 1) for i in range(n)]
    Ks = [F * math.exp(k) for k in ks]
    C = [bs.black76(F, K, T, smile.iv(K), df, "C") for K in Ks]

    dens = []
    for i in range(1, n - 1):
        h1 = Ks[i] - Ks[i - 1]
        h2 = Ks[i + 1] - Ks[i]
        # Non-uniform second derivative
        d2 = 2.0 * (h1 * C[i + 1] - (h1 + h2) * C[i] + h2 * C[i - 1]) / (h1 * h2 * (h1 + h2))
        p = max(d2, 0.0) / df
        width = 0.5 * (h1 + h2)
        dens.append((Ks[i], p * width))
    tot = sum(p for _s, p in dens)
    if tot <= 0:
        return []
    return [(s, p / tot) for s, p in dens]


def transform_density(dens, S0, T, sigma_p, drift_p):
    """Re-centre and re-scale a Q-density into a P-density.

    An affine map in log-space keeps the market's skew and kurtosis (the parts
    we have no better estimate of) while substituting our own mean and variance
    (the parts we do).
    """
    if not dens or T <= 0 or S0 <= 0:
        return dens
    xs = [(math.log(s / S0), p) for s, p in dens]
    mu_q = sum(x * p for x, p in xs)
    var_q = sum((x - mu_q) ** 2 * p for x, p in xs)
    sd_q = math.sqrt(max(var_q, 1e-12))
    sd_p = max(sigma_p * math.sqrt(T), 1e-9)
    mu_p = (drift_p - 0.5 * sigma_p * sigma_p) * T
    scale = sd_p / sd_q
    out = []
    for (x, p) in xs:
        x_new = mu_p + (x - mu_q) * scale
        out.append((S0 * math.exp(x_new), p))
    tot = sum(p for _s, p in out)
    return [(s, p / tot) for s, p in out] if tot > 0 else out


def evaluate(position, dens, moment=None, fees_per_spread=0.0):
    """Expectancy of a position under a discrete density of terminal spot."""
    if not dens:
        return {}
    moment = moment or marketcal.expiry_moment(position.near_expiry)
    entry = position.net_price
    vals, probs = [], []
    for S, p in dens:
        v = sum(l.qty * (bs.intrinsic(S, l.strike, l.right)
                         if l.expiry <= position.near_expiry
                         else l.value(S, position.spot_entry, moment))
                for l in position.legs)
        vals.append(v - entry - fees_per_spread)
        probs.append(p)

    ev = sum(v * p for v, p in zip(vals, probs))
    pop = sum(p for v, p in zip(vals, probs) if v > 0)
    var = sum((v - ev) ** 2 * p for v, p in zip(vals, probs))
    sd = math.sqrt(max(var, 0.0))

    order = sorted(zip(vals, probs))
    cum, cvar, tail = 0.0, 0.0, 0.05
    for v, p in order:
        take = min(p, tail - cum)
        if take <= 0:
            break
        cvar += v * take
        cum += take
    cvar = cvar / cum if cum > 0 else (order[0][0] if order else 0.0)

    def quantile(qp):
        c = 0.0
        for v, p in order:
            c += p
            if c >= qp:
                return v
        return order[-1][0] if order else 0.0

    return {
        "ev": ev, "pop": pop, "sd": sd, "cvar5": cvar,
        "p05": quantile(0.05), "p25": quantile(0.25), "p50": quantile(0.50),
        "p75": quantile(0.75), "p95": quantile(0.95),
        "sharpe": (ev / sd) if sd > 1e-9 else None,
    }


# ============================================================ target ladder
def _solve_spot_for_value(position, target_pnl, moment, lo, hi, tol=1e-4, iters=60):
    """Bisection for the spot at which P&L hits a target on a monotone branch."""
    f_lo = position.pnl(lo, moment) - target_pnl
    f_hi = position.pnl(hi, moment) - target_pnl
    if f_lo * f_hi > 0:
        return None
    for _ in range(iters):
        mid = 0.5 * (lo + hi)
        f_mid = position.pnl(mid, moment) - target_pnl
        if abs(f_mid) < tol or (hi - lo) < 1e-4:
            return mid
        if f_lo * f_mid < 0:
            hi, f_hi = mid, f_mid
        else:
            lo, f_lo = mid, f_mid
    return 0.5 * (lo + hi)


def _solve_date_for_value(position, target_pnl, S, now, max_days):
    """Earliest date the target is reached with spot pinned -- the theta path."""
    start = marketcal.to_et(now).replace(tzinfo=None).date()
    for d in range(1, int(max_days) + 1):
        day = start + dt.timedelta(days=d)
        if not marketcal.is_trading_day(day):
            continue
        moment = marketcal.et_to_utc(dt.datetime.combine(day, dt.time(16, 0)))
        if moment >= marketcal.expiry_moment(position.near_expiry):
            break
        if position.pnl(S, moment) >= target_pnl:
            return day
    return None


def build_targets(position, spot, sigma_p, drift_p, now=None, horizon_frac=0.6):
    """The take-profit / stop ladder, priced and probability-weighted.

    Each rung answers three questions a trader actually asks:
      - what does the spread have to be worth for me to take this off?
      - where does the underlying have to go, or how long do I have to wait?
      - how likely is that, before expiry?
    """
    now = now or marketcal.now_utc()
    max_p, max_l, _ub_up, _ub_loss = position.extremes()
    entry = position.net_price
    dte_total = max(position.dte(now), 0.01)
    horizon_days = max(dte_total * horizon_frac, 1.0)
    eval_date = marketcal.to_et(now).replace(tzinfo=None).date() + dt.timedelta(days=int(horizon_days))
    if eval_date >= position.near_expiry:
        eval_date = position.near_expiry - dt.timedelta(days=1)
    if eval_date <= marketcal.to_et(now).replace(tzinfo=None).date():
        eval_date = marketcal.to_et(now).replace(tzinfo=None).date()
    eval_moment = min(marketcal.et_to_utc(dt.datetime.combine(eval_date, dt.time(16, 0))),
                      marketcal.expiry_moment(position.near_expiry) - dt.timedelta(hours=1))

    credit = position.is_credit
    rungs = []
    if credit:
        prem = abs(entry)
        plan = [("T1", config.CREDIT_TP1_FRAC * prem, "take %d%% of credit"
                 % (config.CREDIT_TP1_FRAC * 100)),
                ("T2", config.CREDIT_TP2_FRAC * prem, "take %d%% of credit"
                 % (config.CREDIT_TP2_FRAC * 100)),
                ("T3", max_p if max_p is not None else prem, "let it expire worthless")]
        stop_pnl = -config.CREDIT_STOP_MULT * prem
        if max_l is not None:
            stop_pnl = max(stop_pnl, max_l)
        stop_label = "loss = %.1fx credit" % config.CREDIT_STOP_MULT
    else:
        prem = abs(entry)
        plan = [("T1", config.DEBIT_TP1_FRAC * prem, "+%d%% on premium"
                 % (config.DEBIT_TP1_FRAC * 100)),
                ("T2", config.DEBIT_TP2_FRAC * prem, "+%d%% on premium"
                 % (config.DEBIT_TP2_FRAC * 100)),
                ("T3", config.DEBIT_TP3_FRAC * prem, "+%d%% on premium"
                 % (config.DEBIT_TP3_FRAC * 100))]
        if max_p is not None:
            plan = [(n, min(v, max_p), d) for n, v, d in plan]
        stop_pnl = -config.DEBIT_STOP_FRAC * prem
        stop_label = "-%d%% of premium" % (config.DEBIT_STOP_FRAC * 100)

    lo_b, hi_b = spot * 0.30, spot * 3.0
    T_h = max(marketcal.vol_year_fraction(eval_date, now), 1e-4)
    reach = sigma_p * math.sqrt(T_h)            # 1 sigma over the eval horizon
    max_reach = 3.5 * reach                      # beyond this, "price target" is fiction

    def resolve(rung, pnl_target):
        """Fill in the price and/or time route to a P&L level."""
        up = _solve_spot_for_value(position, pnl_target, eval_moment, spot, hi_b)
        dn = _solve_spot_for_value(position, pnl_target, eval_moment, lo_b, spot)
        # Discard "solutions" that are really the bisection running into its
        # own bracket -- a credit spread's max profit is only reached at
        # expiry, not at a spot 80% away.
        if up is not None and (up >= hi_b * 0.98 or math.log(up / spot) > max_reach):
            up = None
        if dn is not None and (dn <= lo_b * 1.02 or math.log(spot / dn) > max_reach):
            dn = None

        if up is not None and dn is not None:
            # Two-sided (straddle/strangle): either barrier works.
            rung["spot_up"], rung["spot_down"] = up, dn
            rung["spot"] = up if abs(up - spot) < abs(dn - spot) else dn
            rung["spot_move_pct"] = (rung["spot"] / spot - 1.0) * 100.0
            rung["prob"] = bs.prob_touch_either(spot, dn, up, sigma_p, T_h, drift_p)
            rung["path"] = "price-either"
            return
        one = up if up is not None else dn
        if one is not None:
            rung["spot"] = one
            rung["spot_move_pct"] = (one / spot - 1.0) * 100.0
            rung["prob"] = bs.prob_touch(spot, one, sigma_p, T_h, drift_p)
            rung["path"] = "price"
            return

        # No reachable price level -> this rung is reached by decay, if at all.
        day = _solve_date_for_value(position, pnl_target, spot, now, dte_total)
        bes = position.breakevens()
        if day:
            rung["date"] = day.isoformat()
            rung["days"] = (day - marketcal.to_et(now).replace(tzinfo=None).date()).days
            rung["path"] = "time"
            t_day = max(marketcal.vol_year_fraction(day, now), 1e-4)
        else:
            rung["path"] = "expiry"
            rung["date"] = position.near_expiry.isoformat()
            rung["days"] = max(int(round(dte_total)), 0)
            t_day = max(marketcal.vol_year_fraction(position.near_expiry, now), 1e-4)
        # Decay only pays if spot stays inside the profit zone.
        below = [b for b in bes if b < spot]
        above = [b for b in bes if b > spot]
        if below and above:
            rung["hold_range"] = [max(below), min(above)]
            rung["prob"] = bs.prob_no_touch_double(spot, max(below), min(above),
                                                   sigma_p, t_day, drift_p)
        elif bes:
            nearest = min(bes, key=lambda b: abs(b - spot))
            rung["hold_range"] = [nearest, None] if nearest < spot else [None, nearest]
            rung["prob"] = 1.0 - bs.prob_touch(spot, nearest, sigma_p, t_day, drift_p)

    for name, pnl_target, desc in plan:
        if pnl_target is None:
            continue
        rung = {"name": name, "pnl": pnl_target, "desc": desc,
                "spread_price": entry + pnl_target,
                "pct_of_max": (pnl_target / max_p * 100.0) if max_p and max_p > 0 else None}
        # "Expire worthless" is definitionally a time outcome, never a price one.
        if credit and name == "T3":
            rung["path"] = "expiry"
            rung["date"] = position.near_expiry.isoformat()
            rung["days"] = max(int(round(dte_total)), 0)
            bes = position.breakevens()
            t_e = max(marketcal.vol_year_fraction(position.near_expiry, now), 1e-4)
            below = [b for b in bes if b < spot]
            above = [b for b in bes if b > spot]
            if below and above:
                rung["hold_range"] = [max(below), min(above)]
                rung["prob"] = bs.prob_between(spot, max(below), min(above),
                                               sigma_p, t_e, drift_p)
            elif bes:
                nearest = min(bes, key=lambda b: abs(b - spot))
                rung["hold_range"] = [nearest, None] if nearest < spot else [None, nearest]
                rung["prob"] = (bs.prob_between(spot, nearest, 1e9, sigma_p, t_e, drift_p)
                                if nearest < spot else
                                bs.prob_between(spot, 0.0, nearest, sigma_p, t_e, drift_p))
        else:
            resolve(rung, pnl_target)
        rungs.append(rung)

    stop = {"name": "STOP", "pnl": stop_pnl, "desc": stop_label,
            "spread_price": entry + stop_pnl}
    resolve(stop, stop_pnl)

    time_stop = None
    if dte_total > config.TIME_STOP_DTE:
        d = position.near_expiry - dt.timedelta(days=config.TIME_STOP_DTE)
        time_stop = {"name": "TIME", "date": d.isoformat(),
                     "desc": "close at %d DTE -- gamma risk outruns theta" % config.TIME_STOP_DTE}

    return {"targets": rungs, "stop": stop, "time_stop": time_stop,
            "eval_date": eval_date.isoformat()}


def suggested_qty(risk_per_share, account=None, risk_pct=None):
    """Contracts to trade so one loser costs the configured share of the book.

    `risk_per_share` is the defined max loss where one exists, and a tail-risk
    (VaR) estimate where it does not -- sizing an undefined-risk structure off
    "1 contract" is how accounts get destroyed by a single gap.
    """
    account = account or config.ACCOUNT_SIZE
    risk_pct = risk_pct or config.RISK_PER_TRADE_PCT
    if not risk_per_share:
        return 1
    risk_per = abs(risk_per_share) * 100.0
    if risk_per <= 0:
        return 1
    return max(1, int((account * risk_pct) // risk_per))


# ============================================================ constructors
def _pick(contracts, right, target_strike):
    best, bestd = None, None
    for c in contracts:
        if c["right"] != right or not c.get("mid"):
            continue
        d = abs(c["strike"] - target_strike)
        if bestd is None or d < bestd:
            best, bestd = c, d
    return best


def _pick_by_delta(contracts, right, target_delta):
    best, bestd = None, None
    for c in contracts:
        if c["right"] != right or not c.get("mid") or c.get("delta") is None:
            continue
        d = abs(abs(c["delta"]) - abs(target_delta))
        if bestd is None or d < bestd:
            best, bestd = c, d
    return best


def build(strategy, symbol, exp_metrics, contracts, spot, spec=None):
    """Assemble a Position for one of the supported structures.

    `spec` carries strategy-specific targets (deltas, widths).  Returns None if
    the chain cannot support the structure with tradeable quotes.
    """
    spec = spec or {}
    expiry = marketcal.parse_date(exp_metrics["expiry"])
    smile = exp_metrics.get("smile_obj")
    r = exp_metrics.get("rate") or config.DEFAULT_RISK_FREE
    q = exp_metrics.get("div_yield") or 0.0
    F = exp_metrics["forward"]

    def slope_at(K):
        if not smile or K <= 0 or F <= 0:
            return 0.0
        return smile.slope(math.log(K / F))

    def leg(c, qty):
        if not c:
            return None
        return _mk_leg(c, qty, expiry, slope_at(c["strike"]), r, q)

    def ok(legs):
        return None if any(l is None for l in legs) else legs

    S = spot
    if strategy == "long_call":
        c = _pick_by_delta(contracts, "C", spec.get("delta", 0.40))
        legs = ok([leg(c, 1)])
        direction = "bullish"
    elif strategy == "long_put":
        c = _pick_by_delta(contracts, "P", spec.get("delta", 0.40))
        legs = ok([leg(c, 1)])
        direction = "bearish"
    elif strategy == "bull_call_spread":
        lo = _pick_by_delta(contracts, "C", spec.get("long_delta", 0.50))
        hi = _pick_by_delta(contracts, "C", spec.get("short_delta", 0.25))
        legs = ok([leg(lo, 1), leg(hi, -1)]) if lo and hi and hi["strike"] > lo["strike"] else None
        direction = "bullish"
    elif strategy == "bear_put_spread":
        hi = _pick_by_delta(contracts, "P", spec.get("long_delta", 0.50))
        lo = _pick_by_delta(contracts, "P", spec.get("short_delta", 0.25))
        legs = ok([leg(hi, 1), leg(lo, -1)]) if lo and hi and hi["strike"] > lo["strike"] else None
        direction = "bearish"
    elif strategy == "bull_put_spread":
        sh = _pick_by_delta(contracts, "P", spec.get("short_delta", 0.25))
        if not sh:
            return None
        width = spec.get("width") or _default_width(contracts, S)
        lg = _pick(contracts, "P", sh["strike"] - width)
        legs = ok([leg(sh, -1), leg(lg, 1)]) if lg and lg["strike"] < sh["strike"] else None
        direction = "bullish"
    elif strategy == "bear_call_spread":
        sh = _pick_by_delta(contracts, "C", spec.get("short_delta", 0.25))
        if not sh:
            return None
        width = spec.get("width") or _default_width(contracts, S)
        lg = _pick(contracts, "C", sh["strike"] + width)
        legs = ok([leg(sh, -1), leg(lg, 1)]) if lg and lg["strike"] > sh["strike"] else None
        direction = "bearish"
    elif strategy == "iron_condor":
        width = spec.get("width") or _default_width(contracts, S)
        sp = _pick_by_delta(contracts, "P", spec.get("short_delta", 0.18))
        sc = _pick_by_delta(contracts, "C", spec.get("short_delta", 0.18))
        if not sp or not sc:
            return None
        lp = _pick(contracts, "P", sp["strike"] - width)
        lc = _pick(contracts, "C", sc["strike"] + width)
        legs = ok([leg(sp, -1), leg(lp, 1), leg(sc, -1), leg(lc, 1)])
        if legs and not (lp["strike"] < sp["strike"] < sc["strike"] < lc["strike"]):
            legs = None
        direction = "neutral"
    elif strategy == "iron_butterfly":
        width = spec.get("width") or _default_width(contracts, S) * 2
        atm = _pick(contracts, "C", F)
        atp = _pick(contracts, "P", F)
        # Both short legs must sit on the SAME strike or it is not a butterfly;
        # and the wings must actually be outside them.
        if not atm or not atp or abs(atm["strike"] - atp["strike"]) > 1e-9:
            return None
        lp = _pick(contracts, "P", atp["strike"] - width)
        lc = _pick(contracts, "C", atm["strike"] + width)
        if not lp or not lc or lp["strike"] >= atp["strike"] or lc["strike"] <= atm["strike"]:
            return None
        legs = ok([leg(atp, -1), leg(atm, -1), leg(lp, 1), leg(lc, 1)])
        direction = "neutral"
    elif strategy == "long_straddle":
        c = _pick(contracts, "C", F)
        p = _pick(contracts, "P", F)
        legs = ok([leg(c, 1), leg(p, 1)])
        direction = "volatility"
    elif strategy == "long_strangle":
        c = _pick_by_delta(contracts, "C", spec.get("delta", 0.25))
        p = _pick_by_delta(contracts, "P", spec.get("delta", 0.25))
        legs = ok([leg(c, 1), leg(p, 1)]) if c and p and c["strike"] > p["strike"] else None
        direction = "volatility"
    elif strategy == "short_strangle":
        c = _pick_by_delta(contracts, "C", spec.get("delta", 0.16))
        p = _pick_by_delta(contracts, "P", spec.get("delta", 0.16))
        legs = ok([leg(c, -1), leg(p, -1)]) if c and p and c["strike"] > p["strike"] else None
        direction = "neutral"
    elif strategy == "cash_secured_put":
        p = _pick_by_delta(contracts, "P", spec.get("delta", 0.30))
        legs = ok([leg(p, -1)])
        direction = "bullish"
    elif strategy == "covered_call":
        c = _pick_by_delta(contracts, "C", spec.get("delta", 0.30))
        short = leg(c, -1)
        if short is None:
            return None
        # The long stock is what makes this "covered".  Modelling it as a bare
        # short call reports unbounded upside risk on a structure whose upside
        # is capped at the strike and whose real risk is the stock falling.
        stock = Leg("S", 0.0, expiry, 1, S, 0.0, r=r, q=q)
        legs = [stock, short]
        direction = "bullish"
    else:
        return None

    if not legs:
        return None
    return Position(symbol, strategy, legs, S, direction=direction,
                    label=STRATEGY_LABELS.get(strategy, strategy))


def _default_width(contracts, spot, risk_budget=None):
    """Spread width snapped to the listed strike grid and sized to the account.

    Width driven purely off spot breaks on expensive underlyings: 5% of SPY is
    a $37-wide spread risking $3,327 on one contract, which is 13% of a $25k
    account when the risk policy says 2%.  Anchoring the width to the risk
    budget keeps the minimum tradeable size (1 contract) inside the policy.
    """
    ks = sorted({c["strike"] for c in contracts})
    if len(ks) < 3:
        return max(spot * 0.05, 1.0)
    gaps = sorted(ks[i + 1] - ks[i] for i in range(len(ks) - 1))
    step = gaps[len(gaps) // 2] or 1.0
    if risk_budget is None:
        risk_budget = config.ACCOUNT_SIZE * config.RISK_PER_TRADE_PCT
    # A credit spread's max loss is width minus credit, so a width of ~1.5x the
    # per-share budget lands the actual risk close to the budget.
    target = min(max(risk_budget / 100.0 * 1.5, 2 * step), spot * 0.08)
    return max(step * round(target / step), step)


STRATEGY_LABELS = {
    "long_call": "Long Call",
    "long_put": "Long Put",
    "bull_call_spread": "Bull Call Spread",
    "bear_put_spread": "Bear Put Spread",
    "bull_put_spread": "Bull Put Spread (credit)",
    "bear_call_spread": "Bear Call Spread (credit)",
    "iron_condor": "Iron Condor",
    "iron_butterfly": "Iron Butterfly",
    "long_straddle": "Long Straddle",
    "long_strangle": "Long Strangle",
    "short_strangle": "Short Strangle",
    "cash_secured_put": "Cash-Secured Put",
    "covered_call": "Covered Call",
}

DEFINED_RISK = {"bull_call_spread", "bear_put_spread", "bull_put_spread",
                "bear_call_spread", "iron_condor", "iron_butterfly",
                "long_call", "long_put", "long_straddle", "long_strangle"}
