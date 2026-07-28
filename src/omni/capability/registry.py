"""The capability registry — what the orchestrator is allowed to call.

v1 exposed its analysis as 641 HTTP routes and made the user assemble them.
v2 keeps the analysis and drops the assembly: each capability is a descriptor
the planner can reason about, so the system does the cross-analysis instead of
the person.

Three properties make a descriptor useful to a planner rather than just a
catalogue entry:

**Licence-aware.** `touches_byo` says whether invoking this can only produce
private coverage. A plan that would blend a byo_only result into a shared
answer is caught while planning, not when the database rejects the write.

**Honest about provenance.** `is_proxy` / `proxy_of` carry forward the one
genuinely good idea in v1's signal registry, which labelled three of its own
sentiment signals "not real sentiment data". A planner that cannot tell a
measurement from a stand-in will present a stand-in as a measurement.

**Self-calibrating.** `reliability` is derived from the prediction ledger, not
declared. A capability whose claims keep resolving wrong gets down-weighted
automatically. That closes resolution back onto capability selection, which is
the feedback edge v1 never had.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Awaitable, Callable, Sequence


class Callability(str, Enum):
    """Whether v2 can invoke this without v1's HTTP layer. From the census."""

    YES = "yes"
    NEEDS_EXTRACTION = "needs-extraction"
    NO = "no"


class Maturity(str, Enum):
    """The census grade, carried through so the planner can avoid stubs."""

    WIRED = "wired"
    STUB = "stub"
    ORPHANED = "orphaned"
    FABRICATED = "fabricated"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Capability:
    name: str
    description: str

    consumes: tuple[str, ...] = ()
    produces: tuple[str, ...] = ()

    # Which entity kinds this serves. Empty means any. Without it a planner
    # will happily route an equity to a crypto price feed, because both
    # produce price_snapshot and one is cheaper.
    entity_kinds: tuple[str, ...] = ()

    # Licence. A capability reaching a byo_only provider can only ever produce
    # private coverage, whoever asked for it.
    provider_key: str | None = None
    touches_byo: bool = False

    # Honesty about what the output actually is.
    is_proxy: bool = False
    proxy_of: tuple[str, ...] = ()
    provenance: str = ""

    # Planning inputs.
    cost: float = 1.0
    maturity: Maturity = Maturity.UNKNOWN
    callability: Callability = Callability.YES

    origin: str = ""
    call: Callable[..., Awaitable[object]] | None = None

    @property
    def invocable(self) -> bool:
        """Whether the planner may actually schedule this.

        A descriptor can exist for something not yet callable — that is how the
        registry doubles as the migration backlog — but the planner must never
        pick one.
        """
        return (
            self.call is not None
            and self.callability is Callability.YES
            and self.maturity not in (Maturity.FABRICATED, Maturity.ORPHANED)
        )


class Registry:
    def __init__(self) -> None:
        self._by_name: dict[str, Capability] = {}
        self._reliability: dict[str, float] = {}

    def add(self, capability: Capability) -> None:
        if capability.name in self._by_name:
            raise ValueError(f"duplicate capability: {capability.name}")
        self._by_name[capability.name] = capability

    def get(self, name: str) -> Capability | None:
        return self._by_name.get(name)

    def __len__(self) -> int:
        return len(self._by_name)

    def observe_reliability(self, name: str, hit_rate: float) -> None:
        """Record a calibrated hit rate for this capability's claims.

        Fed from the prediction ledger. Not declared by the capability, because
        a capability's own opinion of its reliability is worth nothing.
        """
        if not 0.0 <= hit_rate <= 1.0:
            raise ValueError(f"hit rate out of range: {hit_rate}")
        self._reliability[name] = hit_rate

    def reliability(self, name: str) -> float | None:
        """Calibrated hit rate, or None when there is not enough evidence yet.

        None is not zero. An uncalibrated capability is unproven, not bad, and
        a planner must be able to tell those apart.
        """
        return self._reliability.get(name)

    def producing(
        self, claim_type: str, *, allow_byo: bool = True,
        invocable_only: bool = True, entity_kind: str | None = None,
    ) -> list[Capability]:
        """Capabilities that can produce this claim type, best first.

        `allow_byo=False` is how a planner building a shareable answer excludes
        anything that would taint it.
        """
        out = [
            c for c in self._by_name.values()
            if claim_type in c.produces
            and (allow_byo or not c.touches_byo)
            and (c.invocable if invocable_only else True)
            and (entity_kind is None or not c.entity_kinds
                 or entity_kind in c.entity_kinds)
        ]
        return sorted(out, key=self._rank)

    def _rank(self, c: Capability) -> tuple:
        # Calibrated capabilities first, then by hit rate, then by cheapness.
        # Uncalibrated sorts after calibrated rather than at the bottom: it is
        # unproven, not known-bad, and it needs to run to ever get calibrated.
        r = self._reliability.get(c.name)
        return (0 if r is not None else 1, -(r or 0.0), c.cost, c.name)

    def backlog(self) -> list[Capability]:
        """Descriptors that exist but cannot be called yet.

        The registry is also the migration list: everything the census marked
        `needs-extraction`, plus anything with no implementation bound.
        """
        return sorted(
            (c for c in self._by_name.values() if not c.invocable),
            key=lambda c: (c.callability.value, c.name),
        )

    def summary(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for c in self._by_name.values():
            counts[f"maturity:{c.maturity.value}"] = (
                counts.get(f"maturity:{c.maturity.value}", 0) + 1
            )
            counts[f"callability:{c.callability.value}"] = (
                counts.get(f"callability:{c.callability.value}", 0) + 1
            )
        counts["invocable"] = sum(1 for c in self._by_name.values() if c.invocable)
        counts["byo"] = sum(1 for c in self._by_name.values() if c.touches_byo)
        counts["total"] = len(self._by_name)
        return counts
