#!/usr/bin/env bash
# Register the MHC Typer OOD apps. Run by root (sudo) AFTER deploy/install.sh,
# so the cards don't launch env-less. Copies the app definitions into the OOD
# sys apps dir; they then appear under Interactive Apps -> Bioinformatics.
set -euo pipefail
SRC=/srv/kapurlab/tools/mhc_gui/ood/apps
DST=/var/www/ood/apps/sys
for app in mhc_gui mhc_gui_dev mhc_gui_sandbox; do
  [ -d "$SRC/$app" ] || continue
  echo "Installing $app -> $DST/$app"
  rm -rf "$DST/$app"
  cp -a "$SRC/$app" "$DST/$app"
  chmod -R go+rX "$DST/$app"
done
echo "Done. 'MHC Typer' appears under Interactive Apps -> Bioinformatics."
