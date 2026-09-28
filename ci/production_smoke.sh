#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
neutron_version="$(python3 "$root/ops/neutron_package.py")"
app_revision="${GITHUB_SHA:-$(git -C "$root" rev-parse HEAD)}"

printf 'Neutron PyPI version: %s\n' "$neutron_version"

build_args=(
    --build-arg "OMNI_REVISION=$app_revision"
    --build-arg "NEUTRON_PACKAGE_VERSION=$neutron_version"
    --label "org.opencontainers.image.revision=$app_revision"
    --label "com.omnianalyst.neutron.version=$neutron_version"
)
run_id="${GITHUB_RUN_ID:-$$}"
run_attempt="${GITHUB_RUN_ATTEMPT:-0}"
job_suffix="$(printf '%s' "${GITHUB_JOB:-local}:$$" | shasum | cut -c1-12)"
project="omni-ci-${run_id}-${run_attempt}-${job_suffix}"
# Per-job tags, not daemon-global :latest aliases: a shared runner (or a
# co-running local job) can swap a mutable tag between inspect and startup,
# making this test exercise another run's images.
export OMNI_CI_API_IMAGE="omni-api:$project"
export OMNI_CI_SCHEDULER_IMAGE="omni-scheduler:$project"
export OMNI_CI_POSTGRES_CONTAINER="${project}-postgres"

docker build "${build_args[@]}" --file "$root/Dockerfile" \
    --tag "$OMNI_CI_API_IMAGE" "$root"
docker build "${build_args[@]}" --file "$root/Dockerfile.scheduler" \
    --tag "$OMNI_CI_SCHEDULER_IMAGE" "$root"

for image in "$OMNI_CI_API_IMAGE" "$OMNI_CI_SCHEDULER_IMAGE"; do
    recorded_version="$(docker image inspect --format '{{ index .Config.Labels "com.omnianalyst.neutron.version" }}' "$image")"
    [[ "$recorded_version" == "$neutron_version" ]]
done

export API_PORT="$((18000 + ($$ % 1000)))"
export POSTGRES_DB=omni_ci_production
export POSTGRES_USER=postgres
export POSTGRES_PASSWORD=synthetic-ci-postgres-password
export OMNI_JWT_SECRET=synthetic-ci-jwt-secret-at-least-thirty-two-characters
export OMNI_CREDENTIAL_KEY=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=
export OMNI_REVISION="$app_revision"
export NEUTRON_PACKAGE_VERSION="$neutron_version"
export DEBUG=false
export FRED_API_KEY=
export POLYGON_API_KEY=
export COINGECKO_API_KEY=
export ETHERSCAN_API_KEY=
export SEC_USER_AGENT=
export LICENSED_REDISTRIBUTION_PROVIDERS=
export HYPERLIQUID_WALLET_ADDRESS=
export HYPERLIQUID_PRIVATE_KEY=
export COMPOSE_DISABLE_ENV_FILE=true

compose=(
    docker compose
    --env-file "$root/ci/fixtures/production-smoke.env"
    --project-name "$project"
    --file "$root/docker-compose.prod.yml"
    --file "$root/ci/compose.production-smoke.yml"
)

cleanup() {
    exit_code=$?
    if [[ $exit_code -ne 0 ]]; then
        "${compose[@]}" logs --no-color || true
    fi
    "${compose[@]}" down --volumes --remove-orphans || true
    exit "$exit_code"
}
trap cleanup EXIT

"${compose[@]}" config --quiet
"${compose[@]}" up --detach --no-build postgres
postgres_ready=false
for _ in {1..60}; do
    if "${compose[@]}" exec -T postgres \
        pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB" >/dev/null 2>&1; then
        postgres_ready=true
        break
    fi
    sleep 2
done
[[ "$postgres_ready" == true ]]

"${compose[@]}" up --detach --no-build api
health=
for _ in {1..90}; do
    if health="$("${compose[@]}" exec -T api python -c 'import json, urllib.request; print(json.load(urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=3))["status"])' 2>/dev/null)"; then
        break
    fi
    sleep 2
done
[[ "$health" == "ok" ]]

"${compose[@]}" up --detach --no-build scheduler
scheduler_ready=false
for _ in {1..30}; do
    scheduler_logs="$("${compose[@]}" logs --no-color scheduler)"
    if [[ "$scheduler_logs" == *"scheduler up:"* ]]; then
        scheduler_ready=true
        break
    fi
    sleep 2
done
[[ "$scheduler_ready" == true ]]

expected_migration=0
for migration in "$root"/migrations/[0-9][0-9][0-9]_*.sql; do
    filename="${migration##*/}"
    version=$((10#${filename%%_*}))
    if ((version > expected_migration)); then
        expected_migration=$version
    fi
done
actual_migration="$("${compose[@]}" exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atqc 'SELECT max(version) FROM _neutron_migrations')"
[[ "$actual_migration" == "$expected_migration" ]]

printf 'Production API and scheduler healthy at migration %s\n' "$actual_migration"
