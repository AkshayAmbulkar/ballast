"""CLI commands for Phase 4: `ballast var`, `ballast backtest`, `ballast stress`."""

from __future__ import annotations

from decimal import Decimal

import pandas as pd
from sqlalchemy import Engine, text

from ballast.risk.backtest import basel_zone, christoffersen_independence, conditional_coverage, kupiec_pof
from ballast.risk.engine import compute_var, run_backtest
from ballast.risk.market import RiskDataError, load_market_data
from ballast.risk.snapshot import build_snapshot
from ballast.risk.stress import run_stress


def _fmt(df: pd.DataFrame, formats: dict[str, str]) -> str:
    out = df.copy()
    for col, f in formats.items():
        if col in out:
            out[col] = out[col].map(lambda v, f=f: "n/a" if v is None or v != v else f.format(v))
    return out.to_string(index=False)


def _portfolio_id(engine: Engine, name: str) -> int:
    with engine.connect() as conn:
        return conn.execute(text("SELECT portfolio_id FROM portfolios WHERE name = :n"), {"n": name}).scalar_one()


def _latest_day(md) -> object:
    return max(md.positions.as_of_date)


def cmd_var(engine: Engine, args) -> int:
    try:
        md = load_market_data(engine, args.portfolio)
        on = _latest_day(md)
        res = compute_var(md, on, args.confidence, args.lookback, args.sims, args.seed)
    except RiskDataError as exc:
        print(f"Cannot compute VaR: {exc}")
        return 1
    pct = f"{args.confidence:.0%}"
    print(f"\nValue-at-Risk: {args.portfolio} as of {res.as_of}  (1-day, {pct} confidence)")
    print(f"Portfolio market value ${res.market_value:,.2f}; {res.lookback_days} days of history; "
          f"Monte Carlo {args.sims:,} scenarios\n")
    rows = [
        {"method": m, "var_usd": r.var, "var_pct_mv": r.var / res.market_value * 100, "es_usd": r.es,
         "es_pct_mv": r.es / res.market_value * 100}
        for m, r in res.methods.items()
    ]
    print(_fmt(pd.DataFrame(rows), {"var_usd": "{:,.2f}", "var_pct_mv": "{:.2f}", "es_usd": "{:,.2f}",
                                    "es_pct_mv": "{:.2f}"}))
    print("\nHistorical VaR of each asset class on its own:\n")
    sa = pd.DataFrame([{"asset_class": k, "var_usd": v.var, "es_usd": v.es} for k, v in res.standalone.items()])
    print(_fmt(sa, {"var_usd": "{:,.2f}", "es_usd": "{:,.2f}"}))
    parts = [res.standalone[k].var for k in ("equity", "options", "rates (bonds + swaps)") if k in res.standalone]
    total = res.methods["historical"].var
    print(f"\nSum of equity, options and rates VaRs ${sum(parts):,.2f} vs portfolio VaR ${total:,.2f}: "
          f"diversification benefit ${sum(parts) - total:,.2f}")
    if res.unpriced:
        print(f"Not included (no terms or inputs): {', '.join(res.unpriced)}")

    pid = _portfolio_id(engine, args.portfolio)
    with engine.begin() as conn:
        for method, r in res.methods.items():
            conn.execute(
                text(
                    """
                    INSERT INTO var_results (portfolio_id, as_of_date, method, confidence, var_usd, es_usd,
                                             market_value, scenarios, lookback_days)
                    VALUES (:p, :d, :m, :c, :v, :e, :mv, :n, :lb)
                    ON CONFLICT (portfolio_id, as_of_date, method, confidence) DO UPDATE
                    SET var_usd = EXCLUDED.var_usd, es_usd = EXCLUDED.es_usd, market_value = EXCLUDED.market_value,
                        scenarios = EXCLUDED.scenarios, lookback_days = EXCLUDED.lookback_days, computed_at = now()
                    """
                ),
                {"p": pid, "d": res.as_of, "m": method, "c": Decimal(str(args.confidence)),
                 "v": round(r.var, 2), "e": round(r.es, 2), "mv": round(res.market_value, 2),
                 "n": r.scenarios or None, "lb": res.lookback_days},
            )
    return 0


def cmd_backtest(engine: Engine, args) -> int:
    try:
        md = load_market_data(engine, args.portfolio)
    except RiskDataError as exc:
        print(f"Cannot run backtest: {exc}")
        return 1
    days = run_backtest(md, args.confidence, args.lookback)
    if len(days) < 20:
        print(f"Only {len(days)} days could be backtested; need more history.")
        return 1
    p = 1 - args.confidence
    print(f"\nVaR backtest: {args.portfolio}, {args.confidence:.0%} 1-day VaR set at each previous close")
    print(f"{len(days)} days, {days[0].date} to {days[-1].date}; an exception is a loss larger than VaR\n")
    rows = []
    for method in ("historical", "parametric"):
        hits = [d.actual_pnl < -d.var[method] for d in days]
        x, n = sum(hits), len(hits)
        last = hits[-250:]
        rows.append({
            "method": method, "exceptions": x, "expected": n * p, "rate_pct": x / n * 100,
            "kupiec_p": kupiec_pof(x, n, p).p_value,
            "indep_p": christoffersen_independence(hits).p_value,
            "cc_p": conditional_coverage(x, n, p, hits).p_value,
            "last250": f"{sum(last)} ({basel_zone(sum(last))})" if len(last) == 250 else "n/a",
        })
    print(_fmt(pd.DataFrame(rows), {"expected": "{:.1f}", "rate_pct": "{:.2f}", "kupiec_p": "{:.3f}",
                                    "indep_p": "{:.3f}", "cc_p": "{:.3f}"}))
    print("\np-values below 0.05 reject the model: kupiec = wrong number of exceptions, indep = exceptions")
    print("cluster together, cc = both combined. last250 = Basel traffic light over the latest 250 days.")

    worst = sorted(days, key=lambda d: d.actual_pnl)[:5]
    print("\nLargest losses in the backtest:\n")
    print(_fmt(pd.DataFrame([{"date": d.date, "actual_pnl": d.actual_pnl, "hist_var": d.var["historical"],
                               "param_var": d.var["parametric"]} for d in worst]),
               {"actual_pnl": "{:,.2f}", "hist_var": "{:,.2f}", "param_var": "{:,.2f}"}))

    pid = _portfolio_id(engine, args.portfolio)
    conf = Decimal(str(args.confidence))
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM var_backtest WHERE portfolio_id = :p AND confidence = :c"), {"p": pid, "c": conf})
        conn.execute(
            text(
                """
                INSERT INTO var_backtest (portfolio_id, test_date, method, confidence, var_usd, actual_pnl, exception)
                VALUES (:p, :d, :m, :c, :v, :a, :x)
                """
            ),
            [
                {"p": pid, "d": d.date, "m": m, "c": conf, "v": round(v, 2), "a": round(d.actual_pnl, 2),
                 "x": d.actual_pnl < -v}
                for d in days for m, v in d.var.items()
            ],
        )
    print(f"\nSaved {len(days) * 2} rows to var_backtest (see also the var_backtest_by_year view).")
    return 0


def cmd_stress(engine: Engine, args) -> int:
    try:
        md = load_market_data(engine, args.portfolio)
        on = _latest_day(md)
        snap = build_snapshot(md, on, ladder_steps=500)
    except RiskDataError as exc:
        print(f"Cannot run stress tests: {exc}")
        return 1
    results = run_stress(md, snap)
    if not results:
        print("No stress episode falls inside the loaded history (load prices and rates from 2022).")
        return 0
    print(f"\nStress tests: {args.portfolio} as of {on}  (each episode's actual moves applied instantly)\n")
    print(_fmt(pd.DataFrame([{"episode": r.episode.name, "from": r.start_used, "to": r.end_used,
                               "spy_pct": None if r.spy_return is None else r.spy_return * 100,
                               "ust10y_bp": r.ten_year_change_bp} for r in results]),
               {"spy_pct": "{:+.1f}", "ust10y_bp": "{:+.0f}"}))
    print("\nP&L by asset class ($):\n")
    print(_fmt(pd.DataFrame([{"episode": r.episode.name, **r.pnl} for r in results]),
               {k: "{:,.0f}" for k in ("equity", "options", "bonds", "swaps", "total")}))
    print("\nEpisodes:")
    for r in results:
        print(f"  {r.episode.name:<15} {r.episode.description}")
    return 0
