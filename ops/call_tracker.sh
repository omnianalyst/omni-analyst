#!/usr/bin/env bash
# Track public crypto calls (see ops/call_tracker.py). Runs inside the
# scheduler container (httpx available); the ledger lives in the repo and is
# copied in and out around the run. Append-only semantics are the script's.
set -euo pipefail
cd /home/tyler/omni-v2
docker cp docs/research/calls_ledger.jsonl omni-v2-scheduler-1:/tmp/calls_ledger.jsonl
docker exec -i omni-v2-scheduler-1 python - --ledger /tmp/calls_ledger.jsonl < ops/call_tracker.py
docker cp omni-v2-scheduler-1:/tmp/calls_ledger.jsonl docs/research/calls_ledger.jsonl
