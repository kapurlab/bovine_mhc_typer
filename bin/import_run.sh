#!/bin/bash
# Import one ONT run from the OneDrive inbox into the shared library, then move
# the OneDrive copy into the dated archive. Args:
#   import_run.sh <remote:> <inbox> <archive> <library_dir> <run_name>
# Prints progress (unbuffered) for the GUI's polling log.
set -uo pipefail
REMOTE="$1"; INBOX="$2"; ARCHIVE="$3"; LIB="$4"; RUN="$5"
SRC="${REMOTE}${INBOX}/${RUN}"
DST="${LIB}/${RUN}"

echo "[import] $RUN"
echo "[import] copy  $SRC  ->  $DST"
rclone copy "$SRC" "$DST" --transfers 8 --stats 5s --stats-one-line
rc=$?
if [ "$rc" -ne 0 ]; then
  echo "[import] FAILED: rclone copy exited $rc"
  exit "$rc"
fi

BC=$(ls -d "$DST"/[Bb]arcode* 2>/dev/null | wc -l)
echo "[import] copied — $BC barcodes"
if [ "$BC" -eq 0 ]; then
  echo "[import] WARNING: no barcode dirs — leaving in inbox for review, not archiving"
  exit 0
fi

MONTH=$(date +%Y-%m)
echo "[import] archive -> ${REMOTE}${ARCHIVE}/${MONTH}/${RUN}"
rclone move "$SRC" "${REMOTE}${ARCHIVE}/${MONTH}/${RUN}"
echo "[import] done: $RUN"
