-- claim_type_policy rows for 'convergence' (041) and 'dial' (042), plus the
-- reserved scope entity a global dial anchors on.
--
-- Both 041 and 042 are enum-value-only, per the constraint recorded in 011's
-- header: Postgres will not let a transaction that ADDs an enum value also
-- INSERT a row referencing it. This is that split landing for both.
--
-- convergence -- 1 hour.
--   A convergence is an assertion that N independent claim families agreed
--   inside a window. It is only as fresh as the shortest-lived family that can
--   constitute it, and orderbook_snapshot is 5 minutes (039). One hour is the
--   deliberate compromise: shorter would mark every convergence stale before a
--   fill loop could act on it, and much longer would let a corroboration
--   assembled from this morning's book present as current evidence this
--   evening. Revisit once the class has resolved predictions and the useful
--   half-life is measured rather than reasoned about.
--
-- dial -- 100 years, which is to say: never stale.
--   This one is not a cadence judgement and should not be read as one. An
--   editorial dial does not decay. It holds until a human records a new
--   version, and a new version is a NEW claim with its own knowledge_date --
--   that is the entire reason dials are stored bitemporally rather than as
--   code constants. Marking a dial stale would open a gap the fill engine
--   cannot close, because no adapter fetches an editorial parameter; the gap
--   would be re-detected forever and burn budget on every sweep. A very long
--   interval expresses "staleness is not the right question for this claim
--   type" within a NOT NULL column.

INSERT INTO claim_type_policy (claim_type, default_staleness, note) VALUES
    ('convergence', INTERVAL '1 hour',
     'only as fresh as the shortest-lived family that can constitute it'),
    ('dial', INTERVAL '100 years',
     'an editorial parameter does not decay; it is superseded by a new version, never expired');

-- The scope entity a global (non-entity-specific) dial anchors on.
--
-- claim.entity_id is NOT NULL REFERENCES entity(id) (001:50), so a dial that
-- applies to no particular entity still needs one. The W2 agent created this
-- row lazily on first global write, which works but puts a schema side effect
-- in a write path -- a caller reading dials would never create it, a caller
-- writing one would, and the difference is invisible at the call site. Seeding
-- it here makes the row a fact of the schema instead.
--
-- It is inert to everything else: the gap engine drives off `demand` rows and
-- this entity is never demanded, and entities/identify.py filters kind =
-- 'company'. Dial.entity_id reports None for it, so the anchor never reaches a
-- caller.
INSERT INTO entity (kind, symbol, name)
VALUES ('dial_scope', 'global', 'Global dial scope')
ON CONFLICT (kind, symbol) DO NOTHING;

-- DOWN

DELETE FROM claim_type_policy WHERE claim_type IN ('convergence', 'dial');
DELETE FROM entity WHERE kind = 'dial_scope' AND symbol = 'global';
