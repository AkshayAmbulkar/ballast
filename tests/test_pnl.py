"""Positions and P&L: a hand-worked example, an accounting identity, and a
cross-check of the SQL views against the independent Python simulation."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text

from ballast.portfolio.pnl import reference_pnl
from conftest import D, insert_action, insert_instrument, insert_portfolio, insert_prices, insert_trade

D1, D2, D3, D4, D5 = (date(2024, 1, d) for d in (2, 3, 4, 5, 8))


@pytest.fixture
def worked_example(db):
    """
    One stock, five trading days, with a split, a dividend, fees and a partial sale.

    Day  Close   Event                                   Shares
    D1   100.00  BUY 100 @ 100.50, fee 1.00               100
    D2   102.00                                           100
    D3    51.50  2-for-1 split (ex-date D3)               200
    D4    52.00  dividend 0.50/share (ex-date D4)         200
    D5    53.00  SELL 100 @ 52.80, fee 1.00               100

    Expected daily P&L (MV change - trade cash + dividends):
    D1  10,000 - 0      - 10,051            = -51
    D2  10,200 - 10,000                     = 200
    D3  10,300 - 10,200                     = 100
    D4  10,400 - 10,300 + 200 x 0.50        = 200
    D5   5,300 - 10,400 - (-5,280 + 1)      = 179
                                     Total  = 628

    Check by economics: cost 50.25/share after the split (100.50 / 2).
    Sold 100 @ 52.80 -> +255.00; 100 held @ 53.00 -> +275.00;
    fees -2.00; dividends +100.00. Total 628.00.
    """
    with db.begin() as conn:
        iid = insert_instrument(conn, "XYZ")
        insert_prices(conn, iid, {D1: "100", D2: "102", D3: "51.5", D4: "52", D5: "53"})
        insert_action(conn, iid, D3, "split", "2")
        insert_action(conn, iid, D4, "cash_dividend", "0.50")
        pid = insert_portfolio(conn)
        insert_trade(conn, pid, iid, "T1", D1, "100", "100.50", "1.00")
        insert_trade(conn, pid, iid, "T2", D5, "-100", "52.80", "1.00")
    return db, pid, iid


def test_worked_example_daily_pnl(worked_example):
    engine, pid, _ = worked_example
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT as_of_date, quantity, market_value, dividend_income, daily_pnl "
                "FROM position_pnl_daily WHERE portfolio_id = :p ORDER BY as_of_date"
            ),
            {"p": pid},
        ).all()

    assert [r.as_of_date for r in rows] == [D1, D2, D3, D4, D5]
    assert [r.quantity for r in rows] == [D(100), D(100), D(200), D(200), D(100)]
    assert [r.market_value for r in rows] == [D(10000), D(10200), D(10300), D(10400), D(5300)]
    assert [r.dividend_income for r in rows] == [D(0), D(0), D(0), D(100), D(0)]
    assert [r.daily_pnl for r in rows] == [D(-51), D(200), D(100), D(200), D(179)]


def test_worked_example_cumulative(worked_example):
    engine, pid, _ = worked_example
    with engine.connect() as conn:
        cum = conn.execute(
            text("SELECT cumulative_pnl FROM portfolio_pnl_daily WHERE portfolio_id = :p ORDER BY as_of_date DESC LIMIT 1"),
            {"p": pid},
        ).scalar_one()
    assert cum == D(628)


def test_accounting_identity(worked_example):
    """Lifetime P&L = final market value - net cash paid for trades + dividends received."""
    engine, pid, _ = worked_example
    with engine.connect() as conn:
        lifetime_pnl, final_mv, cash, divs = conn.execute(
            text(
                """
                SELECT SUM(daily_pnl),
                       (SELECT market_value FROM position_pnl_daily WHERE portfolio_id = :p
                        ORDER BY as_of_date DESC LIMIT 1),
                       SUM(trade_cash),
                       SUM(dividend_income)
                FROM position_pnl_daily WHERE portfolio_id = :p
                """
            ),
            {"p": pid},
        ).one()
    assert lifetime_pnl == final_mv - cash + divs


def test_sql_matches_independent_simulation(worked_example):
    engine, pid, _ = worked_example
    with engine.connect() as conn:
        sql_rows = conn.execute(
            text(
                "SELECT instrument_id, as_of_date, quantity, market_value, trade_cash, dividend_income, daily_pnl "
                "FROM position_pnl_daily WHERE portfolio_id = :p ORDER BY instrument_id, as_of_date"
            ),
            {"p": pid},
        ).all()
    ref = reference_pnl(engine, pid)
    assert len(sql_rows) == len(ref)
    for s, r in zip(sql_rows, ref):
        assert (s.instrument_id, s.as_of_date) == (r.instrument_id, r.as_of_date)
        assert s.quantity == r.quantity
        assert s.market_value == r.market_value
        assert s.trade_cash == r.trade_cash
        assert s.dividend_income == r.dividend_income
        assert s.daily_pnl == r.daily_pnl


def test_short_position_pays_dividend(db):
    with db.begin() as conn:
        iid = insert_instrument(conn, "SHRT")
        insert_prices(conn, iid, {D1: "50", D2: "50"})
        insert_action(conn, iid, D2, "cash_dividend", "1")
        pid = insert_portfolio(conn)
        insert_trade(conn, pid, iid, "S1", D1, "-10", "50")
    with db.connect() as conn:
        pnl = conn.execute(
            text("SELECT daily_pnl FROM position_pnl_daily WHERE portfolio_id = :p AND as_of_date = :d"),
            {"p": pid, "d": D2},
        ).scalar_one()
    assert pnl == D(-10)  # short 10 shares owes the 1.00 dividend on each


def test_holdings_latest_weights_sum_to_one(db):
    with db.begin() as conn:
        a = insert_instrument(conn, "AAA")
        b = insert_instrument(conn, "BBB")
        insert_prices(conn, a, {D1: "10", D2: "12"})
        insert_prices(conn, b, {D1: "20", D2: "18"})
        pid = insert_portfolio(conn)
        insert_trade(conn, pid, a, "A1", D1, "100", "10")
        insert_trade(conn, pid, b, "B1", D1, "50", "20")
    with db.connect() as conn:
        rows = conn.execute(
            text("SELECT symbol, market_value, weight FROM holdings_latest WHERE portfolio_id = :p ORDER BY symbol"),
            {"p": pid},
        ).all()
    assert [r.market_value for r in rows] == [D(1200), D(900)]
    assert sum(r.weight for r in rows) == pytest.approx(Decimal(1))


def test_trade_on_non_trading_day_is_captured_and_flagged(db):
    saturday = date(2024, 1, 6)
    with db.begin() as conn:
        iid = insert_instrument(conn, "WKND")
        insert_prices(conn, iid, {D4: "10", D5: "11"})
        pid = insert_portfolio(conn)
        insert_trade(conn, pid, iid, "W1", saturday, "10", "10")
    with db.connect() as conn:
        rows = conn.execute(
            text("SELECT as_of_date, quantity, daily_pnl FROM position_pnl_daily WHERE portfolio_id = :p"),
            {"p": pid},
        ).all()
        flagged = conn.execute(text("SELECT trade_ref FROM dq_trades_off_calendar")).scalars().all()
    # The Saturday trade lands in the next trading day's bucket, so no P&L is lost...
    assert [(r.as_of_date, r.quantity, r.daily_pnl) for r in rows] == [(D5, D(10), D(10))]
    # ...and the data-quality check still flags it for review.
    assert flagged == ["W1"]
