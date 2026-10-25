"""Discount curves and bootstrapping.

Two bootstraps are provided:

* `bootstrap_par_curve` turns U.S. Treasury constant-maturity (CMT) yields into
  discount factors. Treasury publishes these as par yields on a bond-equivalent
  (semiannual) basis, so a bond paying a semiannual coupon equal to the par yield
  prices at exactly 100 on the curve.
* `bootstrap_from_bond_prices` solves zero rates from bond prices, the textbook
  method (Hull, Options, Futures, and Other Derivatives, chapter 4).

Between knots, discount factors are interpolated log-linearly, which means the
continuously compounded forward rate is constant between knots.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import numpy as np
from scipy.optimize import brentq

BILL_CUTOFF_YEARS = 0.5


class CurveError(ValueError):
    pass


@dataclass(frozen=True)
class DiscountCurve:
    """Discount factors at knot times (years). DF(0) = 1 is implied."""

    times: np.ndarray
    discount_factors: np.ndarray

    def __post_init__(self):
        t = np.asarray(self.times, dtype=float)
        d = np.asarray(self.discount_factors, dtype=float)
        if t.ndim != 1 or t.shape != d.shape or len(t) == 0:
            raise CurveError("times and discount factors must be non-empty 1-D arrays of equal length")
        if np.any(np.diff(t) <= 0) or t[0] <= 0:
            raise CurveError("knot times must be positive and strictly increasing")
        if np.any(d <= 0):
            raise CurveError("discount factors must be positive")
        object.__setattr__(self, "times", t)
        object.__setattr__(self, "discount_factors", d)
        object.__setattr__(self, "_t", np.concatenate([[0.0], t]))
        object.__setattr__(self, "_log_df", np.concatenate([[0.0], np.log(d)]))

    def df(self, t):
        """Discount factor at time(s) t in years."""
        t = np.asarray(t, dtype=float)
        if np.any(t < 0):
            raise CurveError("negative time")
        log_df = np.interp(t, self._t, self._log_df)
        beyond = t > self._t[-1]
        if np.any(beyond):  # extrapolate with the last segment's forward rate
            slope = (self._log_df[-1] - self._log_df[-2]) / (self._t[-1] - self._t[-2])
            log_df = np.where(beyond, self._log_df[-1] + slope * (t - self._t[-1]), log_df)
        return np.exp(log_df)

    def zero_rate(self, t, compounding: int | None = None):
        """Zero rate to time t: continuous if compounding is None, else compounded `compounding` times a year."""
        t = np.asarray(t, dtype=float)
        if np.any(t <= 0):
            raise CurveError("zero rate needs t > 0")
        d = self.df(t)
        if compounding is None:
            return -np.log(d) / t
        m = float(compounding)
        return m * (d ** (-1.0 / (m * t)) - 1.0)

    def forward_rate(self, t1, t2):
        """Continuously compounded forward rate between t1 and t2."""
        t1, t2 = np.asarray(t1, dtype=float), np.asarray(t2, dtype=float)
        if np.any(t2 <= t1):
            raise CurveError("forward rate needs t2 > t1")
        return (np.log(self.df(t1)) - np.log(self.df(t2))) / (t2 - t1)


def bootstrap_par_curve(tenors_years: Sequence[float], par_yields: Sequence[float]) -> DiscountCurve:
    """Discount curve from par yields (decimals, bond-equivalent basis).

    * Tenors under 6 months are Treasury bills: one payment at maturity, so
      DF(t) = 1 / (1 + y t), the bond-equivalent simple-interest convention.
    * From 6 months out, par yields are interpolated linearly to a semiannual
      grid (0.5, 1.0, ... up to the longest tenor) and solved one point at a time.
      A par bond with coupon c = y/2 per half-year satisfies
          1 = c (DF_1 + ... + DF_n) + DF_n
      so  DF_n = (1 - c (DF_1 + ... + DF_{n-1})) / (1 + c).
    Beyond the longest tenor, par yields are held flat.
    """
    t = np.asarray(tenors_years, dtype=float)
    y = np.asarray(par_yields, dtype=float)
    if t.shape != y.shape or len(t) < 2:
        raise CurveError("need at least two tenors with yields")
    order = np.argsort(t)
    t, y = t[order], y[order]
    if np.any(np.diff(t) <= 0):
        raise CurveError("duplicate tenors")
    if not np.all(np.isfinite(y)):
        raise CurveError("par yields must be finite")
    if t[-1] < BILL_CUTOFF_YEARS:
        raise CurveError("need at least one tenor of 6 months or longer")

    knot_t: list[float] = []
    knot_df: list[float] = []
    for ti, yi in zip(t, y):
        if ti < BILL_CUTOFF_YEARS - 1e-12:
            knot_t.append(ti)
            knot_df.append(1.0 / (1.0 + yi * ti))

    n_periods = int(round(t[-1] * 2))
    grid = np.arange(1, n_periods + 1) / 2.0
    par_on_grid = np.interp(grid, t, y)
    running_sum = 0.0
    for g, yg in zip(grid, par_on_grid):
        c = yg / 2.0
        d = (1.0 - c * running_sum) / (1.0 + c)
        if d <= 0:
            raise CurveError(f"bootstrap produced a non-positive discount factor at {g} years")
        knot_t.append(float(g))
        knot_df.append(d)
        running_sum += d

    return DiscountCurve(np.array(knot_t), np.array(knot_df))


@dataclass(frozen=True)
class BondQuote:
    maturity_years: float
    annual_coupon: float      # cash per year per `face`, e.g. 8.0 for an 8% bond on 100 face
    price: float              # dirty price
    frequency: int = 2
    face: float = 100.0


def bootstrap_from_bond_prices(quotes: Sequence[BondQuote]) -> tuple[np.ndarray, np.ndarray]:
    """Continuously compounded zero rates at each bond maturity, solved in maturity order.

    Coupon dates that fall between known maturities use a zero rate interpolated
    linearly between the previous knot and the rate being solved for.
    Returns (maturities, zero_rates).
    """
    quotes = sorted(quotes, key=lambda q: q.maturity_years)
    knots_t: list[float] = []
    knots_r: list[float] = []

    for q in quotes:
        T = q.maturity_years
        if knots_t and T <= knots_t[-1]:
            raise CurveError("duplicate maturities")
        coupon = q.annual_coupon / q.frequency
        n = int(round(T * q.frequency))
        times = [T - k / q.frequency for k in range(n)][::-1] if coupon > 0 else [T]
        times = [s for s in times if s > 1e-12]
        flows = [coupon] * (len(times) - 1) + [coupon + q.face] if coupon > 0 else [q.face]

        def pv(r_T: float) -> float:
            total = 0.0
            for s, cf in zip(times, flows):
                if knots_t and s <= knots_t[-1]:
                    r = float(np.interp(s, knots_t, knots_r))
                elif knots_t:
                    w = (s - knots_t[-1]) / (T - knots_t[-1])
                    r = knots_r[-1] + w * (r_T - knots_r[-1])
                else:
                    r = r_T
                total += cf * np.exp(-r * s)
            return total - q.price

        r_T = brentq(pv, -0.5, 2.0, xtol=1e-14)
        knots_t.append(T)
        knots_r.append(r_T)

    return np.array(knots_t), np.array(knots_r)
