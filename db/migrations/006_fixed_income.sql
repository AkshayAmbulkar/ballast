-- 006: Fixed income.
--
-- * bond_terms: contract terms for Treasury instruments.
-- * Coupons are recorded in corporate_actions as action_type 'coupon'.
-- * zero_curves: the bootstrapped Treasury curve for every date, queryable in SQL.
-- * The P&L views now count coupons as income alongside dividends, so the
--   dividend_income column becomes income (dividends + coupons).
--
-- Bond units: quantity is the number of 100-face units (300 = $30,000 face),
-- and prices are dirty prices per 100 face, so market value = quantity x price
-- exactly as for equities. Full conventions: docs/methodology/02-fixed-income.md

ALTER TABLE corporate_actions DROP CONSTRAINT corporate_actions_action_type_check;
ALTER TABLE corporate_actions ADD CONSTRAINT corporate_actions_action_type_check
    CHECK (action_type IN ('cash_dividend', 'split', 'coupon'));

COMMENT ON COLUMN corporate_actions.value IS
    'cash_dividend: as-traded cash per share. split: new shares per old share. coupon: cash per 100-face unit, credited on the payment date.';


CREATE TABLE bond_terms (
    instrument_id     BIGINT        PRIMARY KEY REFERENCES instruments ON DELETE CASCADE,
    coupon_rate       NUMERIC(9,6)  NOT NULL CHECK (coupon_rate >= 0 AND coupon_rate < 1),
    issue_date        DATE          NOT NULL,
    maturity_date     DATE          NOT NULL,
    coupon_frequency  SMALLINT      NOT NULL DEFAULT 2 CHECK (coupon_frequency IN (1, 2, 4, 12)),
    CONSTRAINT bond_matures_after_issue CHECK (maturity_date > issue_date)
);

COMMENT ON COLUMN bond_terms.coupon_rate IS 'Annual coupon as a decimal: 0.0425 means 4.25 percent.';


CREATE TABLE zero_curves (
    curve           TEXT             NOT NULL DEFAULT 'UST',
    curve_date      DATE             NOT NULL,
    tenor_months    INTEGER          NOT NULL CHECK (tenor_months > 0),
    discount_factor DOUBLE PRECISION NOT NULL CHECK (discount_factor > 0),
    zero_rate_cc    DOUBLE PRECISION NOT NULL,
    zero_rate_bey   DOUBLE PRECISION NOT NULL,
    par_yield       DOUBLE PRECISION NOT NULL,
    built_at        TIMESTAMPTZ      NOT NULL DEFAULT now(),
    PRIMARY KEY (curve, curve_date, tenor_months)
);

CREATE INDEX zero_curves_date_idx ON zero_curves (curve_date);

COMMENT ON TABLE zero_curves IS
    'Bootstrapped from Treasury CMT par yields. Rates are decimals. zero_rate_bey compounds semiannually.';


-- Treasury instruments that cannot be priced because their terms are missing.
CREATE VIEW dq_bonds_missing_terms AS
SELECT i.instrument_id, i.symbol
FROM instruments i
LEFT JOIN bond_terms b USING (instrument_id)
WHERE i.asset_class = 'treasury'
  AND b.instrument_id IS NULL;


-- ---------- P&L views: income = dividends + coupons ----------

DROP VIEW holdings_latest;
DROP VIEW portfolio_pnl_daily;
DROP VIEW position_pnl_daily;

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
      AND ca.action_type IN ('cash_dividend', 'coupon')
      AND b.prev_date IS NOT NULL
      AND ca.ex_date >  b.prev_date
      AND ca.ex_date <= b.as_of_date
) inc;

CREATE VIEW portfolio_pnl_daily AS
SELECT portfolio_id,
       as_of_date,
       SUM(market_value) AS market_value,
       SUM(trade_cash)   AS net_trade_cash,
       SUM(income)       AS income,
       SUM(daily_pnl)    AS daily_pnl,
       SUM(SUM(daily_pnl)) OVER (PARTITION BY portfolio_id ORDER BY as_of_date) AS cumulative_pnl
FROM position_pnl_daily
GROUP BY portfolio_id, as_of_date;

CREATE VIEW holdings_latest AS
WITH last_date AS (
    SELECT portfolio_id, MAX(as_of_date) AS as_of_date
    FROM positions_daily
    GROUP BY portfolio_id
),
lifetime AS (
    SELECT portfolio_id,
           instrument_id,
           SUM(daily_pnl) AS total_pnl,
           SUM(income)    AS total_income
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
       l.total_income
FROM positions_daily pd
JOIN last_date ld   ON ld.portfolio_id = pd.portfolio_id AND ld.as_of_date = pd.as_of_date
JOIN instruments i  ON i.instrument_id = pd.instrument_id
JOIN lifetime l     ON l.portfolio_id  = pd.portfolio_id AND l.instrument_id = pd.instrument_id
WHERE pd.quantity <> 0;
