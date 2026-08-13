#!/usr/bin/env bash
# Daily launch-cohort sweep. Cohorts cannot be backfilled: a day missed is a
# day of base rate lost, so this runs unattended and logs rather than failing
# loudly into a shell nobody reads.
set -uo pipefail
cd /home/user/omni-v2
docker compose -f docker-compose.prod.yml exec -T scheduler \
  python -m omni.research.launches >> /home/user/omni-v2/ops/launch_sweep.log 2>&1
