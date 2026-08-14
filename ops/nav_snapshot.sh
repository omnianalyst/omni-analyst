#!/usr/bin/env bash
# Daily NAV mark. A series cannot be backfilled, so this runs unattended and
# logs rather than failing into a shell nobody reads.
set -uo pipefail
root="${OMNI_ROOT:-/home/user/omni-v2}"
log="$root/ops/nav_snapshot.log"
cd "$root" || exit $?

printf 'nav_snapshot start at %s\n' "$(date -u +%FT%TZ)" >> "$log"
docker compose -f docker-compose.prod.yml exec -T scheduler \
  python - < ops/nav_snapshot.py >> "$log" 2>&1
status=$?
if (( status == 0 )); then
  printf 'nav_snapshot end exit 0 at %s\n' "$(date -u +%FT%TZ)" >> "$log"
else
  printf 'nav_snapshot failure exit %d at %s\n' "$status" "$(date -u +%FT%TZ)" >> "$log"
fi
exit "$status"
