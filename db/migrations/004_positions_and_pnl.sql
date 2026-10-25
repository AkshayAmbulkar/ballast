-- 004: Positions and daily P&L, derived from the trade ledger.
--
-- Daily P&L for one position on trading day d (previous trading day p):
--
--     pnl_d = MV_d - MV_p - trade_cash_(p, d] + dividends_(p, d]
--
--   MV_d                market value at d = quantity_d * close_d (as traded)
--   trade_cash_(p, d]   sum of (quantity * price + fees) for trades after p up to d
--                       (cash paid for buys is positive, cash received for sells negative)
--   dividends_(p, d]    sum of (dividend per share * quantity held at close of p)
--                       for ex-dates after p up to d. Recognised on the ex-date
--                       (accrual basis), when the price drops by the dividend.
--
-- Splits: a trade of q shares on day t counts as q * (product of split ratios
-- with ex_date in (t, d]) shares on day d.
--
-- Full derivation and known limitations: docs/methodology/01-positions-and-pnl.md

-- Exact product aggregate for NUMERIC. (exp(sum(ln(x))) would introduce
-- floating-point error into share counts.) Returns 1 when there are no rows.
CREATE AGGREGATE numeric_product(NUMERIC) (
    SFUNC    = numeric_mul,
    STYPE    = NUMERIC,
    INITCOND = '1'
);

-- Cumulative split ratio for splits with ex_date in (p_after, p_through].
CREATE FUNCTION split_factor(p_instrument_id BIGINT, p_after DATE, p_through DATE)
RETURNS NUMERIC
LANGUAGE sql
STABLE
AS $$
    SELECT numeric_product(value)
    FROM corporate_actions
    WHERE instrument_id = p_instrument_id
      AND action_type   = 'split'
      AND ex_date >  p_after
      AND ex_date <= p_through
$$;


-- Quantity and market value of every position on every trading day,
-- starting from its first trade.
CREATE VIEW positions_daily AS
WITH holdings AS (
    SELECT portfolio_id, instrument_id, MIN(trade_date) AS first_trade_date
    FROM trades
    GROUP BY portfolio_id, instrument_id
)
SELECT h.portfolio_id,
       h.instrument_id,
       p.price_date              AS as_of_date,
       q.quantity,
       p.close,
       q.quantity * p.close      AS market_value
FROM holdings h
JOIN prices p
  ON p.instrument_id = h.instrument_id
 AND p.price_date   >= h.first_trade_date
CROSS JOIN LATERAL (
    SELECT COALESCE(SUM(t.quantity * split_factor(t.instrument_id, t.trade_date, p.price_date)), 0) AS quantity
    FROM trades t
    WHERE t.portfolio_id  = h.portfolio_id
      AND t.instrument_id = h.instrument_id
      AND t.trade_date   <= p.price_date
) q;


-- Daily P&L per position.
CREATE VIEW position_pnl_daily AS
WITH base AS (
    SELECT pd.*,
           LAG(pd.as_of_date)               OVER w      AS prev_date,
           COALESCE(LAG(pd.quantity)        OVER w, 0)  AS prev_quantity,
           COALESCE(LAG(pd.market_value)    OVER w, 0)  AS prev_market_value
    FROM positions_daily pd
    WINDOW w AS (PARTITION BY pd.portfolio_id, pd.instrument_id ORDER BY pd.as_of_date)
)
SELECT b.portfolio_id,
       b.instrument_id,
       b.as_of_date,
       b.quantity,
       b.close,
       b.market_value,
       b.prev_market_value,
       tc.trade_cash,
       dv.dividend_income,
       b.market_value - b.prev_market_value - tc.trade_cash + dv.dividend_income AS daily_pnl
FROM base b
CROSS JOIN LATERAL (
    -- First row of a position (prev_date IS NULL) picks up every trade up to that day.
    SELECT COALESCE(SUM(t.quantity * t.price + t.fees), 0) AS trade_cash
    FROM trades t
    WHERE t.portfolio_id  = b.portfolio_id
      AND t.instrument_id = b.instrument_id
      AND t.trade_date   <= b.as_of_date
      AND (b.prev_date IS NULL OR t.trade_date > b.prev_date)
) tc
CROSS JOIN LATERAL (
    SELECT COALESCE(SUM(ca.value * b.prev_quantity), 0) AS dividend_income
    FROM corporate_actions ca
    WHERE ca.instrument_id = b.instrument_id
      AND ca.action_type   = 'cash_dividend'
      AND b.prev_date IS NOT NULL
      AND ca.ex_date >  b.prev_date
      AND ca.ex_date <= b.as_of_date
) dv;


-- Daily P&L for the whole portfolio, with running cumulative P&L.
CREATE VIEW portfolio_pnl_daily AS
SELECT portfolio_id,
       as_of_date,
       SUM(market_value)    AS market_value,
       SUM(trade_cash)      AS net_trade_cash,
       SUM(dividend_income) AS dividend_income,
       SUM(daily_pnl)       AS daily_pnl,
       SUM(SUM(daily_pnl)) OVER (PARTITION BY portfolio_id ORDER BY as_of_date) AS cumulative_pnl
FROM position_pnl_daily
GROUP BY portfolio_id, as_of_date;


-- Open positions on each portfolio's most recent valuation date.
CREATE VIEW holdings_latest AS
WITH last_date AS (
    SELECT portfolio_id, MAX(as_of_date) AS as_of_date
    FROM positions_daily
    GROUP BY portfolio_id
),
lifetime AS (
    SELECT portfolio_id,
           instrument_id,
           SUM(daily_pnl)       AS total_pnl,
           SUM(dividend_income) AS total_dividends
    FROM position_pnl_daily
    GROUP BY portfolio_id, instrument_id
)
SELECT pd.portfolio_id,
       pd.as_of_date,
       i.symbol,
       i.asset_class,
       pd.quantity,
       pd.close,
       pd.market_value,
       pd.market_value / NULLIF(SUM(pd.market_value) OVER (PARTITION BY pd.portfolio_id), 0) AS weight,
       l.total_pnl,
       l.total_dividends
FROM positions_daily pd
JOIN last_date ld   ON ld.portfolio_id = pd.portfolio_id AND ld.as_of_date = pd.as_of_date
JOIN instruments i  ON i.instrument_id = pd.instrument_id
JOIN lifetime l     ON l.portfolio_id  = pd.portfolio_id AND l.instrument_id = pd.instrument_id
WHERE pd.quantity <> 0;
