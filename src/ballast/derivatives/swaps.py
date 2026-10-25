"""SOFR overnight index swaps (OIS).

A receive-fixed swap on notional N exchanges, each period k:
  fixed leg:     N x K x tau_k                    (tau_k = days / 360)
  floating leg:  N x (A_k - 1)                    (A_k = SOFR compounded in arrears over the period)

Valuation on day d with discount factors DF(.) measured from d:
* Fixed leg PV   = N K sum_k tau_k DF(e_k), over periods paying after d.
* Floating leg   : the current period has realized growth A from its start to d
  (known SOFR fixings) and projected growth 1/DF(e) from d to its end, so its
  PV is N (A - DF(e)). Each future period is worth N (DF(s_k) - DF(e_k)). The
  sum telescopes to N (A - DF(final payment date)).
* Receive-fixed PV = fixed leg - floating leg. A pay-fixed swap is the same
  instrument held in negative quantity.

Simplifications (documented in docs/methodology/03-derivatives.md): unadjusted
annual dates, no payment lag, and one curve for projection and discounting.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Mapping

import numpy as np

from ballast.derivatives.sofr import compound_factor
from ballast.fixed_income.curve import DiscountCurve
from ballast.fixed_income.schedule import add_months

DAYS_PER_YEAR = 365.25
NOTIONAL_UNIT = 100.0


@dataclass(frozen=True)
class OISSwap:
    effective_date: date
    maturity_date: date
    fixed_rate: float              # decimal
    months_per_period: int = 12

    def periods(self) -> list[tuple[date, date]]:
        dates = [self.effective_date]
        k = 1
        while True:
            d = add_months(self.effective_date, self.months_per_period * k)
            if d >= self.maturity_date:
                break
            dates.append(d)
            k += 1
        dates.append(self.maturity_date)
        return list(zip(dates[:-1], dates[1:]))


def _t(on: date, d: date) -> float:
    return (d - on).days / DAYS_PER_YEAR


def _remaining(swap: OISSwap, on: date) -> list[tuple[date, date]]:
    return [(s, e) for s, e in swap.periods() if e > on]


def annuity(swap: OISSwap, curve: DiscountCurve, on: date) -> float:
    """sum tau_k DF(e_k) over periods still to pay."""
    rem = _remaining(swap, on)
    if not rem:
        return 0.0
    tau = np.array([(e - s).days / 360.0 for s, e in rem])
    dfs = curve.df(np.array([_t(on, e) for _, e in rem]))
    return float(np.sum(tau * dfs))


def floating_leg_pv(swap: OISSwap, curve: DiscountCurve, on: date, realized_growth: float) -> float:
    """PV per 1 notional. `realized_growth` = SOFR growth from the current period's start to `on` (1 if not started)."""
    rem = _remaining(swap, on)
    if not rem:
        return 0.0
    first_start = rem[0][0]
    final_df = float(curve.df(_t(on, rem[-1][1])))
    if first_start >= on:   # forward-starting
        return float(curve.df(_t(on, first_start))) - final_df
    return realized_growth - final_df


def realized_growth(swap: OISSwap, fixings: Mapping[date, float], on: date) -> float:
    rem = _remaining(swap, on)
    if not rem or rem[0][0] >= on:
        return 1.0
    return compound_factor(fixings, rem[0][0], on)


def pv_per_100(swap: OISSwap, curve: DiscountCurve, on: date, growth: float) -> float:
    """Receive-fixed PV per 100 notional, excluding any payment made on `on` itself."""
    fixed = swap.fixed_rate * annuity(swap, curve, on)
    return NOTIONAL_UNIT * (fixed - floating_leg_pv(swap, curve, on, growth))


def par_rate(swap: OISSwap, curve: DiscountCurve, on: date, growth: float = 1.0) -> float:
    """Fixed rate that makes the swap worth zero on `on`."""
    return floating_leg_pv(swap, curve, on, growth) / annuity(swap, curve, on)


def payments(swap: OISSwap, fixings: Mapping[date, float], through: date) -> list[tuple[date, float]]:
    """Net receive-fixed payment per 100 notional for every period ending on or before `through`."""
    out = []
    for s, e in swap.periods():
        if e > through:
            break
        tau = (e - s).days / 360.0
        growth = compound_factor(fixings, s, e)
        out.append((e, NOTIONAL_UNIT * (swap.fixed_rate * tau - (growth - 1.0))))
    return out
