"""Portfolio-level interest-rate analytics for bonds and SOFR swaps.

Every rate-sensitive position is reduced to one function, curve -> value per
unit, so the same bumps, key rates and scenarios apply to bonds and swaps alike
and their risks add up on the same 11 Treasury tenors.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Callable

import numpy as np
import pandas as pd
from sqlalchemy import Engine, text

from ballast.fixed_income.bonds import price_from_curve, yield_from_dirty_price, yield_risk
from ballast.fixed_income.curve import DiscountCurve
from ballast.fixed_income.risk import (
    SCENARIOS,
    ParCurve,
    curve_risk,
    historical_shifts,
    key_rate_dv01s,
    scenario_shifts,
)
from ballast.fixed_income.store import load_bonds, load_par_yields, par_curve_from_row, par_curve_on


def tenor_label(months: int) -> str:
    return f"{months}M" if months < 12 else f"{months // 12}Y"


@dataclass(frozen=True)
class RatePosition:
    symbol: str
    kind: str                                   # "bond" or "swap"
    quantity: float                             # units of 100 face / 100 notional
    value_fn: Callable[[DiscountCurve], float]  # value per unit
    details: dict


def _latest_holdings(engine: Engine, portfolio: str) -> tuple[date | None, dict[str, float]]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT h.as_of_date, h.symbol, h.quantity
                FROM holdings_latest h JOIN portfolios p USING (portfolio_id)
                WHERE p.name = :n AND h.asset_class IN ('treasury', 'swap')
                """
            ),
            {"n": portfolio},
        ).all()
        as_of = conn.execute(
            text("SELECT MAX(h.as_of_date) FROM holdings_latest h JOIN portfolios p USING (portfolio_id) WHERE p.name = :n"),
            {"n": portfolio},
        ).scalar()
    return as_of, {s: float(q) for _, s, q in rows}


def rate_positions(engine: Engine, portfolio: str, as_of: date, held: dict[str, float]) -> list[RatePosition]:
    from ballast.derivatives.store import load_sofr_fixings, load_swaps
    from ballast.derivatives.swaps import pv_per_100, realized_growth

    out: list[RatePosition] = []
    for b in load_bonds(engine):
        if b.symbol in held:
            out.append(
                RatePosition(b.symbol, "bond", held[b.symbol],
                             (lambda bond: lambda c: price_from_curve(bond, c, as_of).dirty)(b.bond), {"bond": b.bond})
            )
    swaps = [s for s in load_swaps(engine) if s.symbol in held and s.swap is not None]
    if swaps:
        fixings = load_sofr_fixings(engine)
        for s in swaps:
            growth = realized_growth(s.swap, fixings, as_of)
            out.append(
                RatePosition(s.symbol, "swap", held[s.symbol],
                             (lambda sw, g: lambda c: pv_per_100(sw, c, as_of, g))(s.swap, growth),
                             {"swap": s.swap, "growth": growth})
            )
    return out


@dataclass(frozen=True)
class RatesRiskReport:
    as_of: date
    curve_date: date
    bonds: pd.DataFrame
    swaps: pd.DataFrame
    key_rates: pd.DataFrame
    scenarios: pd.DataFrame
    total_dv01: float


def rates_risk_report(engine: Engine, portfolio: str) -> RatesRiskReport | None:
    from ballast.derivatives.swaps import par_rate

    as_of, held = _latest_holdings(engine, portfolio)
    if not held or as_of is None:
        return None
    found = par_curve_on(engine, as_of)
    if found is None:
        raise RuntimeError(f"No Treasury curve within a week of {as_of}. Run `ballast load-rates`.")
    curve_date, par = found
    curve = par.build()
    positions = rate_positions(engine, portfolio, as_of, held)

    bond_rows, swap_rows, kr_rows = [], [], []
    for p in positions:
        cr = curve_risk(p.value_fn, par)
        if p.kind == "bond":
            bond = p.details["bond"]
            px = price_from_curve(bond, curve, as_of)
            ytm = yield_from_dirty_price(bond, as_of, px.dirty)
            bond_rows.append(
                {
                    "symbol": p.symbol, "face": p.quantity * bond.face, "clean": px.clean, "accrued": px.accrued,
                    "dirty": px.dirty, "ytm_pct": ytm * 100, "market_value": p.quantity * px.dirty,
                    "mod_duration": yield_risk(bond, as_of, ytm).modified_duration,
                    "eff_duration": cr.effective_duration, "convexity": cr.effective_convexity,
                    "dv01_usd": p.quantity * cr.dv01,
                }
            )
        else:
            swap = p.details["swap"]
            swap_rows.append(
                {
                    "symbol": p.symbol, "notional": abs(p.quantity) * 100,
                    "direction": "receive fixed" if p.quantity > 0 else "pay fixed",
                    "fixed_pct": swap.fixed_rate * 100,
                    "par_today_pct": par_rate(swap, curve, as_of, p.details["growth"]) * 100,
                    "pv_per_100": cr.price, "market_value": p.quantity * cr.price,
                    "dv01_usd": p.quantity * cr.dv01,
                }
            )
        kr = key_rate_dv01s(p.value_fn, par) * p.quantity
        kr_rows.append({"symbol": p.symbol, **{tenor_label(m): v for m, v in zip(par.tenor_months, kr)}})

    key_rates = pd.DataFrame(kr_rows).set_index("symbol")
    key_rates.loc["Total"] = key_rates.sum()
    kr_total = key_rates.loc["Total"].to_numpy()

    def book_value(pc: ParCurve) -> float:
        c = pc.build()
        return sum(p.quantity * p.value_fn(c) for p in positions)

    base = book_value(par)
    named = [(name, desc, scenario_shifts(name, par)) for name, (desc, _) in SCENARIOS.items()]
    hist = historical_2022(engine, par)
    if hist is not None:
        named.append(hist)
    scenario_rows = [
        {
            "scenario": name, "description": desc,
            "full_reval_pnl": book_value(par.shifted(shifts)) - base,
            "key_rate_estimate": -float(np.dot(kr_total, shifts)),
        }
        for name, desc, shifts in named
    ]
    total_dv01 = float(sum(r["dv01_usd"] for r in bond_rows) + sum(r["dv01_usd"] for r in swap_rows))
    return RatesRiskReport(as_of, curve_date, pd.DataFrame(bond_rows), pd.DataFrame(swap_rows),
                           key_rates, pd.DataFrame(scenario_rows), total_dv01)


def historical_2022(engine: Engine, today: ParCurve) -> tuple[str, str, np.ndarray] | None:
    """Actual par-yield moves over calendar 2022 (the fastest hiking cycle in decades), applied to today's curve."""
    wide = load_par_yields(engine, date(2022, 1, 1), date(2022, 12, 31))
    if wide.empty:
        return None
    start, end = par_curve_from_row(wide.iloc[0]), par_curve_from_row(wide.iloc[-1])
    if start is None or end is None or start.tenor_months != today.tenor_months:
        return None
    desc = f"Actual moves {wide.index[0]} to {wide.index[-1]}"
    return "hist_2022", desc, historical_shifts(start, end)


def curve_snapshot(engine: Engine, on: date | None = None) -> tuple[date, pd.DataFrame] | None:
    if on is None:
        wide = load_par_yields(engine)
        if wide.empty:
            return None
        on = wide.index[-1]
    found = par_curve_on(engine, on)
    if found is None:
        return None
    curve_date, par = found
    curve = par.build()
    t = par.tenors_years
    df = pd.DataFrame(
        {
            "tenor": [tenor_label(m) for m in par.tenor_months],
            "par_pct": np.array(par.par_yields) * 100,
            "zero_bey_pct": curve.zero_rate(t, compounding=2) * 100,
            "zero_cc_pct": curve.zero_rate(t) * 100,
            "discount_factor": curve.df(t),
            "fwd_6m_bey_pct": 2 * (curve.df(t) / curve.df(t + 0.5) - 1) * 100,
        }
    )
    return curve_date, df
