#!/usr/bin/env bash
# The carry rebalance, unattended.
#
# No schedule logic here on purpose: `run_due_cycle` refuses outside the
# 03-07 UTC window and refuses inside the six-week hold, so firing this daily
# yields exactly one rebalance every six weeks at the quiet hour. Putting the
# cadence in cron as well would be two schedulers disagreeing, and the one in
# code is the one with the funding boundary in front of it.
#
# A refusal is the normal outcome on ~41 of every 42 days.
set -uo pipefail
root="${OMNI_ROOT:-/home/user/omni-v2}"
log="$root/ops/carry_cycle.log"
cd "$root" || exit $?

printf 'carry_cycle start at %s\n' "$(date -u +%FT%TZ)" >> "$log"
flock --nonblock /tmp/omni-carry-cycle.lock \
  docker compose -f docker-compose.prod.yml exec -T scheduler \
  python - --live < ops/cycle_one.py >> "$log" 2>&1
status=$?
if (( status == 0 )); then
  printf 'carry_cycle end exit 0 at %s\n' "$(date -u +%FT%TZ)" >> "$log"
else
  printf 'carry_cycle failure exit %d at %s\n' "$status" "$(date -u +%FT%TZ)" >> "$log"
fi
exit "$status"
