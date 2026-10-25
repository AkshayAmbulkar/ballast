"""Coupon schedules for fixed-rate bonds.

Dates are generated backward from maturity in whole-month steps. Each date is
computed directly from maturity (not by stepping from the previous date), so a
31st never drifts to the 30th. If maturity falls on a month end, every coupon
falls on a month end (the end-of-month rule): a note maturing 31 Aug pays on
28/29 Feb and 31 Aug.
"""

from __future__ import annotations

import calendar
from datetime import date


def is_month_end(d: date) -> bool:
    return d.day == calendar.monthrange(d.year, d.month)[1]


def add_months(d: date, months: int, end_of_month: bool = False) -> date:
    years, month_index = divmod(d.month - 1 + months, 12)
    year, month = d.year + years, month_index + 1
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, last_day if end_of_month else min(d.day, last_day))


def coupon_dates(issue_date: date, maturity_date: date, frequency: int = 2) -> list[date]:
    """Coupon payment dates after issue_date, up to and including maturity, ascending."""
    if 12 % frequency:
        raise ValueError("frequency must divide 12 (1, 2, 3, 4, 6 or 12)")
    if maturity_date <= issue_date:
        raise ValueError("maturity must be after issue date")
    step = 12 // frequency
    eom = is_month_end(maturity_date)
    out: list[date] = []
    k = 0
    while True:
        d = add_months(maturity_date, -step * k, eom)
        if d <= issue_date:
            break
        out.append(d)
        k += 1
    return out[::-1]


def coupon_period(issue_date: date, maturity_date: date, frequency: int, on: date) -> tuple[date, date]:
    """The regular coupon period (start, end] that contains `on`.

    For a date in the first period, the start is the regular coupon date before
    the first coupon (a quasi-coupon date), which may precede the issue date.
    """
    dates = coupon_dates(issue_date, maturity_date, frequency)
    if on >= maturity_date:
        raise ValueError("bond has matured")
    step = 12 // frequency
    eom = is_month_end(maturity_date)
    for i, d in enumerate(dates):
        if on < d:
            start = dates[i - 1] if i > 0 else add_months(d, -step, eom)
            return start, d
    raise AssertionError("unreachable")
