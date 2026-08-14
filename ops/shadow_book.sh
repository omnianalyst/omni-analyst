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
# A rule that refuses is skipped, not substituted. Exit 1 means at least one
# rule produced nothing; the book is still correct, it is just thinner.
set -uo pipefail
cd /home/user/omni-v2
docker compose -f docker-compose.prod.yml exec -T scheduler \
  python - < ops/shadow_book_record.py >> /home/user/omni-v2/ops/shadow_book.log 2>&1
echo "exit $? at $(date -u +%FT%TZ)" >> /home/user/omni-v2/ops/shadow_book.log
