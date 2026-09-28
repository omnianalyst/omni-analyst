#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

for tool in git docker uv npm python3; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        printf 'Missing %s. Install it before starting Omni Analyst.\n' "$tool" >&2
        exit 1
    fi
done

if ! docker compose version >/dev/null 2>&1; then
    printf 'Docker Compose is not installed. Install the Compose plugin and try again.\n' >&2
    exit 1
fi

if ! docker info >/dev/null 2>&1; then
    printf 'Docker is not running. Start Docker and try again.\n' >&2
    exit 1
fi

python3 "$root/ops/init_secrets.py" --env-file "$root/.env"

cd "$root"
docker compose up -d postgres
for _ in {1..30}; do
    if docker compose exec -T postgres pg_isready -U postgres -d omni_v2 >/dev/null 2>&1; then
        break
    fi
    sleep 1
done
if ! docker compose exec -T postgres pg_isready -U postgres -d omni_v2 >/dev/null 2>&1; then
    printf 'Postgres did not become ready. Check: docker compose logs postgres\n' >&2
    exit 1
fi

uv sync --locked --extra dev
if [[ ! -d ui/node_modules ]]; then
    npm ci --prefix ui
fi

printf '\nStarting Omni Analyst. Open http://localhost:5173 for first-run setup.\n'
uv run --locked python -m uvicorn omni.main:app --host 127.0.0.1 --port 8000 &
api_pid=$!
scheduler_pid=
ui_pid=

cleanup() {
    trap - EXIT INT TERM
    for pid in "$api_pid" "$scheduler_pid" "$ui_pid"; do
        if [[ -n "$pid" ]]; then
            kill "$pid" 2>/dev/null || true
            wait "$pid" 2>/dev/null || true
        fi
    done
}
trap cleanup EXIT INT TERM

api_ready=false
for _ in {1..180}; do
    if python3 -c 'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=2).read()' >/dev/null 2>&1; then
        api_ready=true
        break
    fi
    if ! kill -0 "$api_pid" 2>/dev/null; then
        break
    fi
    sleep 1
done
if [[ "$api_ready" != true ]]; then
    printf 'API did not become healthy. Check the output above.\n' >&2
    exit 1
fi

uv run --locked python -m omni.scheduler &
scheduler_pid=$!
npm --prefix ui run dev -- --host 127.0.0.1 --port 5173 &
ui_pid=$!

while kill -0 "$api_pid" 2>/dev/null && kill -0 "$scheduler_pid" 2>/dev/null && kill -0 "$ui_pid" 2>/dev/null; do
    sleep 1
done
printf 'An Omni Analyst process stopped. Check the output above.\n' >&2
exit 1
