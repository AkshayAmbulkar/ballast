"""Optimization and attribution runs for a portfolio, independent of how they are shown.

The CLI prints these results, the REST API returns them and the dashboard snapshot
stores them, so the calculations live here once.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import date

import numpy as np

from ballast.construction.attribution import AttributionResult, link
from ballast.construction.black_litterman import View, equilibrium_returns, posterior, view_matrices
from ballast.construction.data import (
    TRADING_DAYS,
    AttributionInputs,
    Universe,
    load_attribution_inputs,
    load_universe,
    parse_benchmark,
)
from ballast.construction.optimize import (
    OptimizationError,
    Portfolio,
    describe,
    frontier,
    max_sharpe,
    min_variance,
    risk_contributions,
    risk_parity,
)

PORTFOLIO_ORDER = ("current", "min_variance", "max_sharpe", "risk_parity", "max_sharpe_hist")


@dataclass(frozen=True)
class OptimizationRun:
    universe: Universe
    views: list[View]
    view_text: list[str]
    max_weight: float
    delta: float
    tau: float
    implied_excess: np.ndarray            # pi: returns in excess of the risk-free rate implied by today's weights
    expected: np.ndarray                  # Black-Litterman total expected returns
    weights: dict[str, np.ndarray]        # PORTFOLIO_ORDER -> weights
    stats: dict[str, Portfolio]           # evaluated with the Black-Litterman returns
    risk_share: np.ndarray                # share of today's portfolio variance by asset
    frontier: list[Portfolio]


def run_optimization(engine, portfolio: str, lookback: int = 756, max_weight: float = 0.35,
                     views: list[str] | None = None, delta: float = 2.5, tau: float = 0.05) -> OptimizationRun:
    """Raises ConstructionDataError (missing data), ViewError (bad view) or OptimizationError
    (a solver failure, with the step that failed)."""
    u = load_universe(engine, portfolio, lookback)
    parsed = [View.parse(v) for v in (views or [])]
    P, Q = view_matrices(parsed, u.symbols) if parsed else (None, None)
    # Reverse optimization gives returns in excess of the risk-free rate. Absolute views are stated
    # as total returns, so the risk-free rate is taken off them; in relative views it cancels.
    pi = equilibrium_returns(u.cov, u.current_weights, delta)
    Q_excess = None if Q is None else Q - u.risk_free * P.sum(axis=1)
    mu = u.risk_free + posterior(pi, u.cov, tau, P, Q_excess).mean

    steps = {
        "min_variance": lambda: min_variance(u.cov, max_weight),
        "max_sharpe": lambda: max_sharpe(mu, u.cov, u.risk_free, max_weight),
        "risk_parity": lambda: risk_parity(u.cov),
        "max_sharpe_hist": lambda: max_sharpe(u.mean, u.cov, u.risk_free, max_weight),
    }
    weights = {"current": u.current_weights}
    for name, solve in steps.items():
        try:
            weights[name] = solve()
        except OptimizationError as exc:
            raise OptimizationError(f"{name}: {exc}") from None
    try:
        curve = frontier(mu, u.cov, points=8, max_weight=max_weight)
    except OptimizationError as exc:
        raise OptimizationError(f"frontier: {exc}") from None

    return OptimizationRun(
        universe=u, views=parsed, view_text=list(views or []), max_weight=max_weight, delta=delta, tau=tau,
        implied_excess=pi, expected=mu, weights=weights,
        stats={k: describe(w, mu, u.cov, u.risk_free) for k, w in weights.items()},
        risk_share=risk_contributions(u.current_weights, u.cov),
        frontier=[describe(w, mu, u.cov, u.risk_free) for w in curve],
    )


@dataclass(frozen=True)
class PeriodStats:
    total_return: float
    annualized: float
    volatility: float


@dataclass(frozen=True)
class YearAttribution:
    year: int
    portfolio_return: float
    benchmark_return: float
    allocation: float
    selection: float
    interaction: float


@dataclass(frozen=True)
class AttributionRun:
    benchmark_text: str
    inputs: AttributionInputs
    linked: AttributionResult             # whole period, Carino-linked effects by segment
    portfolio: PeriodStats
    benchmark: PeriodStats
    active_return: float                  # cumulative portfolio return minus cumulative benchmark return
    active_annualized: float
    tracking_error: float
    information_ratio: float
    by_year: list[YearAttribution]


def _daily_items(a: AttributionInputs, idx) -> list[dict]:
    return [{"w": a.port_weights[t], "r": a.port_returns[t], "W": a.bench_weights, "b": a.bench_returns[t]}
            for t in idx]


def run_attribution(engine, portfolio: str, benchmark: str = "SPY:0.6,TLT:0.4",
                    since: date | None = None) -> AttributionRun:
    """Raises ConstructionDataError for missing data or a malformed benchmark."""
    a = load_attribution_inputs(engine, portfolio, parse_benchmark(benchmark), since)
    n = len(a.days)
    total = link(_daily_items(a, range(n)), a.segments)
    ann = lambda R: (1 + R) ** (TRADING_DAYS / n) - 1
    vol = lambda x: float(np.std(x, ddof=1) * math.sqrt(TRADING_DAYS))
    p, b = a.portfolio_daily, a.benchmark_daily
    te = vol(p - b)
    active_ann = ann(total.portfolio_return) - ann(total.benchmark_return)

    years = defaultdict(list)
    for t, d in enumerate(a.days):
        years[d.year].append(t)
    by_year = []
    for y, idx in sorted(years.items()):
        res = link(_daily_items(a, idx), a.segments)
        e = res.effects.sum()
        by_year.append(YearAttribution(y, res.portfolio_return, res.benchmark_return,
                                       float(e.allocation), float(e.selection), float(e.interaction)))
    return AttributionRun(
        benchmark_text=benchmark, inputs=a, linked=total,
        portfolio=PeriodStats(total.portfolio_return, ann(total.portfolio_return), vol(p)),
        benchmark=PeriodStats(total.benchmark_return, ann(total.benchmark_return), vol(b)),
        active_return=total.portfolio_return - total.benchmark_return, active_annualized=active_ann,
        tracking_error=te, information_ratio=active_ann / te if te > 0 else float("nan"), by_year=by_year,
    )
