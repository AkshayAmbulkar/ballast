# 05 · Portfolio construction and performance attribution

This note defines how Ballast builds alternative allocations and explains past performance against a benchmark. The code is in [`src/ballast/construction/`](../../src/ballast/construction/) and is tested by [`tests/test_construction_math.py`](../../tests/test_construction_math.py) and [`tests/test_construction_db.py`](../../tests/test_construction_db.py).

## 1. Universe and inputs

Construction uses the portfolio's **stocks and ETFs** (the assets with total-return history): AAPL, GLD, JPM, MSFT, NVDA, SPY, TLT and XOM in the seed portfolio. Bonds, options and the swap are overlays and are left out.

| Input | Definition |
|---|---|
| Returns | Daily simple returns from the adjusted close, last 756 trading days (about 3 years) |
| Covariance | Sample covariance × 252 |
| Historical means | Average daily return × 252 (shown for comparison; too noisy to optimize on) |
| Risk-free rate | 3-month Treasury zero rate from the bootstrapped curve, as an annual simple rate |
| Current weights | Today's market values of those assets, normalised |

## 2. Optimizers

All portfolios are fully invested (weights sum to 1). The constrained ones are long-only with a cap per asset (default 35%) and are solved with sequential quadratic programming (SciPy SLSQP).

| Portfolio | Objective |
|---|---|
| `min_variance` | Lowest volatility |
| `max_sharpe` | Highest (expected return − risk-free) / volatility, using Black-Litterman returns |
| `max_sharpe_hist` | The same, using raw historical means, to show how unstable that is |
| `risk_parity` | Equal risk contribution: each asset supplies the same share of portfolio variance |
| frontier | Minimum volatility for evenly spaced target returns, from the minimum-variance portfolio to the highest return the caps allow |

**Checks against closed forms.** Without constraints, minimum variance is Σ⁻¹1 / 1′Σ⁻¹1 and the tangency portfolio is Σ⁻¹(μ − r_f) normalised; the solver reproduces both. Every frontier point has a Sharpe ratio no higher than the max-Sharpe portfolio.

**The top of the frontier.** With a 35% cap, the highest attainable return comes from a single corner portfolio (35% in each of the two highest-returning assets and 30% in the third). Rounding can make that one point look infeasible to the solver, so the top target sits 10⁻⁹ (a ten-millionth of a percentage point) below it. Targets are imposed as "return ≥ target", which gives the same portfolios as an equality on the efficient part of the frontier. A test checks 100 universes like the seed portfolio's.

**Risk parity** solves the convex problem of Spinu (2013), minimize ½ y′Σy − Σᵢ bᵢ ln yᵢ, and rescales w = y / Σy. Each asset's share of variance, wᵢ(Σw)ᵢ / w′Σw, then equals its budget bᵢ. The problem is solved with damped Newton steps (gradient Σy − b/y, Hessian Σ + diag(b/y²)), which converge to machine precision, and the result is rejected unless every risk share matches its budget to 10⁻⁸. With uncorrelated assets this is inverse-volatility weighting, which a test confirms exactly; another checks 200 covariance matrices with volatilities from 0.5% to 150% and correlations up to 0.995.

## 3. Black-Litterman

Raw historical means make optimizers pile into whatever happened to do well. Black-Litterman starts instead from the returns that make a reference allocation optimal, and moves them only as far as the investor's views justify.

1. **Implied (equilibrium) excess returns:** π = δ Σ w_ref, with risk aversion δ = 2.5 and w_ref today's weights. (With market-capitalisation weights this is the classic CAPM equilibrium; using the portfolio's own weights asks "what must I believe to hold what I hold?")
2. **Views:** `--view NVDA:0.25` (NVDA returns 25% a year) or `--view NVDA-SPY:0.05` (NVDA beats SPY by 5% a year). Absolute views are total returns, so the risk-free rate is subtracted; in relative views it cancels.
3. **Posterior** (He and Litterman, 1999), with τ = 0.05 and view uncertainty Ω = diag(P τΣ P′):
   μ_BL = π + τΣP′(PτΣP′ + Ω)⁻¹(Q − Pπ). This is the Woodbury form of the textbook precision-weighted formula; it is identical in theory and stays numerically stable as views approach certainty (Ω → 0), where the textbook form loses precision.

Expected returns reported are total returns: risk-free + excess.

**Validation against He and Litterman (1999).** Their seven-country example (correlations, volatilities and market weights from the paper) is reproduced in the tests:

| | Australia | Canada | France | Germany | Japan | UK | USA |
|---|---|---|---|---|---|---|---|
| Equilibrium returns, published (%) | 3.9 | 6.9 | 8.4 | 9.0 | 4.3 | 6.8 | 7.6 |
| Posterior with "Germany beats the rest of Europe by 5%", published (%) | 4.3 | 7.6 | 9.3 | 11.0 | 4.5 | 7.0 | 8.1 |
| Optimal weights, published (%) | 1.5 | 2.1 | ≈ −4 | 35.4 | 11.0 | −9.5 | 58.6 |

Ballast matches every figure to the published decimal (France computes to −3.97). A further test confirms the paper's key property: the view changes the weights only of the assets in it (Germany, France, UK), while the others stay at their equilibrium weights scaled by 1/(1 + τ).

## 4. Performance attribution

`ballast attribution` explains the portfolio's return against a benchmark (default 60% SPY / 40% TLT, rebalanced daily).

**Segments.** Each holding belongs to a segment: stocks and SPY to `equity`; TLT, Treasuries and the swap to `rates`; GLD to `gold`; options to their underlying's segment. The `instruments.segment` column overrides the default.

**Daily returns.** A segment's return on day t is its P&L (from the P&L views, including dividends, coupons and swap payments) divided by its market value at the previous close; its weight is that market value's share of the total.

**Brinson-Fachler effects** for segment s, with portfolio weight and return (w, r), benchmark weight and return (W, b), and total benchmark return B:

| Effect | Formula | Question it answers |
|---|---|---|
| Allocation | (w − W)(b − B) | Did we overweight segments that beat the benchmark? |
| Selection | W (r − b) | Did our holdings beat their segment's benchmark? |
| Interaction | (w − W)(r − b) | The overlap of the two |

They add up exactly to the day's active return. A segment held only by the portfolio (gold here) uses its own return as its benchmark, so its whole contribution counts as allocation; a segment only in the benchmark is treated the same way in reverse.

**Linking over time.** Daily effects do not add up to the cumulative active return, because returns compound. Carino (1999) scales each day's effects by kₜ / K, with k = [ln(1+R) − ln(1+B)] / (R − B), so the linked effects add up exactly to the cumulative portfolio return minus the cumulative benchmark return. A test checks this over 500 random days.

The report also gives annualized return and volatility, tracking error and information ratio. Daily effects are stored in `attribution_daily`.

## 5. Known limitations

- **Sample covariance** with no shrinkage. With 8 assets and 756 days it is well conditioned, but a larger universe would call for a shrinkage estimator.
- **Mean-variance assumptions:** one period, normally distributed returns, no transaction costs or turnover limits.
- **Reference weights for Black-Litterman** are the portfolio's own, not market capitalisations, which are not in the data.
- **Attribution uses end-of-day positions,** so trades at the close contribute from the following day, consistent with the P&L views.
