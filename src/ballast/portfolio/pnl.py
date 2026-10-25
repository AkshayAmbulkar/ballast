"""Independent reference implementation of daily position P&L.

The production numbers come from the SQL views in
db/migrations/004_positions_and_pnl.sql, which are set-based (window
functions and lateral joins). This module computes the same thing a
different way: it walks forward one trading day at a time like a back-office
position keeper. The test suite checks the two agree exactly, which guards
against errors in either one.

All arithmetic is Decimal, so agreement is exact rather than approximate.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import Engine, text

ZERO = Decimal(0)


@dataclass(frozen=True)
class PnlRow:
    portfolio_id: int
    instrument_id: int
    as_of_date: date
    quantity: Decimal
    close: Decimal
    market_value: Decimal
    trade_cash: Decimal
    dividend_income: Decimal
    daily_pnl: Decimal


def simulate_position(
    portfolio_id: int,
    instrument_id: int,
    trades: list[tuple[date, Decimal, Decimal, Decimal]],     # (trade_date, quantity, price, fees)
    closes: list[tuple[date, Decimal]],                        # (price_date, close), any order
    dividends: dict[date, Decimal],                            # ex_date -> as-traded cash per share
    splits: dict[date, Decimal],                               # ex_date -> ratio
) -> list[PnlRow]:
    """Walk one position forward day by day.

    On each trading day d (previous trading day p):
      1. credit dividends with ex-date in (p, d] on the shares held at p's close
      2. apply splits with ex-date in (p, d] to the share count
      3. apply trades dated in (p, d]
      4. mark to d's close; P&L = change in market value - trade cash + dividends
    """
    if not trades:
        return []
    trades = sorted(trades, key=lambda t: t[0])
    first_trade = trades[0][0]
    days = sorted(d for d, _ in closes if d >= first_trade)
    close_on = dict(closes)

    shares = ZERO
    prev_mv = ZERO
    prev_day: date | None = None
    out: list[PnlRow] = []

    for day in days:
        def in_bucket(x: date) -> bool:
            return x <= day and (prev_day is None or x > prev_day)

        dividend_income = ZERO
        if prev_day is not None:
            dividend_income = sum((v * shares for d, v in dividends.items() if in_bucket(d)), ZERO)
            for d, ratio in sorted(splits.items()):
                if in_bucket(d):
                    shares *= ratio

        trade_cash = ZERO
        for t_date, qty, price, fees in trades:
            if in_bucket(t_date):
                shares += qty
                trade_cash += qty * price + fees

        mv = shares * close_on[day]
        out.append(
            PnlRow(
                portfolio_id=portfolio_id,
                instrument_id=instrument_id,
                as_of_date=day,
                quantity=shares,
                close=close_on[day],
                market_value=mv,
                trade_cash=trade_cash,
                dividend_income=dividend_income,
                daily_pnl=mv - prev_mv - trade_cash + dividend_income,
            )
        )
        prev_mv = mv
        prev_day = day

    return out


def reference_pnl(engine: Engine, portfolio_id: int) -> list[PnlRow]:
    """Run the day-by-day simulation for every position in a portfolio, reading raw tables only."""
    with engine.connect() as conn:
        instrument_ids = [
            r[0]
            for r in conn.execute(
                text("SELECT DISTINCT instrument_id FROM trades WHERE portfolio_id = :p ORDER BY 1"),
                {"p": portfolio_id},
            )
        ]
        rows: list[PnlRow] = []
        for iid in instrument_ids:
            trades = [
                tuple(r)
                for r in conn.execute(
                    text(
                        "SELECT trade_date, quantity, price, fees FROM trades "
                        "WHERE portfolio_id = :p AND instrument_id = :i"
                    ),
                    {"p": portfolio_id, "i": iid},
                )
            ]
            closes = [
                tuple(r)
                for r in conn.execute(
                    text("SELECT price_date, close FROM prices WHERE instrument_id = :i"), {"i": iid}
                )
            ]
            actions = conn.execute(
                text("SELECT ex_date, action_type, value FROM corporate_actions WHERE instrument_id = :i"),
                {"i": iid},
            ).all()
            dividends = {d: v for d, t, v in actions if t == "cash_dividend"}
            splits = {d: v for d, t, v in actions if t == "split"}
            rows.extend(simulate_position(portfolio_id, iid, trades, closes, dividends, splits))
    return rows
