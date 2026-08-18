"""Judge the shadow book's forward record: does each edge still hold?

Runs after the scoring pass, so the night's outcomes exist before the night's
judgement of them. Every book gets a state row for the night, including the
ones with nothing to say: a book recorded as `insufficient` tonight is the
monitor confirming it looked, not an error.

Insufficient history is the expected state for months after a book starts --
the forward record cannot be backfilled, which is the entire reason the
shadow book exists rather than another backtest. A decayed state on a
promoted book is the alert this pass exists to raise; it is computed, not
interpreted, and it lands on System next to the research record that
justified the promotion in the first place.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from omni.config import settings
from omni.db import connect
from omni.research.allocation import RULES
from omni.research.decay import (
    evaluate_edge,
    latest_edge_states,
    outcomes_for,
    record_edge_state,
)
from omni.scheduler.health import EXPECTED_OPERATION_INTERVALS, record_loop_health


async def main() -> int:
    now = datetime.now(UTC)
    client = await connect(settings.database_url)
    try:
        written = 0
        present = 0
        for book in RULES:
            outcomes = await outcomes_for(client.pool, book)
            evaluation = evaluate_edge(book, now.date(), outcomes)
            if await record_edge_state(client.pool, evaluation):
                written += 1
            else:
                present += 1
            detail = (
                f"mean session excess {evaluation.mean_session_excess}, "
                f"p {evaluation.decay_p}, window "
                f"{evaluation.window_start} -> {evaluation.window_end}"
                if evaluation.state != "insufficient"
                else evaluation.reason
            )
            promoted = "promoted" if evaluation.promoted else "control"
            print(
                f"{book:<28} {evaluation.state:<12} {promoted:<8} {detail}"
            )

        alerts = [
            row for row in await latest_edge_states(client.pool)
            if row.promoted and row.state == "decayed"
        ]
        print()
        print(
            f"evaluated {len(RULES)} books, {written} written, {present} already "
            f"present, {len(alerts)} promoted edge(s) decayed"
        )
        await record_loop_health(
            client.pool,
            loop_name="shadow_decay",
            ok=True,
            result=(
                f"evaluated {len(RULES)} books, {len(alerts)} promoted "
                f"edge(s) decayed"
            ),
            expected_interval_seconds=EXPECTED_OPERATION_INTERVALS["shadow_decay"],
        )
        return 0
    except BaseException as exc:
        try:
            await record_loop_health(
                client.pool,
                loop_name="shadow_decay",
                ok=False,
                error=f"{type(exc).__name__}: {exc}",
                expected_interval_seconds=EXPECTED_OPERATION_INTERVALS[
                    "shadow_decay"
                ],
            )
        except Exception:  # noqa: BLE001,S110
            pass
        raise
    finally:
        await client.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
