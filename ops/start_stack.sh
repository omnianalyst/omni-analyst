#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

if [[ $# -ne 0 ]]; then
    printf 'Usage: %s\n' "$0" >&2
    exit 2
fi

for tool in git docker uv npm python3; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        printf 'Missing %s. Install it before starting Omni Analyst.\n' "$tool" >&2
        exit 1
    fi
done
if ! docker compose version >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
    printf 'Docker and Docker Compose must be running.\n' >&2
    exit 1
fi
if ! python3 -c 'import tomllib' >/dev/null 2>&1; then
    printf 'Python 3.11 or newer is required to read the package pin.\n' >&2
    exit 1
fi

release_paths=(
    src migrations ui pyproject.toml uv.lock Dockerfile Dockerfile.scheduler
    docker-compose.prod.yml Caddyfile ops/neutron_package.py
    ops/init_secrets.py ops/start_stack.sh
)
if [[ -n "$(git status --porcelain --untracked-files=all -- "${release_paths[@]}")" ]]; then
    printf 'Commit changes to release files before building revision-stamped images.\n' >&2
    exit 1
fi
uv lock --check

export OMNI_REVISION="$(git rev-parse HEAD)"
export NEUTRON_PACKAGE_VERSION="$(python3 ops/neutron_package.py)"

python3 ops/init_secrets.py --production
npm ci --prefix ui
npm --prefix ui run build
docker compose -f docker-compose.prod.yml config --quiet
docker compose -f docker-compose.prod.yml build api scheduler
docker compose -f docker-compose.prod.yml up -d postgres api
printf 'API and Postgres started. Scheduler stays stopped pending the memory investigation.\n'
