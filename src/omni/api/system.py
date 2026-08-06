"""System status: loop freshness and fill health from the data itself.

The scheduler and autonomous runner have no heartbeat table -- and they should
not need one. A loop that is alive writes rows; a loop that is dead stops. So
the cheapest, most honest health signal is the timestamp of each loop's last
output, read straight from the tables it writes. A prediction loop that last
wrote an hour ago is either idle or stuck; this endpoint surfaces the age so a
caller can tell.

Read-only. Any authenticated user (there is no operator/admin role yet, so the
auth model's "any signed-in principal" is the strongest gate available). It
reveals loop freshness, demand volume and provider fill rates. The public
``/health`` stays minimal for anonymous uptime checks.
"""

from __future__ import annotations

from datetime import UTC, datetime

from neutron import App, Router
from neutron.error import unauthorized
from starlette.requests import Request

from omni.auth import resolve_audience_from_request


def build_router(app: App) -> Router:
    router = Router()

    @router.get("/system/status")
    async def system_status(request: Request) -> dict:
        if resolve_audience_from_request(request) is None:
            raise unauthorized("Authentication required")
        pool = app.db.pool
        now = datetime.now(UTC)

        # Each loop's last output. NULL means the loop has never run (a fresh
        # deployment), which is a distinct state from stale.
        freshness = await pool.fetch(
            """
            SELECT 'prediction' AS loop_name, MAX(created_at) AS last_at FROM prediction
            UNION ALL SELECT 'finding', MAX(created_at) FROM finding WHERE status = 'surfaced'
            UNION ALL SELECT 'fill', MAX(finished_at) FROM fill_attempt
            UNION ALL SELECT 'demand', MAX(created_at) FROM demand
            UNION ALL SELECT 'claim_ingest', MAX(observed_at) FROM claim
            """
        )
        loops = []
        for row in freshness:
            last_at = row["last_at"]
            age_s = (now - last_at).total_seconds() if last_at is not None else None
            loops.append(
                {
                    "loop": row["loop_name"],
                    "last_activity": last_at.isoformat() if last_at else None,
                    "age_seconds": age_s,
                    "never_run": last_at is None,
                }
            )

        # Demand state: how much outstanding work is there, and how much has the
        # system given up on (unfillable) recently?
        demand = await pool.fetchrow(
            """
            SELECT
              count(*) FILTER (WHERE active) AS active_demand,
              count(*) AS total_demand
            FROM demand
            """
        )

        # Fill outcomes in the last hour: filled vs unfillable vs error. A rising
        # error count is a provider failing (auth, network, schema); a rising
        # unfillable count is demand the catalog cannot satisfy.
        fills = await pool.fetch(
            """
            SELECT outcome, count(*) AS n
            FROM fill_attempt
            WHERE finished_at > now() - interval '1 hour'
            GROUP BY outcome
            """
        )
        fill_recent = {row["outcome"]: row["n"] for row in fills}

        # Production in the last 24h: the throughput that proves the engine is
        # turning, not just idling.
        production = await pool.fetchrow(
            """
            SELECT
              count(*) FILTER (WHERE created_at > now() - interval '24 hours') AS predictions_24h,
              (SELECT count(*) FROM finding WHERE created_at > now() - interval '24 hours') AS findings_24h
            FROM prediction
            """
        )

        return {
            "now": now.isoformat(),
            "loops": loops,
            "demand": {
                "active": demand["active_demand"],
                "total": demand["total_demand"],
            },
            "fill_last_hour": fill_recent,
            "production_24h": {
                "predictions": production["predictions_24h"],
                "findings": production["findings_24h"],
            },
        }

    return router
