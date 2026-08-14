"""Score completed forward shadow-book decisions from recorded price marks."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from omni.config import settings
from omni.db import connect
from omni.research.allocation import RULES
from omni.research.shadow_book import (
    ShadowBookRefused,
    decisions_for,
    record_outcome,
    score_decision,
    unscored_decisions,
)
from omni.research.shadow_run import BENCHMARK, SECTORS, load_panel
from omni.scheduler.health import EXPECTED_OPERATION_INTERVALS, record_loop_health


@dataclass(frozen=True)
class PassResult:
    scored: int
    pending: int


async def score_book(pool, book: str, prices, *, through: date) -> PassResult:
    decisions = await decisions_for(pool, book)
    positions = {decision.id: index for index, decision in enumerate(decisions)}
    pending = 0
    scored = 0

    for decision in await unscored_decisions(pool, book, through=through):
        index = positions[decision.id]
        if index + 1 >= len(decisions):
            pending += 1
            print(f"{book:<30} PENDING  no subsequent decision closes the period")
            continue

        following = decisions[index + 1]
        if following.effective_from > through:
            pending += 1
            print(
                f"{book:<30} PENDING  next period starts {following.effective_from}; "
                f"prices end {through}"
            )
            continue

        previous_weights = decisions[index - 1].weights if index > 0 else None
        try:
            score = score_decision(
                decision,
                prices,
                previous_weights=previous_weights,
                period_end=following.effective_from,
            )
        except ShadowBookRefused as exc:
            pending += 1
            print(f"{book:<30} PENDING  {exc}")
            continue

        await record_outcome(
            pool,
            decision_id=decision.id,
            period_start=score.period_start,
            period_end=score.period_end,
            sessions=score.sessions,
            realised_return=Decimal(str(score.realised_return)),
            benchmark_return=Decimal(str(score.benchmark_return)),
            cost_charged=Decimal(str(score.cost_charged)),
            turnover=Decimal(str(score.turnover)),
            limits=score.limits,
        )
        scored += 1
        print(
            f"{book:<30} SCORED   {score.period_start} -> {score.period_end}, "
            f"{score.sessions} sessions"
        )

    return PassResult(scored=scored, pending=pending)


async def main() -> int:
    client = await connect(settings.database_url)
    try:
        panel, audience = await load_panel(client.pool, [*SECTORS, BENCHMARK])
        through = panel.index[-1].date()
        print(f"audience       {audience}")
        print(f"prices through {through}")
        print()

        scored = 0
        pending = 0
        for book in RULES:
            result = await score_book(client.pool, book, panel, through=through)
            scored += result.scored
            pending += result.pending

        print()
        print(f"scored {scored}, pending {pending}, across {len(RULES)} books")
        await record_loop_health(
            client.pool,
            loop_name="shadow_scoring",
            ok=True,
            result=f"scored {scored}, pending {pending}, across {len(RULES)} books",
            expected_interval_seconds=EXPECTED_OPERATION_INTERVALS["shadow_scoring"],
        )
        return 0
    except BaseException as exc:
        try:
            await record_loop_health(
                client.pool,
                loop_name="shadow_scoring",
                ok=False,
                error=f"{type(exc).__name__}: {exc}",
                expected_interval_seconds=EXPECTED_OPERATION_INTERVALS[
                    "shadow_scoring"
                ],
            )
        except Exception:  # noqa: BLE001,S110
            pass
        raise
    finally:
        await client.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
