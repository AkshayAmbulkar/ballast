"""Read-only reports as JSON-ready data, shared by the REST API and the dashboard snapshot.

Every function returns plain dicts and lists of str, int, float, bool and None:
dates are ISO strings, NaN and infinity become None, and nothing is written to the
database. Errors are raised as NotFound (unknown portfolio), InvalidInput (bad
parameters) or Unavailable (data not loaded yet), each with a message safe to show.
"""

from __future__ import annotations

import functools
import math
from datetime import date, datetime, timezone

import numpy as np
import pandas as pd
from sqlalchemy import Engine, text

from ballast.construction.analysis import PORTFOLIO_ORDER, run_attribution, run_optimization
from ballast.construction.black_litterman import ViewError
from ballast.construction.data import ConstructionDataError, parse_benchmark
from ballast.construction.optimize import OptimizationError
from ballast.derivatives.analytics import greeks_report
from ballast.fixed_income.analytics import curve_snapshot, rates_risk_report
from ballast.risk.engine import compute_var, run_backtest, summarize_backtest
from ballast.risk.market import MarketData, RiskDataError, load_market_data
from ballast.risk.snapshot import build_snapshot
from ballast.risk.stress import run_stress
from ballast.validation import etf_duration_check, sofr_average_check

DEFAULT_PORTFOLIO = "Ballast Core"
DEFAULT_BENCHMARK = "SPY:0.6,TLT:0.4"


class ServiceError(Exception):
    """Base class; the message is meant for the caller."""


class NotFound(ServiceError):
    pass


class InvalidInput(ServiceError):
    pass


class Unavailable(ServiceError):
    pass


# ---------- conversion ----------

def num(x) -> float | None:
    if x is None:
        return None
    x = float(x)
    return x if math.isfinite(x) else None


def iso(d) -> str | None:
    if d is None:
        return None
    if isinstance(d, (datetime, pd.Timestamp)):
        return d.isoformat()
    return d.isoformat() if isinstance(d, date) else str(d)


def _plain(v):
    if isinstance(v, (date, datetime, pd.Timestamp)):
        return iso(v)
    if isinstance(v, (bool, np.bool_)):
        return bool(v)
    if isinstance(v, (int, np.integer)):
        return int(v)
    if isinstance(v, (float, np.floating)) or hasattr(v, "is_finite"):      # floats and Decimals
        return num(v)
    return v


def clean(obj):
    """Recursively convert to JSON-ready values (numpy scalars, dates, Decimals, NaN)."""
    if isinstance(obj, dict):
        return {str(k): clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, np.ndarray)):
        return [clean(v) for v in obj]
    return _plain(obj.item() if isinstance(obj, np.generic) else obj)


def _json(fn):
    """Every public report passes through clean(), so callers always get valid JSON data."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return clean(fn(*args, **kwargs))
    return wrapper


def records(df: pd.DataFrame, columns: list[str] | None = None) -> list[dict]:
    if df is None or df.empty:
        return []
    cols = columns or list(df.columns)
    return [{c: _plain(v) for c, v in zip(cols, row)} for row in df[cols].itertuples(index=False, name=None)]


# ---------- portfolios and holdings ----------

def _require_portfolio(engine: Engine, name: str) -> int:
    with engine.connect() as conn:
        pid = conn.execute(text("SELECT portfolio_id FROM portfolios WHERE name = :n"), {"n": name}).scalar()
    if pid is None:
        raise NotFound(f"No portfolio named {name!r}")
    return int(pid)


@_json
def list_portfolios(engine: Engine) -> list[dict]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT p.name, MAX(h.as_of_date), SUM(h.market_value)
                FROM portfolios p LEFT JOIN holdings_latest h USING (portfolio_id)
                GROUP BY p.name ORDER BY p.name
                """
            )
        ).all()
    return [{"name": n, "as_of": iso(d), "market_value": num(mv)} for n, d, mv in rows]


@_json
def summary(engine: Engine, portfolio: str = DEFAULT_PORTFOLIO) -> dict:
    """Holdings, today's and lifetime P&L, allocation, and the daily value and P&L history."""
    pid = _require_portfolio(engine, portfolio)
    with engine.connect() as conn:
        holdings = pd.read_sql(
            text(
                """
                SELECT h.symbol, i.name, h.asset_class, i.segment, h.quantity, h.close AS price, h.market_value,
                       h.weight, p.daily_pnl AS day_pnl, h.total_pnl, h.total_income
                FROM holdings_latest h
                JOIN instruments i USING (symbol)
                LEFT JOIN position_pnl_daily p
                       ON p.portfolio_id = h.portfolio_id AND p.instrument_id = i.instrument_id
                      AND p.as_of_date = h.as_of_date
                WHERE h.portfolio_id = :p
                ORDER BY h.market_value DESC
                """
            ),
            conn, params={"p": pid},
        )
        history = pd.read_sql(
            text(
                """
                SELECT as_of_date AS date, market_value, daily_pnl, cumulative_pnl, income
                FROM portfolio_pnl_daily WHERE portfolio_id = :p ORDER BY as_of_date
                """
            ),
            conn, params={"p": pid},
        )
    if history.empty:
        raise Unavailable("No positions yet. Run `ballast bootstrap`.")
    last = history.iloc[-1]
    for col in ("quantity", "price", "market_value", "weight", "day_pnl", "total_pnl", "total_income"):
        holdings[col] = holdings[col].astype(float)
    by_class = (holdings.groupby("asset_class", as_index=False)["market_value"].sum()
                .sort_values("market_value", ascending=False))
    by_class["weight"] = by_class.market_value / float(holdings.market_value.sum())
    return {
        "portfolio": portfolio,
        "as_of": iso(last.date),
        "market_value": num(last.market_value),
        "day_pnl": num(last.daily_pnl),
        "lifetime_pnl": num(last.cumulative_pnl),
        "lifetime_income": num(history.income.astype(float).sum()),
        "inception": iso(history.date.iloc[0]),
        "holdings": records(holdings),
        "by_asset_class": records(by_class),
        "history": records(history[["date", "market_value", "daily_pnl", "cumulative_pnl"]]),
    }


# ---------- fixed income and derivatives ----------

@_json
def treasury_curve(engine: Engine, on: date | None = None) -> dict:
    snap = curve_snapshot(engine, on)
    if snap is None:
        raise Unavailable("No Treasury curve. Run `ballast load-rates`.")
    curve_date, df = snap
    return {"date": iso(curve_date), "points": records(df)}


@_json
def rates_risk(engine: Engine, portfolio: str = DEFAULT_PORTFOLIO) -> dict:
    _require_portfolio(engine, portfolio)
    try:
        rep = rates_risk_report(engine, portfolio)
    except RuntimeError as exc:
        raise Unavailable(str(exc)) from None
    if rep is None:
        return {"as_of": None, "curve_date": None, "bonds": [], "swaps": [], "key_rates": None,
                "scenarios": [], "bond_dv01": 0.0, "swap_dv01": 0.0, "total_dv01": 0.0}
    kr = rep.key_rates
    bond_dv01 = float(rep.bonds.dv01_usd.sum()) if not rep.bonds.empty else 0.0
    return {
        "as_of": iso(rep.as_of),
        "curve_date": iso(rep.curve_date),
        "bonds": records(rep.bonds),
        "swaps": records(rep.swaps),
        "key_rates": {
            "tenors": list(kr.columns),
            "positions": [{"symbol": s, "dv01": [num(v) for v in kr.loc[s]]} for s in kr.index if s != "Total"],
            "total": [num(v) for v in kr.loc["Total"]],
        },
        "scenarios": records(rep.scenarios.assign(convexity_effect=lambda d: d.full_reval_pnl - d.key_rate_estimate)),
        "bond_dv01": bond_dv01,
        "swap_dv01": rep.total_dv01 - bond_dv01,
        "total_dv01": rep.total_dv01,
    }


@_json
def greeks(engine: Engine, portfolio: str = DEFAULT_PORTFOLIO) -> dict:
    _require_portfolio(engine, portfolio)
    rep = greeks_report(engine, portfolio)
    if rep is None:
        return {"as_of": None, "options": [], "exposure": [], "total_vega": 0.0, "total_theta": 0.0}
    return {
        "as_of": iso(rep.as_of),
        "options": records(rep.options),
        "exposure": records(rep.exposure),
        "total_vega": num(rep.options.vega_usd.sum()),
        "total_theta": num(rep.options.theta_usd_day.sum()),
    }


# ---------- risk ----------

def _market_data(engine: Engine, portfolio: str) -> MarketData:
    _require_portfolio(engine, portfolio)
    try:
        return load_market_data(engine, portfolio)
    except RiskDataError as exc:
        raise Unavailable(str(exc)) from None


@_json
def value_at_risk(engine: Engine, portfolio: str = DEFAULT_PORTFOLIO, confidence: float = 0.99,
                  lookback: int = 500, sims: int = 10_000, seed: int = 7, md: MarketData | None = None) -> dict:
    md = md or _market_data(engine, portfolio)
    try:
        res = compute_var(md, max(md.positions.as_of_date), confidence, lookback, sims, seed)
    except RiskDataError as exc:
        raise Unavailable(str(exc)) from None
    parts = [res.standalone[k].var for k in ("equity", "options", "rates (bonds + swaps)") if k in res.standalone]
    return {
        "as_of": iso(res.as_of),
        "confidence": confidence,
        "market_value": res.market_value,
        "lookback_days": res.lookback_days,
        "simulations": sims,
        "methods": [{"method": m, "var": r.var, "es": r.es} for m, r in res.methods.items()],
        "standalone": [{"name": k, "var": v.var, "es": v.es} for k, v in res.standalone.items()],
        "sum_of_standalone": sum(parts),
        "diversification_benefit": sum(parts) - res.methods["historical"].var,
        "unpriced": list(res.unpriced),
    }


@_json
def backtest(engine: Engine, portfolio: str = DEFAULT_PORTFOLIO, confidence: float = 0.99,
             lookback: int = 500, md: MarketData | None = None) -> dict:
    md = md or _market_data(engine, portfolio)
    days = run_backtest(md, confidence, lookback)
    if len(days) < 20:
        raise Unavailable(f"Only {len(days)} days could be backtested; need more history.")
    return {
        "confidence": confidence,
        "start": iso(days[0].date),
        "end": iso(days[-1].date),
        "days": [{"date": iso(d.date), "pnl": d.actual_pnl, "var_historical": d.var["historical"],
                  "var_parametric": d.var["parametric"]} for d in days],
        "tests": [
            {"method": s.method, "exceptions": s.exceptions, "observations": s.observations, "expected": s.expected,
             "rate": s.rate, "kupiec_p": num(s.kupiec.p_value), "independence_p": num(s.independence.p_value),
             "conditional_coverage_p": num(s.conditional.p_value), "last250": s.last250, "basel_zone": s.zone}
            for s in summarize_backtest(days, confidence)
        ],
    }


@_json
def stress(engine: Engine, portfolio: str = DEFAULT_PORTFOLIO, md: MarketData | None = None) -> dict:
    md = md or _market_data(engine, portfolio)
    on = max(md.positions.as_of_date)
    try:
        snap = build_snapshot(md, on, ladder_steps=500)
    except RiskDataError as exc:
        raise Unavailable(str(exc)) from None
    return {
        "as_of": iso(on),
        "episodes": [
            {"name": r.episode.name, "description": r.episode.description, "start": iso(r.start_used),
             "end": iso(r.end_used), "spy_return": num(r.spy_return), "ust10y_change_bp": num(r.ten_year_change_bp),
             "pnl": {k: num(v) for k, v in r.pnl.items()}}
            for r in run_stress(md, snap)
        ],
    }


# ---------- construction and attribution ----------

@_json
def optimization(engine: Engine, portfolio: str = DEFAULT_PORTFOLIO, lookback: int = 756, max_weight: float = 0.35,
                 views: list[str] | None = None, delta: float = 2.5, tau: float = 0.05) -> dict:
    """Also returns the covariance matrix and implied returns, so a client can redo
    Black-Litterman with its own views."""
    _require_portfolio(engine, portfolio)
    try:
        run = run_optimization(engine, portfolio, lookback, max_weight, views, delta, tau)
    except ViewError as exc:
        raise InvalidInput(str(exc)) from None
    except OptimizationError as exc:
        raise InvalidInput(f"Optimization failed ({exc})") from None
    except ConstructionDataError as exc:
        raise Unavailable(str(exc)) from None
    u = run.universe
    vols = np.sqrt(np.diag(u.cov))
    return {
        "as_of": iso(u.as_of),
        "risk_free": u.risk_free,
        "lookback_days": u.lookback_days,
        "returns_from": iso(u.start),
        "max_weight": max_weight,
        "delta": delta,
        "tau": tau,
        "views": run.view_text,
        "symbols": list(u.symbols),
        "assets": [
            {"symbol": s, "weight_now": num(u.current_weights[i]), "hist_return": num(u.mean[i]),
             "volatility": num(vols[i]), "implied_return": num(u.risk_free + run.implied_excess[i]),
             "bl_return": num(run.expected[i]), "risk_share": num(run.risk_share[i])}
            for i, s in enumerate(u.symbols)
        ],
        "covariance": [[num(x) for x in row] for row in u.cov],
        "portfolios": [
            {"name": k, "expected_return": num(run.stats[k].expected_return), "volatility": num(run.stats[k].volatility),
             "sharpe": num(run.stats[k].sharpe), "weights": {s: num(w) for s, w in zip(u.symbols, run.weights[k])}}
            for k in PORTFOLIO_ORDER
        ],
        "frontier": [{"expected_return": num(p.expected_return), "volatility": num(p.volatility),
                      "sharpe": num(p.sharpe)} for p in run.frontier],
    }


@_json
def attribution(engine: Engine, portfolio: str = DEFAULT_PORTFOLIO, benchmark: str = DEFAULT_BENCHMARK,
                since: date | None = None) -> dict:
    _require_portfolio(engine, portfolio)
    try:
        parse_benchmark(benchmark)
    except (ConstructionDataError, ValueError) as exc:
        raise InvalidInput(f"Benchmark {benchmark!r}: {exc}") from None
    try:
        run = run_attribution(engine, portfolio, benchmark, since)
    except ConstructionDataError as exc:
        raise Unavailable(str(exc)) from None
    a = run.inputs
    effects = run.linked.effects
    growth_p = np.cumprod(1 + a.portfolio_daily)
    growth_b = np.cumprod(1 + a.benchmark_daily)
    stats = lambda s: {"total_return": s.total_return, "annualized": s.annualized, "volatility": s.volatility}
    return {
        "benchmark": benchmark,
        "start": iso(a.days[0]),
        "end": iso(a.days[-1]),
        "days": len(a.days),
        "portfolio": stats(run.portfolio),
        "benchmark_stats": stats(run.benchmark),
        "active_return": run.active_return,
        "active_annualized": run.active_annualized,
        "tracking_error": run.tracking_error,
        "information_ratio": num(run.information_ratio),
        "effects": [{"segment": s, **{k: num(effects.loc[s, k]) for k in effects.columns}} for s in effects.index],
        "totals": {k: num(effects[k].sum()) for k in effects.columns},
        "by_year": [{"year": y.year, "portfolio_return": y.portfolio_return, "benchmark_return": y.benchmark_return,
                     "allocation": y.allocation, "selection": y.selection, "interaction": y.interaction}
                    for y in run.by_year],
        "growth": [{"date": iso(d), "portfolio": num(p), "benchmark": num(b)}
                   for d, p, b in zip(a.days, growth_p, growth_b)],
    }


# ---------- validation ----------

@_json
def validation(engine: Engine) -> dict:
    """Ballast's numbers against independently published figures."""
    sofr = sofr_average_check(engine)
    tlt = etf_duration_check(engine, "TLT", "DGS20")
    return {
        "sofr_30day_average": None if sofr is None else {
            "days": len(sofr.comparison),
            "start": iso(sofr.comparison.published_on.iloc[0]),
            "end": iso(sofr.comparison.published_on.iloc[-1]),
            "exact_matches": sofr.exact_matches,
            "largest_diff_bp": sofr.largest_diff_bp,
        },
        "tlt_empirical_duration": None if tlt is None else {
            "symbol": tlt.symbol, "series": tlt.series, "start": iso(tlt.first.day), "end": iso(tlt.last.day),
            "observations": tlt.estimate.observations, "duration": tlt.estimate.duration,
            "r_squared": num(tlt.estimate.r_squared),
        },
    }


# ---------- everything, for the static dashboard ----------

@_json
def snapshot(engine: Engine, portfolio: str = DEFAULT_PORTFOLIO, benchmark: str = DEFAULT_BENCHMARK) -> dict:
    md = _market_data(engine, portfolio)
    return {
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "portfolio": portfolio,
        "summary": summary(engine, portfolio),
        "curve": treasury_curve(engine),
        "rates_risk": rates_risk(engine, portfolio),
        "greeks": greeks(engine, portfolio),
        "var": {"99": value_at_risk(engine, portfolio, 0.99, md=md), "95": value_at_risk(engine, portfolio, 0.95, md=md)},
        "backtest": backtest(engine, portfolio, 0.99, md=md),
        "stress": stress(engine, portfolio, md=md),
        "optimization": optimization(engine, portfolio),
        "attribution": attribution(engine, portfolio, benchmark),
        "validation": validation(engine),
    }
