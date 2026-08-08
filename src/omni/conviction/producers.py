"""Per-kind routing of prediction producers.

A producer is the coverage-only coroutine the predict loop dispatches per
demanded entity (``produce_*_prediction_from_coverage``). Which producers apply
to which entity is a property of the kind, not the producer: ``dcf_valuation``
reads EDGAR company facts and is meaningless for a token; ``trend.sma`` reads a
price window and is already kind-agnostic. This registry is the single source
of that mapping so the predict loop never hardcodes ``e.kind = 'company'``
again -- a clause that quietly starved every non-equity of any prediction.

A kind with no registered producer returns an empty tuple; the loop skips it.
That is correct, not an error: a kind with no applicable producer has nothing
to say, and refusing to say nothing is how the store stays honest.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import UUID

from omni.conviction.carry import produce_carry_prediction_from_coverage
from omni.conviction.predict import produce_dcf_prediction_from_coverage
from omni.conviction.trend import produce_trend_prediction_from_coverage


@dataclass(frozen=True)
class Producer:
    method: str
    entity_kinds: tuple[str, ...]
    produce: Callable[..., Awaitable[UUID | None]]
    requires_claim_types: tuple[str, ...]


PRODUCERS: tuple[Producer, ...] = (
    Producer(
        method="fundamentals.dcf_valuation",
        entity_kinds=("company",),
        produce=produce_dcf_prediction_from_coverage,
        requires_claim_types=("price_snapshot", "fundamental_metric"),
    ),
    Producer(
        method="trend.sma",
        entity_kinds=("company", "crypto_asset"),
        produce=produce_trend_prediction_from_coverage,
        requires_claim_types=("price_snapshot",),
    ),
    Producer(
        # crypto_asset only. A funding rate is a property of a perpetual
        # market; an equity has none, so registering this for `company` would
        # produce refusals forever while burning the fill budget doing it --
        # the same reason DCF stays company-only.
        method="carry.funding",
        entity_kinds=("crypto_asset",),
        produce=produce_carry_prediction_from_coverage,
        requires_claim_types=("funding_rate", "price_snapshot"),
    ),
)


def producers_for(kind: str) -> tuple[Producer, ...]:
    return tuple(p for p in PRODUCERS if kind in p.entity_kinds)
