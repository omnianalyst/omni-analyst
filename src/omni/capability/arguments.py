"""ArgumentSpec and materialize: build an analysis argument from claims.

A capability that derives a claim (or runs an analysis) needs its inputs
assembled from the coverage store -- a ``price_snapshot`` is one point, a
backtest needs a *return series*. ``ArgumentSpec`` is the frozen declaration of
how one argument is built; ``materialize`` honours it.

Inputs are read **only** through ``visible_claims`` (audience-scoped) -- the
sanctioned reader. A private claim fetched under ``byo_only`` terms is invisible
to another audience; that is enforced by ``visible_claims`` and is asserted in
the tests, not left as a comment.

Abstention is a **value** (``Abstention``), never an exception and never a
padded series. Below ``min_obs`` the result names which argument was short and
by how much, so a later ``fill_analysis`` can record ``unfillable`` with the
reason. This is the discipline AGENTS.md makes load-bearing: no invented
defaults, no padded series.

A null observation (FRED's ``"."``) is coverage of a period, not an input; it is
skipped at extraction. The transform then drops the first undefined element for
return/diff transforms, and ``window`` takes the trailing N **after** the
transform -- reversing that order silently returns N-1 observations.

The returned ``Materialized`` carries ``rows`` -- one ``ProvenanceRow`` per
surviving observation (post-extraction, post-transform, post-window), each with
the id, dates, post-transform value and licence fields the original claim
carried. A consumer that needs those (dates for ``compute_divergence``'s
bitemporal rule, licence fields for ``resolve_derived_licence``) reads them from
``rows`` rather than re-querying: the rows are exactly the set
``claim_ids`` was derived from, so the edge set and the licence set come from
one union and cannot diverge.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, replace
from datetime import datetime
from uuid import UUID

from omni.coverage.visibility import visible_claims

_SHAPES = {"scalar", "series", "list"}
_TRANSFORMS = {"level", "log_return", "simple_return", "diff"}
_SCOPES = {"objective", "related", "explicit"}


@dataclass(frozen=True)
class ArgumentSpec:
    """How one analysis argument is materialized from claims.

    Field meanings (the shape proposed in D2, unchanged):

    - ``name``: kwarg name the analysis function receives.
    - ``claim_type``: which claims to gather (passed to ``visible_claims``).
    - ``key``: restrict gathering to one series among several sharing
      ``claim_type`` (passed to ``visible_claims``'s ``key``). ``None`` (the
      default) gathers every claim of the type -- today's behaviour, so a spec
      over a single-key type is unchanged.
    - ``shape``: ``scalar`` (latest), ``series`` (ordered), ``list``
      (unordered collection, e.g. one value per related entity).
    - ``transform``: applied to the level series before windowing.
    - ``window``: trailing N observations **after** transform; ``None`` = all.
    - ``min_obs``: abstain below this; never pad.
    - ``min_calendar_days``: minimum span (in days) between the earliest and
      latest surviving observation's ``align_on`` date; ``None`` = no calendar
      check. This is the spacing/freshness dimension ``min_obs`` cannot express:
      12 daily and 12 monthly observations both clear ``min_obs=12``, but only
      the monthly set spans ~a year. A spec over a monthly series sets this so a
      daily series cannot satisfy it.
    - ``value_field``: dotted path to the scalar inside the claim's JSONB
      ``value`` (default ``"value"`` reproduces the existing ``_scalar``
      discipline).
    - ``entity_scope``: ``objective``/``explicit`` materialize for the passed
      ``entity_id``; ``related`` gathers entities joined by ``relation`` and
      aligns them on the common index.
    - ``relation``: ``entity_edge`` relation when ``entity_scope="related"``.
    - ``align_on``: claim column used as the series index / alignment key.
    """

    name: str
    claim_type: str
    key: str | None = None
    shape: str = "series"
    transform: str = "level"
    window: int | None = None
    min_obs: int | None = None
    min_calendar_days: int | None = None
    value_field: str = "value"
    entity_scope: str = "objective"
    relation: str | None = None
    align_on: str = "event_date"

    def __post_init__(self) -> None:
        if self.shape not in _SHAPES:
            raise ValueError(f"ArgumentSpec {self.name!r}: unknown shape {self.shape!r}")
        if self.transform not in _TRANSFORMS:
            raise ValueError(
                f"ArgumentSpec {self.name!r}: unknown transform {self.transform!r}"
            )
        if self.entity_scope not in _SCOPES:
            raise ValueError(
                f"ArgumentSpec {self.name!r}: unknown entity_scope {self.entity_scope!r}"
            )
        if self.entity_scope == "related" and not self.relation:
            raise ValueError(
                f"ArgumentSpec {self.name!r}: entity_scope='related' requires a relation"
            )
        if self.window is not None and self.window <= 0:
            raise ValueError(f"ArgumentSpec {self.name!r}: window must be positive")
        if self.min_obs is not None and self.min_obs <= 0:
            raise ValueError(f"ArgumentSpec {self.name!r}: min_obs must be positive")
        if self.min_calendar_days is not None and self.min_calendar_days <= 0:
            raise ValueError(
                f"ArgumentSpec {self.name!r}: min_calendar_days must be positive"
            )
        if self.align_on not in ("event_date", "knowledge_date"):
            raise ValueError(
                f"ArgumentSpec {self.name!r}: align_on must be a claim date column"
            )


@dataclass(frozen=True)
class AnalysisOutputSpec:
    """One argument sourced from a sibling capability's output, not a claim.

    A composite that consumes another capability's score names that capability
    here. At resolution time the sibling is run first (through the same
    name-keyed machinery in ``orchestrator.analysis``), and the ``result_key``
    field of its output dict feeds this argument as a ``Materialized`` value.

    Distinct from ``ArgumentSpec``, not an overloaded ``shape=
    "analysis_output"``, because every field ``ArgumentSpec`` carries for
    claim extraction -- ``claim_type``, ``key``, ``transform``, ``window``,
    ``value_field``, ``entity_scope``, ``relation``, ``align_on`` -- is
    meaningless for a computed score. An ``ArgumentSpec`` that silently
    ignores eight of its fields is a trap for the next author: they set
    ``claim_type`` expecting it to scope a query, and nothing happens. The
    same reasoning QF1 applied to ``DeclaredAnalysis`` vs
    ``DerivedCapability`` (the contracts differ at the place that matters)
    applies here: the source is different enough that overloading one type
    creates dead fields and silent-ignore paths, not a simplification.

    ``result_key`` defaults to ``"score"`` because every registered
    market_risk sub-analysis returns ``{"score": float, ...}``. A sibling
    whose headline output lives under a different key overrides it.
    """

    name: str
    capability: str
    result_key: str = "score"


@dataclass(frozen=True)
class Abstention:
    """An honest refusal to materialize, carrying the reason a caller records.

    Not ``None`` (the reason must travel to the ``unfillable`` record) and not
    an exception (the caller inspects it, per D2's compute contract). The
    ``argument`` names which spec fell short; ``reason`` includes the count.
    """

    argument: str
    reason: str

    def __repr__(self) -> str:
        return f"Abstention({self.reason})"


@dataclass(frozen=True)
class ProvenanceRow:
    """One observation's full provenance, carried alongside the materialized value.

    Exactly the fields a consumer needs to rebuild what ``materialize`` used to
    drop (dates, licence) without a second query: ``compute_divergence`` indexes
    on ``event_date`` and takes the newest ``knowledge_date``; licence
    resolution reads ``redistributable`` and ``audience_user_id``. ``id``
    matches the corresponding entry in ``Materialized.claim_ids``; ``value`` is
    the post-transform scalar at this position (the level for
    ``transform="level"``), so it agrees with the materialized ``.value``.
    """

    id: UUID
    event_date: datetime
    knowledge_date: datetime
    value: float
    redistributable: str
    audience_user_id: UUID | None


@dataclass(frozen=True)
class Materialized:
    """A successful materialization: the value plus the claims that produced it.

    ``claim_ids`` is the provenance a derived claim must declare as
    ``claim_input`` edges (migration 002's deferred trigger rejects a derived
    claim with no inputs). ``rows`` carries the per-observation provenance
    (dates, licence, value) for the same surviving set -- the post-transform,
    post-window observations the ``.value`` was actually computed from -- so a
    consumer rebuilds what it needs (``DivergenceInput`` lists, licence fields)
    from one read rather than re-querying. ``claim_ids`` is
    ``tuple(r.id for r in rows)`` by construction, so the edge set and the
    licence set come from one union and cannot diverge.
    """

    value: float | list[float] | AlignedSeries
    claim_ids: tuple[UUID, ...]
    rows: tuple[ProvenanceRow, ...]


@dataclass(frozen=True)
class AlignedSeries:
    """Multi-entity series on a shared intersection index.

    ``index`` is the common ``align_on`` values (ascending); ``by_entity``
    maps each related entity to its values, positionally aligned to ``index``
    (length ``len(index)`` each). Intersection, never union: a union with holes
    is a padded series wearing a different hat.
    """

    index: tuple
    by_entity: dict[UUID, tuple[float, ...]]


# ----------------------------------------------------------- claim extraction


def _extract_scalar(raw, value_field: str) -> float | None:
    """Pull a float out of a claim's JSONB ``value`` following ``value_field``.

    Reproduces ``capability.derived._scalar``'s null-discipline (``None`` ->
    skip; a JSON string is decoded; a non-dict or missing/empty scalar yields
    ``None``) and generalizes the lookup to a dotted ``value_field`` path. Not
    imported from ``derived`` because (a) it is a private name coupling this
    pure module to ``derived``'s heavy transitive imports and (b) ``value_field``
    needs path traversal that ``_scalar``'s hardcoded ``raw.get("value")``
    cannot do -- importing it would force a second extractor for the
    non-default path anyway. The discipline is single-sourced here.
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return None
    cur = raw
    for part in value_field.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
        if cur is None:
            return None
    try:
        return float(cur)
    except (TypeError, ValueError):
        return None


def _short_reason(name: str, observed: int, required: int) -> str:
    return (
        f"{name}: {observed} of {required} required observations "
        f"(short {required - observed})"
    )


async def _levels_for_entity(pool, spec: ArgumentSpec, *, entity_id, audience):
    """Visible claims for one entity as ``(keys, levels, prov)`` ascending.

    Null observations are skipped at extraction. Multiple claims for one
    ``align_on`` value (different sources, or the same source at a later
    ``knowledge_date``) collapse to the latest-knowable -- the point-in-time
    value for that period. ``prov`` carries the full per-observation provenance
    (id, dates, licence, extracted level) in lockstep with ``keys``/``levels``.
    """
    rows = await visible_claims(
        pool,
        audience=audience,
        entity_id=entity_id,
        claim_type=spec.claim_type,
        key=spec.key,
    )
    latest: dict = {}  # align_on -> (knowledge_date, ProvenanceRow)
    for r in rows:
        level = _extract_scalar(r["value"], spec.value_field)
        if level is None:
            continue
        key = r[spec.align_on]
        kdate = r["knowledge_date"]
        existing = latest.get(key)
        if existing is None or kdate > existing[0]:
            latest[key] = (
                kdate,
                ProvenanceRow(
                    id=r["id"],
                    event_date=r["event_date"],
                    knowledge_date=kdate,
                    value=level,
                    redistributable=r["redistributable"],
                    audience_user_id=r["audience_user_id"],
                ),
            )
    ordered = sorted(latest.items(), key=lambda kv: kv[0])
    keys = [kv[0] for kv in ordered]
    levels = [kv[1][1].value for kv in ordered]
    prov = [kv[1][1] for kv in ordered]
    return keys, levels, prov


def _apply_transform(keys, levels, prov, transform: str):
    """Transform the level series, dropping the first element for returns/diff.

    The transformed value at position ``i`` is keyed to ``keys[i]`` (the end of
    the period). A non-positive level makes an adjacent return undefined and is
    skipped -- it is not a valid input for that transform. This is an exact
    data comparison (``<= 0``), not the computed-stat ``== 0`` hazard: levels
    are stored values, and a stored ``0.0`` is exactly zero. The carried
    provenance row's ``value`` is updated to the post-transform scalar so it
    agrees with the materialized ``.value``.
    """
    if transform == "level":
        return list(keys), list(levels), list(prov)
    t_keys, t_vals, t_prov = [], [], []
    for i in range(1, len(levels)):
        prev, cur = levels[i - 1], levels[i]
        if transform == "log_return":
            if prev <= 0 or cur <= 0:
                continue
            value = math.log(cur / prev)
        elif transform == "simple_return":
            if prev <= 0:
                continue
            value = (cur - prev) / prev
        elif transform == "diff":
            value = cur - prev
        else:  # pragma: no cover - validated at construction
            raise ValueError(f"unknown transform: {transform}")
        t_keys.append(keys[i])
        t_vals.append(value)
        t_prov.append(replace(prov[i], value=value))
    return t_keys, t_vals, t_prov


def _apply_window(keys, vals, prov, window: int | None):
    """Trailing ``window`` observations, after transform. ``None`` or too-large
    returns the series unchanged (the ``min_obs`` floor catches a true
    shortfall)."""
    if window is None or window <= 0 or len(vals) <= window:
        return list(keys), list(vals), list(prov)
    return list(keys[-window:]), list(vals[-window:]), list(prov[-window:])


def _floor(spec: ArgumentSpec, observed: int) -> Abstention | None:
    if spec.min_obs is not None and observed < spec.min_obs:
        return Abstention(spec.name, _short_reason(spec.name, observed, spec.min_obs))
    return None


def _empty_abstention(spec: ArgumentSpec) -> Abstention:
    if spec.min_obs is not None:
        return Abstention(spec.name, _short_reason(spec.name, 0, spec.min_obs))
    return Abstention(spec.name, f"{spec.name}: no observations")


def _calendar_short(spec: ArgumentSpec, keys) -> Abstention | None:
    """``min_calendar_days`` guard: the span (in days) between the earliest and
    latest surviving ``align_on`` date. Returns an ``Abstention`` naming the
    shortfall if the windowed observations do not span enough calendar time.

    This is the dimension ``min_obs`` cannot express: 12 daily and 12 monthly
    observations both clear ``min_obs=12``, but only the monthly set spans ~a
    year. The keys are the post-transform, post-window ``align_on`` values
    (datetimes), so the span is over exactly the observations the value was
    computed from. ``None`` (the default) disables the check, preserving every
    existing spec's count-only behaviour.
    """
    if spec.min_calendar_days is None:
        return None
    span_days = (max(keys) - min(keys)).days
    if span_days < spec.min_calendar_days:
        return Abstention(
            spec.name,
            f"{spec.name}: {span_days} of {spec.min_calendar_days} required "
            f"calendar days (short {spec.min_calendar_days - span_days})",
        )
    return None


def _shape_scalar(vals, prov) -> Materialized:
    return Materialized(value=vals[-1], claim_ids=(prov[-1].id,), rows=(prov[-1],))


def _shape_series(vals, prov) -> Materialized:
    return Materialized(
        value=list(vals), claim_ids=tuple(p.id for p in prov), rows=tuple(prov)
    )


async def _materialize_one(spec, pool, *, entity_id, audience):
    keys, levels, prov = await _levels_for_entity(
        pool, spec, entity_id=entity_id, audience=audience
    )
    t_keys, t_vals, t_prov = _apply_transform(keys, levels, prov, spec.transform)
    _w_keys, w_vals, w_prov = _apply_window(t_keys, t_vals, t_prov, spec.window)

    if not w_vals:
        return _empty_abstention(spec)
    short = _floor(spec, len(w_vals))
    if short is not None:
        return short
    cal = _calendar_short(spec, _w_keys)
    if cal is not None:
        return cal

    if spec.shape == "scalar":
        return _shape_scalar(w_vals, w_prov)
    return _shape_series(w_vals, w_prov)  # "series" and "list" share this path


async def _related_entity_ids(pool, entity_id, relation: str) -> list[UUID]:
    rows = await pool.fetch(
        "SELECT to_entity FROM entity_edge WHERE from_entity = $1 AND relation = $2",
        entity_id,
        relation,
    )
    return [r["to_entity"] for r in rows]


async def _materialize_related(spec, pool, *, entity_id, audience):
    eids = await _related_entity_ids(pool, entity_id, spec.relation)
    if not eids:
        return Abstention(
            spec.name, f"{spec.name}: no entities related via {spec.relation!r}"
        )

    per: dict[UUID, tuple[list, list[float], list[ProvenanceRow]]] = {}
    for eid in eids:
        keys, levels, prov = await _levels_for_entity(
            pool, spec, entity_id=eid, audience=audience
        )
        per[eid] = _apply_transform(keys, levels, prov, spec.transform)

    if spec.shape == "list":
        vals: list[float] = []
        prov_out: list[ProvenanceRow] = []
        for eid, (_tk, tv, tp) in per.items():
            if tv:
                vals.append(tv[-1])
                prov_out.append(tp[-1])
        if spec.window is not None and spec.window > 0 and len(vals) > spec.window:
            vals, prov_out = vals[-spec.window:], prov_out[-spec.window:]
        if not vals:
            return _empty_abstention(spec)
        short = _floor(spec, len(vals))
        if short is not None:
            return short
        return Materialized(
            value=list(vals),
            claim_ids=tuple(p.id for p in prov_out),
            rows=tuple(prov_out),
        )

    # scalar / series: align on the intersection of the transformed keys.
    keysets = [set(tk) for (tk, _tv, _tp) in per.values()]
    common = sorted(set.intersection(*keysets))
    windowed = common[-spec.window:] if spec.window else common

    by_entity: dict[UUID, tuple[float, ...]] = {}
    all_prov: list[ProvenanceRow] = []
    for eid, (tk, tv, tp) in per.items():
        idx = {k: i for i, k in enumerate(tk)}
        sel = [idx[k] for k in windowed]
        by_entity[eid] = tuple(tv[i] for i in sel)
        all_prov.extend(tp[i] for i in sel)

    if not windowed:
        return Abstention(spec.name, f"{spec.name}: empty intersection of related series")
    short = _floor(spec, len(windowed))
    if short is not None:
        return short

    if spec.shape == "scalar":
        if len(eids) != 1:
            return Abstention(
                spec.name,
                f"{spec.name}: scalar needs exactly one related entity, found {len(eids)}",
            )
        return Materialized(
            value=by_entity[eids[0]][-1],
            claim_ids=(all_prov[-1].id,),
            rows=(all_prov[-1],),
        )

    return Materialized(
        value=AlignedSeries(index=tuple(windowed), by_entity=by_entity),
        claim_ids=tuple(p.id for p in all_prov),
        rows=tuple(all_prov),
    )


async def materialize(spec: ArgumentSpec, pool, *, entity_id: UUID, audience):
    """Materialize ``spec`` against the store, or abstain.

    Reads through ``visible_claims`` scoped to ``audience`` only. Returns a
    ``Materialized`` value (with contributing ``claim_ids`` and per-observation
    provenance ``rows``) or an ``Abstention`` naming the shortfall. Never pads,
    never fabricates.
    """
    if spec.entity_scope in ("objective", "explicit"):
        return await _materialize_one(spec, pool, entity_id=entity_id, audience=audience)
    return await _materialize_related(spec, pool, entity_id=entity_id, audience=audience)
