-- Retire the surfaced findings written before the disconfirming search existed.
--
-- Until now the only producer of findings hardcoded
-- `searched_for_disconfirming=True` and set `supporting` to a restatement of the
-- call itself -- "up directional call from trend.sma". No counter-case was ever
-- looked for, and `disconfirming` is empty on every one of these rows because
-- nothing ever populated it.
--
-- That is invisible in the database and load-bearing in the UI: the finding card
-- renders an empty disconfirming list as "the checks ran and found none", which
-- for these rows asserts a search that did not happen. The row shape cannot
-- distinguish "looked, found nothing" from "never looked", so the rows cannot be
-- relabelled -- they have to go.
--
-- Deleting a surfaced finding does not delete its prediction. `surface_once`
-- re-assesses any prediction with no finding attached, so each of these is
-- reconsidered on the next pass and either surfaces with real evidence on both
-- sides or is refused as `no_disconfirming_evidence_was_gathered`. Either
-- outcome is honest; the current one is not.
--
-- Refusals are deliberately kept. A refusal's meaning is its reason -- derived
-- from calibration and the threshold, both computed correctly even then -- and
-- the evidence lists are never rendered for one. They remain the denominator
-- behind the published hit rate, which must not be quietly reduced.
--
-- Scoped by the tautology's exact shape rather than by date, so a re-run is a
-- no-op and nothing written by the real search is ever caught: the new producer
-- never emits this phrasing (enforced by a test in tests/test_disconfirm.py).

DELETE FROM finding
WHERE status = 'surfaced'
  AND supporting::text LIKE '%directional call from%';

-- DOWN

-- Irreversible by construction. The deleted rows asserted evidence that was
-- never gathered; restoring them would mean re-fabricating it. Roll forward.
