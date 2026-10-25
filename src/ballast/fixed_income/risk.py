"""Curve-based interest-rate risk: effective duration, key-rate DV01 and scenarios.

All shocks are applied to the input par yields, then the curve is rebuilt and
every position fully revalued. Because par yields are interpolated linearly
between tenors, shocking one tenor moves the curve only between its neighbours,
and shocking every tenor by the same amount is an exact parallel par shift. So
key-rate DV01s add up to the parallel DV01 (up to second-order effects).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Callable

import numpy as np

from ballast.fixed_income.curve import DiscountCurve, bootstrap_par_curve

BP = 1e-4


@dataclass(frozen=True)
class ParCurve:
    """Market inputs for one date: tenors (years) and par yields (decimals)."""

    tenor_months: tuple[int, ...]
    par_yields: tuple[float, ...]

    @property
    def tenors_years(self) -> np.ndarray:
        return np.array(self.tenor_months, dtype=float) / 12.0

    def build(self) -> DiscountCurve:
        return bootstrap_par_curve(self.tenors_years, self.par_yields)

    def shifted(self, shifts_bp) -> "ParCurve":
        shifts = np.broadcast_to(np.asarray(shifts_bp, dtype=float), (len(self.par_yields),))
        return replace(self, par_yields=tuple(float(y + s * BP) for y, s in zip(self.par_yields, shifts)))


PriceFn = Callable[[DiscountCurve], float]


@dataclass(frozen=True)
class CurveRisk:
    price: float
    dv01: float                 # value gained if all par yields fall 1bp
    effective_duration: float   # years
    effective_convexity: float  # years^2


def curve_risk(price_fn: PriceFn, par: ParCurve, bump_bp: float = 1.0) -> CurveRisk:
    """Central-difference duration and convexity under a parallel par-yield shift."""
    p0 = price_fn(par.build())
    up = price_fn(par.shifted(bump_bp).build())
    down = price_fn(par.shifted(-bump_bp).build())
    h = bump_bp * BP
    return CurveRisk(
        price=p0,
        dv01=(down - up) / 2.0 / bump_bp,
        effective_duration=(down - up) / (2.0 * p0 * h),
        effective_convexity=(up + down - 2.0 * p0) / (p0 * h * h),
    )


def key_rate_dv01s(price_fn: PriceFn, par: ParCurve, bump_bp: float = 1.0) -> np.ndarray:
    """Value gained per 1bp fall at each input tenor, one tenor at a time (central difference)."""
    out = np.zeros(len(par.par_yields))
    for i in range(len(out)):
        shift = np.zeros(len(out))
        shift[i] = bump_bp
        up = price_fn(par.shifted(shift).build())
        down = price_fn(par.shifted(-shift).build())
        out[i] = (down - up) / 2.0 / bump_bp
    return out


# ---------- scenarios: shift in bp as a function of tenor in years ----------

def _ramp(t: np.ndarray, short_bp: float, long_bp: float, short_end: float = 2.0, long_end: float = 10.0) -> np.ndarray:
    return np.interp(t, [short_end, long_end], [short_bp, long_bp])


SCENARIOS: dict[str, tuple[str, Callable[[np.ndarray], np.ndarray]]] = {
    "parallel_up_100": ("All tenors +100bp", lambda t: np.full_like(t, 100.0)),
    "parallel_down_100": ("All tenors -100bp", lambda t: np.full_like(t, -100.0)),
    "bear_steepener": ("2Y and shorter +0bp, rising to +50bp at 10Y and longer", lambda t: _ramp(t, 0.0, 50.0)),
    "bull_flattener": ("2Y and shorter +0bp, falling to -50bp at 10Y and longer", lambda t: _ramp(t, 0.0, -50.0)),
    "twist_steepener": ("2Y and shorter -25bp, rising to +25bp at 10Y and longer", lambda t: _ramp(t, -25.0, 25.0)),
}


def scenario_shifts(name: str, par: ParCurve) -> np.ndarray:
    return SCENARIOS[name][1](par.tenors_years)


def historical_shifts(start: ParCurve, end: ParCurve) -> np.ndarray:
    """Actual change in each tenor's par yield between two dates, in bp."""
    if start.tenor_months != end.tenor_months:
        raise ValueError("the two curves must have the same tenors")
    return (np.array(end.par_yields) - np.array(start.par_yields)) / BP
