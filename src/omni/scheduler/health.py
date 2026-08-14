from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger("omni.scheduler.worker")

_DEGRADED_THRESHOLD = 3
_MAX_RESULT_LENGTH = 2000

EXPECTED_OPERATION_INTERVALS: dict[str, float] = {
    "sweep": 300.0,
    "fill": 30.0,
    "resolve": 60.0,
    "predict": 300.0,
    "surface": 300.0,
    "alerts": 60.0,
    "autonomous.macro": 86_400.0,
    "autonomous.sector": 43_200.0,
    "autonomous.demand": 3_600.0,
    "autonomous.synthesis": 300.0,
    "autonomous.meta": 86_400.0,
    "venue_reconciliation": 360.0,
    "carry": 86_400.0,
    "nav": 86_400.0,
    "shadow_decision": 86_400.0,
    "shadow_scoring": 86_400.0,
    "launch_sweep": 21_600.0,
}


def _bounded(value: object | None) -> str | None:
    if value is None:
        return None
    return str(value)[:_MAX_RESULT_LENGTH]


async def record_loop_health(
    pool,
    *,
    loop_name: str,
    ok: bool,
    error: str | None = None,
    result: object | None = None,
    expected_interval_seconds: float | None = None,
) -> int:
    if ok:
        row = await pool.fetchrow(
            """
            INSERT INTO loop_health
                (loop_name, last_success_at, last_failure_at,
                 consecutive_failures, last_error, expected_interval_seconds,
                 last_status, last_result)
            VALUES ($1, now(), NULL, 0, NULL, $2, 'success', $3)
            ON CONFLICT (loop_name) DO UPDATE SET
                last_success_at           = now(),
                consecutive_failures      = 0,
                expected_interval_seconds = EXCLUDED.expected_interval_seconds,
                last_status               = 'success',
                last_result               = EXCLUDED.last_result,
                updated_at                = now()
            RETURNING consecutive_failures
            """,
            loop_name,
            expected_interval_seconds,
            _bounded(result),
        )
        return int(row["consecutive_failures"])

    row = await pool.fetchrow(
        """
        INSERT INTO loop_health
            (loop_name, last_success_at, last_failure_at,
             consecutive_failures, last_error, expected_interval_seconds,
             last_status, last_result)
        VALUES ($1, NULL, now(), 1, $2, $3, 'failure', $4)
        ON CONFLICT (loop_name) DO UPDATE SET
            last_failure_at           = now(),
            consecutive_failures      = loop_health.consecutive_failures + 1,
            last_error                = EXCLUDED.last_error,
            expected_interval_seconds = EXCLUDED.expected_interval_seconds,
            last_status               = 'failure',
            last_result               = EXCLUDED.last_result,
            updated_at                = now()
        RETURNING consecutive_failures
        """,
        loop_name,
        _bounded(error),
        expected_interval_seconds,
        _bounded(result),
    )
    consecutive = int(row["consecutive_failures"])
    if consecutive >= _DEGRADED_THRESHOLD:
        logger.warning(
            "loop '%s' degraded: %d consecutive failures (last: %s)",
            loop_name,
            consecutive,
            error,
        )
    return consecutive


async def run_with_health(pool, *, loop_name: str, interval: float, fn):
    try:
        result = await fn()
    except asyncio.CancelledError:
        raise
    except BaseException as exc:
        try:
            await record_loop_health(
                pool,
                loop_name=loop_name,
                ok=False,
                error=f"{type(exc).__name__}: {exc}",
                expected_interval_seconds=interval,
            )
        except Exception:
            logger.exception("could not record failure health for %s", loop_name)
        raise
    await record_loop_health(
        pool,
        loop_name=loop_name,
        ok=True,
        result=result,
        expected_interval_seconds=interval,
    )
    return result
