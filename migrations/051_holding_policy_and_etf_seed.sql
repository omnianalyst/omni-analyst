-- claim_type_policy for 'holding' (050), plus the broad ETF entities the
-- exposure tool's four-bucket framework needs.
--
-- holding -- 90 days.
--   Most ETFs publish full holdings monthly or quarterly (N-PORT filings are
--   publicly delayed 60 days). Quarterly rebalancing means the composition
--   drifts slowly. 90 days marks a holding claim stale at the next expected
--   filing, which is the moment a gap should open and the fill engine should
--   look for a fresh one.

INSERT INTO claim_type_policy (claim_type, default_staleness, note) VALUES
    ('holding', INTERVAL '90 days',
     'ETF/fund holdings from issuer disclosures; rebalances quarterly');

-- The four-bucket hedge framework entities. These are broad-market ETFs that
-- each own a distinct risk regime (growth, cash/yield, deflation-rally,
-- currency-debasement). They are NOT sector ETFs (kind='sector_etf') or index
-- barometers (kind='index') -- they are the vehicles for a static allocation,
-- and the exposure tool maps their holdings to compute overlap and
-- concentration across the portfolio.
--
-- polygon = symbol because Polygon prices all of these under their ticker.
INSERT INTO entity (kind, symbol, name, identifiers) VALUES
    ('etf', 'VTI', 'Vanguard Total Stock Market ETF',
     '{"polygon": "VTI", "bucket": "growth"}'),
    ('etf', 'VXUS', 'Vanguard Total International Stock ETF',
     '{"polygon": "VXUS", "bucket": "growth"}'),
    ('etf', 'QQQ', 'Invesco QQQ Trust',
     '{"polygon": "QQQ", "bucket": "growth"}'),
    ('etf', 'TLT', 'iShares 20+ Year Treasury Bond ETF',
     '{"polygon": "TLT", "bucket": "deflation_rally"}'),
    ('etf', 'GLD', 'SPDR Gold Shares',
     '{"polygon": "GLD", "bucket": "currency_debasement"}'),
    ('etf', 'SLV', 'iShares Silver Trust',
     '{"polygon": "SLV", "bucket": "currency_debasement"}'),
    ('etf', 'SHV', 'iShares Short Treasury Bond ETF',
     '{"polygon": "SHV", "bucket": "cash_yield"}')
ON CONFLICT (kind, symbol) DO UPDATE SET
    name = EXCLUDED.name,
    identifiers = entity.identifiers || EXCLUDED.identifiers;

-- DOWN

DELETE FROM claim_type_policy WHERE claim_type = 'holding';
DELETE FROM entity WHERE kind = 'etf' AND symbol IN (
    'VTI', 'VXUS', 'QQQ', 'TLT', 'GLD', 'SLV', 'SHV'
);
