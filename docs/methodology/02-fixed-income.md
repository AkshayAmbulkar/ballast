# 02 · Fixed income: curve, bond pricing and interest-rate risk

This note defines how Ballast builds the U.S. Treasury curve, values bonds, and measures interest-rate risk. The code is in [`src/ballast/fixed_income/`](../../src/ballast/fixed_income/). Each formula below is checked by a test in [`tests/test_fixed_income_math.py`](../../tests/test_fixed_income_math.py), several of them against published textbook figures.

## 1. Input data

FRED publishes the Treasury's constant-maturity (CMT) yields for 11 tenors: 1, 3 and 6 months and 1, 2, 3, 5, 7, 10, 20 and 30 years (`DGS1MO` … `DGS30`). The Treasury describes its official curve as **"a par yield curve"** fitted to indicative bid-side prices of the most recently auctioned bills, notes and bonds, with bill rates converted to bond-equivalent yields ([Treasury yield curve methodology](https://home.treasury.gov/policy-issues/financing-the-government/interest-rate-statistics/treasury-yield-curve-methodology)).

So each CMT yield is read as a **par yield on a bond-equivalent (semiannual) basis**: a bond paying a semiannual coupon equal to that yield is worth exactly 100.

A date's curve is used only if it has at least 6 tenors including 3 months, 2 years and 10 years; missing tenors are filled by the interpolation below.

## 2. Bootstrapping the zero curve

**Bills (under 6 months)** pay once at maturity, so with the bond-equivalent simple-interest convention:

$$DF(t) = \frac{1}{1 + y\,t}$$

**Six months and longer.** Par yields are interpolated linearly in maturity onto a semiannual grid (0.5, 1.0, …, 30 years) and held flat beyond 30 years. On the grid, a par bond with coupon $c = y_n/2$ per half-year satisfies

$$1 = c\sum_{i=1}^{n} DF_i + DF_n \quad\Longrightarrow\quad DF_n = \frac{1 - c\sum_{i=1}^{n-1} DF_i}{1 + c}$$

which is solved one point at a time from the short end.

**Between knots**, $\ln DF$ is interpolated linearly in time. This makes the continuously compounded forward rate constant between knots, and keeps discount factors positive and decreasing whenever the knots are.

Zero rates are reported two ways: continuous, $r = -\ln DF(t)/t$, and bond-equivalent (semiannual), $z = 2\,(DF(t)^{-1/(2t)} - 1)$.

`ballast build-curves` stores every date's curve in the `zero_curves` table (62 points per day: 1M, 3M and every 6 months to 30 years), so the curve history can be queried in SQL.

## 3. Bonds

| Item | Convention |
|---|---|
| Units | `quantity` = number of 100-face units (300 = $30,000 face) |
| Price | Dirty price per 100 face; clean = dirty − accrued |
| Coupon dates | Generated backward from maturity; month-end maturities pay on month ends (31 Aug → 28/29 Feb) |
| Accrued interest | Actual/Actual (ICMA): coupon × days accrued ÷ days in period |
| Curve pricing | $\text{dirty} = \sum_i CF_i \cdot DF(t_i)$, with $t_i$ = days ÷ 365.25 |
| Yield to maturity | Street convention: $\text{dirty} = \sum_i CF_i\,(1 + y/2)^{-(w + i)}$, $w$ = fraction of the current coupon period left |
| Settlement | Valued for the trade date itself (no T+1 lag) |

A coupon paid on the valuation date belongs to the previous holder: it is excluded from that day's price and credited as income instead.

### Yield-based risk

With cash-flow times $t_i$ in years, present values $PV_i$ and price $P$, compounding $m$ times a year:

$$D_{mac} = \frac{\sum t_i\,PV_i}{P}, \qquad D_{mod} = \frac{D_{mac}}{1 + y/m}, \qquad C = \frac{\sum t_i\,(t_i + 1/m)\,PV_i}{P\,(1 + y/m)^2}, \qquad DV01 = D_{mod}\cdot P \cdot 0.0001$$

## 4. Curve-based risk

Yield-based measures assume one flat yield moves. Curve-based measures shock the **input par yields**, rebuild the curve, and fully reprice.

- **Effective duration and convexity** (central differences, $h$ = 1bp parallel par shift):
  $D_{eff} = \dfrac{P_{-} - P_{+}}{2P_0 h}$, $\quad C_{eff} = \dfrac{P_{+} + P_{-} - 2P_0}{P_0 h^2}$.
- **DV01** is the value gained when every par yield falls 1bp.
- **Key-rate DV01**: the same, but shocking one CMT tenor at a time. Because par yields are interpolated linearly, a shock at one tenor moves the curve only between its neighbouring tenors. Shocking all tenors together is an exact parallel shift, so key-rate DV01s add up to the parallel DV01; a test checks this to a relative tolerance of 10⁻⁶.

Small negative key-rate DV01s at tenors shorter than a bond's maturity are expected with par-curve shocks. Raising one short par yield raises the discount factors the bootstrap needs at longer maturities to keep later par bonds at 100.

### Scenarios

Each scenario is a shift to the 11 par yields, followed by full revaluation. Ballast also reports the first-order estimate $-\sum_k KRDV01_k \cdot \Delta y_k$, so the gap between the two columns is the convexity (second-order) effect.

| Scenario | Shift |
|---|---|
| `parallel_up_100`, `parallel_down_100` | ±100bp at every tenor |
| `bear_steepener` | 0bp at ≤2Y, rising linearly to +50bp at ≥10Y |
| `bull_flattener` | 0bp at ≤2Y, falling linearly to −50bp at ≥10Y |
| `twist_steepener` | −25bp at ≤2Y, rising linearly to +25bp at ≥10Y |
| `hist_2022` | The actual change in each tenor over calendar 2022, applied to today's curve |

## 5. Bonds inside the portfolio

`ballast price-bonds` writes each bond's dirty price into the `prices` table (source `model:UST_CMT`) and its coupons into `corporate_actions` as `coupon`. The positions and P&L views from [01](01-positions-and-pnl.md) then treat bonds like any other holding: the `income` column is dividends plus coupons.

On a coupon date the dirty price drops by the coupon and the same amount is credited as income, so daily P&L shows no artificial jump. In testing, a 300-unit position's market value fell $631.66 on its coupon date while $637.50 of coupon income was credited, for a daily P&L of +$5.84, in line with surrounding days.

**One calendar.** Bonds are marked on every day the equities are, using the most recent curve on or before that day (no more than 7 days old). This keeps portfolio totals consistent on days only one market is open, such as Columbus Day, when stocks trade but the bond market is closed.

## 6. Empirical duration of an ETF

`ballast rate-sensitivity` regresses an ETF's daily total return (from the adjusted close) on the daily change in a Treasury yield:

$$r_t = \alpha + \beta\,\Delta y_t + \varepsilon_t, \qquad D_{emp} = -\beta$$

For TLT against the 20-year yield, this gives a market-implied duration that can be compared with model durations of long Treasuries, and splits TLT's return into the part explained by the change in yields ($-D_{emp}\,\Delta y$) and the rest (carry, curve shape and noise).

## 7. Validation

| Test | Benchmark |
|---|---|
| Bootstrap from bond prices | Hull, *Options, Futures, and Other Derivatives*, ch. 4: zero rates 10.127%, 10.469%, 10.536%, 10.681%, 10.808% reproduced to 3 decimals |
| Duration and convexity | Hull, ch. 4: 3-year 10% bond at a 12% continuous yield: price 94.213, duration 2.653, convexity 7.570 reproduced |
| Par bootstrap | Every par bond on the semiannual grid reprices to 100 within 10⁻¹⁰, on an upward-sloping and an inverted curve |
| Flat curve | A flat par curve gives a flat zero curve; effective duration equals modified duration |
| Accrued interest | 108 of 181 days on a 4.25% note: 2.125 × 108 ÷ 181 |
| Analytic vs numeric | Analytic DV01 and convexity match finite differences |
| Key rates | Sum to parallel DV01; zero exposure to tenors beyond maturity |
| P&L | SQL views and the independent day-by-day simulation agree exactly with bonds included |

## 8. Known limitations

- **The bonds are illustrative.** The three seeded Treasuries are hypothetical securities, valued off the CMT curve rather than from market quotes for a specific CUSIP. Real holdings can be added through `bond_terms.csv`.
- **The CMT curve is a smoothed on-the-run curve.** Off-the-run bonds trade at small spreads to it, which this model ignores.
- **Linear par interpolation** is simpler than the Treasury's own monotone convex method, so curve shapes between published tenors differ slightly.
- **Redemption at maturity is not modeled.** None of the seeded bonds matures within the data window.
- **No settlement lag, financing (repo) or bid-ask.**
