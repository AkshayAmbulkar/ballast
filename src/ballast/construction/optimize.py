"""Mean-variance optimization and risk parity.

Weights sum to 1. Constrained problems (long-only, a maximum weight per asset)
are solved with sequential quadratic programming (SciPy SLSQP); unconstrained
problems have closed forms, which the tests use to check the solver:

* minimum variance:  w = S^-1 1 / (1' S^-1 1)
* tangency (max Sharpe):  w = S^-1 (mu - rf) / (1' S^-1 (mu - rf))

Risk parity (equal risk contribution) solves the convex problem of Spinu (2013),
    minimize  1/2 y' S y - sum_i b_i ln(y_i),  y > 0,
whose solution, rescaled to w = y / sum(y), gives each asset a share b_i of
portfolio variance: w_i (S w)_i / (w' S w) = b_i. It is solved by damped Newton
steps, which converge to machine precision, and the result is checked.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.optimize import minimize


class OptimizationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Portfolio:
    weights: np.ndarray
    expected_return: float
    volatility: float
    sharpe: float


def describe(w, mu, cov, rf: float) -> Portfolio:
    w = np.asarray(w, dtype=float)
    ret = float(w @ mu)
    vol = float(np.sqrt(max(w @ cov @ w, 0.0)))
    return Portfolio(w, ret, vol, (ret - rf) / vol if vol > 0 else float("nan"))


def _solve(objective, n: int, max_weight: float | None, long_only: bool, extra=(), x0=None) -> np.ndarray:
    lower = 0.0 if long_only else None
    upper = max_weight if max_weight is not None else None
    if long_only and max_weight is not None and max_weight * n < 1 - 1e-12:
        raise OptimizationError(f"max weight {max_weight:.0%} x {n} assets cannot sum to 100%")
    cons = [{"type": "eq", "fun": lambda w: np.sum(w) - 1.0, "jac": lambda w: np.ones_like(w)}, *extra]
    start = np.full(n, 1.0 / n) if x0 is None else x0
    res = minimize(objective, start, jac=True, method="SLSQP", bounds=[(lower, upper)] * n,
                   constraints=cons, options={"maxiter": 1000, "ftol": 1e-15})
    if not res.success:
        raise OptimizationError(res.message)
    return res.x


def min_variance(cov, max_weight: float | None = None, long_only: bool = True) -> np.ndarray:
    cov = np.asarray(cov, dtype=float)
    return _solve(lambda w: (w @ cov @ w, 2 * cov @ w), len(cov), max_weight, long_only)


def max_sharpe(mu, cov, rf: float, max_weight: float | None = None, long_only: bool = True) -> np.ndarray:
    mu, cov = np.asarray(mu, dtype=float), np.asarray(cov, dtype=float)
    if np.all(mu <= rf):
        raise OptimizationError("no asset is expected to beat the risk-free rate")

    def neg_sharpe(w):
        var = w @ cov @ w
        vol = np.sqrt(var)
        excess = w @ mu - rf
        grad = -(mu * vol - excess * (cov @ w) / vol) / var
        return -excess / vol, grad

    return _solve(neg_sharpe, len(mu), max_weight, long_only, x0=min_variance(cov, max_weight, long_only))


FRONTIER_TOP_SLACK = 1e-9   # 0.0000001 percentage points below the highest attainable return


def frontier(mu, cov, points: int = 10, max_weight: float | None = None, long_only: bool = True) -> list[np.ndarray]:
    """Minimum-variance portfolios for evenly spaced target returns, from the global minimum
    variance portfolio up to the highest return the constraints allow.

    The target is imposed as `return >= target`: above the minimum-variance return the constraint
    binds, so this gives the same portfolios as an equality. With weight caps, only one portfolio
    (a corner of the constraints) attains the very highest return, and rounding can make that single
    point look infeasible to the solver, so the top target sits FRONTIER_TOP_SLACK below it."""
    mu, cov = np.asarray(mu, dtype=float), np.asarray(cov, dtype=float)
    w_gmv = min_variance(cov, max_weight, long_only)
    w_top = _solve(lambda w: (-(w @ mu), -mu), len(mu), max_weight, long_only)
    lo, hi = float(w_gmv @ mu), float(w_top @ mu) - FRONTIER_TOP_SLACK
    targets = np.linspace(lo, max(lo, hi), points)
    out = []
    for target in targets:
        cons = ({"type": "ineq", "fun": lambda w, t=target: w @ mu - t, "jac": lambda w: mu},)
        out.append(_solve(lambda w: (w @ cov @ w, 2 * cov @ w), len(mu), max_weight, long_only, extra=cons, x0=w_gmv))
    return out


def risk_parity(cov, budgets=None, max_iter: int = 200) -> np.ndarray:
    cov = np.asarray(cov, dtype=float)
    n = len(cov)
    b = np.full(n, 1.0 / n) if budgets is None else np.asarray(budgets, dtype=float) / np.sum(budgets)
    if np.any(b <= 0):
        raise OptimizationError("risk budgets must be positive")
    y = 1.0 / np.sqrt(np.diag(cov))
    y *= np.sqrt(1.0 / (y @ cov @ y))         # start on the right scale: y' S y = sum(b) = 1 at the optimum
    for _ in range(max_iter):
        grad = cov @ y - b / y
        step = -np.linalg.solve(cov + np.diag(b / y**2), grad)
        decrement = float(np.sqrt(max(-grad @ step, 0.0)))   # Newton decrement
        if decrement < 1e-13:
            break
        t = 1.0 if decrement < 0.25 else 1.0 / (1.0 + decrement)   # damped Newton (Nesterov)
        while np.any(y + t * step <= 0):
            t *= 0.5
        y = y + t * step
    w = y / y.sum()
    if np.max(np.abs(risk_contributions(w, cov) - b)) > 1e-8:
        raise OptimizationError("risk parity did not converge")
    return w


def risk_contributions(w, cov) -> np.ndarray:
    """Share of portfolio variance from each asset: w_i (S w)_i / (w' S w). Sums to 1."""
    w, cov = np.asarray(w, dtype=float), np.asarray(cov, dtype=float)
    return w * (cov @ w) / (w @ cov @ w)
