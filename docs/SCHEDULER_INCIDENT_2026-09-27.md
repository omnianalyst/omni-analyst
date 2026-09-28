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

Before restarting production: review and merge the missing-key fix; verify a
larger production-like coverage snapshot and normal shutdown under the same
limit; prepare a short monitored canary with a rollback trigger. The live
scheduler should remain stopped until that plan is reviewed.
