"""Fixed-rate bond pricing, yield, duration, convexity and DV01.

Conventions (U.S. Treasury style):
* Prices are per 100 face. "Dirty" includes accrued interest; "clean" does not.
* Accrued interest is Actual/Actual (ICMA): coupon x days accrued / days in period.
* Yield to maturity uses the street convention: semiannual compounding with a
  fractional first period, so cash flow i is discounted by (1 + y/f)^(w + i),
  where w is the fraction of the current coupon period still to run.
* Curve pricing discounts each cash flow at DF(t) with t = days / 365.25.
* No settlement lag: prices are for value on the valuation date itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np
from scipy.optimize import brentq

from ballast.fixed_income.curve import DiscountCurve
from ballast.fixed_income.schedule import coupon_dates, coupon_period

DAYS_PER_YEAR = 365.25
BP = 1e-4


@dataclass(frozen=True)
class Bond:
    coupon_rate: float        # decimal, 0.0425 for 4.25%
    issue_date: date
    maturity_date: date
    frequency: int = 2
    face: float = 100.0

    @property
    def coupon_amount(self) -> float:
        return self.face * self.coupon_rate / self.frequency

    def coupon_dates(self) -> list[date]:
        return coupon_dates(self.issue_date, self.maturity_date, self.frequency)

    def cashflows_after(self, on: date) -> list[tuple[date, float]]:
        """Cash flows paid strictly after `on` (a coupon due on `on` belongs to the previous holder)."""
        flows = [(d, self.coupon_amount) for d in self.coupon_dates() if d > on]
        if flows:
            last_date, last_amount = flows[-1]
            flows[-1] = (last_date, last_amount + self.face)
        return flows

    def accrued_interest(self, on: date) -> float:
        if on < self.issue_date or on >= self.maturity_date:
            return 0.0
        start, end = coupon_period(self.issue_date, self.maturity_date, self.frequency, on)
        accrual_start = max(start, self.issue_date)
        return self.coupon_amount * (on - accrual_start).days / (end - start).days


@dataclass(frozen=True)
class BondPrice:
    dirty: float
    accrued: float

    @property
    def clean(self) -> float:
        return self.dirty - self.accrued


def price_from_curve(bond: Bond, curve: DiscountCurve, on: date) -> BondPrice:
    flows = bond.cashflows_after(on)
    if not flows:
        return BondPrice(0.0, 0.0)
    times = np.array([(d - on).days / DAYS_PER_YEAR for d, _ in flows])
    amounts = np.array([a for _, a in flows])
    return BondPrice(float(np.sum(amounts * curve.df(times))), bond.accrued_interest(on))


# ---------- yield-based measures ----------

@dataclass(frozen=True)
class YieldRisk:
    price: float              # dirty price at the given yield
    macaulay_duration: float  # years
    modified_duration: float  # years
    convexity: float          # years^2
    dv01: float               # price change for a 1bp fall in yield, per `face`


def cashflow_yield_risk(times: np.ndarray, amounts: np.ndarray, y: float, compounding: int | None) -> YieldRisk:
    """Price and risk of cash flows at times (years) for yield y.

    compounding=None means continuous; otherwise y compounds `compounding` times a year.
    """
    t = np.asarray(times, dtype=float)
    a = np.asarray(amounts, dtype=float)
    if compounding is None:
        pv = a * np.exp(-y * t)
        price = pv.sum()
        mac = (t * pv).sum() / price
        modified = mac
        convexity = (t * t * pv).sum() / price
    else:
        m = float(compounding)
        growth = 1.0 + y / m
        pv = a * growth ** (-m * t)
        price = pv.sum()
        mac = (t * pv).sum() / price
        modified = mac / growth
        convexity = (t * (t + 1.0 / m) * pv).sum() / price / growth**2
    return YieldRisk(float(price), float(mac), float(modified), float(convexity), float(modified * price * BP))


def _street_times(bond: Bond, on: date) -> tuple[np.ndarray, np.ndarray]:
    """Cash-flow times in years under the street convention: (w + i) / f."""
    flows = bond.cashflows_after(on)
    start, end = coupon_period(bond.issue_date, bond.maturity_date, bond.frequency, on)
    w = (end - on).days / (end - start).days
    periods = w + np.arange(len(flows))
    return periods / bond.frequency, np.array([a for _, a in flows])


def yield_risk(bond: Bond, on: date, ytm: float) -> YieldRisk:
    times, amounts = _street_times(bond, on)
    return cashflow_yield_risk(times, amounts, ytm, bond.frequency)


def yield_from_dirty_price(bond: Bond, on: date, dirty_price: float) -> float:
    times, amounts = _street_times(bond, on)

    def error(y: float) -> float:
        return cashflow_yield_risk(times, amounts, y, bond.frequency).price - dirty_price

    return float(brentq(error, -0.9 * bond.frequency, 2.0, xtol=1e-14))
