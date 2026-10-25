"""CLI commands for Phase 5: `ballast optimize` and `ballast attribution`."""

from __future__ import annotations

import math
from collections import defaultdict

import numpy as np
import pandas as pd
from sqlalchemy import Engine, text

from ballast.construction.attribution import brinson_fachler, link
from ballast.construction.black_litterman import View, ViewError, equilibrium_returns, posterior, view_matrices
from ballast.construction.data import (
    TRADING_DAYS,
    ConstructionDataError,
    load_attribution_inputs,
    load_universe,
    parse_benchmark,
)
from ballast.construction.optimize import (
    OptimizationError,
    describe,
    frontier,
    max_sharpe,
    min_variance,
    risk_contributions,
    risk_parity,
)


def _fmt(df: pd.DataFrame, formats: dict[str, str]) -> str:
    out = df.copy()
    for col, f in formats.items():
        if col in out:
            out[col] = out[col].map(lambda v, f=f: "n/a" if v is None or v != v else f.format(v))
    return out.to_string(index=False)


def cmd_optimize(engine: Engine, args) -> int:
    try:
        u = load_universe(engine, args.portfolio, args.lookback)
        views = [View.parse(v) for v in (args.view or [])]
        P, Q = view_matrices(views, u.symbols) if views else (None, None)
    except (ConstructionDataError, ViewError) as exc:
        print(f"Cannot optimize: {exc}")
        return 1
    # Reverse optimization gives returns in excess of the risk-free rate. Absolute views are stated
    # as total returns, so the risk-free rate is taken off them; in relative views it cancels.
    pi = equilibrium_returns(u.cov, u.current_weights, args.delta)
    Q_excess = None if Q is None else Q - u.risk_free * P.sum(axis=1)
    post = posterior(pi, u.cov, args.tau, P, Q_excess)
    mu = u.risk_free + post.mean                     # total expected returns

    print(f"\nPortfolio construction: {args.portfolio} as of {u.as_of}")
    print(f"{len(u.symbols)} stocks and ETFs; {u.lookback_days} days of returns from {u.start}; "
          f"risk-free {u.risk_free:.2%} (3-month Treasury)")
    print("Expected returns, total % a year. implied = returns that make today's weights optimal;")
    print("bl_return = Black-Litterman blend of implied with "
          + (f"view(s) {'; '.join(args.view)}" if views else "no views (equal to implied)") + "\n")
    print(_fmt(pd.DataFrame({
        "symbol": u.symbols, "weight_now": u.current_weights * 100, "hist_return": u.mean * 100,
        "volatility": np.sqrt(np.diag(u.cov)) * 100, "implied": (u.risk_free + pi) * 100, "bl_return": mu * 100,
    }), {k: "{:.1f}" for k in ("weight_now", "hist_return", "volatility", "implied", "bl_return")}))

    steps = {
        "min_variance": lambda: min_variance(u.cov, args.max_weight),
        "max_sharpe": lambda: max_sharpe(mu, u.cov, u.risk_free, args.max_weight),
        "risk_parity": lambda: risk_parity(u.cov),
        "max_sharpe_hist": lambda: max_sharpe(u.mean, u.cov, u.risk_free, args.max_weight),
        "frontier": lambda: frontier(mu, u.cov, points=8, max_weight=args.max_weight),
    }
    weights = {"current": u.current_weights}
    for name, solve in steps.items():
        try:
            result = solve()
        except OptimizationError as exc:
            print(f"Optimization failed ({name}): {exc}")
            return 1
        if name == "frontier":
            curve = result
        else:
            weights[name] = result

    stats = {k: describe(w, mu, u.cov, u.risk_free) for k, w in weights.items()}
    print(f"\nPortfolios (long-only, max {args.max_weight:.0%} per asset; risk parity is unconstrained by the cap).")
    print("Return and Sharpe use the Black-Litterman returns; max_sharpe_hist uses raw historical means.\n")
    print(_fmt(pd.DataFrame([{"portfolio": k, "exp_return": s.expected_return * 100, "volatility": s.volatility * 100,
                               "sharpe": s.sharpe, "largest": u.symbols[int(np.argmax(s.weights))],
                               "largest_pct": float(np.max(s.weights)) * 100} for k, s in stats.items()]),
               {"exp_return": "{:.2f}", "volatility": "{:.2f}", "sharpe": "{:.2f}", "largest_pct": "{:.1f}"}))

    print("\nWeights (%):\n")
    table = pd.DataFrame({k: w * 100 for k, w in weights.items()}, index=u.symbols).T.reset_index(names="portfolio")
    print(_fmt(table, {s: "{:.1f}" for s in u.symbols}))
    rc = risk_contributions(weights["current"], u.cov)
    print("\nShare of today's portfolio risk by asset (%): "
          + ", ".join(f"{s} {x * 100:.0f}" for s, x in sorted(zip(u.symbols, rc), key=lambda t: -t[1])))

    print("\nEfficient frontier (Black-Litterman returns):\n")
    print(_fmt(pd.DataFrame([{"exp_return": describe(w, mu, u.cov, u.risk_free).expected_return * 100,
                               "volatility": describe(w, mu, u.cov, u.risk_free).volatility * 100,
                               "sharpe": describe(w, mu, u.cov, u.risk_free).sharpe} for w in curve]),
               {"exp_return": "{:.2f}", "volatility": "{:.2f}", "sharpe": "{:.2f}"}))
    _save_optimization(engine, args, u, stats, views)
    return 0


def _save_optimization(engine: Engine, args, u, stats, views) -> None:
    with engine.begin() as conn:
        pid = conn.execute(text("SELECT portfolio_id FROM portfolios WHERE name = :n"), {"n": args.portfolio}).scalar_one()
        for method, s in stats.items():
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
                 "mw": args.max_weight, "views": "; ".join(args.view or []) or None},
            ).scalar_one()
            conn.execute(
                text("INSERT INTO optimization_weights (run_id, instrument_id, weight) VALUES (:r, :i, :w)"),
                [{"r": run_id, "i": iid, "w": float(w)} for iid, w in zip(u.instrument_ids, s.weights)],
            )


def cmd_attribution(engine: Engine, args) -> int:
    try:
        bench = parse_benchmark(args.benchmark)
        a = load_attribution_inputs(engine, args.portfolio, bench, args.since)
    except (ConstructionDataError, ValueError) as exc:
        print(f"Cannot run attribution: {exc}")
        return 1
    daily = [{"w": a.port_weights[t], "r": a.port_returns[t], "W": a.bench_weights, "b": a.bench_returns[t]}
             for t in range(len(a.days))]
    total = link(daily, a.segments)
    years = defaultdict(list)
    for d, item in zip(a.days, daily):
        years[d.year].append(item)

    p, bmk = a.portfolio_daily, a.benchmark_daily
    n = len(a.days)
    ann = lambda R: (1 + R) ** (TRADING_DAYS / n) - 1
    te = float(np.std(p - bmk, ddof=1) * math.sqrt(TRADING_DAYS))
    active_ann = ann(total.portfolio_return) - ann(total.benchmark_return)
    print(f"\nPerformance attribution: {args.portfolio} vs benchmark {args.benchmark} (rebalanced daily)")
    print(f"{n} days, {a.days[0]} to {a.days[-1]}\n")
    print(_fmt(pd.DataFrame([
        {"": "portfolio", "total_return": total.portfolio_return * 100, "annualized": ann(total.portfolio_return) * 100,
         "volatility": float(np.std(p, ddof=1) * math.sqrt(TRADING_DAYS)) * 100},
        {"": "benchmark", "total_return": total.benchmark_return * 100, "annualized": ann(total.benchmark_return) * 100,
         "volatility": float(np.std(bmk, ddof=1) * math.sqrt(TRADING_DAYS)) * 100},
    ]), {"total_return": "{:+.2f}", "annualized": "{:+.2f}", "volatility": "{:.2f}"}))
    print(f"\nActive return {(total.portfolio_return - total.benchmark_return) * 100:+.2f}% "
          f"({active_ann * 100:+.2f}% a year); tracking error {te * 100:.2f}%; "
          f"information ratio {active_ann / te:.2f}")

    print("\nBrinson-Fachler attribution, whole period (percentage points, Carino-linked):\n")
    eff = total.effects.reset_index(names="segment")
    eff.loc[len(eff)] = ["TOTAL", *eff[["allocation", "selection", "interaction", "total"]].sum().tolist()]
    print(_fmt(eff.assign(**{c: eff[c] * 100 for c in ("allocation", "selection", "interaction", "total")}),
               {c: "{:+.2f}" for c in ("allocation", "selection", "interaction", "total")}))

    print("\nBy year (percentage points):\n")
    rows = []
    for y, items in sorted(years.items()):
        res = link(items, a.segments)
        e = res.effects.sum()
        rows.append({"year": y, "portfolio": res.portfolio_return * 100, "benchmark": res.benchmark_return * 100,
                     "allocation": e.allocation * 100, "selection": e.selection * 100,
                     "interaction": e.interaction * 100})
    print(_fmt(pd.DataFrame(rows), {c: "{:+.2f}" for c in ("portfolio", "benchmark", "allocation", "selection",
                                                           "interaction")}))
    print("\nallocation = over/underweighting segments that beat/lagged the benchmark;")
    print("selection = holdings beating/lagging their own segment's benchmark.")
    _save_attribution(engine, args, a)
    return 0


def _save_attribution(engine: Engine, args, a) -> None:
    rows = []
    for t, d in enumerate(a.days):
        e = brinson_fachler(a.port_weights[t], a.port_returns[t], a.bench_weights, a.bench_returns[t])
        for j, seg in enumerate(a.segments):
            rows.append({"d": d, "s": seg, "bm": args.benchmark, "pw": a.port_weights[t, j], "pr": a.port_returns[t, j],
                         "bw": a.bench_weights[j], "br": a.bench_returns[t, j], "al": e["allocation"][j],
                         "se": e["selection"][j], "it": e["interaction"][j]})
    with engine.begin() as conn:
        pid = conn.execute(text("SELECT portfolio_id FROM portfolios WHERE name = :n"), {"n": args.portfolio}).scalar_one()
        conn.execute(text("DELETE FROM attribution_daily WHERE portfolio_id = :p AND benchmark = :b"),
                     {"p": pid, "b": args.benchmark})
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
