"""What the shadow book runs on: the universe, the panel, and the next session.

This lives in `src/` rather than in `ops/` because the deployed image ships only
`src/` and `migrations/`. An ops script is piped in over stdin, so anything it
imports has to be installed -- and the first version of this put `load_panel`
in the script, which worked locally and failed in production with
`No module named 'ops'`. It is also the right home on the merits: choosing an
audience, refusing an empty panel and deciding which session a decision may
apply to are all decisions, not glue.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pandas as pd

# The sector ETFs and benchmark the current three shadow books record. The
# separately seeded allocation funds are not folded into this universe: that
# would silently change the three existing rules.
SECTORS = ["XLB", "XLC", "XLE", "XLF", "XLI", "XLK", "XLP", "XLRE", "XLU", "XLV", "XLY"]
BENCHMARK = "SPY"

# Charged against turnover into the recorded weights. 2 bps is the ETF spread
# assumption `etf_replication` already uses; stating it on the row means a later
# reader cannot discover a kinder number after seeing the outcome.
COST_BPS = Decimal(2)

_PANEL = """
SELECT e.symbol, c.event_date, (c.value->>'close')::float8 AS close
FROM claim c
JOIN entity e ON e.id = c.entity_id
WHERE c.claim_type = 'price_snapshot'
  AND e.symbol = ANY($1::text[])
  AND c.value->>'close' IS NOT NULL
  AND c.audience_user_id IS NOT DISTINCT FROM $2
ORDER BY e.symbol, c.event_date, c.knowledge_date
"""

_AUDIENCE = """
SELECT c.audience_user_id
FROM claim c
JOIN entity e ON e.id = c.entity_id
WHERE c.claim_type = 'price_snapshot' AND e.symbol = ANY($1::text[])
GROUP BY c.audience_user_id
ORDER BY count(*) DESC
LIMIT 1
"""


async def load_panel(pool, symbols: list[str]) -> tuple[pd.DataFrame, object]:
    """The adjusted-close panel for these symbols, from one audience.

    Scoped to a single `audience_user_id` rather than pooled across all of them.
    These are `byo_only` Polygon claims, and a panel assembled from two
    audiences would make this deployment the redistributor -- one user's
    licensed data feeding another's decision record, which is exactly what the
    credential class forbids.
    """
    audience = await pool.fetchval(_AUDIENCE, symbols)
    rows = await pool.fetch(_PANEL, symbols, audience)
    frame = pd.DataFrame(rows, columns=["symbol", "date", "close"])
    if frame.empty:
        raise RuntimeError(
            f"no price claims for {', '.join(symbols)} under audience {audience}"
        )
    frame["date"] = (
        pd.to_datetime(frame["date"], utc=True).dt.tz_convert(None).dt.normalize()
    )
    frame = frame.drop_duplicates(["symbol", "date"], keep="last")
    panel = frame.pivot(index="date", columns="symbol", values="close").sort_index()
    return panel, audience


def next_session(last_close: date, *, today: date) -> date:
    """The first session a decision made now may apply to.

    Two bounds, and the later one wins. The next business day after the last
    close is the market's answer; strictly after today is the point-in-time
    rule's answer, and it is what stops a run on a stale panel from stamping a
    decision onto a session that has already happened. Holidays are not modelled
    -- if the chosen day is a market holiday the scorer opens the window at the
    first session on or after it, which is the same book either way.
    """
    candidate = last_close + timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    if candidate <= today:
        candidate = today + timedelta(days=1)
        while candidate.weekday() >= 5:
            candidate += timedelta(days=1)
    return candidate


__all__ = [
    "BENCHMARK",
    "COST_BPS",
    "SECTORS",
    "load_panel",
    "next_session",
]
