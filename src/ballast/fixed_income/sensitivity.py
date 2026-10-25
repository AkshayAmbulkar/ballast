"""Empirical rate sensitivity: how much an asset's price moves when yields move.

Regress the asset's daily total return on the daily change in a Treasury yield:

    r_t = a + b * dy_t + e_t        (dy in decimal, so 1bp = 0.0001)

The empirical duration is -b: the percentage price change per 1.00 (100%)
change in yield, i.e. a duration of 16 means about -0.16% per +1bp.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class EmpiricalDuration:
    duration: float
    alpha: float
    r_squared: float
    observations: int


def empirical_duration(returns, yield_changes) -> EmpiricalDuration:
    r = np.asarray(returns, dtype=float)
    dy = np.asarray(yield_changes, dtype=float)
    mask = np.isfinite(r) & np.isfinite(dy)
    r, dy = r[mask], dy[mask]
    if len(r) < 20:
        raise ValueError(f"need at least 20 observations, got {len(r)}")
    if np.allclose(dy, dy[0]):
        raise ValueError("yield changes have no variation")
    X = np.column_stack([np.ones_like(dy), dy])
    (alpha, beta), *_ = np.linalg.lstsq(X, r, rcond=None)
    fitted = X @ np.array([alpha, beta])
    ss_res = float(np.sum((r - fitted) ** 2))
    ss_tot = float(np.sum((r - r.mean()) ** 2))
    return EmpiricalDuration(
        duration=float(-beta),
        alpha=float(alpha),
        r_squared=1.0 - ss_res / ss_tot if ss_tot > 0 else float("nan"),
        observations=int(len(r)),
    )
