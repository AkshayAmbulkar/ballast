"""Historical stress tests: replay a real market episode on today's portfolio.

For each episode, every risk factor's actual move between the start and end dates
(each equity's total return, each Treasury par yield's change) is applied to the
current portfolio as an instantaneous shock, with full revaluation. Options are
repriced directly rather than from the VaR price grid, because stress moves can
be larger than the grid covers.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np

from ballast.risk.market import MarketData
from ballast.risk.snapshot import Snapshot


@dataclass(frozen=True)
class Episode:
    name: str
    start: date
    end: date
    description: str


EPISODES = (
    Episode("rates_2022", date(2022, 1, 3), date(2022, 12, 30),
            "2022: fastest Fed hiking cycle in decades; stocks and bonds fell together"),
    Episode("svb_2023", date(2023, 3, 8), date(2023, 3, 13),
            "Silicon Valley Bank failure: 2-year yield collapsed, bank stocks fell"),
    Episode("yen_carry_2024", date(2024, 7, 31), date(2024, 8, 5),
            "Yen carry-trade unwind: global equity selloff on 5 Aug 2024"),
    Episode("deepseek_2025", date(2025, 1, 24), date(2025, 1, 27),
            "DeepSeek: NVIDIA fell about 17% in one day"),
    Episode("tariffs_2025", date(2025, 4, 2), date(2025, 4, 8),
            "U.S. tariff announcement of 2 Apr 2025 and the selloff that followed"),
)


@dataclass(frozen=True)
class StressResult:
    episode: Episode
    start_used: date
    end_used: date
    pnl: dict[str, float]          # by asset class, plus "total"
    spy_return: float | None
    ten_year_change_bp: float | None


def _as_of(index, d: date):
    eligible = [x for x in index if x <= d]
    return eligible[-1] if eligible else None


def run_episode(md: MarketData, snap: Snapshot, ep: Episode) -> StressResult | None:
    s, e = _as_of(md.calendar, ep.start), _as_of(md.calendar, ep.end)
    if s is None or e is None or s >= e or s < md.calendar[0]:
        return None
    start_px, end_px = md.adj_close.loc[s], md.adj_close.loc[e]
    if start_px.isna().any() or end_px.isna().any():
        return None
    returns = (end_px / start_px - 1.0).reindex(md.equity_symbols).to_numpy(dtype=float)
    shifts = (md.par.loc[e] - md.par.loc[s]).to_numpy(dtype=float)
    if np.isnan(shifts).any():
        return None
    eq = returns[None, :]
    pnl = {"equity": float((eq @ snap.stock_values)[0])}
    rate = snap._rate_pnl(shifts[None, :])
    pnl["bonds"], pnl["swaps"] = float(rate["bonds"][0]), float(rate["swaps"][0])
    pnl["options"] = float(sum(o.pnl_direct(returns[md.equity_symbols.index(o.underlying)]) for o in snap.options))
    pnl["total"] = sum(pnl.values())
    spy = float(returns[md.equity_symbols.index("SPY")]) if "SPY" in md.equity_symbols else None
    ten = (float(shifts[md.tenor_months.index(120)]) / 1e-4) if 120 in md.tenor_months else None
    return StressResult(ep, s, e, pnl, spy, ten)


def run_stress(md: MarketData, snap: Snapshot, episodes=EPISODES) -> list[StressResult]:
    return [r for r in (run_episode(md, snap, ep) for ep in episodes) if r is not None]
