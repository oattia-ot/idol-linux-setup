#!/usr/bin/env bash
# ============================================================
#  unzip-one.sh
#  Native extraction of a SINGLE zip, stripping the first-level
#  folder that OpenText packages typically contain.
#
#  After extract:
#    - ownership of DEST is set to the current user:group
#    - chmod +x is applied under DEST (and ELF bits restored)
#
#  Usage:
#    ./unzip-one.sh "/path/to/file.zip" "/path/to/destination"
#
#  Returns exit code 0 on success, 1 on failure.
# ============================================================

set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "ERROR: Missing arguments"
  echo "Usage: $0 <zipfile.zip> <destination_folder>"
  exit 1
fi

ZIPFILE="$1"
DEST="$2"
TEMP_DIR="${DEST}/_tmp_extract"

# Current invoking user (prefer SUDO_USER when run via sudo so files are not root-owned)
CURRENT_USER="${SUDO_USER:-$(id -un)}"
CURRENT_GROUP="$(id -gn "$CURRENT_USER" 2>/dev/null || id -gn)"
CURRENT_UID="$(id -u "$CURRENT_USER" 2>/dev/null || id -u)"
CURRENT_GID="$(id -g "$CURRENT_USER" 2>/dev/null || id -g)"

if [[ ! -f "$ZIPFILE" ]]; then
  echo "ERROR: ZIP not found: $ZIPFILE"
  exit 1
fi

echo "[Unzip-One] Source: $ZIPFILE"
echo "[Unzip-One] Dest:   $DEST"
echo "[Unzip-One] Size:   $(du -h "$ZIPFILE" | cut -f1)"
echo "[Unzip-One] Owner:  ${CURRENT_USER}:${CURRENT_GROUP} (${CURRENT_UID}:${CURRENT_GID})"

if [[ -d "$DEST" ]]; then
  echo "[Unzip-One] Removing existing destination..."
  rm -rf "$DEST"
fi
mkdir -p "$DEST"
mkdir -p "$TEMP_DIR"

echo "[Unzip-One] Extracting (this can take a long time for multi-GB packages)..."
if command -v unzip &>/dev/null; then
  unzip -q -o "$ZIPFILE" -d "$TEMP_DIR"
elif command -v tar &>/dev/null; then
  # tar can handle many zip formats on modern systems
  tar -xf "$ZIPFILE" -C "$TEMP_DIR" 2>/dev/null || {
    echo "ERROR: tar failed; try installing unzip"
    rm -rf "$TEMP_DIR"
    exit 1
  }
else
  echo "ERROR: Neither unzip nor tar found. Install unzip."
  exit 1
fi
echo "[Unzip-One] Extract finished; stripping top-level folder if present..."

# Count entries in TEMP_DIR
mapfile -t ENTRIES < <(find "$TEMP_DIR" -mindepth 1 -maxdepth 1)
COUNT=${#ENTRIES[@]}

if [[ $COUNT -eq 1 && -d "${ENTRIES[0]}" ]]; then
  INNER="${ENTRIES[0]}"
  echo "[Unzip-One] Single root folder: $(basename "$INNER") - moving contents up..."
  # Move contents of INNER into DEST
  shopt -s dotglob
  mv "$INNER"/* "$DEST"/ 2>/dev/null || true
  shopt -u dotglob
  # Remove empty INNER if still there
  rmdir "$INNER" 2>/dev/null || rm -rf "$INNER"
else
  echo "[Unzip-One] Multiple root items or files - moving everything as-is..."
  shopt -s dotglob
  mv "$TEMP_DIR"/* "$DEST"/ 2>/dev/null || true
  shopt -u dotglob
fi

rm -rf "$TEMP_DIR"

# Ownership: current user/group for everything under DEST
echo "[Unzip-One] Setting ownership to ${CURRENT_USER}:${CURRENT_GROUP}..."
if command -v chown &>/dev/null; then
  # Prefer numeric ids when the target user may not resolve in this context
  chown -R "${CURRENT_UID}:${CURRENT_GID}" "$DEST" 2>/dev/null \
    || chown -R "${CURRENT_USER}:${CURRENT_GROUP}" "$DEST" 2>/dev/null \
    || echo "[Unzip-One] WARNING: could not chown $DEST (may need root)"
fi

# chmod +x on top-level entries (as requested) and restore ELF executable bits
echo "[Unzip-One] Applying chmod +x and restoring executable bits..."
(
  cd "$DEST"
  # Top-level: chmod +x ./* (ignore failures for non-files / missing globs)
  shopt -s nullglob
  chmod +x ./* 2>/dev/null || true
  shopt -u nullglob
)

# OpenText Linux packages store ELF binaries without an extension. Some unzip
# builds drop the Unix executable bit; restore +x on extension-less ELF files
# in the top levels so discovery/systemd can launch them.
find "$DEST" -maxdepth 3 -type f ! -name "*.*" -print0 2>/dev/null \
  | while IFS= read -r -d "" f; do
      if head -c 4 "$f" 2>/dev/null | grep -q $'\x7fELF'; then
        chmod a+x "$f" 2>/dev/null || true
      fi
    done

# Also ensure common launcher scripts are executable
find "$DEST" -maxdepth 4 -type f \( -name "*.sh" -o -name "nifi" -o -name "nifi.sh" \) -print0 2>/dev/null \
  | while IFS= read -r -d "" f; do
      chmod a+x "$f" 2>/dev/null || true
    done

echo "[Unzip-One] Done."
exit 0
