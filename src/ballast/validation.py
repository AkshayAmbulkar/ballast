"""Checks of Ballast's models against independently published figures.

* SOFR compounding: Ballast's 30-day average SOFR vs the New York Fed's published averages.
* ETF rate sensitivity: TLT's empirical duration (a regression on Treasury yields), to set
  beside the fund's published duration.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd
from sqlalchemy import Engine, text

from ballast.derivatives.sofr import FixingError, sofr_average
from ballast.derivatives.store import load_sofr_fixings
from ballast.fixed_income.sensitivity import EmpiricalDuration, empirical_duration
from ballast.fixed_income.store import etf_rate_sensitivity_data


@dataclass(frozen=True)
class SofrCheck:
    comparison: pd.DataFrame        # published_on, nyfed_pct, ballast_pct, diff_bp

    @property
    def exact_matches(self) -> int:
        """Days where Ballast's value equals the published one at the published 5 decimals."""
        df = self.comparison
        return int((df.ballast_pct.round(5) == df.nyfed_pct.round(5)).sum())

    @property
    def largest_diff_bp(self) -> float:
        return float(self.comparison.diff_bp.abs().max())


def sofr_average_check(engine: Engine) -> SofrCheck | None:
    """None when SOFR fixings or the published averages are not loaded, or do not overlap."""
    fixings = load_sofr_fixings(engine)
    with engine.connect() as conn:
        published = conn.execute(
            text("SELECT obs_date, value_pct FROM rate_observations WHERE series_id = 'SOFR30DAYAVG' ORDER BY obs_date")
        ).all()
    if not fixings or not published:
        return None
    first = min(fixings)
    rows = []
    for pub, value in published:
        if (pub - first).days < 31:
            continue
        try:
            ours = sofr_average(fixings, pub) * 100
        except FixingError:
            continue
        rows.append((pub, float(value), ours))
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=["published_on", "nyfed_pct", "ballast_pct"])
    df["diff_bp"] = (df.ballast_pct - df.nyfed_pct) * 100
    return SofrCheck(df)


@dataclass(frozen=True)
class DurationCheck:
    symbol: str
    series: str
    first: pd.Series                # day, close, adj_close, value_pct
    last: pd.Series
    estimate: EmpiricalDuration

    @property
    def yield_change(self) -> float:
        return (self.last.value_pct - self.first.value_pct) / 100

    @property
    def price_return(self) -> float:
        return self.last.close / self.first.close - 1

    @property
    def total_return(self) -> float:
        return self.last.adj_close / self.first.adj_close - 1


def etf_duration_check(engine: Engine, symbol: str = "TLT", series: str = "DGS20",
                       since: date | None = None) -> DurationCheck | None:
    """None when fewer than 21 overlapping days are loaded."""
    df = etf_rate_sensitivity_data(engine, symbol, series, since)
    if len(df) < 21:
        return None
    returns = df.adj_close.pct_change().to_numpy()[1:]
    dy = (df.value_pct.diff() / 100).to_numpy()[1:]
    return DurationCheck(symbol.upper(), series, df.iloc[0], df.iloc[-1], empirical_duration(returns, dy))
