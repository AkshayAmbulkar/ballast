"""Risk-factor history and portfolio data, loaded once for VaR, backtests and stress tests.

Risk factors, on the portfolio's valuation calendar (equity trading days):
* one simple daily return per equity/ETF, from the adjusted close (total return);
* the daily change in each Treasury CMT par yield, as decimals, with each day
  using the latest curve published on or before it (at most a week old).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd
from sqlalchemy import Engine, text

from ballast.derivatives.store import (
    CurveBook,
    OptionContract,
    UnderlyingHistory,
    load_options,
    load_sofr_fixings,
    load_swaps,
    load_underlying,
)
from ballast.derivatives.swaps import OISSwap
from ballast.fixed_income.bonds import Bond
from ballast.fixed_income.store import MAX_CURVE_AGE_DAYS, load_bonds, load_par_yields, valuation_calendar


class RiskDataError(RuntimeError):
    pass


@dataclass
class MarketData:
    portfolio: str
    calendar: list[date]
    equity_symbols: list[str]
    equity_returns: pd.DataFrame          # calendar x symbols, simple returns
    adj_close: pd.DataFrame               # calendar x symbols, forward-filled
    tenor_months: tuple[int, ...]
    par: pd.DataFrame                     # calendar x tenors, decimals (as of each day)
    par_changes: pd.DataFrame             # calendar x tenors, daily changes
    positions: pd.DataFrame               # as_of_date, symbol, asset_class, quantity, close, market_value
    daily_pnl: pd.Series                  # portfolio P&L by date
    bonds: dict[str, Bond]
    swaps: dict[str, OISSwap]
    options: dict[str, OptionContract]
    fixings: dict[date, float]
    curves: CurveBook
    _underlyings: dict[int, UnderlyingHistory] = field(default_factory=dict)
    _factors: pd.DataFrame | None = None
    _engine: Engine | None = None

    def underlying(self, instrument_id: int) -> UnderlyingHistory:
        if instrument_id not in self._underlyings:
            self._underlyings[instrument_id] = load_underlying(self._engine, instrument_id)
        return self._underlyings[instrument_id]

    def factor_history(self, through: date, lookback: int) -> pd.DataFrame:
        """The last `lookback` complete days of factor changes up to and including `through`."""
        if self._factors is None:
            self._factors = pd.concat([self.equity_returns, self.par_changes], axis=1).dropna(how="any")
        return self._factors.loc[:through].tail(lookback)


def _as_of_par(wide: pd.DataFrame, calendar: list[date]) -> pd.DataFrame:
    combined = sorted(set(calendar) | set(wide.index))
    filled = wide.reindex(combined).ffill()
    last_obs = pd.Series(list(wide.index), index=list(wide.index), dtype=object).reindex(combined).ffill()
    out = filled.loc[calendar]
    stale = [(d - last_obs.loc[d]).days > MAX_CURVE_AGE_DAYS if isinstance(last_obs.loc[d], date) else True
             for d in calendar]
    out[np.array(stale)] = np.nan
    return out


def load_market_data(engine: Engine, portfolio: str) -> MarketData:
    with engine.connect() as conn:
        pid = conn.execute(text("SELECT portfolio_id FROM portfolios WHERE name = :n"), {"n": portfolio}).scalar()
        if pid is None:
            raise RiskDataError(f"No portfolio named {portfolio!r}")
        positions = pd.read_sql(
            text(
                """
                SELECT pd.as_of_date, i.symbol, i.asset_class, pd.quantity, pd.close, pd.market_value
                FROM positions_daily pd JOIN instruments i USING (instrument_id)
                WHERE pd.portfolio_id = :p AND pd.quantity <> 0
                ORDER BY pd.as_of_date, i.symbol
                """
            ),
            conn, params={"p": pid},
        )
        pnl = pd.read_sql(
            text("SELECT as_of_date, daily_pnl FROM portfolio_pnl_daily WHERE portfolio_id = :p ORDER BY as_of_date"),
            conn, params={"p": pid},
        )
        px = pd.read_sql(
            text(
                """
                SELECT p.price_date, i.symbol, p.adj_close
                FROM prices p JOIN instruments i USING (instrument_id)
                WHERE i.asset_class IN ('equity', 'etf') AND p.adj_close IS NOT NULL
                """
            ),
            conn,
        )
    if positions.empty:
        raise RiskDataError("The portfolio has no positions. Run `ballast bootstrap` first.")

    calendar = valuation_calendar(engine)
    adj = px.pivot(index="price_date", columns="symbol", values="adj_close").astype(float)
    adj = adj.reindex(calendar).ffill()
    returns = adj / adj.shift(1) - 1.0

    wide = load_par_yields(engine)
    if wide.empty:
        raise RiskDataError("No Treasury yields loaded. Run `ballast load-rates` first.")
    par = _as_of_par(wide, calendar)
    par_changes = par.diff()

    for col in ("quantity", "close", "market_value"):
        positions[col] = positions[col].astype(float)

    return MarketData(
        portfolio=portfolio,
        calendar=list(calendar),
        equity_symbols=list(adj.columns),
        equity_returns=returns,
        adj_close=adj,
        tenor_months=tuple(int(t) for t in par.columns),
        par=par,
        par_changes=par_changes,
        positions=positions,
        daily_pnl=pd.Series(pnl.daily_pnl.astype(float).values, index=list(pnl.as_of_date)),
        bonds={b.symbol: b.bond for b in load_bonds(engine)},
        swaps={s.symbol: s.swap for s in load_swaps(engine) if s.swap is not None},
        options={c.symbol: c for c in load_options(engine)},
        fixings=load_sofr_fixings(engine),
        curves=CurveBook(engine),
        _engine=engine,
    )
