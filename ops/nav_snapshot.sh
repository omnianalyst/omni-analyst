#!/usr/bin/env bash
# Daily NAV mark. A series cannot be backfilled, so this runs unattended and
# logs rather than failing into a shell nobody reads.
set -uo pipefail
cd /home/user/omni-v2
docker compose -f docker-compose.prod.yml exec -T scheduler \
  python - < ops/nav_snapshot.py >> /home/user/omni-v2/ops/nav_snapshot.log 2>&1
