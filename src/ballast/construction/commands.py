"""CLI commands for Phase 5: `ballast optimize` and `ballast attribution`.

The calculations are in ballast.construction.analysis; these functions print and store them.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from sqlalchemy import Engine, text

from ballast.construction.analysis import AttributionRun, OptimizationRun, run_attribution, run_optimization
from ballast.construction.attribution import brinson_fachler
from ballast.construction.black_litterman import ViewError
from ballast.construction.data import ConstructionDataError
from ballast.construction.optimize import OptimizationError


def _fmt(df: pd.DataFrame, formats: dict[str, str]) -> str:
    out = df.copy()
    for col, f in formats.items():
        if col in out:
            out[col] = out[col].map(lambda v, f=f: "n/a" if v is None or v != v else f.format(v))
    return out.to_string(index=False)


def cmd_optimize(engine: Engine, args) -> int:
    try:
        run = run_optimization(engine, args.portfolio, args.lookback, args.max_weight, args.view,
                               args.delta, args.tau)
    except (ConstructionDataError, ViewError) as exc:
        print(f"Cannot optimize: {exc}")
        return 1
    except OptimizationError as exc:
        name, _, detail = str(exc).partition(": ")
        print(f"Optimization failed ({name}): {detail}")
        return 1
    u = run.universe

    print(f"\nPortfolio construction: {args.portfolio} as of {u.as_of}")
    print(f"{len(u.symbols)} stocks and ETFs; {u.lookback_days} days of returns from {u.start}; "
          f"risk-free {u.risk_free:.2%} (3-month Treasury)")
    print("Expected returns, total % a year. implied = returns that make today's weights optimal;")
    print("bl_return = Black-Litterman blend of implied with "
          + (f"view(s) {'; '.join(run.view_text)}" if run.views else "no views (equal to implied)") + "\n")
    print(_fmt(pd.DataFrame({
        "symbol": u.symbols, "weight_now": u.current_weights * 100, "hist_return": u.mean * 100,
        "volatility": np.sqrt(np.diag(u.cov)) * 100, "implied": (u.risk_free + run.implied_excess) * 100,
        "bl_return": run.expected * 100,
    }), {k: "{:.1f}" for k in ("weight_now", "hist_return", "volatility", "implied", "bl_return")}))

    print(f"\nPortfolios (long-only, max {args.max_weight:.0%} per asset; risk parity is unconstrained by the cap).")
    print("Return and Sharpe use the Black-Litterman returns; max_sharpe_hist uses raw historical means.\n")
    print(_fmt(pd.DataFrame([{"portfolio": k, "exp_return": s.expected_return * 100, "volatility": s.volatility * 100,
                               "sharpe": s.sharpe, "largest": u.symbols[int(np.argmax(s.weights))],
                               "largest_pct": float(np.max(s.weights)) * 100} for k, s in run.stats.items()]),
               {"exp_return": "{:.2f}", "volatility": "{:.2f}", "sharpe": "{:.2f}", "largest_pct": "{:.1f}"}))

    print("\nWeights (%):\n")
    table = pd.DataFrame({k: w * 100 for k, w in run.weights.items()}, index=u.symbols).T.reset_index(names="portfolio")
    print(_fmt(table, {s: "{:.1f}" for s in u.symbols}))
    print("\nShare of today's portfolio risk by asset (%): "
          + ", ".join(f"{s} {x * 100:.0f}" for s, x in sorted(zip(u.symbols, run.risk_share), key=lambda t: -t[1])))

    print("\nEfficient frontier (Black-Litterman returns):\n")
    print(_fmt(pd.DataFrame([{"exp_return": p.expected_return * 100, "volatility": p.volatility * 100,
                               "sharpe": p.sharpe} for p in run.frontier]),
               {"exp_return": "{:.2f}", "volatility": "{:.2f}", "sharpe": "{:.2f}"}))
    _save_optimization(engine, args.portfolio, run)
    return 0


def _save_optimization(engine: Engine, portfolio: str, run: OptimizationRun) -> None:
    u = run.universe
    with engine.begin() as conn:
        pid = conn.execute(text("SELECT portfolio_id FROM portfolios WHERE name = :n"), {"n": portfolio}).scalar_one()
        for method, s in run.stats.items():
            run_id = conn.execute(
                text(
                    """
                    INSERT INTO optimization_runs (portfolio_id, as_of_date, method, expected_return, volatility,
                                                   sharpe, risk_free, lookback_days, max_weight, views)
                    VALUES (:p, :d, :m, :r, :v, :s, :rf, :lb, :mw, :views) RETURNING run_id
                    """
                ),
                {"p": pid, "d": u.as_of, "m": method, "r": s.expected_return, "v": s.volatility,
                 "s": None if s.sharpe != s.sharpe else s.sharpe, "rf": u.risk_free, "lb": u.lookback_days,
                 "mw": run.max_weight, "views": "; ".join(run.view_text) or None},
            ).scalar_one()
            conn.execute(
                text("INSERT INTO optimization_weights (run_id, instrument_id, weight) VALUES (:r, :i, :w)"),
                [{"r": run_id, "i": iid, "w": float(w)} for iid, w in zip(u.instrument_ids, s.weights)],
            )


def cmd_attribution(engine: Engine, args) -> int:
    try:
        run = run_attribution(engine, args.portfolio, args.benchmark, args.since)
    except (ConstructionDataError, ValueError) as exc:
        print(f"Cannot run attribution: {exc}")
        return 1
    a = run.inputs
    print(f"\nPerformance attribution: {args.portfolio} vs benchmark {args.benchmark} (rebalanced daily)")
    print(f"{len(a.days)} days, {a.days[0]} to {a.days[-1]}\n")
    print(_fmt(pd.DataFrame([
        {"": name, "total_return": s.total_return * 100, "annualized": s.annualized * 100, "volatility": s.volatility * 100}
        for name, s in (("portfolio", run.portfolio), ("benchmark", run.benchmark))
    ]), {"total_return": "{:+.2f}", "annualized": "{:+.2f}", "volatility": "{:.2f}"}))
    print(f"\nActive return {run.active_return * 100:+.2f}% ({run.active_annualized * 100:+.2f}% a year); "
          f"tracking error {run.tracking_error * 100:.2f}%; information ratio {run.information_ratio:.2f}")

    print("\nBrinson-Fachler attribution, whole period (percentage points, Carino-linked):\n")
    eff = run.linked.effects.reset_index(names="segment")
    eff.loc[len(eff)] = ["TOTAL", *eff[["allocation", "selection", "interaction", "total"]].sum().tolist()]
    print(_fmt(eff.assign(**{c: eff[c] * 100 for c in ("allocation", "selection", "interaction", "total")}),
               {c: "{:+.2f}" for c in ("allocation", "selection", "interaction", "total")}))

    print("\nBy year (percentage points):\n")
    print(_fmt(pd.DataFrame([{"year": y.year, "portfolio": y.portfolio_return * 100, "benchmark": y.benchmark_return * 100,
                               "allocation": y.allocation * 100, "selection": y.selection * 100,
                               "interaction": y.interaction * 100} for y in run.by_year]),
               {c: "{:+.2f}" for c in ("portfolio", "benchmark", "allocation", "selection", "interaction")}))
    print("\nallocation = over/underweighting segments that beat/lagged the benchmark;")
    print("selection = holdings beating/lagging their own segment's benchmark.")
    _save_attribution(engine, args.portfolio, run)
    return 0


def _save_attribution(engine: Engine, portfolio: str, run: AttributionRun) -> None:
    a = run.inputs
    rows = []
    for t, d in enumerate(a.days):
        e = brinson_fachler(a.port_weights[t], a.port_returns[t], a.bench_weights, a.bench_returns[t])
        for j, seg in enumerate(a.segments):
            rows.append({"d": d, "s": seg, "bm": run.benchmark_text, "pw": a.port_weights[t, j],
                         "pr": a.port_returns[t, j], "bw": a.bench_weights[j], "br": a.bench_returns[t, j],
                         "al": e["allocation"][j], "se": e["selection"][j], "it": e["interaction"][j]})
    with engine.begin() as conn:
        pid = conn.execute(text("SELECT portfolio_id FROM portfolios WHERE name = :n"), {"n": portfolio}).scalar_one()
        conn.execute(text("DELETE FROM attribution_daily WHERE portfolio_id = :p AND benchmark = :b"),
                     {"p": pid, "b": run.benchmark_text})
        conn.execute(
            text(
                """
                INSERT INTO attribution_daily (portfolio_id, as_of_date, segment, benchmark, port_weight, port_return,
                                               bench_weight, bench_return, allocation, selection, interaction)
                VALUES (:p, :d, :s, :bm, :pw, :pr, :bw, :br, :al, :se, :it)
                """
            ),
            [{"p": pid, **r} for r in rows],
        )
