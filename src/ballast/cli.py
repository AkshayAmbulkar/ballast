"""Command-line entry point: `ballast <command>`. Run `ballast --help` for the list."""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

import pandas as pd
from sqlalchemy import Engine, text

from ballast.config import SEED_DIR, get_settings, secrets_file, security_warnings
from ballast.db import get_engine, migrate
from ballast.market_data.load import DEFAULT_HISTORY_START, load_prices, load_rates
from ballast.portfolio.seed import seed_instruments, seed_trades

DQ_VIEWS = ("dq_trades_off_calendar", "dq_trade_price_outliers", "dq_unexplained_price_jumps")


def cmd_migrate(engine: Engine, args) -> int:
    applied = migrate(engine)
    print(f"Applied {len(applied)} migration(s): {', '.join(applied)}" if applied else "Schema is up to date.")
    return 0


def cmd_seed_instruments(engine: Engine, args) -> int:
    print(f"Upserted {seed_instruments(engine, args.instruments_file)} instruments.")
    return 0


def cmd_load_prices(engine: Engine, args) -> int:
    counts = load_prices(engine, symbols=args.symbols, start=args.start, repair=not args.no_repair)
    for symbol, n in counts.items():
        print(f"  {symbol:<6} {n:>5} price rows")
    return 0


def cmd_load_rates(engine: Engine, args) -> int:
    key = get_settings().fred_api_key
    if not key:
        print(f"FRED_API_KEY is not set in {secrets_file()}, so rates were skipped. "
              "Get a free key at https://fred.stlouisfed.org/docs/api/api_key.html")
        return 0 if args.allow_missing_key else 1
    counts = load_rates(engine, key, series_ids=args.series, start=args.start)
    for series_id, n in counts.items():
        print(f"  {series_id:<7} {n:>5} observations")
    return 0


def cmd_seed_trades(engine: Engine, args) -> int:
    print(f"Upserted {seed_trades(engine, args.trades_file)} trades.")
    return 0


def cmd_check(engine: Engine, args) -> int:
    failures = 0
    with engine.connect() as conn:
        for view in DQ_VIEWS:
            df = pd.read_sql(text(f"SELECT * FROM {view}"), conn)
            status = "PASS" if df.empty else f"FAIL ({len(df)} rows)"
            print(f"  {view:<30} {status}")
            if not df.empty:
                failures += 1
                print(df.to_string(index=False))
    return 1 if failures else 0


def cmd_report(engine: Engine, args) -> int:
    with engine.connect() as conn:
        pid = conn.execute(text("SELECT portfolio_id FROM portfolios WHERE name = :n"), {"n": args.portfolio}).scalar()
        if pid is None:
            print(f"No portfolio named {args.portfolio!r}.")
            return 1
        holdings = pd.read_sql(
            text(
                "SELECT symbol, quantity, close, market_value, weight, total_pnl, total_dividends, as_of_date "
                "FROM holdings_latest WHERE portfolio_id = :p ORDER BY market_value DESC"
            ),
            conn,
            params={"p": pid},
        )
        summary = conn.execute(
            text(
                "SELECT as_of_date, market_value, cumulative_pnl FROM portfolio_pnl_daily "
                "WHERE portfolio_id = :p ORDER BY as_of_date DESC LIMIT 1"
            ),
            {"p": pid},
        ).one_or_none()

    if summary is None:
        print("No positions yet.")
        return 0
    as_of, mv, cum = summary
    print(f"\n{args.portfolio}  as of {as_of}")
    print(f"Market value   ${mv:,.2f}")
    print(f"Lifetime P&L   ${cum:,.2f}  (price P&L + dividends, net of fees)\n")
    out = holdings.drop(columns="as_of_date").assign(
        quantity=lambda d: d.quantity.astype(float).map("{:,.0f}".format),
        close=lambda d: d.close.astype(float).map("{:,.2f}".format),
        market_value=lambda d: d.market_value.astype(float).map("{:,.2f}".format),
        weight=lambda d: d.weight.astype(float).map("{:.1%}".format),
        total_pnl=lambda d: d.total_pnl.astype(float).map("{:,.2f}".format),
        total_dividends=lambda d: d.total_dividends.astype(float).map("{:,.2f}".format),
    )
    print(out.to_string(index=False))
    return 0


def cmd_bootstrap(engine: Engine, args) -> int:
    """Everything needed to go from an empty database to a P&L report."""
    steps = [
        ("Migrating schema", cmd_migrate),
        ("Seeding instruments", cmd_seed_instruments),
        ("Loading prices from Yahoo Finance", cmd_load_prices),
        ("Loading rates from FRED", cmd_load_rates),
        ("Seeding trades", cmd_seed_trades),
        ("Running data-quality checks", cmd_check),
        ("Building report", cmd_report),
    ]
    for label, fn in steps:
        print(f"\n== {label}")
        code = fn(engine, args)
        if code != 0:
            print(f"Stopped: '{label}' failed.")
            return code
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ballast", description="Ballast portfolio risk platform")
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name, fn, help_):
        p = sub.add_parser(name, help=help_)
        p.set_defaults(func=fn)
        return p

    add("migrate", cmd_migrate, "apply database migrations")
    p = add("seed-instruments", cmd_seed_instruments, "load an instrument master CSV")
    p.add_argument("--file", dest="instruments_file", type=Path, default=SEED_DIR / "instruments.csv")

    p = add("load-prices", cmd_load_prices, "load prices, dividends and splits from Yahoo Finance")
    p.add_argument("--symbols", nargs="*", help="only these symbols (default: all equities and ETFs)")
    p.add_argument("--start", type=date.fromisoformat, default=DEFAULT_HISTORY_START)
    p.add_argument("--no-repair", action="store_true", help="skip yfinance price repair (faster, less accurate)")

    p = add("load-rates", cmd_load_rates, "load Treasury yields and SOFR from FRED")
    p.add_argument("--series", nargs="*", help="only these FRED series IDs")
    p.add_argument("--start", type=date.fromisoformat, default=DEFAULT_HISTORY_START)
    p.set_defaults(allow_missing_key=False)

    p = add("seed-trades", cmd_seed_trades, "load a trade ledger CSV")
    p.add_argument("--file", dest="trades_file", type=Path, default=SEED_DIR / "trades.csv",
                   help="keep real trades in data/private/ (git-ignored), never in data/seed/")
    add("check", cmd_check, "run data-quality checks (exit code 1 on failure)")

    p = add("report", cmd_report, "print holdings and lifetime P&L")
    p.add_argument("--portfolio", default="Ballast Core")

    p = add("bootstrap", cmd_bootstrap, "run every step from empty database to report")
    p.add_argument("--start", type=date.fromisoformat, default=DEFAULT_HISTORY_START)
    p.add_argument("--no-repair", action="store_true")
    p.add_argument("--portfolio", default="Ballast Core")
    p.add_argument("--instruments-file", type=Path, default=SEED_DIR / "instruments.csv")
    p.add_argument("--trades-file", type=Path, default=SEED_DIR / "trades.csv")
    p.set_defaults(symbols=None, series=None, allow_missing_key=True)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    for warning in security_warnings():
        print(f"SECURITY WARNING: {warning}\n", file=sys.stderr)
    return args.func(get_engine(), args)


if __name__ == "__main__":
    sys.exit(main())
