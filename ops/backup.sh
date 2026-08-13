#!/usr/bin/env bash
# Physical backup of the omni_v2 Postgres database.
#
# The DB is the source of truth for irreplaceable provenance -- bitemporal
# claims, the prediction ledger (barriers fixed at write time), calibration
# buckets, and the fill_attempt audit trail. None of it can be regenerated
# from upstream providers, because knowledge_date semantics depend on when
# THIS system observed the data, not on what a provider serves today. A single
# volume loss destroys all of it, and the conviction gate then refuses to
# surface anything until months of calibration re-accumulate from zero.
#
# Custom format (-Fc) is parallel-restoreable and compresses. Keeps a local
# rolling window; ship /opt/omni-backups off-box for site-level resilience --
# the local copy does not survive the box dying. Restore with:
#   pg_restore --clean --if-exists -d omni_v2 <file>.dump
# (into a stopped stack, then bring it back up).
#
# THIS IS THE SCRIPT THAT RUNS. Until 2026-08-12 an unversioned
# /opt/omni-backup.sh was the cron target and this file was documentation
# nobody executed. They had diverged: that one wrote plain SQL through
# `docker exec` and never shipped off-box, this one wrote custom format over a
# host port the production stack does not publish. Install this one:
#
#   sudo install -m 755 ops/backup.sh /opt/omni-backup.sh
#
# Reaching Postgres through `docker exec` rather than a TCP port is the
# deliberate half taken from the old script: the production compose file does
# not publish 5432, so a host-port connection works on the dev stack and fails
# silently in production -- exactly where the backup matters.
set -euo pipefail

PG_CONTAINER="${OMNI_PG_CONTAINER:-omni_postgres}"
PGUSER="${PGUSER:-postgres}"
PGDATABASE="${PGDATABASE:-omni_v2}"
BACKUP_DIR="${OMNI_BACKUP_DIR:-/opt/omni-backups}"
RETENTION_DAYS="${OMNI_BACKUP_RETENTION:-14}"

mkdir -p "$BACKUP_DIR"
stamp=$(date -u +%Y%m%dT%H%M%SZ)
dest="$BACKUP_DIR/${PGDATABASE}-${stamp}.dump"

# Write to a partial name and rename only on success. A truncated dump under
# the final name is worse than no dump: it looks like a backup, satisfies the
# retention count, and fails at the moment it is needed.
pg_dump_ok=0
docker exec "$PG_CONTAINER" pg_dump -Fc -U "$PGUSER" "$PGDATABASE" > "$dest.partial" && pg_dump_ok=1

if [[ "$pg_dump_ok" -ne 1 || ! -s "$dest.partial" ]]; then
  rm -f "$dest.partial"
  echo "backup FAILED: pg_dump produced no usable output" >&2
  exit 1
fi

# Custom-format archives begin with the magic string "PGDMP". Checking it costs
# nothing and catches the case where pg_dump wrote an error page, or the format
# flag was lost, or the redirect captured stderr instead of the archive.
#
# NOT `pg_restore --list`: that needs a SEEKABLE file, so it cannot read a pipe,
# and the host carries no postgres client tools. An earlier version of this
# check used it and failed on a perfectly good 170 MB dump -- a verification
# step that rejects valid backups is worse than none, because it trains the
# operator to ignore it.
#
# Truncation is caught by pg_dump's own exit status above, which is the signal
# that actually detects a short write.
if [[ "$(head -c 5 "$dest.partial")" != "PGDMP" ]]; then
  rm -f "$dest.partial"
  echo "backup FAILED: output is not a custom-format archive (no PGDMP header)" >&2
  exit 1
fi

mv "$dest.partial" "$dest"

# Prune local snapshots older than the retention window. Partials from a failed
# run are swept too; without this they accumulate invisibly.
find "$BACKUP_DIR" -name "${PGDATABASE}-*.dump" -mtime +"$RETENTION_DAYS" -delete
find "$BACKUP_DIR" -name "*.partial" -mtime +1 -delete

echo "backed up $PGDATABASE -> $dest ($(du -h "$dest" | cut -f1))"

# Off-box replication (site resilience). Set OMNI_RSYNC_TARGET=user@host:/path
# to enable. Without it this script only protects against deletion/corruption,
# not the box itself dying.
if [[ -n "${OMNI_RSYNC_TARGET:-}" ]]; then
  rsync -a --delete "$BACKUP_DIR"/ "$OMNI_RSYNC_TARGET"/
  echo "synced $BACKUP_DIR -> $OMNI_RSYNC_TARGET"
else
  echo "OMNI_RSYNC_TARGET unset; local-only backup. Set it to a second node for site resilience." >&2
fi
