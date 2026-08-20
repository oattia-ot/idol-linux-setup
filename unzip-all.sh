#!/usr/bin/env bash
# ============================================================
#  unzip-all.sh
#  Extract every .zip under a source directory into a destination
#  base (each zip becomes a subfolder named after the zip).
#
#  Usage:
#    ./unzip-all.sh "/path/to/zips" "/path/to/base"
# ============================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ $# -lt 2 ]]; then
  echo "Usage: $0 <zip_source_dir> <destination_base>"
  exit 1
fi

SRC="$1"
DEST_BASE="$2"

if [[ ! -d "$SRC" ]]; then
  echo "ERROR: Source directory not found: $SRC"
  exit 1
fi

mkdir -p "$DEST_BASE"
shopt -s nullglob
ZIPS=("$SRC"/*.zip "$SRC"/*.ZIP)
if [[ ${#ZIPS[@]} -eq 0 ]]; then
  echo "No .zip files found in $SRC"
  exit 0
fi

FAIL=0
for z in "${ZIPS[@]}"; do
  name=$(basename "$z")
  name_noext="${name%.*}"
  dest="$DEST_BASE/$name_noext"
  echo "=== Extracting $name -> $dest ==="
  if bash "$SCRIPT_DIR/unzip-one.sh" "$z" "$dest"; then
    echo "OK: $name"
  else
    echo "FAILED: $name"
    FAIL=1
  fi
  echo
done

exit $FAIL
