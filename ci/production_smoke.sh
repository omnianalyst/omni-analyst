#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
neutron_python="${1:-$root/../../Neutron/python}"
neutron_repo="$(git -C "$neutron_python" rev-parse --show-toplevel)"
neutron_revision="$(git -C "$neutron_repo" rev-parse HEAD)"
app_revision="${GITHUB_SHA:-$(git -C "$root" rev-parse HEAD)}"
wheel_dir="$root/vendor/ci-wheel-$neutron_revision"

mkdir -p "$wheel_dir"
uv run python "$root/ops/build_neutron_wheel.py" build \
    --project "$neutron_python" --out-dir "$wheel_dir"
wheel_candidates=("$wheel_dir"/neutron_py-*.whl)
if [[ ${#wheel_candidates[@]} -ne 1 || ! -f "${wheel_candidates[0]}" ]]; then
    printf 'expected one Neutron wheel in %s\n' "$wheel_dir" >&2
    exit 1
fi
wheel="${wheel_candidates[0]}"
wheel_relative="${wheel#"$root/"}"
wheel_digest="$(python3 -c 'import hashlib, pathlib, sys; print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())' "$wheel")"

printf 'Neutron source: %s@%s\n' "$neutron_repo" "$neutron_revision"
printf 'Neutron wheel: %s sha256:%s\n' "$wheel_relative" "$wheel_digest"

build_args=(
    --build-arg "NEUTRON_WHEEL=$wheel_relative"
    --build-arg "OMNI_REVISION=$app_revision"
    --build-arg "NEUTRON_REVISION=$neutron_revision"
    --label "org.opencontainers.image.revision=$app_revision"
    --label "com.omnianalyst.neutron.revision=$neutron_revision"
    --label "com.omnianalyst.neutron.wheel.sha256=$wheel_digest"
)
docker build "${build_args[@]}" --file "$root/Dockerfile" --tag omni-api:latest "$root"
docker build "${build_args[@]}" --file "$root/Dockerfile.scheduler" --tag omni-scheduler:latest "$root"

for image in omni-api:latest omni-scheduler:latest; do
    recorded_revision="$(docker image inspect --format '{{ index .Config.Labels "com.omnianalyst.neutron.revision" }}' "$image")"
    recorded_digest="$(docker image inspect --format '{{ index .Config.Labels "com.omnianalyst.neutron.wheel.sha256" }}' "$image")"
    [[ "$recorded_revision" == "$neutron_revision" ]]
    [[ "$recorded_digest" == "$wheel_digest" ]]
done

run_id="${GITHUB_RUN_ID:-$$}"
run_attempt="${GITHUB_RUN_ATTEMPT:-0}"
project="omni-ci-${run_id}-${run_attempt}"
export OMNI_CI_POSTGRES_CONTAINER="${project}-postgres"
export API_PORT="$((18000 + ($$ % 1000)))"
export POSTGRES_DB=omni_ci_production
export POSTGRES_USER=postgres
export POSTGRES_PASSWORD=synthetic-ci-postgres-password
export OMNI_JWT_SECRET=synthetic-ci-jwt-secret-at-least-thirty-two-characters
export OMNI_CREDENTIAL_KEY=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=
export OMNI_REVISION="$app_revision"
export NEUTRON_REVISION="$neutron_revision"
export NEUTRON_WHEEL="$wheel_relative"
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
