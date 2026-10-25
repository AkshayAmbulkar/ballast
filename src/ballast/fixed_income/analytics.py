"""Portfolio-level fixed-income analytics: bond risk tables, scenarios, curve snapshot."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd
from sqlalchemy import Engine, text

from ballast.fixed_income.bonds import price_from_curve, yield_from_dirty_price, yield_risk
from ballast.fixed_income.risk import (
    SCENARIOS,
    ParCurve,
    curve_risk,
    historical_shifts,
    key_rate_dv01s,
    scenario_shifts,
)
from ballast.fixed_income.store import HeldBond, load_bonds, load_par_yields, par_curve_from_row, par_curve_on


def tenor_label(months: int) -> str:
    return f"{months}M" if months < 12 else f"{months // 12}Y"


@dataclass(frozen=True)
class BondHolding:
    held: HeldBond
    quantity: float     # number of 100-face units


def portfolio_bond_holdings(engine: Engine, portfolio: str) -> tuple[date | None, list[BondHolding]]:
    """Treasury positions on the portfolio's latest valuation date."""
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT h.as_of_date, h.symbol, h.quantity
                FROM holdings_latest h
                JOIN portfolios p ON p.portfolio_id = h.portfolio_id
                WHERE p.name = :n AND h.asset_class = 'treasury'
                """
            ),
            {"n": portfolio},
        ).all()
        as_of = conn.execute(
            text(
                "SELECT MAX(h.as_of_date) FROM holdings_latest h JOIN portfolios p USING (portfolio_id) WHERE p.name = :n"
            ),
            {"n": portfolio},
        ).scalar()
    by_symbol = {b.symbol: b for b in load_bonds(engine)}
    return as_of, [BondHolding(by_symbol[s], float(q)) for _, s, q in rows if s in by_symbol]


def _price_fn(held: HeldBond, on: date):
    return lambda curve: price_from_curve(held.bond, curve, on).dirty


@dataclass(frozen=True)
class BondRiskReport:
    as_of: date
    curve_date: date
    summary: pd.DataFrame
    key_rates: pd.DataFrame
    scenarios: pd.DataFrame


def bond_risk_report(engine: Engine, portfolio: str) -> BondRiskReport | None:
    as_of, holdings = portfolio_bond_holdings(engine, portfolio)
    if not holdings or as_of is None:
        return None
    found = par_curve_on(engine, as_of)
    if found is None:
        raise RuntimeError(f"No Treasury curve within a week of {as_of}. Run `ballast load-rates`.")
    curve_date, par = found
    curve = par.build()

    summary_rows, kr_rows = [], []
    for h in holdings:
        fn = _price_fn(h.held, as_of)
        px = price_from_curve(h.held.bond, curve, as_of)
        ytm = yield_from_dirty_price(h.held.bond, as_of, px.dirty)
        yr = yield_risk(h.held.bond, as_of, ytm)
        cr = curve_risk(fn, par)
        summary_rows.append(
            {
                "symbol": h.held.symbol,
                "face": h.quantity * h.held.bond.face,
                "clean": px.clean,
                "accrued": px.accrued,
                "dirty": px.dirty,
                "ytm_pct": ytm * 100,
                "mod_duration": yr.modified_duration,
                "eff_duration": cr.effective_duration,
                "convexity": cr.effective_convexity,
                "market_value": h.quantity * px.dirty,
                "dv01_usd": h.quantity * cr.dv01,
            }
        )
        kr = key_rate_dv01s(fn, par) * h.quantity
        kr_rows.append({"symbol": h.held.symbol, **{tenor_label(m): v for m, v in zip(par.tenor_months, kr)}})

    summary = pd.DataFrame(summary_rows)
    key_rates = pd.DataFrame(kr_rows).set_index("symbol")
    key_rates.loc["Total"] = key_rates.sum()
    kr_total = key_rates.loc["Total"].to_numpy()

    def portfolio_value(p: ParCurve) -> float:
        c = p.build()
        return sum(h.quantity * price_from_curve(h.held.bond, c, as_of).dirty for h in holdings)

    base_value = portfolio_value(par)
    scenario_rows = []
    named = [(name, desc, scenario_shifts(name, par)) for name, (desc, _) in SCENARIOS.items()]
    hist = historical_2022(engine, par)
    if hist is not None:
        named.append(hist)
    for name, desc, shifts in named:
        full = portfolio_value(par.shifted(shifts)) - base_value
        first_order = -float(np.dot(kr_total, shifts))
        scenario_rows.append(
            {"scenario": name, "description": desc, "full_reval_pnl": full, "key_rate_estimate": first_order}
        )
    return BondRiskReport(as_of, curve_date, summary, key_rates, pd.DataFrame(scenario_rows))


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
