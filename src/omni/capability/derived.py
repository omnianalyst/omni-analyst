"""The derived capabilities that actually run today.

Sibling of ``builtin``: where that module binds ingestion adapters, this one
binds derivations -- claims computed from claims already in the store.
``perception.divergence`` is restated here as a **declaration**: two
``ArgumentSpec``s (``perception_macro`` and ``fundamental_metric``) plus the
existing ``compute_divergence``. ``fill_analysis`` materializes the arguments
through ``visible_claims`` (audience-scoped), abstains if either is short,
and calls the compute adapter.

The adapter (``compute_divergence_declared``) bridges the shape mismatch
between ``materialize``'s output and ``compute_divergence``'s input by building
``DivergenceInput`` lists from the provenance rows ``Materialized`` carries --
the dates and licence fields ``compute_divergence`` needs, read once through
``visible_claims`` during materialization rather than re-queried. See its
docstring for the detail.

``touches_byo=True`` is the safe-direction planning hint; the actual decision
is made per-fill by ``resolve_derived_licence`` over the materialized inputs.
``consumes`` is derived from the spec claim types, not hand-written.
"""

from __future__ import annotations

import json
from uuid import UUID

from omni.capabilities.macro import (
    inflation_measures,
    output_gap,
    sahm_rule,
    yield_curve_inversion,
)
from omni.capability.arguments import ArgumentSpec, Materialized
from omni.capability.registry import Callability, Capability, Maturity, Registry
from omni.coverage.visibility import visible_claims
from omni.fill.derived import DerivedCapability, fill_analysis
from omni.ingest.protocol import ClaimDraft, Unavailable
from omni.perception.divergence import (
    MIN_HISTORY,
    DivergenceInput,
    compute_divergence,
)

NAME = "perception.divergence"
PRODUCES = "perception_divergence"

# TODO(D9): neither spec sets ``key``. Divergence compares ONE perception
# series against ONE fundamental concept, yet a real entity carries several
# keys per claim_type (eight under ``fundamental_metric`` in the live DB); with
# no key, ``materialize`` blends them into one series and ``compute_divergence``
# emits a confident, wrong claim. The correct key is caller-supplied (which
# series pair to compare -- VIX vs Revenues, sentiment vs Earnings), not a
# fixed constant this capability can pick, so it is left unset pending a
# decision by whoever owns divergence. The single-key test fixtures (``vix``,
# ``Revenues``) are why this has never surfaced. The mechanism
# (``ArgumentSpec.key``) now exists; set it when the pairing is decided.
ARGUMENTS: tuple[ArgumentSpec, ...] = (
    ArgumentSpec(
        name="perception_macro",
        claim_type="perception_macro",
        shape="series",
        transform="level",
        min_obs=MIN_HISTORY,
    ),
    ArgumentSpec(
        name="fundamental_metric",
        claim_type="fundamental_metric",
        shape="series",
        transform="level",
        min_obs=MIN_HISTORY,
    ),
)

CONSUMES = tuple(spec.claim_type for spec in ARGUMENTS)


def _scalar(raw) -> float | None:
    """Pull a float out of a claim's JSONB ``value``.

    asyncpg returns jsonb columns as text (no codec is set on the pool), so a
    string is decoded first. A null-valued observation -- FRED's "." for an
    unpublished period -- has no scalar to extract and is skipped: it is
    coverage of the period, not an input to a computation.
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return None
    if not isinstance(raw, dict):
        return None
    val = raw.get("value")
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _to_inputs(rows) -> list[DivergenceInput]:
    out: list[DivergenceInput] = []
    for r in rows:
        val = _scalar(r["value"])
        if val is None:
            continue
        out.append(
            DivergenceInput(
                id=r["id"],
                event_date=r["event_date"],
                knowledge_date=r["knowledge_date"],
                value=val,
                redistributable=r["redistributable"],
                audience_user_id=r["audience_user_id"],
            )
        )
    return out


def _to_inputs_from_rows(rows) -> list[DivergenceInput]:
    """Build ``DivergenceInput`` lists from the provenance rows ``materialize``
    carried -- no re-read.

    Each row already carries the id, dates, post-transform value and licence
    fields ``compute_divergence`` needs. This is the single place the
    ``Materialized``-to-``DivergenceInput`` shape conversion happens, so a
    future analysis that hits the same mismatch puts its adapter here.
    """
    return [
        DivergenceInput(
            id=r.id,
            event_date=r.event_date,
            knowledge_date=r.knowledge_date,
            value=r.value,
            redistributable=r.redistributable,
            audience_user_id=r.audience_user_id,
        )
        for r in rows
    ]


async def compute_divergence_declared(
    pool,
    gap,
    *,
    perception_macro: Materialized,
    fundamental_metric: Materialized,
):
    """Declared-path compute: adapt ``Materialized`` to ``DivergenceInput`` lists.

    The per-observation provenance (id, dates, value, licence) is carried in
    ``Materialized.rows`` by ``materialize`` -- the post-transform, post-window
    set the value was actually computed from, read through ``visible_claims``
    (audience-scoped). This builds the ``DivergenceInput`` lists from those rows
    directly, so ``compute_divergence`` gets the same inputs the old re-read
    produced without a second query.
    """
    perc = _to_inputs_from_rows(perception_macro.rows)
    facts = _to_inputs_from_rows(fundamental_metric.rows)
    return compute_divergence(perc, facts)


async def gather_divergence_inputs(
    pool, gap
) -> tuple[list[DivergenceInput], list[DivergenceInput]]:
    """Load perception_macro and fundamental_metric claims visible to the gap's
    audience, as DivergenceInputs.

    The injected-gather escape hatch: kept as the template for a capability
    whose inputs the declaration cannot describe (multi-entity panels,
    caller-built structures). Divergence itself now uses the declared path.
    """
    audience: UUID | None = gap["audience_user_id"]
    entity_id = gap["entity_id"]

    perception_rows = await visible_claims(
        pool, audience=audience, entity_id=entity_id, claim_type=CONSUMES[0]
    )
    fact_rows = await visible_claims(
        pool, audience=audience, entity_id=entity_id, claim_type=CONSUMES[1]
    )
    return _to_inputs(perception_rows), _to_inputs(fact_rows)


def compute_divergence_claim(
    perception: list[DivergenceInput], facts: list[DivergenceInput]
):
    """Adapt ``compute_divergence`` to the escape-hatch fill path's
    ``(draft, ids) | None`` contract, declaring every input as an edge."""
    draft = compute_divergence(perception, facts)
    if draft is None:
        return None
    return draft, [c.id for c in (*perception, *facts)]


DERIVED = DerivedCapability(
    name=NAME,
    arguments=ARGUMENTS,
    compute=compute_divergence_declared,
)


# ----------------------------------------------------------- yield-curve signal
#
# The first earned claim type after perception_divergence (D2 step 5). A derived
# STATE -- "is the 2Y/10Y curve inverted right now" -- not a horizon-parameterized
# computation, so it earns a claim type: macro.recession_probability consumes the
# inversion flag, and the signal has a real freshness policy. The compute is the
# existing (unchanged) ``macro.yield_curve_inversion``; this declaration gives its
# output a durable home in the coverage store.
#
# v1 sources this from FRED ``DGS2``/``DGS10`` (confirmed in
# ../software/backend/app/workers/signal_fusion_tasks.py:349-350); those series
# ids are the ArgumentSpec keys (the exact purpose D9's ``key`` field exists for:
# two series sharing one claim_type, ``macro_series_point``, distinguished here).

YC_NAME = "macro.yield_curve_signal"
YC_PRODUCES = "yield_curve_signal"
YC_KEY = "2y_10y"

# ``yield_curve_inversion`` looks at ``common_dates[-252:]`` (one trading year of
# daily yields) and counts inversions over ``spreads[-90:]``. ``window`` passes
# the function its full intended lookback when that much history exists;
# ``min_obs`` is the floor below which the 90-day inverted count would be
# silently truncated -- a 45-day count wearing a "90d" label is the fabrication
# vector AGENTS.md warns against. 90, not 252: the headline output
# (``days_inverted_90d``, ``is_inverted``) is fully meaningful at 90 common
# dates; demanding a full trading year would abstain for any entity younger than
# that when the recession signal is already honest at ~four months of data.
YC_WINDOW = 252
YC_MIN_OBS = 90

YC_ARGUMENTS: tuple[ArgumentSpec, ...] = (
    ArgumentSpec(
        name="series_2y",
        claim_type="macro_series_point",
        key="DGS2",
        shape="series",
        transform="level",
        window=YC_WINDOW,
        min_obs=YC_MIN_OBS,
    ),
    ArgumentSpec(
        name="series_10y",
        claim_type="macro_series_point",
        key="DGS10",
        shape="series",
        transform="level",
        window=YC_WINDOW,
        min_obs=YC_MIN_OBS,
    ),
)

YC_CONSUMES = tuple(spec.claim_type for spec in YC_ARGUMENTS)


async def compute_yield_curve_signal_declared(
    pool, gap, *, series_2y: Materialized, series_10y: Materialized
):
    """Declared-path compute: adapt ``Materialized`` to the two
    ``dict[date, float]`` arguments ``yield_curve_inversion`` expects.

    ``yield_curve_inversion`` takes series shaped as dicts keyed by date, not
    the ordered ``Materialized`` shape ``materialize`` returns -- the same kind
    of shape mismatch ``compute_divergence_declared`` bridges for
    ``DivergenceInput`` lists. The adapter lives here, next to that one, not in
    ``arguments.py`` (per D5/D10): the shape conversion is specific to this one
    analysis function's signature.

    Returns a ``ClaimDraft`` of ``claim_type="yield_curve_signal"`` whose
    ``value`` is the durable state a consumer reads (spread, inversion flag,
    90-day count) and whose ``evidence`` carries the recent spread trajectory
    (dates serialized for JSONB) and the input claim ids. The bulky
    ``historical_spreads`` is evidence, not value: it is supporting detail
    reconstructable from the inputs, not the headline signal a consumer joins on.
    Abstains (``None``) when fewer than 90 common dates survive -- the function's
    ``days_inverted_90d`` is a count over ``spreads[-90:]`` and silently shrinks
    below that, so the per-series ``min_obs`` floor is backed by a check on the
    actual intersection.
    """
    d2y = {r.event_date: r.value for r in series_2y.rows}
    d10y = {r.event_date: r.value for r in series_10y.rows}

    common = set(d2y) & set(d10y)
    if len(common) < YC_MIN_OBS:
        return None

    try:
        result = await yield_curve_inversion(d2y, d10y)
    except Unavailable:
        return None

    last_event = max(common)
    latest_knowledge = max(
        r.knowledge_date for r in (*series_2y.rows, *series_10y.rows)
    )

    historical = [
        {
            "date": s["date"].isoformat(),
            "spread": s["spread"],
            "inverted": s["inverted"],
        }
        for s in result["historical_spreads"]
    ]

    return ClaimDraft(
        claim_type=YC_PRODUCES,
        key=YC_KEY,
        value={
            "current_spread": result["current_spread"],
            "is_inverted": result["is_inverted"],
            "days_inverted_90d": result["days_inverted_90d"],
        },
        event_date=last_event,
        knowledge_date=latest_knowledge,
        confidence=1.0,
        unit="percent",
        evidence={
            "series": ["DGS2", "DGS10"],
            "historical_spreads": historical,
            "input_claim_ids": [
                str(r.id) for r in (*series_2y.rows, *series_10y.rows)
            ],
        },
    )


YIELD_CURVE = DerivedCapability(
    name=YC_NAME,
    arguments=YC_ARGUMENTS,
    compute=compute_yield_curve_signal_declared,
)


# --------------------------------------------------------------- sahm-rule signal
#
# The second earned claim type after perception_divergence (D2 step 5), applying
# D10's template a second time. A derived STATE -- "has the Sahm recession
# threshold triggered right now" -- not a horizon-parameterized computation, so
# it earns a claim type: macro.recession_probability consumes the triggered flag
# as a direct argument (sahm_triggered), and the signal has a real freshness
# policy. The compute is the existing (unchanged) ``macro.sahm_rule``; this
# declaration gives its output a durable home in the coverage store.
#
# v1 sources this from FRED ``UNRATE`` (Civilian Unemployment Rate). The series
# id was verified against FRED directly before use -- confirmed: id "UNRATE",
# title "Unemployment Rate", frequency "Monthly", units "Percent", Seasonally
# Adjusted (QF1 found a prose-named id that 404s, so no series id is taken on
# faith). UNRATE is the ArgumentSpec key.

SAHM_NAME = "macro.sahm_rule_signal"
SAHM_PRODUCES = "sahm_rule_signal"
SAHM_KEY = "unrate"

# ``sahm_rule`` reads ``unemployment_values[-3:]`` (the 3-month moving average)
# and ``unemployment_values[-12:]`` (the 12-month low), and raises ``Unavailable``
# below 12 observations. The crux where D10's template needed a daily-vs-monthly
# adjustment: D10 sized ``window``/``min_obs`` against DAILY treasury yields
# (252 observations ~= one trading year). UNRATE is MONTHLY, so 12 observations
# already span ~one calendar year, and a count sized as if the series were daily
# would be wrong in the opposite direction.
#
# Arithmetic (monthly):
#   - ``min_obs = 12``: the function's hard floor. The spec abstains at 11, so
#     ``sahm_rule``'s ``Unavailable`` branch (raises at <12) is unreachable
#     through the declared path -- abstention is the spec's job, not the
#     capability's. There is one input series and no two-series intersection, so
#     unlike YIELD_CURVE no compute-time floor check is needed: per-series
#     ``min_obs`` sees the whole count the function sees.
#   - ``window = 18`` = 12 (the function's trailing-12 lookback, ~one calendar
#     year of monthly data) + 6 months margin for publication lag (a reference
#     month's reading lands in the first week of the following month, so the
#     newest available observation can lag) and a missing mid-series
#     observation, so a single hole does not drop the materialized count below
#     ``min_obs``. ``sahm_rule`` ignores anything older than its trailing-12
#     slice, so the extra observations are margin only -- passing 18 to a
#     function that reads ``[-12:]`` and ``[-3:]`` yields the same trailing-12
#     and trailing-3 as passing exactly 12.
SAHM_MIN_OBS = 12
SAHM_WINDOW = 18

SAHM_ARGUMENTS: tuple[ArgumentSpec, ...] = (
    ArgumentSpec(
        name="unemployment",
        claim_type="macro_series_point",
        key="UNRATE",
        shape="series",
        transform="level",
        window=SAHM_WINDOW,
        min_obs=SAHM_MIN_OBS,
    ),
)

SAHM_CONSUMES = tuple(spec.claim_type for spec in SAHM_ARGUMENTS)


async def compute_sahm_rule_signal_declared(pool, gap, *, unemployment: Materialized):
    """Declared-path compute: adapt ``Materialized`` to the ``Sequence[float]``
    ``sahm_rule`` expects.

    ``sahm_rule`` takes a single ordered float series, not the ``Materialized``
    shape ``materialize`` returns -- the same kind of shape mismatch
    ``compute_yield_curve_signal_declared`` bridges. The adapter lives here,
    next to that one, not in ``arguments.py`` (per D5/D10): the shape conversion
    is specific to this one analysis function's signature.

    Returns a ``ClaimDraft`` of ``claim_type="sahm_rule_signal"`` whose ``value``
    is the durable state a consumer reads (the Sahm indicator and the triggered
    flag ``macro.recession_probability`` consumes as ``sahm_triggered``) and
    whose ``evidence`` carries the supporting ``current_unemployment`` /
    ``12m_low`` (reconstructable from the inputs) and the input claim ids.

    ``min_obs=12`` on the spec guarantees ``sahm_rule`` never sees fewer than
    its 12-observation floor, so its ``Unavailable`` branch is unreachable
    through the declared path; the try/except is defense-in-depth, not the
    abstention mechanism. Unlike YIELD_CURVE there is one input series and no
    two-series intersection, so there is no compute-time floor check to add.

    ``sahm_rule`` returns numpy scalars (``np.mean``/``np.min`` -> ``np.float64``;
    the ``>=`` threshold -> ``np.bool_``), which ``json.dumps`` cannot serialize.
    Casting to native ``float``/``bool`` here is adapter plumbing -- the values
    are identical, only the wrapper changes -- and lives with the rest of the
    shape bridging rather than in ``macro.py`` (whose behaviour is unchanged).
    """
    values = [r.value for r in unemployment.rows]

    try:
        result = await sahm_rule(values)
    except Unavailable:
        return None

    last_row = unemployment.rows[-1]
    latest_knowledge = max(r.knowledge_date for r in unemployment.rows)

    return ClaimDraft(
        claim_type=SAHM_PRODUCES,
        key=SAHM_KEY,
        value={
            "indicator": float(result["value"]),
            "triggered": bool(result["triggered"]),
        },
        event_date=last_row.event_date,
        knowledge_date=latest_knowledge,
        confidence=1.0,
        unit="percent",
        evidence={
            "series": ["UNRATE"],
            "current_unemployment": float(result["current_unemployment"]),
            "12m_low": float(result["12m_low"]),
            "input_claim_ids": [str(r.id) for r in unemployment.rows],
        },
    )


SAHM = DerivedCapability(
    name=SAHM_NAME,
    arguments=SAHM_ARGUMENTS,
    compute=compute_sahm_rule_signal_declared,
)


# ------------------------------------------------------------ inflation signal
#
# The third earned macro claim type after yield_curve_signal (D10) and
# sahm_rule_signal (D14), applying D10's template a third time. A derived
# STATE -- "what is CPI inflation right now" (YoY, MoM-annualized,
# 3m-annualized) -- not a horizon-parameterized computation, so it earns a
# claim type: macro.taylor_rule takes ``inflation`` as a direct argument, and
# the signal has a real freshness policy. The compute is the existing
# (unchanged) ``macro.inflation_measures``; this declaration gives its output a
# durable home in the coverage store.
#
# v1 sources this from FRED ``CPIAUCSL`` (Consumer Price Index for All Urban
# Consumers: All Items, Index 1982-84=100, Seasonally Adjusted). The series id
# was verified against FRED directly (series page confirms title and Monthly
# frequency) AND corroborated by v1
# software/backend/app/services/macroeconomic/fed_data_service.py:106
# ("cpi_all_items": "CPIAUCSL") -- two sources, no prose taken on faith.
# CPIAUCSL is the ArgumentSpec key. inflation_measures runs on an index LEVEL
# series (YoY = index[-1]/index[-13] - 1); a percent-change series would
# double-count, which is why ``transform="level"`` is correct here.

INFLATION_NAME = "macro.inflation_signal"
INFLATION_PRODUCES = "inflation_signal"
INFLATION_KEY = "cpi_all"
INFLATION_SPEC_KEY = "CPIAUCSL"

# ``inflation_measures`` reads ``cpi_values[-1]`` (current), ``[-2]`` (previous
# month), ``[-4]`` (three months ago) and ``[-13]`` (one year ago), and raises
# ``Unavailable`` below 13 observations (the year-ago index is the binding
# floor). The same daily-vs-monthly adjustment D14 made for UNRATE applies
# here: CPIAUCSL is MONTHLY, so 13 observations already span ~one calendar year
# (a YoY reading needs a year of data by definition).
#
# Arithmetic (monthly):
#   - ``min_obs = 13``: the function's hard floor. The spec abstains at 12, so
#     ``inflation_measures``' ``Unavailable`` branch (raises at <13) is
#     unreachable through the declared path -- abstention is the spec's job,
#     not the capability's. One input series, no two-series intersection, so
#     unlike YIELD_CURVE no compute-time floor check is needed.
#   - ``window = 19`` = 13 (the function's trailing-13 year-ago lookback,
#     ~one calendar year of monthly data) + 6 months margin for the ~1-month
#     publication lag and a missing mid-series observation, so a single hole
#     does not drop the materialized count below ``min_obs``. The function
#     ignores anything older than its trailing-13 slice, so the extra six are
#     margin only -- passing 19 to a function that reads ``[-13:]``/``[-4]``/
#     ``[-2]``/``[-1]`` yields the same result as passing exactly 13.
INFLATION_MIN_OBS = 13
INFLATION_WINDOW = 19

INFLATION_ARGUMENTS: tuple[ArgumentSpec, ...] = (
    ArgumentSpec(
        name="cpi",
        claim_type="macro_series_point",
        key=INFLATION_SPEC_KEY,
        shape="series",
        transform="level",
        window=INFLATION_WINDOW,
        min_obs=INFLATION_MIN_OBS,
    ),
)

INFLATION_CONSUMES = tuple(spec.claim_type for spec in INFLATION_ARGUMENTS)


async def compute_inflation_signal_declared(pool, gap, *, cpi: Materialized):
    """Declared-path compute: adapt ``Materialized`` to the ``Sequence[float]``
    ``inflation_measures`` expects.

    ``inflation_measures`` takes a single ordered CPI index series, not the
    ``Materialized`` shape ``materialize`` returns -- the same shape mismatch
    ``compute_sahm_rule_signal_declared`` bridges. The adapter lives here, next
    to that one, not in ``arguments.py`` (per D5/D10): the shape conversion is
    specific to this one analysis function's signature.

    Returns a ``ClaimDraft`` of ``claim_type="inflation_signal"`` whose
    ``value`` is the durable state a consumer reads (the three inflation rates
    ``macro.taylor_rule`` consumes as ``inflation`` -- YoY is the headline) and
    whose ``evidence`` carries the supporting ``current_index`` and ``trend``
    (reconstructable from the inputs) and the input claim ids.

    ``min_obs=13`` on the spec guarantees ``inflation_measures`` never sees
    fewer than its 13-observation floor, so its ``Unavailable`` branch is
    unreachable through the declared path; the try/except is defense-in-depth,
    not the abstention mechanism. One input series, no two-series intersection,
    so no compute-time floor check is needed (unlike YIELD_CURVE).

    ``inflation_measures``/``calculate_inflation_trend`` return numpy scalars
    (``np.mean``/``np.std`` -> ``np.float64``), which ``json.dumps`` cannot
    serialize. Casting to native ``float`` here is adapter plumbing -- the
    values are identical, only the wrapper changes -- and lives with the rest
    of the shape bridging rather than in ``macro.py`` (whose behaviour is
    unchanged).
    """
    values = [r.value for r in cpi.rows]

    try:
        result = await inflation_measures(values)
    except Unavailable:
        return None

    last_row = cpi.rows[-1]
    latest_knowledge = max(r.knowledge_date for r in cpi.rows)
    trend = result["trend"]

    return ClaimDraft(
        claim_type=INFLATION_PRODUCES,
        key=INFLATION_KEY,
        value={
            "yoy": float(result["yoy"]),
            "mom_annualized": float(result["mom_annualized"]),
            "3m_annualized": float(result["3m_annualized"]),
        },
        event_date=last_row.event_date,
        knowledge_date=latest_knowledge,
        confidence=1.0,
        unit="percent",
        evidence={
            "series": [INFLATION_SPEC_KEY],
            "current_index": float(result["current_index"]),
            "trend": {
                "momentum": trend["momentum"],
                "3m_annualized": float(trend["3m_annualized"]),
                "volatility": float(trend["volatility"]),
            },
            "input_claim_ids": [str(r.id) for r in cpi.rows],
        },
    )


INFLATION = DerivedCapability(
    name=INFLATION_NAME,
    arguments=INFLATION_ARGUMENTS,
    compute=compute_inflation_signal_declared,
)


# -------------------------------------------------------------- output-gap signal
#
# The fourth earned macro claim type, and the one that unblocks macro.taylor_rule
# as a composite consuming TWO earned claim types (inflation_signal + this). A
# derived STATE -- "what is the output gap right now" (the CBO percent deviation
# of real GDP from potential) -- not a horizon-parameterized computation, so it
# earns a claim type: taylor_rule takes output_gap as a direct argument, and
# the signal has a real freshness policy. The compute is the new (this session)
# ``macro.output_gap``; this declaration gives its output a durable home.
#
# v1 sources real GDP from FRED ``GDPC1`` and potential GDP from ``GDPPOT``
# (both confirmed against FRED directly: "Real Gross Domestic Product" /
# "Real Potential Gross Domestic Product", quarterly, chained 2017 dollars). The
# canonical level ratio (GDPC1 - GDPPOT) / GDPPOT * 100 is used, NOT v1's
# growth-rate-diff approximation (which v1 itself flagged "simplified"). GDPC1
# and GDPPOT are the ArgumentSpec keys.
#
# Scalar inputs (shape="scalar", min_obs=1): output_gap reads only the latest of
# each series (the gap is a current state, one value per quarter). This is the
# credit_risk/inflation_expectations shape, not sahm's series shape -- the
# function does not need a lookback window. min_obs=1 abstains when either
# series has no observation, so output_gap's Unavailable branch (raises on
# empty / non-positive potential) is unreachable through the declared path.
#
# Alignment caveat: the two series' latest observations are read independently,
# so if GDPC1 and GDPPOT's most recent reference quarters differ the gap is
# approximate. For FRED both publish quarterly and usually align; a future
# common-date intersection (a la yield_curve) would harden it. Documented, not
# silently papered over.
OUTPUT_GAP_NAME = "macro.output_gap_signal"
OUTPUT_GAP_PRODUCES = "output_gap_signal"
OUTPUT_GAP_KEY = "gdpc1_gdppot"
OUTPUT_GAP_SPEC_KEYS = ("GDPC1", "GDPPOT")

OUTPUT_GAP_ARGUMENTS: tuple[ArgumentSpec, ...] = (
    ArgumentSpec(
        name="gdp",
        claim_type="macro_series_point",
        key="GDPC1",
        shape="scalar",
        transform="level",
        min_obs=1,
    ),
    ArgumentSpec(
        name="potential",
        claim_type="macro_series_point",
        key="GDPPOT",
        shape="scalar",
        transform="level",
        min_obs=1,
    ),
)

OUTPUT_GAP_CONSUMES = tuple(spec.claim_type for spec in OUTPUT_GAP_ARGUMENTS)


async def compute_output_gap_signal_declared(
    pool, gap, *, gdp: Materialized, potential: Materialized
):
    """Declared-path compute: adapt two scalar ``Materialized`` values to the
    ``Sequence[float]`` ``output_gap`` expects (it reads ``[-1]``).

    One input observation per series is enough -- the gap is a current state --
    so the adapter wraps each scalar in a one-element list, the same way
    ``_compute_inflation_expectations`` / ``_compute_credit_risk`` adapt scalars
    to sequence-indexing functions. ``min_obs=1`` on both specs guarantees
    neither is empty, so ``output_gap``'s ``Unavailable`` branch is unreachable
    through the declared path; the try/except is defense-in-depth.

    Returns a ``ClaimDraft`` of ``claim_type="output_gap_signal"`` whose
    ``value`` is the durable state ``macro.taylor_rule`` consumes (the percent
    gap) and whose ``evidence`` carries the supporting ``gdp`` / ``potential``
    levels (reconstructable from the inputs) and the input claim ids.
    """
    try:
        result = await output_gap([gdp.value], [potential.value])
    except Unavailable:
        return None

    latest_knowledge = max(r.knowledge_date for r in (*gdp.rows, *potential.rows))
    last_event = max(gdp.rows[-1].event_date, potential.rows[-1].event_date)

    return ClaimDraft(
        claim_type=OUTPUT_GAP_PRODUCES,
        key=OUTPUT_GAP_KEY,
        value={
            "output_gap": float(result["output_gap"]),
        },
        event_date=last_event,
        knowledge_date=latest_knowledge,
        confidence=1.0,
        unit="percent",
        evidence={
            "series": list(OUTPUT_GAP_SPEC_KEYS),
            "gdp": float(result["gdp"]),
            "potential": float(result["potential"]),
            "input_claim_ids": [str(r.id) for r in (*gdp.rows, *potential.rows)],
        },
    )


OUTPUT_GAP = DerivedCapability(
    name=OUTPUT_GAP_NAME,
    arguments=OUTPUT_GAP_ARGUMENTS,
    compute=compute_output_gap_signal_declared,
)


def build_derived_registry() -> Registry:
    """Register the derived capabilities that are wired and tested today."""
    registry = Registry()

    async def call(pool, gap):
        return await fill_analysis(pool, gap, capability=DERIVED)

    async def call_yield_curve(pool, gap):
        return await fill_analysis(pool, gap, capability=YIELD_CURVE)

    async def call_sahm(pool, gap):
        return await fill_analysis(pool, gap, capability=SAHM)

    async def call_inflation(pool, gap):
        return await fill_analysis(pool, gap, capability=INFLATION)

    async def call_output_gap(pool, gap):
        return await fill_analysis(pool, gap, capability=OUTPUT_GAP)

    registry.add(
        Capability(
            name=NAME,
            description=(
                "Cross-domain divergence: fundamentals improving while "
                "perception deteriorates (or the reverse). Computable only "
                "because perception and fundamentals live in one store."
            ),
            consumes=CONSUMES,
            produces=(PRODUCES,),
            touches_byo=True,
            cost=0.1,
            maturity=Maturity.WIRED,
            callability=Callability.YES,
            origin="omni.perception.divergence",
            call=call,
        )
    )
    registry.add(
        Capability(
            name=YC_NAME,
            description=(
                "2Y/10Y treasury yield-curve inversion signal: current spread, "
                "inversion flag and 90-day days-inverted count, derived from "
                "DGS2/DGS10 macro_series_point claims."
            ),
            consumes=YC_CONSUMES,
            produces=(YC_PRODUCES,),
            touches_byo=False,
            cost=0.1,
            maturity=Maturity.WIRED,
            callability=Callability.YES,
            origin="omni.capabilities.macro.yield_curve_inversion",
            call=call_yield_curve,
        )
    )
    registry.add(
        Capability(
            name=SAHM_NAME,
            description=(
                "Sahm recession rule signal: the 3-month moving average minus "
                "the 12-month low of the unemployment rate, and the >=0.5 "
                "triggered flag, derived from UNRATE macro_series_point claims."
            ),
            consumes=SAHM_CONSUMES,
            produces=(SAHM_PRODUCES,),
            touches_byo=False,
            cost=0.1,
            maturity=Maturity.WIRED,
            callability=Callability.YES,
            origin="omni.capabilities.macro.sahm_rule",
            call=call_sahm,
        )
    )
    registry.add(
        Capability(
            name=INFLATION_NAME,
            description=(
                "CPI inflation measures: YoY, month-over-month annualized and "
                "3-month annualized inflation, plus trend, derived from "
                "CPIAUCSL macro_series_point (index level) claims."
            ),
            consumes=INFLATION_CONSUMES,
            produces=(INFLATION_PRODUCES,),
            touches_byo=False,
            cost=0.1,
            maturity=Maturity.WIRED,
            callability=Callability.YES,
            origin="omni.capabilities.macro.inflation_measures",
            call=call_inflation,
        )
    )
    registry.add(
        Capability(
            name=OUTPUT_GAP_NAME,
            description=(
                "CBO output gap: the percent deviation of real GDP from "
                "potential, (GDPC1 - GDPPOT) / GDPPOT * 100, derived from "
                "GDPC1/GDPPOT macro_series_point (index level) claims. "
                "Consumed by macro.taylor_rule."
            ),
            consumes=OUTPUT_GAP_CONSUMES,
            produces=(OUTPUT_GAP_PRODUCES,),
            touches_byo=False,
            cost=0.1,
            maturity=Maturity.WIRED,
            callability=Callability.YES,
            origin="omni.capabilities.macro.output_gap",
            call=call_output_gap,
        )
    )
    return registry
