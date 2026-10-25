# 04 · Risk: VaR, Expected Shortfall, backtesting and stress tests

This note defines how Ballast measures the portfolio's market risk. The code is in [`src/ballast/risk/`](../../src/ballast/risk/) and is tested by [`tests/test_risk_math.py`](../../tests/test_risk_math.py) and [`tests/test_risk_db.py`](../../tests/test_risk_db.py).

All figures are **1-day** and reported as **positive losses in dollars**.

## 1. Risk factors

On the portfolio's valuation calendar (equity trading days), Ballast tracks:

- one **simple daily return per stock or ETF**, from the adjusted close (total return including dividends), and
- the **daily change in each of the 11 Treasury CMT par yields** (decimals), each day using the latest curve published on or before it.

A historical scenario is one past day's set of factor changes. The default history is the last **500 trading days** (about two years).

## 2. Full revaluation

Every scenario reprices the whole portfolio as it stands at the valuation date, as an instantaneous shock (no time passes):

| Position | Revaluation |
|---|---|
| Stocks, ETFs | market value × return |
| Bonds | Each bond is a sum of cash flows, so its value is linear in discount factors. Each scenario's par curve is bootstrapped (vectorized across scenarios; agrees with the scalar bootstrap to 10⁻¹⁴) and the bond is repriced. |
| SOFR swap | Also linear in discount factors: 100 × (K Σ τ_k DF(e_k) − (A − DF(e_last))), with realized SOFR growth A fixed at the valuation date |
| Options | Priced on a grid of 41 spot levels from −35% to +35% (log scale) with the same control-variate tree as the daily marks, then interpolated with a cubic spline (within 0.002 per share of direct pricing in tests). Volatility, rates and dividend yield are held fixed. |

## 3. Three ways to compute VaR

For *n* scenario P&Ls at confidence *a*, let *k* = ⌊n(1 − a)⌋, the number of scenarios allowed to lose more than VaR.

- **VaR** is the (k+1)-th worst loss, so exactly *k* scenarios lose more. With 500 scenarios at 99%, VaR is the 6th-worst loss.
- **Expected Shortfall** is the average of the *k* worst losses, the losses beyond VaR.

| Method | How |
|---|---|
| **Historical simulation** | Revalue the portfolio under each of the last 500 days' actual factor changes. Fat tails and correlations come straight from the data. |
| **Parametric** (delta-normal) | P&L ~ N(0, s′Σs), with *s* the dollar sensitivity to each factor (stock values plus option deltas; key-rate sensitivities for bonds and swaps) and Σ the EWMA covariance (RiskMetrics, λ = 0.94). VaR = z·σ, ES = σ·φ(z)/(1 − a): 2.326σ and 2.665σ at 99%. |
| **Monte Carlo** | 10,000 draws of factor changes from N(0, Σ), with the same full revaluation as historical simulation, so option convexity is captured. Seeded, so results are reproducible. |

Ballast also reports each asset class's VaR on its own. The gap between the sum of those and the portfolio VaR is the **diversification benefit**.

**Why the methods differ.** Parametric VaR treats options as linear (delta only) and assumes normal returns; Monte Carlo keeps normality but revalues options in full; historical simulation drops normality altogether. Comparing the three separates the effect of fat tails from the effect of option convexity.

## 4. Backtesting

For every trading day *t* since January 2024, Ballast sets VaR at the previous close (positions, prices and factor history up to *t*−1) and compares it with the **actual P&L** booked on *t* from the P&L views. An **exception** is a day where the loss exceeded VaR. Historical and parametric VaR are both backtested; results go to the `var_backtest` table, with a `var_backtest_by_year` summary view.

| Test | Question | Statistic |
|---|---|---|
| Kupiec (1995) proportion of failures | Are exceptions as frequent as the confidence level implies? | LR = −2 ln[(1−p)^(n−x) p^x] + 2 ln[(1−x/n)^(n−x) (x/n)^x], χ²(1) |
| Christoffersen (1998) independence | Do exceptions cluster, so that one makes another more likely the next day? | Likelihood ratio on the 2×2 transition counts, χ²(1) |
| Conditional coverage | Both at once | LR_pof + LR_ind, χ²(2) |
| Basel traffic light (1996) | Regulatory zone over the latest 250 days at 99% | green 0–4 exceptions, yellow 5–9, red 10+ |

The Kupiec statistic is checked against an independent binomial likelihood ratio in the tests.

**Actual vs hypothetical P&L.** The backtest uses the P&L actually booked, which also includes things VaR holds fixed: changes in the options' realized volatility, coupon accrual and the passage of time. Regulators call the alternative, revaluing yesterday's positions at today's prices only, "hypothetical P&L".

## 5. Stress tests

Each episode's actual factor moves, from its start date to its end date (each stock's total return, each par yield's change), are applied instantly to today's portfolio with full revaluation. Options are repriced directly, since stress moves can exceed the VaR price grid.

| Episode | Window | What happened |
|---|---|---|
| `rates_2022` | 3 Jan – 30 Dec 2022 | Fastest Fed hiking cycle in decades; stocks and bonds fell together |
| `svb_2023` | 8 – 13 Mar 2023 | Silicon Valley Bank failure; the 2-year yield collapsed |
| `yen_carry_2024` | 31 Jul – 5 Aug 2024 | Yen carry-trade unwind and global equity selloff |
| `deepseek_2025` | 24 – 27 Jan 2025 | NVIDIA fell about 17% in one day |
| `tariffs_2025` | 2 – 8 Apr 2025 | U.S. tariff announcement and the selloff that followed |

## 6. Known limitations

- **No volatility factor.** Option volatility is held fixed in VaR and stress tests, so vega risk is not captured.
- **Instantaneous shocks.** No theta, carry or roll-down within the 1-day horizon.
- **Normality in parametric and Monte Carlo.** Both understate fat tails; historical simulation does not.
- **Equal weighting in historical simulation.** All 500 days count equally, so the VaR reacts slowly when volatility changes; the EWMA methods react faster.
- **Grid edges.** Daily option moves beyond ±35% are clipped to the edge of the price grid in VaR (stress tests reprice directly).
- **SOFR swap on the Treasury curve,** as in [03](03-derivatives.md): swap-spread risk is not modeled.
