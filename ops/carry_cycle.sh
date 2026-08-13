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
cd /home/user/omni-v2
docker compose -f docker-compose.prod.yml exec -T scheduler \
  python - --live < ops/cycle_one.py >> /home/user/omni-v2/ops/carry_cycle.log 2>&1
echo "exit $? at $(date -u +%FT%TZ)" >> /home/user/omni-v2/ops/carry_cycle.log
