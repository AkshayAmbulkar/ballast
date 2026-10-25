-- 001: Instrument master.
-- One row per tradable instrument. Asset classes for later phases (Treasuries,
-- options, swaps) are allowed now; their contract terms get their own tables in
-- later migrations, keyed on instrument_id.

CREATE TABLE instruments (
    instrument_id  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    symbol         TEXT        NOT NULL UNIQUE,
    name           TEXT        NOT NULL,
    asset_class    TEXT        NOT NULL
        CHECK (asset_class IN ('equity', 'etf', 'treasury', 'option', 'swap')),
    currency       CHAR(3)     NOT NULL DEFAULT 'USD',
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT symbol_is_uppercase CHECK (symbol = upper(symbol))
);

COMMENT ON TABLE instruments IS 'Instrument master: one row per tradable instrument.';
