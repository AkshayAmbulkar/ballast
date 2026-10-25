"""Database side of derivatives: terms, market inputs, daily model marks, swap payments.

Options are marked each valuation day with:
* spot  = the underlying's as-traded close,
* sigma = realized volatility of the underlying over the last 63 trading days
          (about 3 months; daily log returns of the adjusted close, x sqrt(252)),
* r     = continuously compounded Treasury zero rate to expiry, from that day's curve,
* q     = trailing 12-month dividends / spot.
European options use Black-Scholes. American options use a 500-step CRR tree with
Hull's control variate: American tree - European tree (same tree) + Black-Scholes.
The tree's discretization noise appears in both tree prices and cancels, which
matters for Greeks: without it, gamma computed by bumping the tree can be off by
a factor of two (found on real NVDA data).
These are model marks, not market quotes: realized volatility usually differs
from the implied volatility that options actually trade at.

Swaps are valued on the Treasury zero curve as a proxy for the SOFR curve, with
realized SOFR fixings for the current period (see swaps.py).
"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

import numpy as np
import pandas as pd
from sqlalchemy import Engine, text

from ballast.derivatives.binomial import crr_price
from ballast.derivatives.black_scholes import bs_price
from ballast.derivatives.swaps import OISSwap, par_rate, payments, pv_per_100, realized_growth
from ballast.fixed_income.curve import DiscountCurve
from ballast.fixed_income.store import (
    MAX_CURVE_AGE_DAYS,
    load_par_yields,
    par_curve_from_row,
    valuation_calendar,
)

OPTION_SOURCE = "model:BS_realized_vol"
SWAP_SOURCE = "model:SOFR_OIS"
VOL_WINDOW = 63
TREE_STEPS = 500
OPTION_HISTORY_DAYS = 400


# ---------- terms ----------

@dataclass(frozen=True)
class OptionContract:
    instrument_id: int
    symbol: str
    underlying_id: int
    underlying: str
    option_type: str
    strike: float
    expiry: date
    american: bool


@dataclass(frozen=True)
class SwapContract:
    instrument_id: int
    symbol: str
    swap: OISSwap | None        # None until the fixed rate is set
    effective: date
    maturity: date
    months_per_period: int


def load_options(engine: Engine) -> list[OptionContract]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT o.instrument_id, i.symbol, o.underlying_id, u.symbol, o.option_type,
                       o.strike, o.expiry_date, o.exercise_style
                FROM option_terms o
                JOIN instruments i ON i.instrument_id = o.instrument_id
                JOIN instruments u ON u.instrument_id = o.underlying_id
                ORDER BY o.expiry_date, i.symbol
                """
            )
        ).all()
    return [OptionContract(r[0], r[1], r[2], r[3], r[4], float(r[5]), r[6], r[7] == "american") for r in rows]


def load_swaps(engine: Engine) -> list[SwapContract]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT s.instrument_id, i.symbol, s.effective_date, s.maturity_date, s.fixed_rate, s.months_per_period
                FROM swap_terms s JOIN instruments i USING (instrument_id)
                ORDER BY s.maturity_date
                """
            )
        ).all()
    out = []
    for iid, sym, eff, mat, k, m in rows:
        swap = OISSwap(eff, mat, float(k), int(m)) if k is not None else None
        out.append(SwapContract(iid, sym, swap, eff, mat, int(m)))
    return out


# ---------- market inputs ----------

class CurveBook:
    """Treasury curves by date, built lazily, looked up as of a day (at most a week old)."""

    def __init__(self, engine: Engine):
        self.wide = load_par_yields(engine)
        self.dates = list(self.wide.index)
        self._built: dict[date, DiscountCurve | None] = {}

    def on(self, day: date) -> DiscountCurve | None:
        i = bisect.bisect_right(self.dates, day) - 1
        while i >= 0 and (day - self.dates[i]).days <= MAX_CURVE_AGE_DAYS:
            cd = self.dates[i]
            if cd not in self._built:
                par = par_curve_from_row(self.wide.loc[cd])
                self._built[cd] = par.build() if par is not None else None
            if self._built[cd] is not None:
                return self._built[cd]
            i -= 1
        return None


def load_sofr_fixings(engine: Engine) -> dict[date, float]:
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT obs_date, value_pct FROM rate_observations WHERE series_id = 'SOFR' ORDER BY obs_date")
        ).all()
    return {d: float(v) / 100.0 for d, v in rows}


@dataclass
class UnderlyingHistory:
    closes: pd.Series          # as-traded close by date
    log_returns: pd.Series     # from adjusted close (total return)
    dividends: pd.Series       # as-traded cash per share by ex-date

    def spot(self, day: date) -> float | None:
        v = self.closes.get(day)
        return float(v) if v is not None else None

    def realized_vol(self, day: date, window: int = VOL_WINDOW) -> float | None:
        r = self.log_returns.loc[:day].tail(window)
        if len(r) < window:
            return None
        return float(r.std(ddof=1) * math.sqrt(252))

    def dividend_yield(self, day: date) -> float:
        s = self.spot(day)
        if not s:
            return 0.0
        recent = self.dividends[(self.dividends.index > day - timedelta(days=365)) & (self.dividends.index <= day)]
        return float(recent.sum()) / s


def load_underlying(engine: Engine, instrument_id: int) -> UnderlyingHistory:
    with engine.connect() as conn:
        px = pd.read_sql(
            text("SELECT price_date, close, adj_close FROM prices WHERE instrument_id = :i ORDER BY price_date"),
            conn, params={"i": instrument_id},
        )
        dv = pd.read_sql(
            text("SELECT ex_date, value FROM corporate_actions WHERE instrument_id = :i AND action_type = 'cash_dividend'"),
            conn, params={"i": instrument_id},
        )
    closes = pd.Series(px.close.astype(float).values, index=list(px.price_date))
    adj = pd.Series(px.adj_close.astype(float).values, index=list(px.price_date)).dropna()
    log_returns = np.log(adj / adj.shift(1)).dropna()
    dividends = pd.Series(dv.value.astype(float).values, index=list(dv.ex_date), dtype=float).sort_index()
    return UnderlyingHistory(closes, log_returns, dividends)


# ---------- option marks ----------

def years_to_expiry(day: date, expiry: date) -> float:
    return (expiry - day).days / 365.0


def option_value(c: OptionContract, S: float, T: float, r: float, q: float, sigma: float,
                 steps: int = TREE_STEPS) -> float:
    if T <= 0:
        return max(S - c.strike, 0.0) if c.option_type == "call" else max(c.strike - S, 0.0)
    european = bs_price(c.option_type, S, c.strike, T, r, sigma, q)
    if not c.american:
        return european
    american_tree = crr_price(c.option_type, S, c.strike, T, r, sigma, steps=steps, q=q, american=True)
    european_tree = crr_price(c.option_type, S, c.strike, T, r, sigma, steps=steps, q=q, american=False)
    return american_tree - european_tree + european


@dataclass(frozen=True)
class OptionInputs:
    spot: float
    T: float
    r: float
    q: float
    sigma: float


def option_inputs(c: OptionContract, u: UnderlyingHistory, curves: CurveBook, day: date) -> OptionInputs | None:
    S = u.spot(day)
    sigma = u.realized_vol(day)
    curve = curves.on(day)
    if S is None or sigma is None or curve is None:
        return None
    T = years_to_expiry(day, c.expiry)
    r = float(curve.zero_rate(max(T, 1 / 365))) if T > 0 else 0.0
    return OptionInputs(S, T, r, u.dividend_yield(day), sigma)


def price_options(engine: Engine) -> dict[str, int]:
    options = load_options(engine)
    if not options:
        return {}
    days = valuation_calendar(engine)
    curves = CurveBook(engine)
    histories: dict[int, UnderlyingHistory] = {}
    result = {}
    for c in options:
        u = histories.setdefault(c.underlying_id, load_underlying(engine, c.underlying_id))
        rows = []
        for day in days:
            if day > c.expiry or (c.expiry - day).days > OPTION_HISTORY_DAYS:
                continue
            inp = option_inputs(c, u, curves, day)
            if inp is None:
                continue
            value = option_value(c, inp.spot, inp.T, inp.r, inp.q, inp.sigma)
            rows.append({"i": c.instrument_id, "d": day, "c": Decimal(f"{value:.6f}"), "s": OPTION_SOURCE})
        _upsert_prices(engine, rows)
        result[c.symbol] = len(rows)
    return result


# ---------- swap marks ----------

def resolve_par_rates(engine: Engine) -> list[str]:
    """Set the fixed rate of any swap booked 'at market' to its par rate on the effective date."""
    curves = CurveBook(engine)
    done = []
    for s in load_swaps(engine):
        if s.swap is not None:
            continue
        curve = curves.on(s.effective)
        if curve is None:
            continue
        k = par_rate(OISSwap(s.effective, s.maturity, 0.0, s.months_per_period), curve, s.effective)
        with engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE swap_terms SET fixed_rate = :k, fixed_rate_source = :src WHERE instrument_id = :i"
                ),
                {"k": Decimal(f"{k:.10f}"), "src": f"par on {s.effective} (UST curve)", "i": s.instrument_id},
            )
        done.append(s.symbol)
    return done


def swap_mark(swap: OISSwap, curve: DiscountCurve, fixings: dict[date, float], day: date) -> float:
    value = pv_per_100(swap, curve, day, realized_growth(swap, fixings, day))
    return 0.0 if abs(value) < 5e-7 else value


def price_swaps(engine: Engine) -> dict[str, int]:
    resolve_par_rates(engine)
    swaps = [s for s in load_swaps(engine) if s.swap is not None]
    if not swaps:
        return {}
    days = valuation_calendar(engine)
    curves = CurveBook(engine)
    fixings = load_sofr_fixings(engine)
    last_fixing = max(fixings) if fixings else None
    result = {}
    for s in swaps:
        rows = []
        for day in days:
            if day < s.effective or day >= s.maturity:
                continue
            curve = curves.on(day)
            if curve is None:
                continue
            try:
                value = swap_mark(s.swap, curve, fixings, day)
            except ValueError:
                continue   # SOFR fixings not yet available for this day
            rows.append({"i": s.instrument_id, "d": day, "c": Decimal(f"{value:.6f}"), "s": SWAP_SOURCE})
        _upsert_prices(engine, rows)
        pays = payments(s.swap, fixings, last_fixing + timedelta(days=1)) if last_fixing else []
        with engine.begin() as conn:
            for pay_date, amount in pays:
                conn.execute(
                    text(
                        """
                        INSERT INTO corporate_actions (instrument_id, ex_date, action_type, value, source)
                        VALUES (:i, :d, 'swap_payment', :v, 'swap_terms')
                        ON CONFLICT (instrument_id, ex_date, action_type) DO UPDATE
                        SET value = EXCLUDED.value, loaded_at = now()
                        """
                    ),
                    {"i": s.instrument_id, "d": pay_date, "v": Decimal(f"{amount:.8f}")},
                )
        result[s.symbol] = len(rows)
    return result


def _upsert_prices(engine: Engine, rows: list[dict]) -> None:
    if not rows:
        return
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO prices (instrument_id, price_date, close, source)
                VALUES (:i, :d, :c, :s)
                ON CONFLICT (instrument_id, price_date) DO UPDATE
                SET close = EXCLUDED.close, source = EXCLUDED.source, loaded_at = now()
                """
            ),
            rows,
        )
