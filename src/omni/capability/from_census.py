"""Build registry descriptors from the re-census.

The census is the inventory of what v1 can do; this turns it into something a
planner can reason over. Deriving the registry rather than hand-writing it is
deliberate — a hand-maintained list drifts from the code within weeks, and the
last census's counts disagreed across twelve documents for exactly that reason.
"""

from __future__ import annotations

import json
from pathlib import Path

from omni.capability.registry import Callability, Capability, Maturity, Registry

CENSUS = Path(__file__).resolve().parent / "recensus.json"

# Routers whose output is not coverage and must never become a capability the
# planner can schedule. Execution acts on the world; analysis describes it, and
# a planner that can place an order while answering a question is a different
# and much more dangerous product.
EXCLUDED_ROUTERS = frozenset({
    "trading", "ai_trading", "auto_trading", "crypto_trading", "orders",
    "auth", "users", "two_factor", "api_keys", "gdpr", "enterprise",
    "websocket", "health", "chat",
})


def _slug(router: str, endpoint: str) -> str:
    method, path = endpoint.split(None, 1)
    tail = path.rstrip("/").split("/")[-1] or "root"
    tail = tail.strip("{}").replace("-", "_").replace(".", "_")
    return f"{router}.{method.lower()}_{tail}"


def _touches_byo(credential_path: str, router: str) -> bool:
    """Conservative: anything not provably keyless is treated as licensed.

    Guessing wrong in this direction over-restricts a plan. Guessing wrong the
    other way redistributes somebody's licensed data.
    """
    return credential_path not in {"keyless", "n/a"}


def load_census(path: Path | None = None) -> list[dict]:
    return json.loads((path or CENSUS).read_text())


def build_registry(rows: list[dict] | None = None) -> Registry:
    rows = rows if rows is not None else load_census()
    registry = Registry()
    seen: set[str] = set()

    for row in rows:
        router = row.get("router", "")
        if router in EXCLUDED_ROUTERS:
            continue

        name = _slug(router, row["endpoint"])
        if name in seen:
            # Dual-mounted routers produce the same handler twice; one
            # descriptor is correct, since it is one callable thing.
            continue
        seen.add(name)

        try:
            maturity = Maturity(row.get("grade", "unknown"))
        except ValueError:
            maturity = Maturity.UNKNOWN
        try:
            callability = Callability(row.get("callable", "no"))
        except ValueError:
            callability = Callability.NO

        registry.add(
            Capability(
                name=name,
                description=row.get("feature", "").strip(),
                provider_key=None,
                touches_byo=_touches_byo(row.get("credential_path", ""), router),
                maturity=maturity,
                callability=callability,
                origin=row.get("impl", ""),
                provenance=row.get("notes", "") or row.get("evidence", ""),
                # No implementation is bound here. A descriptor without a call
                # is a backlog entry, and the planner will not schedule it.
                call=None,
            )
        )
    return registry


def coverage_report(registry: Registry) -> str:
    s = registry.summary()
    lines = [
        f"capabilities: {s['total']}",
        f"  invocable now:      {s['invocable']}",
        f"  needs extraction:   {s.get('callability:needs-extraction', 0)}",
        f"  not callable:       {s.get('callability:no', 0)}",
        f"  touch a paid source:{s['byo']}",
        "",
        "maturity:",
    ]
    for grade in ("wired", "stub", "orphaned", "fabricated", "unknown"):
        lines.append(f"  {grade:12s} {s.get(f'maturity:{grade}', 0)}")
    return "\n".join(lines)
