# 03 · Derivatives: options, implied volatility and SOFR swaps

This note defines how Ballast prices options, measures their Greeks, extracts implied volatility from listed option chains, and values SOFR overnight index swaps. The code is in [`src/ballast/derivatives/`](../../src/ballast/derivatives/), and the formulas are checked by [`tests/test_derivatives_math.py`](../../tests/test_derivatives_math.py) against published figures from Hull and the New York Fed.

## 1. Option pricing

### Black-Scholes-Merton with a continuous dividend yield

$$d_1 = \frac{\ln(S/K) + (r - q + \tfrac{1}{2}\sigma^2)T}{\sigma\sqrt{T}}, \qquad d_2 = d_1 - \sigma\sqrt{T}$$

$$c = S e^{-qT} N(d_1) - K e^{-rT} N(d_2), \qquad p = K e^{-rT} N(-d_2) - S e^{-qT} N(-d_1)$$

Greeks are computed analytically: delta, gamma, vega (per 1.00 of volatility), theta (per year) and rho (per 1.00 of rate). Reports convert vega to $ per volatility point, theta to $ per calendar day and rho to $ per 1% change in rates.

### Binomial trees

`binomial_price` accepts any up/down factors, so textbook trees can be reproduced exactly; `crr_price` uses the Cox-Ross-Rubinstein calibration $u = e^{\sigma\sqrt{\Delta t}}$, $d = 1/u$, with risk-neutral probability $p = (e^{(r-q)\Delta t} - d)/(u - d)$. American options compare continuation value with immediate exercise at every node.

### Validation against Hull, *Options, Futures, and Other Derivatives*

| Example | Published | Ballast |
|---|---|---|
| Black-Scholes, S=42, K=40, r=10%, σ=20%, T=0.5 | call 4.76, put 0.81 | 4.7594, 0.8086 |
| Greeks, S=49, K=50, r=5%, σ=20%, T=20 weeks | price 2.40, delta 0.522, gamma 0.066, vega 12.1, theta −4.31, rho 8.91 | 2.4005, 0.5216, 0.0655, 12.105, −4.305, 8.907 |
| Two-step American put, u=1.2, d=0.8, K=52 | European 4.1923, American 5.0894 | 4.1927, 5.0896 (Hull rounds p to 0.6282) |
| CRR American put, S=K=50, r=10%, σ=40%, 5 months | 4.49 (5 steps); 4.263, 4.272, 4.278, 4.283 (30, 50, 100, 500 steps) | identical |

Further tests: put-call parity with dividends, analytic Greeks against finite differences, Black-76 on the forward equal to Black-Scholes, the European tree converging to Black-Scholes, an American call without dividends never exercised early, and implied-volatility round trips.

## 2. Options inside the portfolio

| Item | Convention |
|---|---|
| Units | `quantity` = shares covered (1 contract = 100); price = premium per share |
| Spot | The underlying's as-traded close |
| Volatility | Realized volatility over the last 63 trading days (about 3 months): standard deviation of daily log returns of the adjusted close × √252 |
| Rate | Continuously compounded Treasury zero rate to expiry, from that day's curve |
| Dividends | Trailing 12-month dividends ÷ spot, as a continuous yield |
| Model | Black-Scholes for European options. American options (listed SPY and NVDA options are American) use a 500-step CRR tree with Hull's control variate: American tree − European tree + Black-Scholes |
| Expiry | Valued at intrinsic on the expiry date; no marks afterwards |

`ballast price-options` writes these marks to `prices` with source `model:BS_realized_vol`, so options flow through the same positions and P&L views as every other holding.

**These are model marks, not market prices.** Options trade at implied volatility, which usually differs from realized volatility. Section 3 measures the market's implied volatility directly.

**Greeks.** European options use the exact Black-Scholes Greeks. American options bump and reprice the same control-variate pricer that produces the marks: spot ±1%, volatility ±1 point, rates ±1bp, and one calendar day for theta.

**Why the control variate.** A binomial tree's price moves in small steps as the strike falls between different nodes, and gamma (a second difference) magnifies that noise. On real data, the plain tree reported the covered NVDA call's gamma at 0.0172 per share, against a true 0.0076. Because the same noise appears in the American and European tree prices, subtracting one from the other cancels it; adding back the exact Black-Scholes price restores the level. A regression test pins this case down.

**Validation on real data.** The NVDA call's Greeks were checked by hand against Black-Scholes: delta 0.199, vega −$28.3 per vol point, theta +$6.4 per day and rho −$8.8 per 1% (short 100 shares), all matching the report. `ballast greeks` also nets option deltas against stock holdings into a delta-adjusted equity exposure per underlying.

## 3. Implied volatility from listed option chains

`ballast vol-smile` downloads one expiry from Yahoo Finance and:

1. Takes mid prices, dropping quotes with no bid, a crossed market, or a bid-ask spread above 50% of the mid.
2. Implies the forward from put-call parity at the five strikes nearest the money, $F = K + e^{rT}(C - P)$, taking the median. This avoids estimating the dividend yield separately.
3. Solves Black-76 implied volatility for each strike's out-of-the-money option (puts below F, calls above), with r the Treasury zero rate to expiry and T measured to 4pm New York on expiry day.
4. Reports ATM volatility, the 90% and 110% moneyness volatilities, the skew between them, and the median gap to Yahoo's own implied volatilities. Every quote is saved to the `option_quotes` table.

Listed SPY options are American; for out-of-the-money options the early-exercise premium is small, so the European formula is the standard approximation. A test prices a synthetic chain with a known skew and checks that the forward and every volatility are recovered to within 10⁻⁶. Yahoo's quotes are often empty outside U.S. market hours, so run the command while the market is open.

## 4. SOFR compounding

Ballast follows the New York Fed's conventions ([SOFR Averages and Index](https://www.newyorkfed.org/markets/opolicy/operating_policy_200212)):

- SOFR for business day *b* applies until the next business day as simple interest, so each rate compounds once over the *n* calendar days it covers: $(1 + \text{SOFR}_b \cdot n/360)$.
- The 30-day average published on day *P* covers calendar days *P*−30 to *P*−1: $\left(\prod (1 + \text{SOFR}_i\, n_i/360) - 1\right) \times 360/30$. If the window starts on a non-business day, the preceding business day's SOFR covers those days.

**Validation.** A test reproduces the New York Fed's published SOFR Index worked example to all 8 decimals (1.00005000, 1.00010084, 1.00014917, 1.00019779, 1.00034365, including a Friday rate applied over the weekend). `ballast sofr-check` compares Ballast's 30-day averages, computed from FRED's daily SOFR, with the averages the New York Fed actually published. On the full history loaded (1,178 publication days, February 2022 to October 2026), every day matches exactly at the published 5 decimals; the largest difference is 0.0005bp, the rounding of the last digit.

## 5. SOFR overnight index swaps

A receive-fixed swap on notional *N* exchanges, each annual period *k*, the fixed amount $N K \tau_k$ for the floating amount $N (A_k - 1)$, where $\tau_k$ = days/360 and $A_k$ is SOFR compounded in arrears over the period.

On valuation day *d*, with discount factors measured from *d*:

$$PV_{fixed} = N K \sum_k \tau_k\, DF(e_k), \qquad PV_{float} = N\,(A - DF(e_{last}))$$

where *A* is the realized SOFR growth from the current period's start to *d*, from actual fixings. The floating leg telescopes because the current period is worth $N(A - DF(e))$ and each future period $N(DF(s_k) - DF(e_k))$. A test checks the telescoped formula against the period-by-period sum. The par rate is $(A - DF(e_{last})) / \sum_k \tau_k DF(e_k)$.

| Item | Convention |
|---|---|
| Units | `quantity` = notional ÷ 100; positive = receive fixed, negative = pay fixed; price = receive-fixed value per 100 notional |
| Fixed rate | A blank `fixed_rate` in `swap_terms.csv` means "traded at market": `ballast price-swaps` sets it to the par rate on the effective date, so the swap is worth zero at inception |
| Payments | Net payments are recorded as `swap_payment` (which can be negative) and credited as income |
| Risk | DV01 and key-rate DV01 on the same 11 Treasury tenors as the bonds, so `ballast rates-risk` adds them up into one book |

**The seeded swap hedges the bonds.** Paying fixed on $50,000 of 10-year notional offsets most of the bonds' DV01. The key-rate table shows what a single swap cannot hedge: the bonds' exposure sits at 2, 7, 10, 20 and 30 years, the swap's mostly at 10 years.

## 6. Known limitations

- **Treasury curve as a SOFR proxy.** There is no free source of SOFR swap rates, so swaps are discounted and projected on the Treasury zero curve. Real SOFR swap rates differ from Treasury yields by the swap spread (negative at long maturities in recent years), so this approximation misses swap-spread risk.
- **Simplified swap conventions.** Annual periods on unadjusted dates, no 2-day payment lag, and no business-day adjustment.
- **Model marks for options.** Realized rather than implied volatility; no volatility surface in the daily marks.
- **No option contract adjustments.** Splits do not adjust option terms; neither seeded option spans a split.
- **Illustrative positions.** The two options and the swap are hypothetical contracts, labeled as such.
