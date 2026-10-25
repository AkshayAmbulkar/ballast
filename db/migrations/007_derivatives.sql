-- 007: Derivatives: options and SOFR swaps.
--
-- * option_terms / swap_terms: contract terms.
-- * Model-marked prices may be zero (an option expiring worthless) or negative
--   (a swap's value), so the positive-price rule now applies to market prices only.
-- * swap_payment: net swap cash flow, which can be negative.
-- * option_quotes: snapshots of listed option chains for implied volatility.
-- * rate_observations gains precision for the NY Fed SOFR averages (5 decimals).
--
-- Units: option quantity = shares covered (1 contract = 100), price per share.
-- Swap quantity = notional / 100, positive = receive fixed, negative = pay fixed,
-- price = receive-fixed value per 100 notional. Details: docs/methodology/03-derivatives.md

-- ---------- precision for SOFR averages ----------
DROP VIEW treasury_cmt_curve;
ALTER TABLE rate_observations ALTER COLUMN value_pct TYPE NUMERIC(12,6);
CREATE VIEW treasury_cmt_curve AS
SELECT o.obs_date, s.tenor_months, s.series_id, o.value_pct
FROM rate_observations o
JOIN rate_series s USING (series_id)
WHERE s.curve = 'UST_CMT';

INSERT INTO rate_series (series_id, curve, tenor_months, description) VALUES
    ('SOFR30DAYAVG', 'SOFR', NULL, '30-day average SOFR (NY Fed), dated by publication day');


-- ---------- price and cash-flow rules ----------
ALTER TABLE prices DROP CONSTRAINT prices_close_check;
ALTER TABLE prices ADD CONSTRAINT prices_close_check CHECK (close > 0 OR source LIKE 'model:%');

ALTER TABLE corporate_actions DROP CONSTRAINT corporate_actions_action_type_check;
ALTER TABLE corporate_actions ADD CONSTRAINT corporate_actions_action_type_check
    CHECK (action_type IN ('cash_dividend', 'split', 'coupon', 'swap_payment'));
ALTER TABLE corporate_actions DROP CONSTRAINT corporate_actions_value_check;
ALTER TABLE corporate_actions ADD CONSTRAINT corporate_actions_value_check
    CHECK (value > 0 OR action_type = 'swap_payment');


-- ---------- contract terms ----------
CREATE TABLE option_terms (
    instrument_id   BIGINT        PRIMARY KEY REFERENCES instruments ON DELETE CASCADE,
    underlying_id   BIGINT        NOT NULL REFERENCES instruments,
    option_type     TEXT          NOT NULL CHECK (option_type IN ('call', 'put')),
    strike          NUMERIC(20,6) NOT NULL CHECK (strike > 0),
    expiry_date     DATE          NOT NULL,
    exercise_style  TEXT          NOT NULL CHECK (exercise_style IN ('european', 'american')),
    multiplier      INTEGER       NOT NULL DEFAULT 100 CHECK (multiplier > 0),
    CONSTRAINT option_not_on_itself CHECK (underlying_id <> instrument_id)
);

CREATE TABLE swap_terms (
    instrument_id     BIGINT       PRIMARY KEY REFERENCES instruments ON DELETE CASCADE,
    float_index       TEXT         NOT NULL DEFAULT 'SOFR' CHECK (float_index = 'SOFR'),
    effective_date    DATE         NOT NULL,
    maturity_date     DATE         NOT NULL,
    fixed_rate        NUMERIC(14,10),
    months_per_period SMALLINT     NOT NULL DEFAULT 12 CHECK (months_per_period IN (1, 3, 6, 12)),
    fixed_rate_source TEXT,
    CONSTRAINT swap_matures_after_start CHECK (maturity_date > effective_date)
);

COMMENT ON COLUMN swap_terms.fixed_rate IS
    'Decimal. NULL means "traded at market": set to the par rate on the effective date by ballast price-swaps.';


-- ---------- listed option quotes (implied volatility) ----------
CREATE TABLE option_quotes (
    underlying_id   BIGINT        NOT NULL REFERENCES instruments,
    snapshot_at     TIMESTAMPTZ   NOT NULL,
    expiry_date     DATE          NOT NULL,
    option_type     TEXT          NOT NULL CHECK (option_type IN ('call', 'put')),
    strike          NUMERIC(20,6) NOT NULL,
    bid             NUMERIC(20,6),
    ask             NUMERIC(20,6),
    last_price      NUMERIC(20,6),
    volume          BIGINT,
    open_interest   BIGINT,
    vendor_iv       DOUBLE PRECISION,
    model_iv        DOUBLE PRECISION,
    forward         DOUBLE PRECISION,
    rate_cc         DOUBLE PRECISION,
    PRIMARY KEY (underlying_id, snapshot_at, expiry_date, option_type, strike)
);


-- ---------- P&L income now includes swap payments ----------
CREATE OR REPLACE VIEW position_pnl_daily AS
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
       inc.income,
       b.market_value - b.prev_market_value - tc.trade_cash + inc.income AS daily_pnl
FROM base b
CROSS JOIN LATERAL (
    SELECT COALESCE(SUM(t.quantity * t.price + t.fees), 0) AS trade_cash
    FROM trades t
    WHERE t.portfolio_id  = b.portfolio_id
      AND t.instrument_id = b.instrument_id
      AND t.trade_date   <= b.as_of_date
      AND (b.prev_date IS NULL OR t.trade_date > b.prev_date)
) tc
CROSS JOIN LATERAL (
    SELECT COALESCE(SUM(ca.value * b.prev_quantity), 0) AS income
    FROM corporate_actions ca
    WHERE ca.instrument_id = b.instrument_id
      AND ca.action_type IN ('cash_dividend', 'coupon', 'swap_payment')
      AND b.prev_date IS NOT NULL
      AND ca.ex_date >  b.prev_date
      AND ca.ex_date <= b.as_of_date
) inc;


-- ---------- data-quality checks adapted to derivatives ----------
-- Option prices legitimately move far more than 40% in a day, and swap values
-- cross zero, so the jump check covers market-priced and bond instruments only.
CREATE OR REPLACE VIEW dq_unexplained_price_jumps AS
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
    JOIN instruments i USING (instrument_id)
    WHERE i.asset_class IN ('equity', 'etf', 'treasury')
)
SELECT i.symbol, m.price_date, m.prev_close, m.close, m.split_ratio,
       m.close * m.split_ratio / m.prev_close - 1 AS split_adjusted_change
FROM moves m
JOIN instruments i ON i.instrument_id = m.instrument_id
WHERE m.prev_close IS NOT NULL
  AND abs(m.close * m.split_ratio / m.prev_close - 1) > 0.40;

-- Swaps are excluded: their value is near zero, so a percentage deviation is meaningless.
CREATE OR REPLACE VIEW dq_trade_price_outliers AS
SELECT t.trade_id, t.trade_ref, i.symbol, t.trade_date, t.price, p.close,
       t.price / p.close - 1 AS deviation
FROM trades t
JOIN prices p      ON p.instrument_id = t.instrument_id AND p.price_date = t.trade_date
JOIN instruments i ON i.instrument_id = t.instrument_id
WHERE i.asset_class <> 'swap'
  AND p.close > 0
  AND abs(t.price / p.close - 1) > 0.10;

-- Options and swaps that cannot be valued because their terms are missing.
CREATE VIEW dq_derivatives_missing_terms AS
SELECT i.instrument_id, i.symbol, i.asset_class
FROM instruments i
LEFT JOIN option_terms o USING (instrument_id)
LEFT JOIN swap_terms s USING (instrument_id)
WHERE (i.asset_class = 'option' AND o.instrument_id IS NULL)
   OR (i.asset_class = 'swap' AND s.instrument_id IS NULL);
