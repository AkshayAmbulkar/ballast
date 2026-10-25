-- 003: Portfolios and trades.
-- Positions are never stored. They are derived from the trade ledger, so the
-- ledger is the single source of truth and history can always be rebuilt.

CREATE TABLE portfolios (
    portfolio_id  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name          TEXT        NOT NULL UNIQUE,
    base_currency CHAR(3)     NOT NULL DEFAULT 'USD',
    description   TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE trades (
    trade_id      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    portfolio_id  BIGINT        NOT NULL REFERENCES portfolios ON DELETE CASCADE,
    instrument_id BIGINT        NOT NULL REFERENCES instruments,
    trade_ref     TEXT          NOT NULL,
    trade_date    DATE          NOT NULL,
    quantity      NUMERIC(20,6) NOT NULL CHECK (quantity <> 0),
    price         NUMERIC(20,6) NOT NULL CHECK (price >= 0),
    fees          NUMERIC(20,6) NOT NULL DEFAULT 0 CHECK (fees >= 0),
    note          TEXT,
    created_at    TIMESTAMPTZ   NOT NULL DEFAULT now(),
    CONSTRAINT trades_ref_unique UNIQUE (portfolio_id, trade_ref)
);

CREATE INDEX trades_position_idx ON trades (portfolio_id, instrument_id, trade_date);

COMMENT ON COLUMN trades.quantity  IS 'Signed: positive = buy, negative = sell. In as-traded units on trade_date.';
COMMENT ON COLUMN trades.trade_ref IS 'External reference (e.g. broker ticket). Makes trade loads idempotent.';
