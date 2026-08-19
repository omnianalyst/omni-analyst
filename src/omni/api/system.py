"""System status: loop freshness and fill health from the data itself.

The scheduler and autonomous runner have no heartbeat table -- and they should
not need one. A loop that is alive writes rows; a loop that is dead stops. So
the cheapest, most honest health signal is the timestamp of each loop's last
output, read straight from the tables it writes. A prediction loop that last
wrote an hour ago is either idle or stuck; this endpoint surfaces the age so a
caller can tell.

The ``loops`` array is that effect-derived view. The ``health`` object is the
process view, read from ``loop_health`` (written by the scheduler each
iteration): it carries the two signals the effect view cannot give -- a
chronically-failing loop's ``consecutive_failures`` and ``last_error`` (an
iterating-but-failing loop stops writing output, so its age looks like idle),
and an alive-but-idle loop's fresh ``last_success_at`` (sweep finding no gaps
writes nothing, yet is healthy). ``health.overall`` is the worst per-loop
verdict so a glance is enough: ok / stale / failing.

Read-only and operator-only. It reveals loop freshness, demand volume and
provider fill rates. The public ``/health`` stays minimal for anonymous uptime
checks.
"""

from __future__ import annotations

from datetime import UTC, datetime

from neutron import App, Router
from neutron.error import forbidden, unauthorized
from starlette.requests import Request

from omni.auth import resolve_audience_from_request, resolve_role_from_request
from omni.scheduler.health import EXPECTED_OPERATION_INTERVALS

# A loop is stale when its last success is older than this many times its own
# scheduled interval -- a loop missing several consecutive beats is stuck, not
# idle. The multiplier is generous on purpose: a healthy loop that takes an
# occasional slow cycle should not read as stale.
_STALE_INTERVAL_MULTIPLIER = 5.0

# Severity ordering for the overall verdict. Higher == worse.
_SEVERITY = {"ok": 0, "stale": 1, "failing": 2}



def build_router(app: App) -> Router:
    router = Router()

    @router.get("/system/status")
    async def system_status(request: Request) -> dict:
        if resolve_audience_from_request(request) is None:
            raise unauthorized("Authentication required")
        if resolve_role_from_request(request) != "operator":
            raise forbidden("Operator access required")
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

        # Claim-store throughput: the volume of data under management and the
        # rate it is arriving. The board answers "what is being processed";
        # a store that stops growing while demand stays active is a provider
        # outage wearing a healthy face.
        claims = await pool.fetchrow(
            """
            SELECT
              count(*) AS total,
              count(*) FILTER (WHERE observed_at > now() - interval '24 hours') AS recent
            FROM claim
            """
        )

        # Process health per loop, from the state rows the scheduler writes each
        # iteration. Complements the effect-derived `loops` above: this is where
        # a chronically-failing or never-yet-succeeded loop becomes visible, and
        # where an alive-but-idle loop reads healthy rather than ambiguously
        # aged. No rows (a fresh deployment) is honest emptiness, not "ok" -- the
        # effect-derived loops already report never_run for that case.
        health_rows = await pool.fetch(
            """
            SELECT loop_name, last_success_at, last_failure_at,
                   consecutive_failures, last_error, expected_interval_seconds,
                   last_status, last_result
            FROM loop_health
            """
        )
        health_loops = []
        worst_severity = -1
        worst_state: str | None = None
        recorded = {r["loop_name"]: r for r in health_rows}
        operation_names = [
            *EXPECTED_OPERATION_INTERVALS,
            *sorted(set(recorded) - set(EXPECTED_OPERATION_INTERVALS)),
        ]
        for operation_name in operation_names:
            r = recorded.get(operation_name)
            default_interval = EXPECTED_OPERATION_INTERVALS.get(operation_name)
            if r is None:
                health_loops.append(
                    {
                        "loop": operation_name,
                        "state": "never_run",
                        "last_status": None,
                        "last_success_at": None,
                        "last_failure_at": None,
                        "consecutive_failures": 0,
                        "last_error": None,
                        "last_result": None,
                        "expected_interval_seconds": default_interval,
                    }
                )
                continue
            consec = int(r["consecutive_failures"])
            last_success = r["last_success_at"]
            interval = r["expected_interval_seconds"] or default_interval
            # A loop with open failures is failing even if it succeeded recently
            # -- the current iteration raised. last_success NULL means it has
            # only ever failed, which is failing too.
            if consec > 0 or r["last_status"] == "failure":
                state = "failing"
            elif (
                r["last_status"] is None
                and last_success is None
                and r["last_failure_at"] is None
            ):
                state = "never_run"
            elif interval is not None and interval > 0 and (
                now - last_success
            ).total_seconds() > _STALE_INTERVAL_MULTIPLIER * float(interval):
                state = "stale"
            else:
                state = "ok"
            health_loops.append(
                {
                    "loop": operation_name,
                    "state": state,
                    "last_status": r["last_status"],
                    "last_success_at": last_success.isoformat()
                    if last_success
                    else None,
                    "last_failure_at": r["last_failure_at"].isoformat()
                    if r["last_failure_at"]
                    else None,
                    "consecutive_failures": consec,
                    "last_error": r["last_error"],
                    "last_result": r["last_result"],
                    "expected_interval_seconds": interval,
                }
            )
            if state != "never_run":
                sev = _SEVERITY[state]
                if sev > worst_severity:
                    worst_severity = sev
                    worst_state = state

        return {
            "now": now.isoformat(),
            "loops": loops,
            "health": {
                "overall": worst_state,
                "loops": health_loops,
            },
            "demand": {
                "active": demand["active_demand"],
                "total": demand["total_demand"],
            },
            "claims": {
                "total": claims["total"],
                "last_24h": claims["recent"],
            },
            "fill_last_hour": fill_recent,
            "production_24h": {
                "predictions": production["predictions_24h"],
                "findings": production["findings_24h"],
            },
        }

    return router
