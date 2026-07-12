#!/bin/bash
# Nightly backstop: import every run currently in the OneDrive inbox into the
# library (each is archived on success). Meant for cron; safe to re-run (already
# imported runs are gone from the inbox). Args:
#   sync_onedrive.sh <remote:> <inbox> <archive> <library_dir>
set -uo pipefail
REMOTE="${1:-rxk104_mhc:}"; INBOX="${2:-For_WGS3_Upload}"
ARCHIVE="${3:-Uploaded_Archive}"; LIB="${4:-/srv/kapurlab/databases/mhc/runs}"
HERE="$(cd "$(dirname "$0")" && pwd)"

for RUN in $(rclone lsf --dirs-only "${REMOTE}${INBOX}" 2>/dev/null | sed 's#/$##'); do
  [ -n "$RUN" ] || continue
  echo "=== $(date -u +%FT%TZ) sync: $RUN ==="
  bash "$HERE/import_run.sh" "$REMOTE" "$INBOX" "$ARCHIVE" "$LIB" "$RUN" || echo "  (import failed, continuing)"
done
