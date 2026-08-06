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
set -euo pipefail

PGHOST="${PGHOST:-localhost}"
PGPORT="${PGPORT:-5434}"
PGUSER="${PGUSER:-postgres}"
PGDATABASE="${PGDATABASE:-omni_v2}"
BACKUP_DIR="${OMNI_BACKUP_DIR:-/opt/omni-backups}"
RETENTION_DAYS="${OMNI_BACKUP_RETENTION:-14}"

mkdir -p "$BACKUP_DIR"
stamp=$(date -u +%Y%m%dT%H%M%SZ)
dest="$BACKUP_DIR/${PGDATABASE}-${stamp}.dump"

pg_dump -Fc -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" "$PGDATABASE" -f "$dest"

# Prune local snapshots older than the retention window.
find "$BACKUP_DIR" -name "${PGDATABASE}-*.dump" -mtime +"$RETENTION_DAYS" -delete

echo "backed up $PGDATABASE -> $dest"

# Off-box replication (site resilience). Set OMNI_RSYNC_TARGET=user@host:/path
# to enable. Without it this script only protects against deletion/corruption,
# not the box itself dying.
if [[ -n "${OMNI_RSYNC_TARGET:-}" ]]; then
  rsync -a --delete "$BACKUP_DIR"/ "$OMNI_RSYNC_TARGET"/
  echo "synced $BACKUP_DIR -> $OMNI_RSYNC_TARGET"
else
  echo "OMNI_RSYNC_TARGET unset; local-only backup. Set it to a second node for site resilience." >&2
fi
