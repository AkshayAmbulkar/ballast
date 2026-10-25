-- 005: Data-quality checks. Each view returns the rows that FAIL a check,
-- so an empty view means the check passed. `ballast check` runs them all.

-- Trades booked on a day with no closing price for that instrument
-- (weekend, holiday, or typo in the date).
CREATE VIEW dq_trades_off_calendar AS
SELECT t.trade_id, t.trade_ref, i.symbol, t.trade_date
FROM trades t
JOIN instruments i ON i.instrument_id = t.instrument_id
WHERE NOT EXISTS (
    SELECT 1 FROM prices p
    WHERE p.instrument_id = t.instrument_id
      AND p.price_date    = t.trade_date
);

-- Trade prices more than 10 percent away from that day's close
-- (fat-finger price, or a price entered in the wrong split units).
CREATE VIEW dq_trade_price_outliers AS
SELECT t.trade_id, t.trade_ref, i.symbol, t.trade_date, t.price, p.close,
       t.price / p.close - 1 AS deviation
FROM trades t
JOIN prices p      ON p.instrument_id = t.instrument_id AND p.price_date = t.trade_date
JOIN instruments i ON i.instrument_id = t.instrument_id
WHERE abs(t.price / p.close - 1) > 0.10;

-- Day-over-day moves above 40 percent after accounting for any split that day.
-- Catches splits missing from corporate_actions or recorded with the wrong ratio.
CREATE VIEW dq_unexplained_price_jumps AS
WITH moves AS (
    SELECT p.instrument_id,
           p.price_date,
           p.close,
           LAG(p.close) OVER (PARTITION BY p.instrument_id ORDER BY p.price_date) AS prev_close,
           COALESCE((SELECT ca.value FROM corporate_actions ca
                     WHERE ca.instrument_id = p.instrument_id
                       AND ca.action_type   = 'split'
                       AND ca.ex_date       = p.price_date), 1) AS split_ratio
    FROM prices p
)
SELECT i.symbol, m.price_date, m.prev_close, m.close, m.split_ratio,
       m.close * m.split_ratio / m.prev_close - 1 AS split_adjusted_change
FROM moves m
JOIN instruments i ON i.instrument_id = m.instrument_id
WHERE m.prev_close IS NOT NULL
  AND abs(m.close * m.split_ratio / m.prev_close - 1) > 0.40;
