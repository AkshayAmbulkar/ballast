"""Inputs for portfolio construction and attribution, read from the database."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd
from sqlalchemy import Engine, text

from ballast.fixed_income.store import par_curve_on, valuation_calendar

TRADING_DAYS = 252
DEFAULT_SEGMENTS = {"equity": "equity", "treasury": "rates", "swap": "rates", "etf": "equity"}


class ConstructionDataError(RuntimeError):
    pass


@dataclass(frozen=True)
class Universe:
    as_of: date
    symbols: list[str]
    instrument_ids: list[int]
    mean: np.ndarray                 # annualized historical mean of daily simple returns
    cov: np.ndarray                  # annualized sample covariance
    current_weights: np.ndarray      # today's weights among these assets
    risk_free: float                 # annual, simple
    lookback_days: int
    start: date


def load_universe(engine: Engine, portfolio: str, lookback: int) -> Universe:
    """Stocks and ETFs with total-return history: the assets an allocation can be built from.
    Bonds, options and swaps are overlays and are left out of the optimization."""
    with engine.connect() as conn:
        px = pd.read_sql(
            text(
                """
                SELECT p.price_date, i.symbol, i.instrument_id, p.adj_close
                FROM prices p JOIN instruments i USING (instrument_id)
                WHERE i.asset_class IN ('equity', 'etf') AND p.adj_close IS NOT NULL
                """
            ),
            conn,
        )
        held = pd.read_sql(
            text(
                """
                SELECT h.symbol, h.market_value, h.as_of_date
                FROM holdings_latest h JOIN portfolios p USING (portfolio_id)
                WHERE p.name = :n AND h.asset_class IN ('equity', 'etf')
                """
            ),
            conn, params={"n": portfolio},
        )
    if px.empty or held.empty:
        raise ConstructionDataError("Need prices and holdings. Run `ballast bootstrap` first.")
    ids = dict(zip(px.symbol, px.instrument_id))
    calendar = valuation_calendar(engine)
    prices = px.pivot(index="price_date", columns="symbol", values="adj_close").astype(float).reindex(calendar).ffill()
    returns = (prices / prices.shift(1) - 1).dropna(how="any").tail(lookback)
    if len(returns) < 120:
        raise ConstructionDataError(f"Only {len(returns)} days of complete return history; need at least 120")
    symbols = list(returns.columns)
    mv = held.set_index("symbol").market_value.astype(float).reindex(symbols).fillna(0.0)
    as_of = held.as_of_date.iloc[0]

    found = par_curve_on(engine, as_of)
    if found is None:
        raise ConstructionDataError("No recent Treasury curve. Run `ballast load-rates`.")
    risk_free = float(np.exp(found[1].build().zero_rate(0.25)) - 1.0)    # 3-month T-bill, annual simple

    return Universe(
        as_of=as_of, symbols=symbols, instrument_ids=[int(ids[s]) for s in symbols],
        mean=returns.mean().to_numpy() * TRADING_DAYS,
        cov=returns.cov().to_numpy() * TRADING_DAYS,
        current_weights=(mv / mv.sum()).to_numpy(),
        risk_free=risk_free, lookback_days=len(returns), start=returns.index[0],
    )


@dataclass(frozen=True)
class AttributionInputs:
    days: list[date]
    segments: list[str]
    port_weights: np.ndarray      # days x segments
    port_returns: np.ndarray      # days x segments
    bench_weights: np.ndarray     # segments (constant, daily rebalanced)
    bench_returns: np.ndarray     # days x segments
    portfolio_daily: np.ndarray   # days
    benchmark_daily: np.ndarray   # days


def parse_benchmark(text_: str) -> dict[str, float]:
    """'SPY:0.6,TLT:0.4' -> {'SPY': 0.6, 'TLT': 0.4} (weights must sum to 1)."""
    out = {}
    for part in text_.split(","):
        sym, w = part.split(":")
        out[sym.strip().upper()] = float(w)
    if abs(sum(out.values()) - 1.0) > 1e-9:
        raise ConstructionDataError(f"benchmark weights sum to {sum(out.values()):.4f}, not 1")
    return out


def load_attribution_inputs(engine: Engine, portfolio: str, benchmark: dict[str, float],
                            since: date | None = None, min_days: int = 20) -> AttributionInputs:
    with engine.connect() as conn:
        pnl = pd.read_sql(
            text(
                """
                SELECT p.as_of_date, i.symbol, i.asset_class, i.segment, u.symbol AS underlying,
                       u.asset_class AS u_class, u.segment AS u_segment,
                       p.prev_market_value, p.daily_pnl
                FROM position_pnl_daily p
                JOIN portfolios pf USING (portfolio_id)
                JOIN instruments i ON i.instrument_id = p.instrument_id
                LEFT JOIN option_terms o ON o.instrument_id = i.instrument_id
                LEFT JOIN instruments u ON u.instrument_id = o.underlying_id
                WHERE pf.name = :n
                """
            ),
            conn, params={"n": portfolio},
        )
        seg_rows = conn.execute(text("SELECT symbol, asset_class, segment FROM instruments")).all()
        px = pd.read_sql(
            text(
                """
                SELECT p.price_date, i.symbol, p.adj_close FROM prices p JOIN instruments i USING (instrument_id)
                WHERE i.symbol = ANY(:s) AND p.adj_close IS NOT NULL
                """
            ),
            conn, params={"s": list(benchmark)},
        )
    if pnl.empty:
        raise ConstructionDataError("No P&L history. Run `ballast bootstrap` first.")
    missing = set(benchmark) - set(px.symbol)
    if missing:
        raise ConstructionDataError(f"No prices for benchmark constituents: {sorted(missing)}")

    def segment_of(cls, seg, und_cls=None, und_seg=None):
        # SQL NULLs arrive as None or float NaN, and NaN is truthy, so test for a real string.
        if isinstance(seg, str) and seg:
            return seg
        if cls == "option":
            return segment_of(und_cls, und_seg)
        return DEFAULT_SEGMENTS.get(cls, cls)

    pnl["seg"] = [segment_of(r.asset_class, r.segment, r.u_class, r.u_segment) for r in pnl.itertuples()]
    lost = pnl.loc[[not isinstance(x, str) for x in pnl.seg], "symbol"].unique()
    if len(lost):
        raise ConstructionDataError(f"Could not assign a segment to: {sorted(lost)}")
    instrument_seg = {s: segment_of(c, g) for s, c, g in seg_rows}
    for col in ("prev_market_value", "daily_pnl"):
        pnl[col] = pnl[col].astype(float)

    grouped = pnl.groupby(["as_of_date", "seg"])[["prev_market_value", "daily_pnl"]].sum().unstack("seg").fillna(0.0)
    capital = grouped["prev_market_value"]
    gains = grouped["daily_pnl"]
    capital = capital[capital.sum(axis=1) > 0]
    if since is not None:
        capital = capital.loc[[d for d in capital.index if d >= since]]

    prices = px.pivot(index="price_date", columns="symbol", values="adj_close").astype(float)
    prices = prices.reindex(sorted(set(prices.index) | set(capital.index))).ffill()
    bench_ret = (prices / prices.shift(1) - 1).reindex(capital.index)
    days = [d for d in capital.index if not bench_ret.loc[d].isna().any()]
    if len(days) < min_days:
        raise ConstructionDataError("Not enough overlapping days between the portfolio and the benchmark")

    bench_segments = {instrument_seg.get(s, "equity") for s in benchmark}
    segments = sorted(set(capital.columns) | bench_segments)
    cap = capital.reindex(index=days, columns=segments).fillna(0.0)
    gain = gains.reindex(index=days, columns=segments).fillna(0.0)
    total_cap = cap.sum(axis=1).to_numpy()
    w = cap.to_numpy() / total_cap[:, None]
    held = cap.to_numpy() > 0
    with np.errstate(invalid="ignore", divide="ignore"):
        r = np.where(held, gain.to_numpy() / cap.to_numpy(), np.nan)

    W = np.zeros(len(segments))
    b = np.full((len(days), len(segments)), np.nan)
    for j, seg in enumerate(segments):
        members = {s: wt for s, wt in benchmark.items() if instrument_seg.get(s, "equity") == seg}
        W[j] = sum(members.values())
        if W[j] > 0:
            b[:, j] = sum(wt * bench_ret.loc[days, s].to_numpy() for s, wt in members.items()) / W[j]
    # Conventions for segments held by only one side, so effects stay meaningful:
    # held but not in the benchmark -> segment benchmark return = own return (pure allocation);
    # in the benchmark but not held -> own return = segment benchmark return (pure allocation).
    b = np.where(np.isnan(b), r, b)
    r = np.where(np.isnan(r), b, r)
    r, b = np.nan_to_num(r), np.nan_to_num(b)
    return AttributionInputs(days, segments, w, r, W, b, (w * r).sum(axis=1), b @ W)
