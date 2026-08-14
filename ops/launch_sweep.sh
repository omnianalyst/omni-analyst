#!/usr/bin/env bash
# Daily launch-cohort sweep. Cohorts cannot be backfilled: a day missed is a
# day of base rate lost, so this runs unattended and logs rather than failing
# loudly into a shell nobody reads.
set -uo pipefail
root="${OMNI_ROOT:-/home/user/omni-v2}"
log="$root/ops/launch_sweep.log"
cd "$root" || exit $?

printf 'launch_sweep start at %s\n' "$(date -u +%FT%TZ)" >> "$log"
docker compose -f docker-compose.prod.yml exec -T scheduler \
  python -m omni.research.launches >> "$log" 2>&1
status=$?
if (( status == 0 )); then
  printf 'launch_sweep end exit 0 at %s\n' "$(date -u +%FT%TZ)" >> "$log"
else
  printf 'launch_sweep failure exit %d at %s\n' "$status" "$(date -u +%FT%TZ)" >> "$log"
fi
exit "$status"
