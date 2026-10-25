"""Database side of fixed income: read CMT yields, store curves, price bonds.

Bond valuation calendar: bonds are marked on the same days as the rest of the
portfolio (every day that has an equity or ETF close), using the most recent
Treasury curve on or before that day. That keeps one calendar for the whole
portfolio, so totals never jump on days when only one market is open (for
example Columbus Day, when stocks trade but the bond market is closed).
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import numpy as np
import pandas as pd
from sqlalchemy import Engine, text

from ballast.fixed_income.bonds import Bond, price_from_curve
from ballast.fixed_income.risk import ParCurve

CURVE = "UST"
MODEL_PRICE_SOURCE = "model:UST_CMT"
MAX_CURVE_AGE_DAYS = 7
MIN_TENORS = 6
REQUIRED_TENORS_MONTHS = (3, 24, 120)   # the curve must be anchored at the short end, belly and long end


def load_par_yields(engine: Engine, start: date | None = None, end: date | None = None) -> pd.DataFrame:
    """CMT par yields as decimals: one row per date, one column per tenor in months."""
    sql = """
        SELECT o.obs_date, s.tenor_months, o.value_pct
        FROM rate_observations o
        JOIN rate_series s USING (series_id)
        WHERE s.curve = 'UST_CMT'
          AND (CAST(:start AS DATE) IS NULL OR o.obs_date >= :start)
          AND (CAST(:end AS DATE) IS NULL OR o.obs_date <= :end)
    """
    with engine.connect() as conn:
        df = pd.read_sql(text(sql), conn, params={"start": start, "end": end})
    if df.empty:
        return pd.DataFrame()
    wide = df.pivot(index="obs_date", columns="tenor_months", values="value_pct").sort_index()
    wide.index = pd.Index([d if isinstance(d, date) else d.date() for d in wide.index], name="obs_date")
    return wide.astype(float) / 100.0


def par_curve_from_row(row: pd.Series) -> ParCurve | None:
    """ParCurve from one date's yields, or None if too many tenors are missing."""
    row = row.dropna()
    tenors = tuple(int(t) for t in row.index)
    if len(tenors) < MIN_TENORS or any(t not in tenors for t in REQUIRED_TENORS_MONTHS):
        return None
    return ParCurve(tenors, tuple(float(v) for v in row.values))


def par_curve_on(engine: Engine, on: date) -> tuple[date, ParCurve] | None:
    """The most recent usable curve on or before `on` (within MAX_CURVE_AGE_DAYS)."""
    wide = load_par_yields(engine, end=on)
    for curve_date in reversed(wide.index):
        if (on - curve_date).days > MAX_CURVE_AGE_DAYS:
            return None
        par = par_curve_from_row(wide.loc[curve_date])
        if par is not None:
            return curve_date, par
    return None


def output_tenors_months(longest_months: int) -> list[int]:
    short = [m for m in (1, 3) if m < 6]
    return short + list(range(6, longest_months + 1, 6))


def build_curves(engine: Engine, rebuild: bool = False) -> int:
    """Bootstrap and store the curve for every date with CMT data. Returns dates built."""
    wide = load_par_yields(engine)
    if wide.empty:
        return 0
    with engine.connect() as conn:
        done = set(conn.execute(text("SELECT DISTINCT curve_date FROM zero_curves WHERE curve = :c"), {"c": CURVE}).scalars())

    rows = []
    built = 0
    for curve_date, row in wide.iterrows():
        if not rebuild and curve_date in done:
            continue
        par = par_curve_from_row(row)
        if par is None:
            continue
        curve = par.build()
        months = np.array(output_tenors_months(max(par.tenor_months)))
        t = months / 12.0
        dfs = curve.df(t)
        zc = curve.zero_rate(t)
        zb = curve.zero_rate(t, compounding=2)
        py = np.interp(t, par.tenors_years, par.par_yields)
        rows.extend(
            {"c": CURVE, "d": curve_date, "m": int(m), "df": float(a), "zc": float(b), "zb": float(z), "py": float(p)}
            for m, a, b, z, p in zip(months, dfs, zc, zb, py)
        )
        built += 1

    if rows:
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO zero_curves (curve, curve_date, tenor_months, discount_factor,
                                             zero_rate_cc, zero_rate_bey, par_yield)
                    VALUES (:c, :d, :m, :df, :zc, :zb, :py)
                    ON CONFLICT (curve, curve_date, tenor_months) DO UPDATE
                    SET discount_factor = EXCLUDED.discount_factor, zero_rate_cc = EXCLUDED.zero_rate_cc,
                        zero_rate_bey = EXCLUDED.zero_rate_bey, par_yield = EXCLUDED.par_yield,
                        built_at = now()
                    """
                ),
                rows,
            )
    return built


@dataclass(frozen=True)
class HeldBond:
    instrument_id: int
    symbol: str
    bond: Bond


def load_bonds(engine: Engine) -> list[HeldBond]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT i.instrument_id, i.symbol, b.coupon_rate, b.issue_date, b.maturity_date, b.coupon_frequency
                FROM instruments i JOIN bond_terms b USING (instrument_id)
                WHERE i.asset_class = 'treasury'
                ORDER BY b.maturity_date
                """
            )
        ).all()
    return [HeldBond(r[0], r[1], Bond(float(r[2]), r[3], r[4], int(r[5]))) for r in rows]


def valuation_calendar(engine: Engine) -> list[date]:
    """Days the portfolio is valued: every day with an equity or ETF close (falls back to curve days)."""
    with engine.connect() as conn:
        days = conn.execute(
            text(
                """
                SELECT DISTINCT p.price_date FROM prices p
                JOIN instruments i USING (instrument_id)
                WHERE i.asset_class IN ('equity', 'etf')
                ORDER BY 1
                """
            )
        ).scalars().all()
        if not days:
            days = conn.execute(
                text("SELECT DISTINCT obs_date FROM rate_observations WHERE series_id LIKE 'DGS%' ORDER BY 1")
            ).scalars().all()
    return list(days)


def price_bonds(engine: Engine) -> dict[str, int]:
    """Mark every bond off the curve on each valuation day, and record its coupons.

    Writes dirty prices into `prices` (source model:UST_CMT) and coupons into
    `corporate_actions`, so positions and P&L treat bonds like any other holding.
    Returns {symbol: number of prices written}.
    """
    bonds = load_bonds(engine)
    if not bonds:
        return {}
    days = valuation_calendar(engine)
    wide = load_par_yields(engine)
    if wide.empty or not days:
        return {b.symbol: 0 for b in bonds}

    curve_dates = list(wide.index)
    curves: dict[date, object] = {}

    def curve_for(day: date):
        i = bisect.bisect_right(curve_dates, day) - 1
        while i >= 0 and (day - curve_dates[i]).days <= MAX_CURVE_AGE_DAYS:
            cd = curve_dates[i]
            if cd not in curves:
                par = par_curve_from_row(wide.loc[cd])
                curves[cd] = par.build() if par is not None else None
            if curves[cd] is not None:
                return curves[cd]
            i -= 1
        return None

    result: dict[str, int] = {}
    last_day = days[-1]
    for held in bonds:
        b = held.bond
        price_rows = []
        for day in days:
            if day < b.issue_date or day >= b.maturity_date:
                continue
            curve = curve_for(day)
            if curve is None:
                continue
            dirty = price_from_curve(b, curve, day).dirty
            price_rows.append(
                {"i": held.instrument_id, "d": day, "c": Decimal(f"{dirty:.6f}"), "s": MODEL_PRICE_SOURCE}
            )
        coupon_rows = [
            {"i": held.instrument_id, "d": d, "v": Decimal(f"{b.coupon_amount:.8f}"), "s": "bond_terms"}
            for d in b.coupon_dates()
            if d <= last_day
        ]
        with engine.begin() as conn:
            if price_rows:
                conn.execute(
                    text(
                        """
                        INSERT INTO prices (instrument_id, price_date, close, source)
                        VALUES (:i, :d, :c, :s)
                        ON CONFLICT (instrument_id, price_date) DO UPDATE
                        SET close = EXCLUDED.close, source = EXCLUDED.source, loaded_at = now()
                        """
                    ),
                    price_rows,
                )
            if coupon_rows:
                conn.execute(
                    text(
                        """
                        INSERT INTO corporate_actions (instrument_id, ex_date, action_type, value, source)
                        VALUES (:i, :d, 'coupon', :v, :s)
                        ON CONFLICT (instrument_id, ex_date, action_type) DO UPDATE
                        SET value = EXCLUDED.value, source = EXCLUDED.source, loaded_at = now()
                        """
                    ),
                    coupon_rows,
                )
        result[held.symbol] = len(price_rows)
    return result


def etf_rate_sensitivity_data(engine: Engine, symbol: str, series_id: str, since: date | None) -> pd.DataFrame:
    """Aligned daily total-return index (adj_close), close and yield for one ETF and one FRED series."""
    with engine.connect() as conn:
        df = pd.read_sql(
            text(
                """
                SELECT p.price_date AS day, p.close, p.adj_close, o.value_pct
                FROM prices p
                JOIN instruments i USING (instrument_id)
                JOIN rate_observations o ON o.obs_date = p.price_date AND o.series_id = :sid
                WHERE i.symbol = :sym
                  AND p.adj_close IS NOT NULL
                  AND (CAST(:since AS DATE) IS NULL OR p.price_date >= :since)
                ORDER BY p.price_date
                """
            ),
            conn,
            params={"sym": symbol.upper(), "sid": series_id, "since": since},
        )
    return df.astype({"close": float, "adj_close": float, "value_pct": float})
