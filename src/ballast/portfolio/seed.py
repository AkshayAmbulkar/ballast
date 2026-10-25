"""Load the instrument master and the trade ledger from CSV files.

A blank price in trades.csv means "executed at that day's close". The close is
looked up in the prices table, so prices must be loaded before trades.
"""

from __future__ import annotations

import csv
from datetime import date
from decimal import Decimal
from pathlib import Path

from sqlalchemy import Engine, text

from ballast.config import SEED_DIR


class SeedError(RuntimeError):
    pass


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as f:
        return [{k: (v or "").strip() for k, v in row.items()} for row in csv.DictReader(f)]


def seed_instruments(engine: Engine, path: Path = SEED_DIR / "instruments.csv") -> int:
    rows = [
        {"symbol": r["symbol"].upper(), "name": r["name"], "asset_class": r["asset_class"]}
        for r in _read_csv(path)
    ]
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO instruments (symbol, name, asset_class)
                VALUES (:symbol, :name, :asset_class)
                ON CONFLICT (symbol) DO UPDATE
                SET name = EXCLUDED.name, asset_class = EXCLUDED.asset_class
                """
            ),
            rows,
        )
    return len(rows)


def seed_bond_terms(engine: Engine, path: Path = SEED_DIR / "bond_terms.csv") -> int:
    """Load coupon, issue and maturity for Treasury instruments (instruments must exist first)."""
    if not path.exists():
        return 0
    rows = _read_csv(path)
    with engine.begin() as conn:
        ids = dict(conn.execute(text("SELECT symbol, instrument_id FROM instruments")).all())
        params = []
        for line_no, r in enumerate(rows, start=2):
            symbol = r["symbol"].upper()
            if symbol not in ids:
                raise SeedError(f"{path.name} line {line_no}: unknown symbol {symbol}")
            params.append(
                {
                    "i": ids[symbol],
                    "c": Decimal(r["coupon_rate"]),
                    "iss": date.fromisoformat(r["issue_date"]),
                    "mat": date.fromisoformat(r["maturity_date"]),
                    "f": int(r.get("coupon_frequency") or 2),
                }
            )
        if params:
            conn.execute(
                text(
                    """
                    INSERT INTO bond_terms (instrument_id, coupon_rate, issue_date, maturity_date, coupon_frequency)
                    VALUES (:i, :c, :iss, :mat, :f)
                    ON CONFLICT (instrument_id) DO UPDATE
                    SET coupon_rate = EXCLUDED.coupon_rate, issue_date = EXCLUDED.issue_date,
                        maturity_date = EXCLUDED.maturity_date, coupon_frequency = EXCLUDED.coupon_frequency
                    """
                ),
                params,
            )
    return len(params)


def seed_option_terms(engine: Engine, path: Path = SEED_DIR / "option_terms.csv") -> int:
    if not path.exists():
        return 0
    rows = _read_csv(path)
    with engine.begin() as conn:
        ids = dict(conn.execute(text("SELECT symbol, instrument_id FROM instruments")).all())
        params = []
        for line_no, r in enumerate(rows, start=2):
            sym, und = r["symbol"].upper(), r["underlying"].upper()
            for s in (sym, und):
                if s not in ids:
                    raise SeedError(f"{path.name} line {line_no}: unknown symbol {s}")
            params.append(
                {
                    "i": ids[sym], "u": ids[und], "t": r["option_type"].lower(), "k": Decimal(r["strike"]),
                    "e": date.fromisoformat(r["expiry_date"]), "x": r["exercise_style"].lower(),
                    "m": int(r.get("multiplier") or 100),
                }
            )
        if params:
            conn.execute(
                text(
                    """
                    INSERT INTO option_terms (instrument_id, underlying_id, option_type, strike, expiry_date,
                                              exercise_style, multiplier)
                    VALUES (:i, :u, :t, :k, :e, :x, :m)
                    ON CONFLICT (instrument_id) DO UPDATE
                    SET underlying_id = EXCLUDED.underlying_id, option_type = EXCLUDED.option_type,
                        strike = EXCLUDED.strike, expiry_date = EXCLUDED.expiry_date,
                        exercise_style = EXCLUDED.exercise_style, multiplier = EXCLUDED.multiplier
                    """
                ),
                params,
            )
    return len(params)


def seed_swap_terms(engine: Engine, path: Path = SEED_DIR / "swap_terms.csv") -> int:
    """A blank fixed_rate means 'traded at market': `ballast price-swaps` sets it to the par rate."""
    if not path.exists():
        return 0
    rows = _read_csv(path)
    with engine.begin() as conn:
        ids = dict(conn.execute(text("SELECT symbol, instrument_id FROM instruments")).all())
        for line_no, r in enumerate(rows, start=2):
            sym = r["symbol"].upper()
            if sym not in ids:
                raise SeedError(f"{path.name} line {line_no}: unknown symbol {sym}")
            fixed = Decimal(r["fixed_rate"]) if r.get("fixed_rate") else None
            conn.execute(
                text(
                    """
                    INSERT INTO swap_terms (instrument_id, effective_date, maturity_date, fixed_rate,
                                            months_per_period, fixed_rate_source)
                    VALUES (:i, :e, :m, :k, :p, :src)
                    ON CONFLICT (instrument_id) DO UPDATE
                    SET effective_date = EXCLUDED.effective_date, maturity_date = EXCLUDED.maturity_date,
                        months_per_period = EXCLUDED.months_per_period,
                        -- keep a par rate already solved for, unless the CSV now gives one explicitly
                        fixed_rate = COALESCE(EXCLUDED.fixed_rate, swap_terms.fixed_rate),
                        fixed_rate_source = COALESCE(EXCLUDED.fixed_rate_source, swap_terms.fixed_rate_source)
                    """
                ),
                {
                    "i": ids[sym], "e": date.fromisoformat(r["effective_date"]),
                    "m": date.fromisoformat(r["maturity_date"]), "k": fixed,
                    "p": int(r.get("months_per_period") or 12), "src": "swap_terms.csv" if fixed is not None else None,
                },
            )
    return len(rows)


def seed_trades(engine: Engine, path: Path = SEED_DIR / "trades.csv") -> int:
    rows = _read_csv(path)
    with engine.begin() as conn:  # all-or-nothing: a bad row loads no trades
        instrument_ids = dict(conn.execute(text("SELECT symbol, instrument_id FROM instruments")).all())

        portfolio_ids: dict[str, int] = {}
        for name in sorted({r["portfolio"] for r in rows}):
            portfolio_ids[name] = conn.execute(
                text(
                    """
                    INSERT INTO portfolios (name) VALUES (:n)
                    ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name
                    RETURNING portfolio_id
                    """
                ),
                {"n": name},
            ).scalar_one()

        for line_no, r in enumerate(rows, start=2):  # line 1 is the header
            symbol = r["symbol"].upper()
            if symbol not in instrument_ids:
                raise SeedError(f"trades.csv line {line_no}: unknown symbol {symbol}")
            instrument_id = instrument_ids[symbol]
            trade_date = date.fromisoformat(r["trade_date"])

            if r["price"]:
                price = Decimal(r["price"])
                note = r["note"] or None
            else:
                price = conn.execute(
                    text("SELECT close FROM prices WHERE instrument_id = :i AND price_date = :d"),
                    {"i": instrument_id, "d": trade_date},
                ).scalar()
                if price is None:
                    raise SeedError(
                        f"trades.csv line {line_no}: no {symbol} close on {trade_date} to price the trade. "
                        "Load prices first, and check the date is a trading day."
                    )
                note = "; ".join(filter(None, [r["note"], "priced at close"]))

            conn.execute(
                text(
                    """
                    INSERT INTO trades (portfolio_id, instrument_id, trade_ref, trade_date, quantity, price, fees, note)
                    VALUES (:p, :i, :ref, :d, :q, :px, :fees, :note)
                    ON CONFLICT (portfolio_id, trade_ref) DO UPDATE
                    SET instrument_id = EXCLUDED.instrument_id, trade_date = EXCLUDED.trade_date,
                        quantity = EXCLUDED.quantity, price = EXCLUDED.price,
                        fees = EXCLUDED.fees, note = EXCLUDED.note
                    """
                ),
                {
                    "p": portfolio_ids[r["portfolio"]],
                    "i": instrument_id,
                    "ref": r["trade_ref"],
                    "d": trade_date,
                    "q": Decimal(r["quantity"]),
                    "px": price,
                    "fees": Decimal(r["fees"] or "0"),
                    "note": note,
                },
            )
    return len(rows)
