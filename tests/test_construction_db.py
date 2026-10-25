"""Attribution inputs from the database match a hand calculation."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text

from ballast.construction.attribution import link
from ballast.construction.data import load_attribution_inputs
from conftest import insert_instrument, insert_portfolio, insert_trade

D1, D2, D3 = date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)


def test_segment_returns_weights_and_benchmark(db):
    """Two stocks (equity segment, no explicit segment) and TLT (rates). Benchmark 60% SPY / 40% TLT."""
    closes = {
        "AAA": [100, 102, 101], "BBB": [50, 50, 51], "SPY": [400, 404, 402], "TLT": [90, 89, 90],
    }
    with db.begin() as conn:
        ids = {}
        for sym, px in closes.items():
            ids[sym] = insert_instrument(conn, sym, "equity" if sym in ("AAA", "BBB") else "etf")
            conn.execute(
                text("INSERT INTO prices (instrument_id, price_date, close, adj_close, source) VALUES (:i, :d, :c, :c, 't')"),
                [{"i": ids[sym], "d": d, "c": Decimal(c)} for d, c in zip((D1, D2, D3), px)],
            )
        conn.execute(text("UPDATE instruments SET segment = 'rates' WHERE symbol = 'TLT'"))
        pid = insert_portfolio(conn, "Book")
        insert_trade(conn, pid, ids["AAA"], "A", D1, "10", "100")      # 1,000
        insert_trade(conn, pid, ids["BBB"], "B", D1, "20", "50")       # 1,000
        insert_trade(conn, pid, ids["TLT"], "T", D1, "10", "90")       #   900

    a = load_attribution_inputs(db, "Book", {"SPY": 0.6, "TLT": 0.4}, min_days=1)
    assert a.days == [D2, D3] and a.segments == ["equity", "rates"]
    eq, rt = 0, 1
    # Day 2: equity capital 2,000 (P&L +20 + 0), rates 900 (P&L -10).
    assert a.port_weights[0, eq] == pytest.approx(2000 / 2900)
    assert a.port_returns[0, eq] == pytest.approx(20 / 2000)
    assert a.port_returns[0, rt] == pytest.approx(-10 / 900)
    assert a.bench_returns[0, eq] == pytest.approx(404 / 400 - 1)
    assert a.bench_returns[0, rt] == pytest.approx(89 / 90 - 1)
    assert list(a.bench_weights) == [0.6, 0.4]
    # Day 3: equity capital 1,020 + 1,000 (P&L -10 + 20), rates 890 (P&L +10).
    assert a.port_returns[1, eq] == pytest.approx(10 / 2020)
    # Linked effects add up to the compounded active return.
    res = link([{"w": a.port_weights[t], "r": a.port_returns[t], "W": a.bench_weights, "b": a.bench_returns[t]}
                for t in range(2)], a.segments)
    assert res.effects["total"].sum() == pytest.approx(res.portfolio_return - res.benchmark_return, abs=1e-14)
