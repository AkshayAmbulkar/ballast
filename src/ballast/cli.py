"""Command-line entry point: `ballast <command>`. Run `ballast --help` for the list."""

from __future__ import annotations

import argparse
import math
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
from sqlalchemy import Engine, text

from ballast.config import SEED_DIR, get_settings, secrets_file, security_warnings
from ballast.db import get_engine, migrate
from ballast.market_data.load import DEFAULT_HISTORY_START, load_prices, load_rates
from ballast.derivatives import chains
from ballast.derivatives.analytics import greeks_report
from ballast.derivatives.black_scholes import PricingError
from ballast.derivatives.sofr import FixingError, sofr_average
from ballast.derivatives.store import load_sofr_fixings, price_options, price_swaps
from ballast.fixed_income.analytics import curve_snapshot, rates_risk_report
from ballast.fixed_income.sensitivity import empirical_duration
from ballast.fixed_income.store import build_curves, etf_rate_sensitivity_data, price_bonds
from ballast.fixed_income.store import par_curve_on
from ballast.portfolio.seed import (
    seed_bond_terms,
    seed_instruments,
    seed_option_terms,
    seed_swap_terms,
    seed_trades,
)

DQ_VIEWS = (
    "dq_trades_off_calendar",
    "dq_trade_price_outliers",
    "dq_unexplained_price_jumps",
    "dq_bonds_missing_terms",
    "dq_derivatives_missing_terms",
)


def cmd_migrate(engine: Engine, args) -> int:
    applied = migrate(engine)
    print(f"Applied {len(applied)} migration(s): {', '.join(applied)}" if applied else "Schema is up to date.")
    return 0


def cmd_seed_instruments(engine: Engine, args) -> int:
    print(f"Upserted {seed_instruments(engine, args.instruments_file)} instruments.")
    print(f"Upserted {seed_bond_terms(engine, args.bond_terms_file)} bond terms.")
    print(f"Upserted {seed_option_terms(engine, args.option_terms_file)} option terms.")
    print(f"Upserted {seed_swap_terms(engine, args.swap_terms_file)} swap terms.")
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
    print(f"Lifetime P&L   ${cum:,.2f}  (price P&L + dividends, coupons and swap payments, net of fees)")
    print("Bonds: quantity = $100-face units, close = dirty price per 100 face")
    print("Options: quantity = shares covered (1 contract = 100), close = premium per share")
    print("Swaps: quantity = notional / 100 (negative = pay fixed), close = value per 100 notional\n")
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


def cmd_rates_risk(engine: Engine, args) -> int:
    report = rates_risk_report(engine, args.portfolio)
    if report is None:
        print("No bond or swap positions in this portfolio.")
        return 0
    print(f"\nInterest-rate risk: {args.portfolio} as of {report.as_of} (curve of {report.curve_date})")
    b = report.bonds
    if not b.empty:
        print("\nBonds, pricing (per 100 face):\n")
        print(_fmt(b[["symbol", "face", "clean", "accrued", "dirty", "ytm_pct", "market_value"]],
                   {"face": "{:,.0f}", "clean": "{:.3f}", "accrued": "{:.3f}", "dirty": "{:.3f}",
                    "ytm_pct": "{:.3f}", "market_value": "{:,.2f}"}))
        print("\nBonds, risk (durations in years; DV01 in $ per position):\n")
        print(_fmt(b[["symbol", "mod_duration", "eff_duration", "convexity", "dv01_usd"]],
                   {"mod_duration": "{:.2f}", "eff_duration": "{:.2f}", "convexity": "{:.1f}", "dv01_usd": "{:,.2f}"}))
    w = report.swaps
    if not w.empty:
        print("\nSOFR swaps (valued on the Treasury curve as a SOFR proxy):\n")
        print(_fmt(w[["symbol", "notional", "direction", "fixed_pct", "par_today_pct", "market_value", "dv01_usd"]],
                   {"notional": "{:,.0f}", "fixed_pct": "{:.4f}", "par_today_pct": "{:.4f}",
                    "market_value": "{:,.2f}", "dv01_usd": "{:,.2f}"}))
    bond_dv01 = float(b.dv01_usd.sum()) if not b.empty else 0.0
    print(f"\nDV01 ($ gained per 1bp parallel fall): bonds {bond_dv01:,.2f}, "
          f"swaps {report.total_dv01 - bond_dv01:,.2f}, total {report.total_dv01:,.2f}")
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


def cmd_price_options(engine: Engine, args) -> int:
    counts = price_options(engine)
    if not counts:
        print("No options with terms to price.")
    for symbol, n in counts.items():
        print(f"  {symbol:<22} {n:>5} daily marks")
    return 0


def cmd_price_swaps(engine: Engine, args) -> int:
    counts = price_swaps(engine)
    if not counts:
        print("No swaps with terms to price.")
    for symbol, n in counts.items():
        print(f"  {symbol:<22} {n:>5} daily marks")
    return 0


def cmd_greeks(engine: Engine, args) -> int:
    report = greeks_report(engine, args.portfolio)
    if report is None:
        print("No option positions in this portfolio.")
        return 0
    o = report.options
    print(f"\nOption Greeks: {args.portfolio} as of {report.as_of}")
    print("(model marks: realized volatility, Treasury zero rate, trailing dividend yield)\n")
    print(_fmt(o[["symbol", "quantity", "spot", "vol_pct", "days", "price", "market_value"]],
               {"quantity": "{:,.0f}", "spot": "{:,.2f}", "vol_pct": "{:.1f}", "price": "{:.3f}",
                "market_value": "{:,.2f}"}))
    print("\nPosition Greeks (delta, gamma in shares; vega $/vol point; theta $/day; rho $/1%):\n")
    print(_fmt(o[["symbol", "delta", "delta_shares", "gamma_shares", "vega_usd", "theta_usd_day", "rho_usd"]],
               {"delta": "{:.3f}", "delta_shares": "{:,.1f}", "gamma_shares": "{:.3f}", "vega_usd": "{:,.2f}",
                "theta_usd_day": "{:,.2f}", "rho_usd": "{:,.2f}"}))
    print(f"\nTotal: vega {o.vega_usd.sum():,.2f} $/vol pt, theta {o.theta_usd_day.sum():,.2f} $/day")
    print("\nDelta-adjusted equity exposure (stock + option delta):\n")
    print(_fmt(report.exposure,
               {"spot": "{:,.2f}", "stock_shares": "{:,.0f}", "option_delta_shares": "{:,.1f}",
                "net_delta_shares": "{:,.1f}", "stock_exposure_usd": "{:,.0f}", "net_exposure_usd": "{:,.0f}"}))
    return 0


def cmd_vol_smile(engine: Engine, args) -> int:
    try:
        chain = chains.fetch_chain(args.symbol, args.expiry, args.min_days)
    except PricingError as exc:
        print(f"Could not load the option chain: {exc}")
        return 1
    found = par_curve_on(engine, datetime.now(timezone.utc).date())
    if found is None:
        print("No recent Treasury curve. Run `ballast load-rates` first.")
        return 1
    curve = found[1].build()
    T0 = chains.years_to_expiry(chain.snapshot_at, chain.expiry)
    r = float(curve.zero_rate(max(T0, 1 / 365)))
    try:
        T, F, table = chains.smile(chain, r)
    except PricingError as exc:
        print(f"Could not build the smile: {exc}")
        return 1
    if len(table) < 5:
        print(f"Only {len(table)} usable quotes (bid and ask both present). Yahoo's quotes are often empty "
              "outside U.S. market hours (9:30-16:00 New York); try again while the market is open.")
        return 1
    _save_quotes(engine, chain, table, F, r)
    s = chains.smile_summary(table)
    print(f"\n{chain.symbol} implied volatility, expiry {chain.expiry} ({T * 365:.0f} days), "
          f"snapshot {chain.snapshot_at:%Y-%m-%d %H:%M} UTC")
    print(f"Spot {chain.spot:,.2f}   forward (put-call parity) {F:,.2f}   r {r * 100:.3f}% (Treasury zero)\n")
    shown = table[(table.moneyness >= 0.85) & (table.moneyness <= 1.15)]
    step = max(1, math.ceil(len(shown) / 25))
    view = shown.iloc[::step].assign(
        model_iv_pct=lambda d: d.model_iv * 100,
        yahoo_iv_pct=lambda d: pd.to_numeric(d.impliedVolatility, errors="coerce") * 100,
    )
    print(_fmt(view[["strike", "option_type", "moneyness", "mid", "model_iv_pct", "yahoo_iv_pct"]],
               {"strike": "{:,.1f}", "moneyness": "{:.3f}", "mid": "{:,.2f}", "model_iv_pct": "{:.2f}",
                "yahoo_iv_pct": "{:.2f}"}))
    pct = lambda x: "n/a" if x != x else f"{x * 100:.2f}%"
    print(f"\nATM vol {pct(s['atm'])}   90% {pct(s['m90'])}   110% {pct(s['m110'])}   "
          f"skew (90% - 110%) {pct(s['skew_90_110'])}")
    if "median_abs_diff_vs_vendor" in s:
        print(f"Median |Ballast IV - Yahoo IV| across {len(table)} strikes: {pct(s['median_abs_diff_vs_vendor'])}")
    print(f"Saved {len(table)} quotes to option_quotes.")
    return 0


def _save_quotes(engine: Engine, chain, table: pd.DataFrame, F: float, r: float) -> None:
    with engine.begin() as conn:
        uid = conn.execute(text("SELECT instrument_id FROM instruments WHERE symbol = :s"), {"s": chain.symbol}).scalar()
        if uid is None:
            return
        num = lambda v: None if v is None or v != v else float(v)
        conn.execute(
            text(
                """
                INSERT INTO option_quotes (underlying_id, snapshot_at, expiry_date, option_type, strike, bid, ask,
                                           last_price, volume, open_interest, vendor_iv, model_iv, forward, rate_cc)
                VALUES (:u, :ts, :e, :t, :k, :b, :a, :l, :v, :oi, :viv, :miv, :f, :r)
                ON CONFLICT DO NOTHING
                """
            ),
            [
                {
                    "u": uid, "ts": chain.snapshot_at, "e": chain.expiry, "t": row.option_type, "k": float(row.strike),
                    "b": num(row.bid), "a": num(row.ask), "l": num(getattr(row, "lastPrice", None)),
                    "v": None if num(getattr(row, "volume", None)) is None else int(row.volume),
                    "oi": None if num(getattr(row, "openInterest", None)) is None else int(row.openInterest),
                    "viv": num(getattr(row, "impliedVolatility", None)), "miv": float(row.model_iv),
                    "f": F, "r": r,
                }
                for row in table.itertuples()
            ],
        )


def cmd_sofr_check(engine: Engine, args) -> int:
    fixings = load_sofr_fixings(engine)
    with engine.connect() as conn:
        published = conn.execute(
            text("SELECT obs_date, value_pct FROM rate_observations WHERE series_id = 'SOFR30DAYAVG' ORDER BY obs_date")
        ).all()
    if not fixings or not published:
        print("Need SOFR and SOFR30DAYAVG from FRED. Run `ballast load-rates`.")
        return 1
    first = min(fixings)
    rows = []
    for pub, value in published:
        if (pub - first).days < 31:
            continue
        try:
            ours = sofr_average(fixings, pub) * 100
        except FixingError:
            continue
        rows.append((pub, float(value), ours))
    if not rows:
        print("No overlapping dates to compare.")
        return 1
    df = pd.DataFrame(rows, columns=["published_on", "nyfed_pct", "ballast_pct"])
    df["diff_bp"] = (df.ballast_pct - df.nyfed_pct) * 100
    exact = int((df.ballast_pct.round(5) == df.nyfed_pct.round(5)).sum())
    print("\n30-day average SOFR: Ballast compounding vs NY Fed published values")
    print(f"{len(df)} publication days, {df.published_on.iloc[0]} to {df.published_on.iloc[-1]}")
    print(f"Exact match at the published 5 decimals: {exact} of {len(df)} ({exact / len(df):.1%})")
    print(f"Largest difference: {df.diff_bp.abs().max():.4f} bp\n")
    print(_fmt(df.tail(5), {"nyfed_pct": "{:.5f}", "ballast_pct": "{:.5f}", "diff_bp": "{:+.4f}"}))
    worst = df.loc[df.diff_bp.abs() > 0.001]
    if not worst.empty:
        print(f"\n{len(worst)} day(s) differ by more than 0.001bp; the largest:")
        print(_fmt(worst.reindex(worst.diff_bp.abs().sort_values(ascending=False).index).head(5),
                   {"nyfed_pct": "{:.5f}", "ballast_pct": "{:.5f}", "diff_bp": "{:+.4f}"}))
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
        ("Marking options", cmd_price_options),
        ("Marking SOFR swaps", cmd_price_swaps),
        ("Seeding trades", cmd_seed_trades),
        ("Running data-quality checks", cmd_check),
        ("Building report", cmd_report),
        ("Interest-rate risk", cmd_rates_risk),
        ("Option Greeks", cmd_greeks),
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
    p.add_argument("--option-terms-file", type=Path, default=SEED_DIR / "option_terms.csv")
    p.add_argument("--swap-terms-file", type=Path, default=SEED_DIR / "swap_terms.csv")

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

    p = sub.add_parser("rates-risk", aliases=["bond-risk"],
                       help="duration, DV01, key-rate DV01 and scenarios for bonds and swaps")
    p.set_defaults(func=cmd_rates_risk)
    p.add_argument("--portfolio", default="Ballast Core")

    add("price-options", cmd_price_options, "mark option positions with Black-Scholes / binomial models")
    add("price-swaps", cmd_price_swaps, "set at-market swap rates, mark swaps, record swap payments")

    p = add("greeks", cmd_greeks, "option Greeks and delta-adjusted equity exposure")
    p.add_argument("--portfolio", default="Ballast Core")

    p = add("vol-smile", cmd_vol_smile, "implied volatility smile from Yahoo's listed option chain")
    p.add_argument("--symbol", default="SPY")
    p.add_argument("--expiry", type=date.fromisoformat, default=None, help="default: first expiry >= --min-days out")
    p.add_argument("--min-days", type=int, default=20)

    add("sofr-check", cmd_sofr_check, "compare Ballast's SOFR compounding with the NY Fed's published 30-day averages")

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
    p.add_argument("--option-terms-file", type=Path, default=SEED_DIR / "option_terms.csv")
    p.add_argument("--swap-terms-file", type=Path, default=SEED_DIR / "swap_terms.csv")
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
