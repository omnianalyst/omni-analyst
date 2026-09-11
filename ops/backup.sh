#!/usr/bin/env bash
set -euo pipefail
umask 077

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
SCRIPT_PATH="$SCRIPT_DIR/$(basename -- "${BASH_SOURCE[0]}")"
CONFIG_FILE="${OMNI_BACKUP_CONFIG:-/etc/omni-backup.env}"

if [[ -f "$CONFIG_FILE" ]]; then
  set -a
  # shellcheck source=/dev/null
  source "$CONFIG_FILE"
  set +a
elif [[ -n "${OMNI_BACKUP_CONFIG:-}" ]]; then
  echo "backup FAILED: config file is not readable: $CONFIG_FILE" >&2
  exit 1
fi

PG_CONTAINER="${OMNI_PG_CONTAINER:-omni_postgres}"
PGUSER="${PGUSER:-postgres}"
PGDATABASE="${PGDATABASE:-omni_v2}"
BACKUP_DIR="${OMNI_BACKUP_DIR:-/opt/omni-backups}"
RETENTION_DAYS="${OMNI_BACKUP_RETENTION:-14}"

error() {
  echo "backup FAILED: $*" >&2
}

fail() {
  error "$*"
  return 1
}

validate_database_name() {
  [[ "$1" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || fail "invalid database name: $1"
}

validate_rsync_target() {
  local target=$1
  local remote

  [[ -n "$target" ]] || fail "OMNI_RSYNC_TARGET is required"
  case "$target" in
    rsync://*/*)
      remote=${target#rsync://}
      remote=${remote%%/*}
      remote=${remote##*@}
      remote=${remote%%:*}
      ;;
    *::* )
      remote=${target%%::*}
      remote=${remote##*@}
      ;;
    *:*)
      remote=${target%%:*}
      remote=${remote##*@}
      ;;
    *)
      fail "OMNI_RSYNC_TARGET must use remote rsync syntax"
      return
      ;;
  esac

  case "$remote" in
    ""|localhost|127.*|::1|\[::1\])
      fail "OMNI_RSYNC_TARGET must name an off-box host"
      ;;
  esac
}

validate_archive() {
  local archive=$1
  local container_archive
  local status=0
  local cleanup_status=0

  container_archive="/tmp/omni-backup-validate-$$-$(basename -- "$archive")"

  [[ -s "$archive" ]] || {
    fail "archive is empty: $archive"
    return
  }
  [[ "$(dd if="$archive" bs=5 count=1 2>/dev/null)" == "PGDMP" ]] || {
    fail "archive is not PostgreSQL custom format: $archive"
    return
  }

  if ! docker cp "$archive" "$PG_CONTAINER:$container_archive"; then
    fail "could not copy archive into $PG_CONTAINER for validation"
    return
  fi

  docker exec "$PG_CONTAINER" pg_restore --list "$container_archive" >/dev/null || status=$?
  docker exec "$PG_CONTAINER" rm -f "$container_archive" >/dev/null || cleanup_status=$?

  if [[ "$status" -ne 0 ]]; then
    error "pg_restore could not read the custom archive catalog: $archive"
    return "$status"
  fi
  if [[ "$cleanup_status" -ne 0 ]]; then
    error "could not remove validation copy from $PG_CONTAINER"
    return "$cleanup_status"
  fi
}

backup_credential_key() {
  local output=$1
  local temporary="${output}.partial"
  : "${OMNI_BACKUP_API_CONTAINER:?set the API container for this database}"
  : "${OMNI_BACKUP_AGE_RECIPIENT:?set an offline recovery public recipient}"
  command -v age >/dev/null || { fail 'age is required'; return 1; }
  if docker exec -i "$OMNI_BACKUP_API_CONTAINER" python - <<'PYKEY' \
      | age --encrypt -r "$OMNI_BACKUP_AGE_RECIPIENT" > "$temporary"
import os
import sys
from cryptography.fernet import Fernet
from omni.credentials.keyring import KEY_ENV, key_path
raw = os.environ.get(KEY_ENV, '').strip().encode()
if not raw:
    raw = key_path().read_bytes().strip()  # never generate a replacement here
Fernet(raw)  # validate, without printing the secret
sys.stdout.buffer.write(raw)
PYKEY
  then
    chmod 600 "$temporary"
    mv -- "$temporary" "$output"
  else
    rm -f -- "$temporary"
    fail 'could not export the existing credential key for recovery'
    return 1
  fi
}

run_backup() (
  set -euo pipefail
  umask 077
  local target=${OMNI_RSYNC_TARGET:-}
  validate_rsync_target "$target"
  validate_database_name "$PGDATABASE"
  [[ "$RETENTION_DAYS" =~ ^[0-9]+$ ]] || { fail 'invalid retention'; exit 1; }
  install -d -m 0700 -- "$BACKUP_DIR"
  exec 9>"$BACKUP_DIR/.backup.lock"
  flock -n 9 || { fail 'another backup is running'; exit 1; }
  local stamp temporary dest key_archive status
  stamp=$(date -u +%Y%m%dT%H%M%SZ)
  temporary=$(mktemp "$BACKUP_DIR/${PGDATABASE}-${stamp}-XXXXXX.partial")
  dest="${temporary%.partial}.dump"
  key_archive="${temporary%.partial}.key.age"
  trap 'rm -f -- "$temporary" "${key_archive}.partial"' EXIT
  if ! docker exec "$PG_CONTAINER" pg_dump -Fc -U "$PGUSER" "$PGDATABASE" > "$temporary"; then
    status=$?
    error "pg_dump failed"
    exit $status
  fi
  if validate_archive "$temporary"; then
    :
  else
    status=$?
    exit $status
  fi
  backup_credential_key "$key_archive"
  chmod 600 "$temporary" "$key_archive"
  mv -- "$temporary" "$dest"
  # No --delete: losing local files must never erase independent remote history.
  if rsync -a -- "$dest" "$key_archive" "$target/"; then
    :
  else
    status=$?
    error "off-box replication failed"
    exit $status
  fi
  echo "created and replicated database/key recovery pair: $(basename -- "$dest")"
  # Local retention only, after replication succeeds. Remote retention is
  # a separate policy, tested by its own restore drill.
  find "$BACKUP_DIR" -maxdepth 1 -type f \
    \( -name "${PGDATABASE}-*.dump" -o -name "${PGDATABASE}-*.key.age" \) \
    -mtime +"$RETENTION_DAYS" -delete
  find "$BACKUP_DIR" -maxdepth 1 -type f -name '*.partial' -mtime +1 -delete
)

restore_database() {
  local archive=$1
  local target_database=$2
  local status

  [[ -f "$archive" ]] || fail "archive does not exist: $archive"
  validate_database_name "$target_database"
  [[ "$target_database" != "$PGDATABASE" ]] || fail "refusing to restore over source database $PGDATABASE"
  validate_archive "$archive"

  if docker exec "$PG_CONTAINER" createdb -U "$PGUSER" -T template0 "$target_database"; then
    :
  else
    status=$?
    error "could not create restore database $target_database"
    return "$status"
  fi

  if docker exec -i "$PG_CONTAINER" pg_restore \
    --exit-on-error --no-owner --no-privileges -U "$PGUSER" -d "$target_database" < "$archive"; then
    echo "restored $archive -> $target_database"
  else
    status=$?
    docker exec "$PG_CONTAINER" dropdb --if-exists -U "$PGUSER" "$target_database" >/dev/null || true
    error "restore failed; removed partial database $target_database"
    return "$status"
  fi
}

DRILL_DATABASE=""

cleanup_drill() {
  if [[ -n "$DRILL_DATABASE" ]]; then
    docker exec "$PG_CONTAINER" dropdb --if-exists -U "$PGUSER" "$DRILL_DATABASE" >/dev/null || true
  fi
}

run_drill() {
  local archive=$1
  local migration
  local minimum=${OMNI_RESTORE_MIN_MIGRATION:-}
  local status

  DRILL_DATABASE="omni_restore_verify_$(date -u +%Y%m%dT%H%M%SZ)_$$"
  trap cleanup_drill EXIT
  restore_database "$archive" "$DRILL_DATABASE"

  if migration=$(docker exec "$PG_CONTAINER" psql -X -U "$PGUSER" -d "$DRILL_DATABASE" -Atc \
    "SELECT max(version) FROM _neutron_migrations;"); then
    migration=${migration//$'\r'/}
    migration=${migration//$'\n'/}
  else
    status=$?
    error "restore drill could not read migration state"
    return "$status"
  fi

  [[ "$migration" =~ ^[0-9]+$ ]] || fail "restore drill returned invalid migration state: $migration"
  if [[ -n "$minimum" ]]; then
    [[ "$minimum" =~ ^[0-9]+$ ]] || fail "OMNI_RESTORE_MIN_MIGRATION must be a non-negative integer"
    (( migration >= minimum )) || fail "restored migration $migration is older than required $minimum"
  fi

  docker exec "$PG_CONTAINER" dropdb --if-exists -U "$PGUSER" "$DRILL_DATABASE" >/dev/null
  echo "restore drill passed at migration $migration; dropped $DRILL_DATABASE"
  DRILL_DATABASE=""
  trap - EXIT
}

install_cron() {
  local cron_file=${OMNI_BACKUP_CRON_FILE:-/etc/cron.d/omni-backup}
  local schedule=${OMNI_BACKUP_CRON_SCHEDULE:-0 3 * * *}
  local cron_user=${OMNI_BACKUP_CRON_USER:-root}
  local log_file=${OMNI_BACKUP_LOG:-/var/log/omni-backup.log}
  local fields=()
  local temporary

  [[ -r "$CONFIG_FILE" ]] || fail "install requires a readable OMNI_BACKUP_CONFIG"
  validate_rsync_target "${OMNI_RSYNC_TARGET:-}"
  read -r -a fields <<< "$schedule"
  [[ "${#fields[@]}" -eq 5 ]] || fail "OMNI_BACKUP_CRON_SCHEDULE must contain five fields"
  [[ "$cron_user" =~ ^[A-Za-z_][A-Za-z0-9_-]*$ ]] || fail "invalid cron user: $cron_user"

  temporary=$(mktemp)
  {
    printf 'SHELL=/bin/bash\n'
    printf '%s %s OMNI_BACKUP_CONFIG=%q %q backup >> %q 2>&1\n' \
      "$schedule" "$cron_user" "$CONFIG_FILE" "$SCRIPT_PATH" "$log_file"
  } > "$temporary"
  install -m 0644 "$temporary" "$cron_file"
  rm -f "$temporary"
  echo "installed $cron_file invoking $SCRIPT_PATH"
}

usage() {
  cat >&2 <<EOF
usage: $0 [backup]
       $0 validate DUMP
       $0 restore DUMP DISPOSABLE_DATABASE
       $0 drill DUMP
       $0 install
EOF
  return 2
}

main() {
  local command=${1:-backup}

  case "$command" in
    backup)
      [[ "$#" -eq 0 || "$#" -eq 1 ]] || usage
      run_backup
      ;;
    validate)
      [[ "$#" -eq 2 ]] || usage
      validate_archive "$2"
      echo "custom archive catalog is readable: $2"
      ;;
    restore)
      [[ "$#" -eq 3 ]] || usage
      restore_database "$2" "$3"
      ;;
    drill)
      [[ "$#" -eq 2 ]] || usage
      run_drill "$2"
      ;;
    install)
      [[ "$#" -eq 1 ]] || usage
      install_cron
      ;;
    *)
      usage
      ;;
  esac
}

main "$@"
