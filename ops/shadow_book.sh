#!/usr/bin/env bash
# The forward shadow book, unattended.
#
# Every run that does not happen is a day the record does not have, and unlike
# every other number in this project it cannot be backfilled -- that is the
# whole reason the book exists rather than another backtest. So this logs and
# exits rather than failing into a shell nobody reads.
#
# Runs after the US close and before the next open. The writer refuses any
# `effective_from` that is not strictly in the future, so a run that fires late
# stamps the following session rather than the one that already happened.
#
# A decision rule that refuses is skipped, not substituted. The scorer likewise
# leaves unavailable outcomes pending. Operational failures from either pass
# remain visible in this wrapper's final status.
set -uo pipefail
root="${OMNI_ROOT:-/home/user/omni-v2}"
log="$root/ops/shadow_book.log"
cd "$root" || exit $?

status=0
printf 'shadow_book start at %s\n' "$(date -u +%FT%TZ)" >> "$log"
printf 'shadow_book decision start at %s\n' "$(date -u +%FT%TZ)" >> "$log"
docker compose -f docker-compose.prod.yml exec -T scheduler \
  python - < ops/shadow_book_record.py >> "$log" 2>&1
decision_status=$?
if (( decision_status == 0 )); then
  printf 'shadow_book decision end exit 0 at %s\n' "$(date -u +%FT%TZ)" >> "$log"
else
  printf 'shadow_book decision failure exit %d at %s\n' \
    "$decision_status" "$(date -u +%FT%TZ)" >> "$log"
  status=$decision_status
fi

printf 'shadow_book scoring start at %s\n' "$(date -u +%FT%TZ)" >> "$log"
docker compose -f docker-compose.prod.yml exec -T scheduler \
  python - < ops/shadow_book_score.py >> "$log" 2>&1
scoring_status=$?
if (( scoring_status == 0 )); then
  printf 'shadow_book scoring end exit 0 at %s\n' "$(date -u +%FT%TZ)" >> "$log"
else
  printf 'shadow_book scoring failure exit %d at %s\n' \
    "$scoring_status" "$(date -u +%FT%TZ)" >> "$log"
fi
if (( status == 0 && scoring_status != 0 )); then
  status=$scoring_status
fi

printf 'shadow_book decay start at %s\n' "$(date -u +%FT%TZ)" >> "$log"
docker compose -f docker-compose.prod.yml exec -T scheduler \
  python - < ops/shadow_decay.py >> "$log" 2>&1
decay_status=$?
if (( decay_status == 0 )); then
  printf 'shadow_book decay end exit 0 at %s\n' "$(date -u +%FT%TZ)" >> "$log"
else
  printf 'shadow_book decay failure exit %d at %s\n' \
    "$decay_status" "$(date -u +%FT%TZ)" >> "$log"
fi
if (( status == 0 && decay_status != 0 )); then
  status=$decay_status
fi

if (( status == 0 )); then
  printf 'shadow_book end exit 0 at %s\n' "$(date -u +%FT%TZ)" >> "$log"
else
  printf 'shadow_book failure exit %d at %s\n' "$status" "$(date -u +%FT%TZ)" >> "$log"
fi
exit "$status"
