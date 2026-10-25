"""Portfolio Greeks for option positions and delta-adjusted equity exposure.

European options use exact Black-Scholes Greeks. American options bump the same
control-variate tree pricer that produces the daily marks:
* delta, gamma: spot bumped by +/-1%
* vega: volatility +/-1 point, reported in $ per vol point
* theta: one calendar day forward, in $ per day
* rho: rates +/-1bp, reported in $ per 1% change in rates
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd
from sqlalchemy import Engine, text

from ballast.derivatives.black_scholes import bs_greeks
from ballast.derivatives.store import (
    CurveBook,
    OptionContract,
    load_options,
    load_underlying,
    option_inputs,
    option_value,
)


@dataclass(frozen=True)
class OptionGreeks:
    price: float
    delta: float          # per share
    gamma: float          # per share per $1
    vega_point: float     # $ per share per 1 vol point
    theta_day: float      # $ per share per calendar day
    rho_pct: float        # $ per share per 1% rate change


def numeric_greeks(c: OptionContract, S: float, T: float, r: float, q: float, sigma: float) -> OptionGreeks:
    """Exact Black-Scholes Greeks for European options; for American ones, bump and
    reprice the control-variate tree pricer that also produces the daily marks."""
    if not c.american and T > 0:
        g = bs_greeks(c.option_type, S, c.strike, T, r, sigma, q)
        return OptionGreeks(g.price, g.delta, g.gamma, g.vega / 100, g.theta / 365, g.rho / 100)
    v = lambda S_=S, T_=T, r_=r, s_=sigma: option_value(c, S_, T_, r_, q, s_)
    base = v()
    h = 0.01 * S
    up, down = v(S_=S + h), v(S_=S - h)
    one_day = 1 / 365
    theta = (v(T_=T - one_day) - base) if T > one_day else (v(T_=0.0) - base)
    return OptionGreeks(
        price=base,
        delta=(up - down) / (2 * h),
        gamma=(up - 2 * base + down) / (h * h),
        vega_point=(v(s_=sigma + 0.01) - v(s_=sigma - 0.01)) / 2,
        theta_day=theta,
        rho_pct=(v(r_=r + 1e-4) - v(r_=r - 1e-4)) / 2 * 100,
    )


@dataclass(frozen=True)
class GreeksReport:
    as_of: date
    options: pd.DataFrame
    exposure: pd.DataFrame


def greeks_report(engine: Engine, portfolio: str) -> GreeksReport | None:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT h.as_of_date, h.symbol, h.asset_class, h.quantity, h.close
                FROM holdings_latest h JOIN portfolios p USING (portfolio_id)
                WHERE p.name = :n
                """
            ),
            {"n": portfolio},
        ).all()
    held = {r[1]: (r[2], float(r[3]), float(r[4])) for r in rows}
    options = [c for c in load_options(engine) if c.symbol in held]
    if not options:
        return None
    as_of = rows[0][0]
    curves = CurveBook(engine)

    opt_rows = []
    for c in options:
        qty = held[c.symbol][1]
        u = load_underlying(engine, c.underlying_id)
        inp = option_inputs(c, u, curves, as_of)
        if inp is None:
            continue
        g = numeric_greeks(c, inp.spot, inp.T, inp.r, inp.q, inp.sigma)
        opt_rows.append(
            {
                "symbol": c.symbol, "underlying": c.underlying, "quantity": qty,
                "spot": inp.spot, "vol_pct": inp.sigma * 100, "days": round(inp.T * 365),
                "price": g.price, "market_value": qty * g.price,
                "delta": g.delta, "delta_shares": qty * g.delta,
                "gamma_shares": qty * g.gamma, "vega_usd": qty * g.vega_point,
                "theta_usd_day": qty * g.theta_day, "rho_usd": qty * g.rho_pct,
            }
        )
    opt = pd.DataFrame(opt_rows)

    exposure_rows = []
    for und in sorted(set(opt.underlying)) if not opt.empty else []:
        stock_qty = held.get(und, (None, 0.0, 0.0))[1]
        spot = float(opt.loc[opt.underlying == und, "spot"].iloc[0])
        option_delta = float(opt.loc[opt.underlying == und, "delta_shares"].sum())
        exposure_rows.append(
            {
                "underlying": und, "spot": spot, "stock_shares": stock_qty,
                "option_delta_shares": option_delta, "net_delta_shares": stock_qty + option_delta,
                "stock_exposure_usd": stock_qty * spot, "net_exposure_usd": (stock_qty + option_delta) * spot,
            }
        )
    return GreeksReport(as_of, opt, pd.DataFrame(exposure_rows))
