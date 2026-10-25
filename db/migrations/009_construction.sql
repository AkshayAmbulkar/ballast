-- 009: Portfolio construction and performance attribution.
-- * instruments.segment groups holdings for attribution (equity, rates, gold, ...).
--   When it is NULL, the segment follows the asset class (see docs/methodology/05-construction.md).
-- * optimization_runs / optimization_weights: each optimizer result.
-- * attribution_daily: daily Brinson-Fachler inputs and effects, before linking.

ALTER TABLE instruments ADD COLUMN segment TEXT;

CREATE TABLE optimization_runs (
    run_id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    portfolio_id    BIGINT           NOT NULL REFERENCES portfolios ON DELETE CASCADE,
    as_of_date      DATE             NOT NULL,
    method          TEXT             NOT NULL,
    expected_return DOUBLE PRECISION NOT NULL,
    volatility      DOUBLE PRECISION NOT NULL,
    sharpe          DOUBLE PRECISION,
    risk_free       DOUBLE PRECISION NOT NULL,
    lookback_days   INTEGER          NOT NULL,
    max_weight      DOUBLE PRECISION,
    views           TEXT,
    computed_at     TIMESTAMPTZ      NOT NULL DEFAULT now()
);

CREATE TABLE optimization_weights (
    run_id        BIGINT           NOT NULL REFERENCES optimization_runs ON DELETE CASCADE,
    instrument_id BIGINT           NOT NULL REFERENCES instruments,
    weight        DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (run_id, instrument_id)
);

CREATE TABLE attribution_daily (
    portfolio_id  BIGINT           NOT NULL REFERENCES portfolios ON DELETE CASCADE,
    as_of_date    DATE             NOT NULL,
    segment       TEXT             NOT NULL,
    benchmark     TEXT             NOT NULL,
    port_weight   DOUBLE PRECISION NOT NULL,
    port_return   DOUBLE PRECISION NOT NULL,
    bench_weight  DOUBLE PRECISION NOT NULL,
    bench_return  DOUBLE PRECISION NOT NULL,
    allocation    DOUBLE PRECISION NOT NULL,
    selection     DOUBLE PRECISION NOT NULL,
    interaction   DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (portfolio_id, as_of_date, segment, benchmark)
);
