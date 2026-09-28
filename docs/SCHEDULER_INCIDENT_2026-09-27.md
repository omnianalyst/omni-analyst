# Scheduler memory incident, 2026-09-27

The production scheduler was temporarily stopped at 19:41:27 UTC after 4,589
restarts and sustained pressure on the shared infra-home host. Its limit was
1 GiB RAM and 1.25 GiB RAM plus swap. `docker stop` waited 30 seconds, then
forced termination (exit 137, `OOMKilled=false` for that final stop). Swap
activity dropped to zero in repeated samples afterward; recent I/O pressure
fell. This contains the incident but does not establish every cause.

On 2026-09-28 the scheduler container was absent from `docker ps -a`. The API,
Postgres, Ship, and Observe remained running. Background coverage and venue
reconciliation are paused. Do not restart the scheduler on the shared host to
profile it, change its memory limit blindly, or start live trading actions.

An isolated PostgreSQL 17 test with 500,000 claims for one demanded fact
reproduced a memory spike in `detect_gaps`: the existing `visible_claims`
query fetched the whole history into Python and raised peak process RSS by
565 MB. The merged database-side summary produced the same gap classes with
about 0.15 MB additional peak RSS. A 31-test suite checks large-history
classification, audience isolation, conflict details, and scheduler startup
and graceful shutdown with an empty provider registry. This demonstrates one
problematic read path; concurrent startup work and other loops may also add
memory pressure.

On 2026-09-28 a full scheduler replica ran on compute-1 with an internal-only
Docker network, a separate PostgreSQL database, the normal 505-company market
universe, 500,000 synthetic claims, and the existing 1 GiB RAM / 1.25 GiB
RAM-plus-swap limit. It started 143 capabilities and two fill workers, completed
two sweeps five minutes apart, and stayed around 123 MiB container memory
usage. A 30-second-grace `docker stop` exited cleanly with code 0, no OOM,
and no restart. The replica
had no provider or venue credentials and no outbound route. Its 22 unfillable
attempts were expected; 12 sleeve-history errors exposed a separate missing-key
constructor bug, now fixed in a separate code change. Evidence is saved at
`Teploy/_internal/COMPLETION_2026-09-25/omni-replica-20260928.log` on the
operator's workstation.

## Production release boundary and restart plan

The live `/opt/omni-analyst` checkout is at `72c0ae4` with uncommitted API and
finance changes, while the running API image identifies revision `cfab313`.
The live database records migration 071; current `main` includes migrations
072 and 073. Scheduler startup calls `migrate()` before starting any loops.
Starting a newly built scheduler against this database would therefore apply
schema changes while the older API remains live. This is a separate deployment
blocker, even though the isolated memory test passed. Do not update the live
checkout, migrate the live database, or restart the scheduler as an incidental
part of this investigation.

Before a production restart:

1. Reconcile the live uncommitted finance/API changes with a clean, reviewed
   release commit. Record the exact API and scheduler image revisions and
   check migrations 072/073 against the running API and live finance data.
2. Take and verify a database backup, then exercise the complete migration and
   matching API/scheduler images on an isolated copy with production-like
   coverage volume. Check memory, swap, loop progress, and graceful shutdown
   under the existing 1 GiB RAM / 1.25 GiB RAM-plus-swap limit.
3. Review the release and rollback procedure before touching production. A
   monitored scheduler canary must have outbound venue/trading actions disabled
   until explicitly approved, a short observation window, and a stop trigger
   for sustained memory growth, swap pressure, repeated restarts, or failed
   sweeps. Preserve Ship and Observe throughout.

The live scheduler remains stopped pending that review. The missing-key fix
from the replica is also required in the release.
