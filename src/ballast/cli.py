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
from ballast.fixed_income.analytics import bond_risk_report, curve_snapshot
from ballast.fixed_income.sensitivity import empirical_duration
from ballast.fixed_income.store import build_curves, etf_rate_sensitivity_data, price_bonds
from ballast.portfolio.seed import seed_bond_terms, seed_instruments, seed_trades

DQ_VIEWS = (
    "dq_trades_off_calendar",
    "dq_trade_price_outliers",
    "dq_unexplained_price_jumps",
    "dq_bonds_missing_terms",
)


def cmd_migrate(engine: Engine, args) -> int:
    applied = migrate(engine)
    print(f"Applied {len(applied)} migration(s): {', '.join(applied)}" if applied else "Schema is up to date.")
    return 0


def cmd_seed_instruments(engine: Engine, args) -> int:
    print(f"Upserted {seed_instruments(engine, args.instruments_file)} instruments.")
    print(f"Upserted {seed_bond_terms(engine, args.bond_terms_file)} bond terms.")
    return 0


def cmd_load_prices(engine: Engine, args) -> int:
    counts = load_prices(engine, symbols=args.symbols, start=args.start, repair=not args.no_repair)
    for symbol, n in counts.items():
        print(f"  {symbol:<6} {n:>5} price rows")
    return 0


def cmd_load_rates(engine: Engine, args) -> int:
    key = get_settings().fred_api_key
    if not key:
        print(f"FRED_API_KEY is not set in {secrets_file()}. Treasury yields are needed for the "
              "yield curve and bond pricing. Get a free key at https://fred.stlouisfed.org/docs/api/api_key.html")
        return 1
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
                "SELECT symbol, quantity, close, market_value, weight, total_pnl, total_income, as_of_date "
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
    print(f"Lifetime P&L   ${cum:,.2f}  (price P&L + dividends and coupons, net of fees)")
    print("Bonds: quantity in $100-face units, close = dirty price per 100 face\n")
    out = holdings.drop(columns="as_of_date").assign(
        quantity=lambda d: d.quantity.astype(float).map("{:,.0f}".format),
        close=lambda d: d.close.astype(float).map("{:,.2f}".format),
        market_value=lambda d: d.market_value.astype(float).map("{:,.2f}".format),
        weight=lambda d: d.weight.astype(float).map("{:.1%}".format),
        total_pnl=lambda d: d.total_pnl.astype(float).map("{:,.2f}".format),
        total_income=lambda d: d.total_income.astype(float).map("{:,.2f}".format),
    )
    print(out.to_string(index=False))
    return 0


def cmd_build_curves(engine: Engine, args) -> int:
    n = build_curves(engine, rebuild=args.rebuild)
    print(f"Bootstrapped and stored {n} daily Treasury curve(s).")
    return 0


def cmd_price_bonds(engine: Engine, args) -> int:
    counts = price_bonds(engine)
    if not counts:
        print("No Treasury instruments with terms to price.")
    for symbol, n in counts.items():
        print(f"  {symbol:<16} {n:>5} daily prices")
    return 0


def _fmt(df: pd.DataFrame, formats: dict[str, str]) -> str:
    out = df.copy()
    for col, f in formats.items():
        if col in out:
            out[col] = out[col].map(f.format)
    return out.to_string(index=False)


def cmd_curve(engine: Engine, args) -> int:
    snap = curve_snapshot(engine, args.date)
    if snap is None:
        print("No Treasury curve available. Run `ballast load-rates` first.")
        return 1
    curve_date, df = snap
    print(f"\nU.S. Treasury curve, {curve_date}  (bootstrapped from CMT par yields; rates in percent)\n")
    print(_fmt(df, {"par_pct": "{:.3f}", "zero_bey_pct": "{:.3f}", "zero_cc_pct": "{:.3f}",
                    "discount_factor": "{:.6f}", "fwd_6m_bey_pct": "{:.3f}"}))
    return 0


def cmd_bond_risk(engine: Engine, args) -> int:
    report = bond_risk_report(engine, args.portfolio)
    if report is None:
        print("No Treasury positions in this portfolio.")
        return 0
    s = report.summary
    print(f"\nFixed income risk: {args.portfolio} as of {report.as_of} (curve of {report.curve_date})")
    print("\nPricing (per 100 face):\n")
    print(_fmt(s[["symbol", "face", "clean", "accrued", "dirty", "ytm_pct", "market_value"]],
               {"face": "{:,.0f}", "clean": "{:.3f}", "accrued": "{:.3f}", "dirty": "{:.3f}",
                "ytm_pct": "{:.3f}", "market_value": "{:,.2f}"}))
    print("\nRisk (durations in years; DV01 in $ per position):\n")
    print(_fmt(s[["symbol", "mod_duration", "eff_duration", "convexity", "dv01_usd"]],
               {"mod_duration": "{:.2f}", "eff_duration": "{:.2f}", "convexity": "{:.1f}", "dv01_usd": "{:,.2f}"}))
    print(f"\nPortfolio bond DV01: ${s.dv01_usd.sum():,.2f} gained per 1bp parallel fall in yields")
    print("\nKey-rate DV01 ($ gained per 1bp fall at each tenor):\n")
    kr = report.key_rates.map("{:,.2f}".format).reset_index()
    print(kr.to_string(index=False))
    sc = report.scenarios.assign(convexity_effect=lambda d: d.full_reval_pnl - d.key_rate_estimate)
    print("\nScenarios ($ P&L: full revaluation vs. first-order key-rate estimate):\n")
    print(_fmt(sc[["scenario", "full_reval_pnl", "key_rate_estimate", "convexity_effect"]],
               {"full_reval_pnl": "{:,.2f}", "key_rate_estimate": "{:,.2f}", "convexity_effect": "{:,.2f}"}))
    print("\nScenario definitions:")
    for name, desc in zip(sc.scenario, sc.description):
        print(f"  {name:<18} {desc}")
    return 0


def cmd_rate_sensitivity(engine: Engine, args) -> int:
    df = etf_rate_sensitivity_data(engine, args.symbol, args.series, args.since)
    if len(df) < 21:
        print(f"Not enough overlapping data for {args.symbol} and {args.series}.")
        return 1
    returns = df.adj_close.pct_change().to_numpy()[1:]
    dy = (df.value_pct.diff() / 100).to_numpy()[1:]
    est = empirical_duration(returns, dy)
    first, last = df.iloc[0], df.iloc[-1]
    dy_total = (last.value_pct - first.value_pct) / 100
    print(f"\n{args.symbol.upper()} vs {args.series}, {first.day} to {last.day} ({est.observations} daily observations)\n")
    print(f"Empirical duration       {est.duration:6.2f}   (R-squared {est.r_squared:.2f})")
    print(f"{args.series} change          {dy_total * 1e4:+7.0f} bp   ({first.value_pct:.2f}% -> {last.value_pct:.2f}%)")
    print(f"Implied price effect     {-est.duration * dy_total:+7.2%}   (-duration x yield change)")
    print(f"Actual price return      {last.close / first.close - 1:+7.2%}")
    print(f"Actual total return      {last.adj_close / first.adj_close - 1:+7.2%}   (price + distributions)")
    return 0


def cmd_bootstrap(engine: Engine, args) -> int:
    """Everything needed to go from an empty database to a P&L report."""
    steps = [
        ("Migrating schema", cmd_migrate),
        ("Seeding instruments", cmd_seed_instruments),
        ("Loading prices from Yahoo Finance", cmd_load_prices),
        ("Loading rates from FRED", cmd_load_rates),
        ("Bootstrapping Treasury curves", cmd_build_curves),
        ("Pricing bonds off the curve", cmd_price_bonds),
        ("Seeding trades", cmd_seed_trades),
        ("Running data-quality checks", cmd_check),
        ("Building report", cmd_report),
        ("Fixed income risk", cmd_bond_risk),
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
    p = add("seed-instruments", cmd_seed_instruments, "load the instrument master and bond terms CSVs")
    p.add_argument("--file", dest="instruments_file", type=Path, default=SEED_DIR / "instruments.csv")
    p.add_argument("--bond-terms-file", type=Path, default=SEED_DIR / "bond_terms.csv")

    p = add("load-prices", cmd_load_prices, "load prices, dividends and splits from Yahoo Finance")
    p.add_argument("--symbols", nargs="*", help="only these symbols (default: all equities and ETFs)")
    p.add_argument("--start", type=date.fromisoformat, default=DEFAULT_HISTORY_START)
    p.add_argument("--no-repair", action="store_true", help="skip yfinance price repair (faster, less accurate)")

    p = add("load-rates", cmd_load_rates, "load Treasury yields and SOFR from FRED")
    p.add_argument("--series", nargs="*", help="only these FRED series IDs")
    p.add_argument("--start", type=date.fromisoformat, default=DEFAULT_HISTORY_START)

    p = add("build-curves", cmd_build_curves, "bootstrap and store the Treasury zero curve for every date")
    p.add_argument("--rebuild", action="store_true", help="rebuild dates that are already stored")
    add("price-bonds", cmd_price_bonds, "mark Treasury holdings off the curve and record coupons")

    p = add("curve", cmd_curve, "show the Treasury curve for a date (default: latest)")
    p.add_argument("--date", type=date.fromisoformat, default=None)

    p = add("bond-risk", cmd_bond_risk, "duration, DV01, key-rate DV01 and scenarios for bond holdings")
    p.add_argument("--portfolio", default="Ballast Core")

    p = add("rate-sensitivity", cmd_rate_sensitivity, "empirical duration of an ETF against a Treasury yield")
    p.add_argument("--symbol", default="TLT")
    p.add_argument("--series", default="DGS20")
    p.add_argument("--since", type=date.fromisoformat, default=None)

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
    p.add_argument("--bond-terms-file", type=Path, default=SEED_DIR / "bond_terms.csv")
    p.add_argument("--trades-file", type=Path, default=SEED_DIR / "trades.csv")
    p.set_defaults(symbols=None, series=None, rebuild=False)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    for warning in security_warnings():
        print(f"SECURITY WARNING: {warning}\n", file=sys.stderr)
    return args.func(get_engine(), args)


if __name__ == "__main__":
    sys.exit(main())
