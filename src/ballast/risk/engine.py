"""Run VaR for a portfolio on a date, and the daily backtest.

Lookback: historical simulation uses the last 500 trading days of factor changes
(about two years); the parametric and Monte Carlo methods use an EWMA
covariance (lambda = 0.94) of the same window.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from ballast.risk.market import MarketData, RiskDataError
from ballast.risk.snapshot import Snapshot, build_snapshot
from ballast.risk.var import TailRisk, ewma_covariance, monte_carlo_factors, parametric, tail_risk

DEFAULT_LOOKBACK = 500
MIN_LOOKBACK = 250


@dataclass(frozen=True)
class VarResult:
    as_of: date
    confidence: float
    market_value: float
    lookback_days: int
    methods: dict[str, TailRisk]           # historical, parametric, monte_carlo
    standalone: dict[str, TailRisk]        # historical VaR of each asset class on its own
    unpriced: tuple[str, ...]


def _split(md: MarketData, factors: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    m = len(md.equity_symbols)
    X = factors.to_numpy(dtype=float)
    return X[:, :m], X[:, m:]


def compute_var(md: MarketData, on: date, confidence: float = 0.99, lookback: int = DEFAULT_LOOKBACK,
                sims: int = 10_000, seed: int = 7, snapshot: Snapshot | None = None) -> VarResult:
    snap = snapshot or build_snapshot(md, on, ladder_steps=500)
    hist = md.factor_history(on, lookback)
    if len(hist) < MIN_LOOKBACK:
        raise RiskDataError(f"Only {len(hist)} days of factor history before {on}; need {MIN_LOOKBACK}")
    eq, rates = _split(md, hist)

    buckets = snap.pnl_by_bucket(eq, rates)
    total = sum(buckets.values())
    methods = {"historical": tail_risk(total, confidence)}

    cov = ewma_covariance(hist.to_numpy(dtype=float))
    methods["parametric"], _ = parametric(snap.sensitivities(), cov, confidence)

    mc = monte_carlo_factors(cov, sims, seed)
    m = len(md.equity_symbols)
    methods["monte_carlo"] = tail_risk(snap.pnl(mc[:, :m], mc[:, m:]), confidence)

    standalone = {}
    for name, combine in (("equity", ["equity"]), ("options", ["options"]),
                          ("bonds", ["bonds"]), ("swaps", ["swaps"]), ("rates (bonds + swaps)", ["bonds", "swaps"])):
        pnl = sum(buckets[b] for b in combine)
        if np.any(pnl != 0):
            standalone[name] = tail_risk(pnl, confidence)
    return VarResult(on, confidence, snap.market_value, len(hist), methods, standalone, snap.unpriced)


@dataclass(frozen=True)
class BacktestDay:
    date: date
    actual_pnl: float
    var: dict[str, float]


def run_backtest(md: MarketData, confidence: float = 0.99, lookback: int = DEFAULT_LOOKBACK,
                 start: date | None = None) -> list[BacktestDay]:
    """For each day t, VaR from the close of t-1 (positions, prices and factor history up to t-1)
    compared with the actual P&L booked on t."""
    days = [d for d in md.calendar if d in md.daily_pnl.index]
    held = set(md.positions.as_of_date)
    out = []
    for prev, day in zip(days[:-1], days[1:]):
        if start and day < start:
            continue
        if prev not in held:
            continue
        hist = md.factor_history(prev, lookback)
        if len(hist) < MIN_LOOKBACK:
            continue
        try:
            snap = build_snapshot(md, prev, ladder_steps=200)
        except RiskDataError:
            continue
        eq, rates = _split(md, hist)
        hs = tail_risk(snap.pnl(eq, rates), confidence).var
        par, _ = parametric(snap.sensitivities(), ewma_covariance(hist.to_numpy(dtype=float)), confidence)
        out.append(BacktestDay(day, float(md.daily_pnl.loc[day]), {"historical": hs, "parametric": par.var}))
    return out
