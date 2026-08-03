"""Calibration-quality experiment: does the conviction gate recover a planted
signal, and refuse noise?

This is the APPARATUS test (credential-free), not the DCF-edge test (that is the
real-data Polygon run). The thesis question "does the conviction gate
calibrate?" has two halves:

  1. Can the gate's statistics recover a known confidence->outcome signal at all?
  2. Will it refuse to manufacture an edge where none exists?

Both are answerable without any credential, by PLANTING outcomes at the
price-path level (so the real resolver decides them) and reading the gate's
behaviour. RESEARCH.md section 4 names overfitting / fooling-yourself as the #1
backtesting failure mode; this test's null-signal refusal is the false-positive
guard at the gate level.

Deliberately deferred to the real-data run (documented so it is a decision, not
an oversight):
  - Walk-forward regime stability: per-window samples sit at the n>=10 floor and
    are noisy on a planted signal; regime stability is a real-backtest concern.
  - Deflated Sharpe Ratio / CPCV: circular on a planted signal; the null refusal
    is the apparatus-level false-positive guard. DSR belongs on the real run
    where multiple-testing across entities/methods is the live threat.

Confidence is derived the production way -- `_first_passage_confidence` over
real straddling barriers -- so confidence and the barriers the resolver scores
against are coupled exactly as in production. record_prediction is called
directly (not produce_dcf_prediction) to isolate the gate's statistics from the
DCF's own behaviour, which is a separate (real-data) question.

Each prediction gets its own entity: the resolver reads a price path PER ENTITY,
so a shared entity would let one prediction's planted price decide another's
outcome (or trip the resolver's both-barriers indeterminate case). The
calibration view groups by method, not entity, so the 300 predictions still
aggregate into one method's ten confidence deciles.
"""

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from omni.conviction.gate import (
    Candidate,
    assess,
    calibrated_threshold,
)
from omni.conviction.ledger import record_prediction, resolve_due_predictions
from omni.conviction.predict import _first_passage_confidence
from omni.conviction.publish import load_calibration

CLAIM_TYPE = "fundamental_metric"
METHOD = "calibration_probe"

# Fixed barriers; sweeping entry from 91->109 sweeps first-passage confidence
# across (0,1). A single price point at 111 crosses upper (hit for an up-call);
# one at 89 crosses lower (miss). No value can span both barriers at once, so
# resolution is unambiguous (ledger._decide_outcome's indeterminate case cannot
# fire on a single-point path).
LOWER = 90.0
UPPER = 110.0
HIT_PRICE = 111.0
MISS_PRICE = 89.0

PER_DECILE = 30  # >= MIN_RESOLVED_FOR_CALIBRATION (10) with margin

BASE = datetime(2025, 1, 1, tzinfo=UTC)
HORIZON = BASE + timedelta(days=30)
PRICE_DAY = BASE + timedelta(days=5)


@pytest.fixture(autouse=True)
async def _clean(db):
    await db.pool.execute("TRUNCATE entity CASCADE")
    yield


async def _entity(db):
    # entity has a UNIQUE(kind, symbol) constraint; each prediction gets its own
    # entity, so the symbol must be unique per call.
    return await db.pool.fetchval(
        "INSERT INTO entity (kind, symbol, name) "
        "VALUES ('company', $1, 'Probe') RETURNING id",
        f"P{uuid4().hex[:10]}",
    )


async def _price(db, entity_id, value):
    await db.pool.execute(
        "INSERT INTO claim (entity_id, claim_type, key, value, source, "
        "event_date, knowledge_date, confidence, redistributable) "
        "VALUES ($1,'price_snapshot','close',$2::jsonb,'probe',$3,$3,1.0,'allowed')",
        entity_id,
        json.dumps({"close": value, "high": value, "low": value}),
        PRICE_DAY,
    )


async def _plant(db, *, p_hit):
    """Record PER_DECILE up-call predictions per confidence decile (one entity
    each) and resolve them through the real resolver with a planted hit rate.

    `p_hit(decile_midpoint) -> float` is the planted calibration curve. Outcomes
    are assigned deterministically by index (no RNG) so the test is reproducible:
    the first round(p*N) predictions in a decile get a target-crossing price
    (hit); the rest get the opposite barrier (miss)."""
    for decile in range(10):
        midpoint = (decile + 0.5) / 10.0  # 0.05, 0.15, ..., 0.95
        entry = LOWER + midpoint * (UPPER - LOWER)  # couples confidence to barriers
        confidence = round(_first_passage_confidence("up", entry, UPPER, LOWER), 6)
        n_hits = round(p_hit(midpoint) * PER_DECILE)
        for i in range(PER_DECILE):
            e = await _entity(db)
            await record_prediction(
                db.pool,
                entity_id=e,
                capability=METHOD,
                method=METHOD,
                direction="up",
                confidence=confidence,
                entry_price=entry,
                upper_barrier=UPPER,
                lower_barrier=LOWER,
                horizon_ends_at=HORIZON,
                audience_user_id=None,
                created_at=BASE,
            )
            await _price(db, e, HIT_PRICE if i < n_hits else MISS_PRICE)

    resolved = await resolve_due_predictions(db.pool, now=HORIZON + timedelta(days=1))
    assert resolved == 10 * PER_DECILE


def _recovered_curve(buckets):
    """decile_index -> (n, hits, hit_rate|None), dense across the 10 deciles."""
    by_low = {round(b.bucket_low, 6): b for b in buckets}
    out = {}
    for decile in range(10):
        b = by_low.get(round(decile / 10.0, 6))
        out[decile] = (b.n, b.hits, b.hit_rate) if b is not None else (0, 0, None)
    return out


def _print_curve(label, curve):
    print(f"\n=== {label} ===")
    print("decile  low   n   hits  hit_rate")
    for decile, (n, hits, hr) in curve.items():
        hr_s = "  -  " if hr is None else f"{hr:.3f}"
        print(f"  {decile + 1:>2}    {decile / 10.0:.1f}  {n:>3}  {hits:>4}   {hr_s}")


async def test_recovers_a_planted_monotonic_signal(db):
    """Under a perfectly-calibrated signal p(hit)=confidence, the gate recovers
    a monotonic curve and derives a threshold at the decile where the hit rate
    first reaches the target."""
    await _plant(db, p_hit=lambda c: c)

    buckets = await load_calibration(db.pool, claim_type=CLAIM_TYPE, method=METHOD)
    curve = _recovered_curve(buckets)
    _print_curve("planted signal p(hit) = confidence", curve)

    # Every decile is populated above the sample floor.
    assert all(n >= 10 for n, _, _ in curve.values())

    # Monotonic non-decreasing hit rate across confidence deciles.
    rates = [curve[d][2] for d in range(10)]
    for d in range(1, 10):
        assert rates[d] is not None and rates[d - 1] is not None
        assert rates[d] >= rates[d - 1] - 0.05, (d, rates[d - 1], rates[d])

    threshold = calibrated_threshold(buckets, target_hit_rate=0.6)
    print(f"\nderived threshold (target=0.6): {threshold}")
    # Deciles 7-10 (midpoints 0.65-0.95) hit >= 0.6; the lowest qualifying low
    # is decile 7's = 0.6.
    assert threshold == pytest.approx(0.6)

    # The gate's selection earns its existence: surfacing at the threshold
    # concentrates hits versus surfacing everything. Read from the same resolved
    # calibration the gate uses, not the planted record.
    sel_hits = sum(b.hits for b in buckets if b.bucket_low >= threshold)
    sel_n = sum(b.n for b in buckets if b.bucket_low >= threshold)
    all_hits = sum(b.hits for b in buckets)
    all_n = sum(b.n for b in buckets)
    sel_rate, all_rate = sel_hits / sel_n, all_hits / all_n
    print(
        f"\nhit-rate, threshold-selected: {sel_rate:.3f}  vs surfacing-all: "
        f"{all_rate:.3f}"
    )
    assert sel_rate > all_rate


async def test_refuses_to_manufacture_an_edge_from_noise(db):
    """Under a null signal p(hit)=0.5 at every confidence, no bucket reaches the
    target hit rate. The gate derives no threshold and refuses to surface even a
    high-confidence candidate -- it does not invent credibility from noise.

    This is the false-positive guard: the most dangerous failure is a gate that
    sounds confident on a signal that is not there (RESEARCH.md section 4:
    overfitting is the #1 backtesting killer)."""
    await _plant(db, p_hit=lambda c: 0.5)

    buckets = await load_calibration(db.pool, claim_type=CLAIM_TYPE, method=METHOD)
    curve = _recovered_curve(buckets)
    _print_curve("null signal p(hit) = 0.5", curve)

    threshold = calibrated_threshold(buckets, target_hit_rate=0.6)
    print(f"\nderived threshold under null (target=0.6): {threshold}")
    assert threshold is None

    candidate = Candidate(
        claim_type=CLAIM_TYPE,
        method=METHOD,
        confidence=0.95,
        supporting=("a planted high-confidence candidate",),
        searched_for_disconfirming=True,
        falsifiable=True,
    )
    verdict = assess(candidate, buckets, target_hit_rate=0.6)
    assert not verdict.surfaced
    print(f"verdict on a 0.95-confidence candidate under null: {verdict.refusal}")
