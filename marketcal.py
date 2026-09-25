"""US equity-options market calendar and clock.

Windows Python has no IANA tz database (`zoneinfo` needs the `tzdata` package),
so the Eastern-time rules are implemented directly.  Holidays are computed from
rules rather than tabulated so the calendar never expires.

Time to expiry matters more here than it looks: a 1-day error on a 7-DTE option
moves its theta by ~15%, which is enough to flip a trade's ranking.
"""

import datetime as dt

# ---------------------------------------------------------------- Eastern tz
_UTC = dt.timezone.utc


def _nth_weekday(year, month, weekday, n):
    """n-th `weekday` (Mon=0) of month; n=-1 means last."""
    if n > 0:
        d = dt.date(year, month, 1)
        offset = (weekday - d.weekday()) % 7
        return d + dt.timedelta(days=offset + 7 * (n - 1))
    d = dt.date(year, month, 28)
    while (d + dt.timedelta(days=1)).month == month:
        d += dt.timedelta(days=1)
    return d - dt.timedelta(days=(d.weekday() - weekday) % 7)


def _dst_bounds(year):
    """US DST: 2am local, 2nd Sunday of March -> 1st Sunday of November."""
    start = _nth_weekday(year, 3, 6, 2)
    end = _nth_weekday(year, 11, 6, 1)
    return start, end


def et_offset(utc_moment):
    """Hours to add to UTC to get Eastern time (-4 in DST, -5 otherwise)."""
    y = utc_moment.year
    start, end = _dst_bounds(y)
    # DST flips at 07:00 UTC on those Sundays (2am ET)
    start_utc = dt.datetime.combine(start, dt.time(7, 0), tzinfo=_UTC)
    end_utc = dt.datetime.combine(end, dt.time(6, 0), tzinfo=_UTC)
    return -4 if start_utc <= utc_moment < end_utc else -5


def to_et(utc_moment):
    if utc_moment.tzinfo is None:
        utc_moment = utc_moment.replace(tzinfo=_UTC)
    return utc_moment + dt.timedelta(hours=et_offset(utc_moment))


def et_to_utc(et_naive):
    """Convert a naive Eastern datetime to aware UTC (iterates once for DST)."""
    guess = et_naive.replace(tzinfo=_UTC) + dt.timedelta(hours=5)
    off = et_offset(guess)
    return et_naive.replace(tzinfo=_UTC) - dt.timedelta(hours=off)


def now_utc():
    return dt.datetime.now(_UTC)


def now_et():
    return to_et(now_utc()).replace(tzinfo=None)


# ------------------------------------------------------------------ holidays
def easter(year):
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    lu = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * lu) // 451
    month, day = divmod(h + lu - 7 * m + 114, 31)
    return dt.date(year, month, day + 1)


def _observed(d):
    """Saturday holidays observe Friday, Sunday holidays observe Monday."""
    if d.weekday() == 5:
        return d - dt.timedelta(days=1)
    if d.weekday() == 6:
        return d + dt.timedelta(days=1)
    return d


_holiday_cache = {}


def holidays(year):
    if year in _holiday_cache:
        return _holiday_cache[year]
    hs = {
        _observed(dt.date(year, 1, 1)),                 # New Year's Day
        _nth_weekday(year, 1, 0, 3),                    # MLK
        _nth_weekday(year, 2, 0, 3),                    # Presidents' Day
        easter(year) - dt.timedelta(days=2),            # Good Friday
        _nth_weekday(year, 5, 0, -1),                   # Memorial Day
        _observed(dt.date(year, 7, 4)),                 # Independence Day
        _nth_weekday(year, 9, 0, 1),                    # Labor Day
        _nth_weekday(year, 11, 3, 4),                   # Thanksgiving
        _observed(dt.date(year, 12, 25)),               # Christmas
    }
    if year >= 2021:
        hs.add(_observed(dt.date(year, 6, 19)))         # Juneteenth
    _holiday_cache[year] = hs
    return hs


def is_trading_day(d):
    if d.weekday() >= 5:
        return False
    return d not in holidays(d.year)


def next_trading_day(d):
    d += dt.timedelta(days=1)
    while not is_trading_day(d):
        d += dt.timedelta(days=1)
    return d


def prev_trading_day(d):
    d -= dt.timedelta(days=1)
    while not is_trading_day(d):
        d -= dt.timedelta(days=1)
    return d


def trading_days_between(a, b):
    """Sessions strictly after `a` up to and including `b`."""
    if b <= a:
        return 0
    n, d = 0, a
    while d < b:
        d += dt.timedelta(days=1)
        if is_trading_day(d):
            n += 1
    return n


def sessions(end, count):
    """The `count` most recent sessions ending on/before `end`."""
    out, d = [], end
    while len(out) < count:
        if is_trading_day(d):
            out.append(d)
        d -= dt.timedelta(days=1)
    return list(reversed(out))


def is_monthly_expiry(d):
    """Third Friday (or Thursday if that Friday is a holiday)."""
    third_fri = _nth_weekday(d.year, d.month, 4, 3)
    if not is_trading_day(third_fri):
        third_fri -= dt.timedelta(days=1)
    return d == third_fri


# --------------------------------------------------------------- market hours
def market_state(moment_et=None):
    et = moment_et or now_et()
    d = et.date()
    if not is_trading_day(d):
        return "closed"
    mins = et.hour * 60 + et.minute
    # Early closes: day after Thanksgiving, Christmas Eve, July 3
    early = (d == _nth_weekday(d.year, 11, 3, 4) + dt.timedelta(days=1)
             or (d.month, d.day) in ((12, 24), (7, 3)))
    close = 13 * 60 if early else 16 * 60
    if mins < 4 * 60:
        return "closed"
    if mins < 9 * 60 + 30:
        return "premarket"
    if mins < close:
        return "open"
    if mins < 20 * 60:
        return "afterhours"
    return "closed"


def session_date(moment_et=None):
    """The trading session a moment belongs to (rolls forward after the close)."""
    et = moment_et or now_et()
    d = et.date()
    if is_trading_day(d) and et.hour < 20:
        return d
    return prev_trading_day(d + dt.timedelta(days=1)) if not is_trading_day(d) else d


# ------------------------------------------------------------- time to expiry
def expiry_moment(expiry_date, am_settled=False):
    """UTC moment the contract stops trading."""
    hour = 9.5 if am_settled else 16.0
    h = int(hour)
    m = int(round((hour - h) * 60))
    return et_to_utc(dt.datetime.combine(expiry_date, dt.time(h, m)))


def year_fraction(expiry_date, now=None, am_settled=False, floor_hours=1.0):
    """Calendar year fraction to expiry, ACT/365, floored so T is never 0."""
    now = now or now_utc()
    delta = (expiry_moment(expiry_date, am_settled) - now).total_seconds()
    delta = max(delta, floor_hours * 3600.0)
    return delta / (365.0 * 86400.0)


def vol_year_fraction(expiry_date, now=None):
    """Business-time year fraction (sessions/252).

    Volatility accrues on trading days, not calendar days.  Using this for the
    expected-move calculation stops long weekends from inflating the move, and
    it is what makes short-dated expected moves line up with the straddle.
    """
    now = now or now_utc()
    et = to_et(now).replace(tzinfo=None)
    today = et.date()
    n = trading_days_between(today, expiry_date)
    if is_trading_day(today):
        mins = et.hour * 60 + et.minute
        remaining = min(max((16 * 60 - mins) / (6.5 * 60.0), 0.0), 1.0)
        n += remaining
    return max(n, 0.05) / 252.0


def dte(expiry_date, now=None):
    now = now or now_utc()
    return (expiry_moment(expiry_date) - now).total_seconds() / 86400.0


def calendar_dte(expiry_date, now=None):
    """Whole calendar days from the session date to expiry.

    This is the DTE the trade plan is written in -- the 21-DTE time stop has
    always been enforced as `(expiry - session date).days <= 21` -- so every
    decision ABOUT the time stop uses it too.  The fractional `dte()` above is
    for pricing only.  Mixing the two is how an expiry 22 calendar days out
    was scored on a 0.7-day horizon and then time-stopped the next session.
    """
    now = now or now_utc()
    return (parse_date(expiry_date) - session_date(to_et(now).replace(tzinfo=None))).days


def parse_date(s):
    if isinstance(s, dt.date):
        return s
    s = str(s).strip()[:10]
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%b %d, %Y", "%Y%m%d"):
        try:
            return dt.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def iso(d):
    return d.isoformat() if hasattr(d, "isoformat") else str(d)


if __name__ == "__main__":
    et = now_et()
    print("ET now       :", et.strftime("%Y-%m-%d %H:%M:%S"), "|", market_state())
    print("session date :", session_date())
    for y in (2026, 2027):
        print(y, sorted(holidays(y)))
    exp = dt.date(2026, 9, 18)
    print("to %s: dte=%.2f  T_cal=%.5f  T_vol=%.5f"
          % (exp, dte(exp), year_fraction(exp), vol_year_fraction(exp)))
