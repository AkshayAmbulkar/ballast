"""Vectorized version of the par-curve bootstrap, for revaluing under many scenarios at once.

`batch_discount_factors` reproduces `curve.bootstrap_par_curve` followed by
`DiscountCurve.df` exactly, for every row of a par-yield matrix. The bootstrap is
sequential along the maturity grid but independent across scenarios, so each
grid step is one vector operation over all scenarios. A test checks agreement
with the scalar implementation.
"""

from __future__ import annotations

import numpy as np

BILL_CUTOFF_YEARS = 0.5


def _linear_interp_weights(x: np.ndarray, xp: np.ndarray) -> np.ndarray:
    """Matrix W with W @ fp == np.interp(x, xp, fp) for any fp (flat beyond the ends)."""
    W = np.zeros((len(x), len(xp)))
    for i, xi in enumerate(x):
        if xi <= xp[0]:
            W[i, 0] = 1.0
        elif xi >= xp[-1]:
            W[i, -1] = 1.0
        else:
            j = int(np.searchsorted(xp, xi, side="right")) - 1
            w = (xi - xp[j]) / (xp[j + 1] - xp[j])
            W[i, j], W[i, j + 1] = 1.0 - w, w
    return W


class BatchCurveBuilder:
    """Precomputes interpolation weights for fixed tenors and query times."""

    def __init__(self, tenors_years, times):
        t = np.asarray(tenors_years, dtype=float)
        self.order = np.argsort(t)
        self.tenors = t[self.order]
        self.bill_idx = np.where(self.tenors < BILL_CUTOFF_YEARS - 1e-12)[0]
        n_periods = int(round(self.tenors[-1] * 2))
        self.grid = np.arange(1, n_periods + 1) / 2.0
        self.par_weights = _linear_interp_weights(self.grid, self.tenors)            # (grid, tenors)
        self.knot_t = np.concatenate([[0.0], self.tenors[self.bill_idx], self.grid])
        self.times = np.asarray(times, dtype=float)
        # log-DF interpolation weights from knots to query times (with last-segment extrapolation)
        kt = self.knot_t
        W = np.zeros((len(self.times), len(kt)))
        for i, ti in enumerate(self.times):
            if ti >= kt[-1]:
                slope_w = (ti - kt[-1]) / (kt[-1] - kt[-2])
                W[i, -1], W[i, -2] = 1.0 + slope_w, -slope_w
            else:
                j = int(np.searchsorted(kt, ti, side="right")) - 1
                w = (ti - kt[j]) / (kt[j + 1] - kt[j])
                W[i, j], W[i, j + 1] = 1.0 - w, w
        self.time_weights = W                                                          # (times, knots)

    def discount_factors(self, par_matrix) -> np.ndarray:
        """(n_scenarios, n_tenors) par yields in the constructor's tenor order -> (n_scenarios, n_times)."""
        Y = np.atleast_2d(np.asarray(par_matrix, dtype=float))[:, self.order]
        n = Y.shape[0]
        log_knots = np.zeros((n, len(self.knot_t)))
        col = 1
        for b in self.bill_idx:
            log_knots[:, col] = -np.log1p(Y[:, b] * self.tenors[b])
            col += 1
        par_grid = Y @ self.par_weights.T                                              # (n, grid)
        running = np.zeros(n)
        for j in range(par_grid.shape[1]):
            c = par_grid[:, j] / 2.0
            d = (1.0 - c * running) / (1.0 + c)
            if np.any(d <= 0):
                raise ValueError("bootstrap produced a non-positive discount factor in some scenario")
            log_knots[:, col] = np.log(d)
            running += d
            col += 1
        return np.exp(log_knots @ self.time_weights.T)
