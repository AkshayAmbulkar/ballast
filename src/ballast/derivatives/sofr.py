"""SOFR compounding, following the New York Fed's published conventions.

* SOFR for business day b (its "value date") is published the next morning.
* On calendar days with no SOFR (weekends, holidays), the rate of the
  preceding business day applies as simple interest, so each business day's
  rate compounds once over the n calendar days it covers: (1 + SOFR x n/360).
* The 30-day SOFR Average published on day P covers the 30 calendar days
  P-30 ... P-1 and is (product - 1) x 360/30. If the window starts on a
  non-business day, the preceding business day's SOFR covers those days.

Source: Federal Reserve Bank of New York, statement on SOFR Averages and Index
(https://www.newyorkfed.org/markets/opolicy/operating_policy_200212).
"""

from __future__ import annotations

import bisect
from datetime import date, timedelta
from typing import Mapping


class FixingError(ValueError):
    pass


def compound_factor(fixings: Mapping[date, float], start: date, end: date) -> float:
    """Growth of 1 from `start` to `end` (calendar days start .. end-1) at daily SOFR.

    `fixings` maps value date -> SOFR as a decimal (0.0430 for 4.30%).
    """
    if end <= start:
        return 1.0
    days = sorted(fixings)
    i = bisect.bisect_right(days, start) - 1
    if i < 0:
        raise FixingError(f"no SOFR on or before {start}")
    factor = 1.0
    cursor = start
    while cursor < end:
        rate_date = days[i]
        next_fixing = days[i + 1] if i + 1 < len(days) else None
        run_end = min(end, next_fixing) if next_fixing else end
        if next_fixing is None and (end - rate_date).days > 5:
            raise FixingError(f"SOFR data ends at {rate_date}, too early to compound to {end}")
        n = (run_end - cursor).days
        factor *= 1.0 + fixings[rate_date] * n / 360.0
        cursor = run_end
        i += 1
    return factor


def sofr_average(fixings: Mapping[date, float], publication_date: date, calendar_days: int = 30) -> float:
    """The NY Fed SOFR Average (decimal) published on `publication_date`."""
    start = publication_date - timedelta(days=calendar_days)
    factor = compound_factor(fixings, start, publication_date)
    return (factor - 1.0) * 360.0 / calendar_days


def sofr_index(fixings: Mapping[date, float], base_date: date, publication_date: date) -> float:
    """SOFR Index on `publication_date`, starting from 1.0 on `base_date` (the first value date)."""
    return compound_factor(fixings, base_date, publication_date)
