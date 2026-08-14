INSERT INTO users (id, email, password_hash, created_at)
VALUES (
    '10000000-0000-0000-0000-000000000001',
    'historical-upgrade@invalid.example',
    'synthetic-ci-password-hash-not-for-authentication',
    '2025-01-02T03:04:05Z'
);

INSERT INTO entity (id, kind, symbol, name, identifiers, created_at)
VALUES (
    '20000000-0000-0000-0000-000000000001',
    'company',
    'CIHIST',
    'CI Historical Fixture',
    '{"cik":"1234567","polygon":"CIHIST"}',
    '2025-01-02T03:04:05Z'
);

INSERT INTO claim (
    id, entity_id, claim_type, key, value, unit, evidence, source,
    event_date, knowledge_date, confidence, credential_owner,
    redistributable, audience_user_id, observed_at
)
VALUES
    (
        '30000000-0000-0000-0000-000000000001',
        '20000000-0000-0000-0000-000000000001',
        'fundamental_metric',
        'revenue',
        '{"value":"1250000","period":"FY2024"}',
        'USD',
        '{"accession":"0001234567-25-000001"}',
        'sec_edgar',
        '2024-12-31T00:00:00Z',
        '2025-02-14T13:30:00Z',
        0.97,
        NULL,
        'allowed',
        NULL,
        '2025-02-14T13:31:00Z'
    ),
    (
        '30000000-0000-0000-0000-000000000002',
        '20000000-0000-0000-0000-000000000001',
        'price_snapshot',
        'close',
        '{"price":"42.75"}',
        'USD',
        '{"provider_timestamp":"2025-02-14T21:00:00Z"}',
        'polygon',
        '2025-02-14T21:00:00Z',
        '2025-02-14T21:00:02Z',
        0.91,
        '10000000-0000-0000-0000-000000000001',
        'byo_only',
        '10000000-0000-0000-0000-000000000001',
        '2025-02-14T21:00:03Z'
    );

INSERT INTO prediction (
    id, entity_id, claim_id, method, direction, confidence, entry_price,
    upper_barrier, lower_barrier, horizon_ends_at, created_at, outcome,
    provenance, audience_user_id
)
VALUES (
    '40000000-0000-0000-0000-000000000001',
    '20000000-0000-0000-0000-000000000001',
    '30000000-0000-0000-0000-000000000002',
    'ci.historical.signal',
    'up',
    0.74,
    42.75,
    47.00,
    39.00,
    '2025-03-14T21:00:00Z',
    '2025-02-14T21:01:00Z',
    'pending',
    '{"fixture":"historical-schema-upgrade","inputs":["30000000-0000-0000-0000-000000000002"]}',
    '10000000-0000-0000-0000-000000000001'
);

INSERT INTO finding (
    id, claim_id, entity_id, audience_user_id, status, method, confidence,
    threshold, calibrated_hit_rate, supporting, disconfirming, prediction_id,
    created_at, deduction_chain, evidence_searched
)
VALUES (
    '50000000-0000-0000-0000-000000000001',
    '30000000-0000-0000-0000-000000000002',
    '20000000-0000-0000-0000-000000000001',
    '10000000-0000-0000-0000-000000000001',
    'surfaced',
    'ci.historical.signal',
    0.74,
    0.70,
    0.76,
    '["private price claim retained"]',
    '["short history"]',
    '40000000-0000-0000-0000-000000000001',
    '2025-02-14T21:02:00Z',
    '[{"layer":"price","claim_id":"30000000-0000-0000-0000-000000000002"}]',
    TRUE
);

INSERT INTO user_settings (user_id, data, updated_at)
VALUES (
    '10000000-0000-0000-0000-000000000001',
    '{"providers":{},"venues":{"questrade":{"enabled":false,"credentials":{"refresh_token":"enc:v1:synthetic-ci-ciphertext"}}}}',
    '2025-02-14T21:03:00Z'
);
