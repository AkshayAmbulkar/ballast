-- 002: Market data.
--
-- Prices are stored AS TRADED: the price you would have seen on the tape that
-- day, not back-adjusted for later stock splits. Splits and dividends live in
-- corporate_actions and are applied explicitly when positions are computed.
-- This is how production position-keeping systems store data, and it keeps
-- trade prices and market prices in the same units.

CREATE TABLE prices (
    instrument_id BIGINT        NOT NULL REFERENCES instruments ON DELETE CASCADE,
    price_date    DATE          NOT NULL,
    close         NUMERIC(20,6) NOT NULL CHECK (close > 0),
    adj_close     NUMERIC(20,6)          CHECK (adj_close > 0),
    volume        BIGINT                 CHECK (volume >= 0),
    source        TEXT          NOT NULL,
    loaded_at     TIMESTAMPTZ   NOT NULL DEFAULT now(),
    PRIMARY KEY (instrument_id, price_date)
);

CREATE INDEX prices_price_date_idx ON prices (price_date);

COMMENT ON COLUMN prices.close     IS 'As-traded closing price (not adjusted for later splits or dividends).';
COMMENT ON COLUMN prices.adj_close IS 'Vendor total-return adjusted close (splits + dividends). Use only for return series, never for valuation.';


CREATE TABLE corporate_actions (
    instrument_id BIGINT        NOT NULL REFERENCES instruments ON DELETE CASCADE,
    ex_date       DATE          NOT NULL,
    action_type   TEXT          NOT NULL CHECK (action_type IN ('cash_dividend', 'split')),
    value         NUMERIC(20,8) NOT NULL CHECK (value > 0),
    source        TEXT          NOT NULL,
    loaded_at     TIMESTAMPTZ   NOT NULL DEFAULT now(),
    PRIMARY KEY (instrument_id, ex_date, action_type)
);

COMMENT ON COLUMN corporate_actions.value IS
    'cash_dividend: as-traded cash amount per share. split: new shares per old share (10 = 10-for-1, 0.1 = 1-for-10 reverse).';


-- Interest-rate time series (Treasury constant-maturity yields, SOFR).
CREATE TABLE rate_series (
    series_id    TEXT PRIMARY KEY,
    curve        TEXT NOT NULL CHECK (curve IN ('UST_CMT', 'SOFR')),
    tenor_months INTEGER CHECK (tenor_months > 0),
    description  TEXT NOT NULL,
    source       TEXT NOT NULL DEFAULT 'FRED',
    CONSTRAINT cmt_has_tenor CHECK (curve <> 'UST_CMT' OR tenor_months IS NOT NULL)
);

CREATE TABLE rate_observations (
    series_id  TEXT          NOT NULL REFERENCES rate_series ON DELETE CASCADE,
    obs_date   DATE          NOT NULL,
    value_pct  NUMERIC(10,4) NOT NULL,
    loaded_at  TIMESTAMPTZ   NOT NULL DEFAULT now(),
    PRIMARY KEY (series_id, obs_date)
);

CREATE INDEX rate_observations_obs_date_idx ON rate_observations (obs_date);

COMMENT ON COLUMN rate_observations.value_pct IS 'Rate in percent as published (4.25 means 4.25 percent).';

-- Reference data: FRED series used to build the Treasury curve (Phase 2) and SOFR.
INSERT INTO rate_series (series_id, curve, tenor_months, description) VALUES
    ('DGS1MO', 'UST_CMT',   1, 'Market yield on U.S. Treasury securities at 1-month constant maturity'),
    ('DGS3MO', 'UST_CMT',   3, 'Market yield on U.S. Treasury securities at 3-month constant maturity'),
    ('DGS6MO', 'UST_CMT',   6, 'Market yield on U.S. Treasury securities at 6-month constant maturity'),
    ('DGS1',   'UST_CMT',  12, 'Market yield on U.S. Treasury securities at 1-year constant maturity'),
    ('DGS2',   'UST_CMT',  24, 'Market yield on U.S. Treasury securities at 2-year constant maturity'),
    ('DGS3',   'UST_CMT',  36, 'Market yield on U.S. Treasury securities at 3-year constant maturity'),
    ('DGS5',   'UST_CMT',  60, 'Market yield on U.S. Treasury securities at 5-year constant maturity'),
    ('DGS7',   'UST_CMT',  84, 'Market yield on U.S. Treasury securities at 7-year constant maturity'),
    ('DGS10',  'UST_CMT', 120, 'Market yield on U.S. Treasury securities at 10-year constant maturity'),
    ('DGS20',  'UST_CMT', 240, 'Market yield on U.S. Treasury securities at 20-year constant maturity'),
    ('DGS30',  'UST_CMT', 360, 'Market yield on U.S. Treasury securities at 30-year constant maturity'),
    ('SOFR',   'SOFR',   NULL, 'Secured Overnight Financing Rate');

-- Convenience view: the Treasury CMT curve by date, one row per tenor.
CREATE VIEW treasury_cmt_curve AS
SELECT o.obs_date,
       s.tenor_months,
       s.series_id,
       o.value_pct
FROM rate_observations o
JOIN rate_series s USING (series_id)
WHERE s.curve = 'UST_CMT';
