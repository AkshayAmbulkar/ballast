# 01 · Positions and daily P&L

This note defines how Ballast turns a trade ledger and market data into positions and daily profit and loss. The production numbers come from the SQL views in [`db/migrations/004_positions_and_pnl.sql`](../../db/migrations/004_positions_and_pnl.sql). An independent day-by-day simulation in [`src/ballast/portfolio/pnl.py`](../../src/ballast/portfolio/pnl.py) computes the same numbers a different way, and the test suite requires the two to agree exactly.

## 1. Source of truth

Positions are never stored. They are derived from the `trades` table each time they are queried. The ledger is the single source of truth, so any historical position can be rebuilt and audited, and correcting a trade automatically corrects every number downstream.

## 2. Prices are stored as traded

Market data vendors usually publish history that is back-adjusted for later stock splits. Yahoo Finance does this for its `Close` column, its dividend amounts, and its volumes. Ballast converts these back to **as-traded** values before storing them:

$$
\text{close}^{\text{as traded}}_t = \text{close}^{\text{Yahoo}}_t \times \prod_{s \,>\, t} r_s
$$

where $r_s$ is the split ratio on date $s$ (10 for a 10-for-1 split). Dividends are converted with the same factor, and volumes are divided by it.

Why: trades are booked in as-traded units. If market prices were split-adjusted and trade prices were not, every position held across a split would be misvalued by the split ratio.

Because Yahoo adjusts for every split up to the present day, Ballast always fetches history through today. Fetching an old window on its own would miss later splits.

## 3. Quantity held

A trade of $q$ shares on day $t$ counts as

$$
q \times \prod_{t \,<\, s \,\le\, d} r_s
$$

shares on day $d$. The position is the sum over all trades with $t \le d$. The product is computed with a custom exact `NUMERIC` aggregate (`numeric_product`) rather than `exp(sum(ln(x)))`, which would put floating-point error into share counts.

## 4. Daily P&L

For a position on trading day $d$, with previous trading day $p$:

$$
\text{P\&L}_d = MV_d - MV_p - \text{TradeCash}_{(p,\,d]} + \text{Dividends}_{(p,\,d]}
$$

| Term | Definition |
|---|---|
| $MV_d$ | quantity on $d$ × as-traded close on $d$ |
| $\text{TradeCash}_{(p,d]}$ | $\sum (\text{quantity} \times \text{price} + \text{fees})$ for trades dated after $p$ up to $d$. Buys are positive (cash out), sells negative (cash in). |
| $\text{Dividends}_{(p,d]}$ | $\sum (\text{dividend per share} \times \text{quantity at } p\text{'s close})$ for ex-dates after $p$ up to $d$ |

Dividends are recognised on the **ex-date** (accrual basis). From Phase 2, bond coupons are credited the same way on their payment date, and the view's `income` column holds dividends plus coupons. That is the day the share price drops by the dividend, so recognising income the same day keeps daily P&L free of an artificial loss. A short position has negative quantity and therefore pays the dividend.

Summing over a position's life gives the accounting identity that the tests check:

$$
\sum_d \text{P\&L}_d = MV_{\text{final}} - \sum \text{TradeCash} + \sum \text{Dividends}
$$

## 5. Worked example

The same example is a unit test (`tests/test_pnl.py`).

| Day | Close | Event | Shares | Daily P&L |
|---|---|---|---|---|
| D1 | 100.00 | Buy 100 @ 100.50, fee 1.00 | 100 | 10,000 − 10,051 = **−51** |
| D2 | 102.00 | | 100 | 10,200 − 10,000 = **200** |
| D3 | 51.50 | 2-for-1 split (ex-date) | 200 | 10,300 − 10,200 = **100** |
| D4 | 52.00 | Dividend 0.50 (ex-date) | 200 | 10,400 − 10,300 + 100 = **200** |
| D5 | 53.00 | Sell 100 @ 52.80, fee 1.00 | 100 | 5,300 − 10,400 + 5,279 = **179** |
| | | | **Total** | **628** |

Cross-check by economics: the cost per share after the split is 50.25. Selling 100 at 52.80 earns 255.00, the 100 shares still held are up 275.00, fees cost 2.00 and dividends add 100.00, for a total of 628.00.

## 6. Data-quality checks

Each check is a SQL view that returns the rows that fail it, so an empty view is a pass. `ballast check` runs them all and exits with code 1 if any fail.

| View | Catches |
|---|---|
| `dq_trades_off_calendar` | Trades dated on a day with no closing price (weekend, holiday, typo) |
| `dq_trade_price_outliers` | Trade prices more than 10% from that day's close (fat finger, wrong split units) |
| `dq_unexplained_price_jumps` | Daily moves above 40% after accounting for any split that day (missing or wrong split) |

A trade on a non-trading day is not lost: it falls into the next trading day's bucket. It is still flagged so it can be reviewed.

## 7. Known limitations

These are deliberate simplifications for Phase 1, listed so they are not mistaken for oversights.

- **Single currency.** All instruments are USD; there is no FX conversion.
- **Exchange calendar.** Each position is valued on the days its own prices exist. Mixing instruments from exchanges with different holidays would make portfolio totals jump on days only one exchange is open.
- **Split and dividend on the same ex-date.** The dividend is taken in post-split units while the shares are counted pre-split. This is rare and is not handled.
- **No financing costs.** Short positions do not pay borrow fees, and cash does not earn interest.
- **Vendor data.** Yahoo Finance data is obtained through `yfinance`, an unofficial library intended for research and education. Price repair is enabled by default to correct known Yahoo errors, but the data is not of institutional quality.
