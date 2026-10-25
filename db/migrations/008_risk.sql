-- 008: Risk results.
-- var_results: each VaR / Expected Shortfall run, by method and confidence.
-- var_backtest: the daily backtest: VaR set at the previous close vs the P&L booked.
-- Methodology: docs/methodology/04-risk.md

CREATE TABLE var_results (
    portfolio_id   BIGINT        NOT NULL REFERENCES portfolios ON DELETE CASCADE,
    as_of_date     DATE          NOT NULL,
    method         TEXT          NOT NULL CHECK (method IN ('historical', 'parametric', 'monte_carlo')),
    confidence     NUMERIC(6,4)  NOT NULL CHECK (confidence > 0.5 AND confidence < 1),
    horizon_days   INTEGER       NOT NULL DEFAULT 1,
    var_usd        NUMERIC(20,2) NOT NULL,
    es_usd         NUMERIC(20,2) NOT NULL,
    market_value   NUMERIC(20,2) NOT NULL,
    scenarios      INTEGER,
    lookback_days  INTEGER       NOT NULL,
    computed_at    TIMESTAMPTZ   NOT NULL DEFAULT now(),
    PRIMARY KEY (portfolio_id, as_of_date, method, confidence)
);

CREATE TABLE var_backtest (
    portfolio_id   BIGINT        NOT NULL REFERENCES portfolios ON DELETE CASCADE,
    test_date      DATE          NOT NULL,
    method         TEXT          NOT NULL CHECK (method IN ('historical', 'parametric')),
    confidence     NUMERIC(6,4)  NOT NULL,
    var_usd        NUMERIC(20,2) NOT NULL,
    actual_pnl     NUMERIC(20,2) NOT NULL,
    exception      BOOLEAN       NOT NULL,
    computed_at    TIMESTAMPTZ   NOT NULL DEFAULT now(),
    PRIMARY KEY (portfolio_id, test_date, method, confidence)
);

-- Exceptions by calendar year and method, for a quick look in SQL.
CREATE VIEW var_backtest_by_year AS
SELECT portfolio_id, method, confidence,
       EXTRACT(YEAR FROM test_date)::INT AS year,
       COUNT(*)                          AS days,
       COUNT(*) FILTER (WHERE exception) AS exceptions,
       ROUND(100.0 * COUNT(*) FILTER (WHERE exception) / COUNT(*), 2) AS exception_rate_pct
FROM var_backtest
GROUP BY portfolio_id, method, confidence, EXTRACT(YEAR FROM test_date);
