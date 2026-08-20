#!/usr/bin/env bash
# clean-unnecessary.sh
# Reads config/cleanup.json and deletes declared Toolkit / BasePath targets.
# All paths under BasePath are resolved from the --basepath argument or the
# BasePath value already present in cleanup.json / config (never hardcoded
# version folders such as 26.2 / 26.3).
#
# Usage:
#   ./clean-unnecessary.sh
#   ./clean-unnecessary.sh --basepath /opt/KnowledgeDiscovery
#   ./clean-unnecessary.sh --dry-run
#   ./clean-unnecessary.sh --force
set -uo pipefail
# NOTE: set -e is intentionally NOT used. Cleanup must continue past individual
# permission failures (e.g. root-owned NARs from the sudo dashboard).

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

DRY_RUN=0
FORCE=0
BASEPATH=""
CLEANUP_JSON="${SCRIPT_DIR}/config/cleanup.json"

while [[ $# -gt 0 ]]; do
  case "$1" in
    -DryRun|--dry-run) DRY_RUN=1; shift ;;
    -Force|--force) FORCE=1; shift ;;
    -BasePath|--basepath) BASEPATH="$2"; shift 2 ;;
    -Config|--config) CLEANUP_JSON="$2"; shift 2 ;;
    *) shift ;;
  esac
done

echo "=== Clean-Unnecessary (Linux) ==="
echo "Toolkit: $SCRIPT_DIR"
echo "Manifest: $CLEANUP_JSON"

if [[ ! -f "$CLEANUP_JSON" ]]; then
  echo "ERROR: cleanup.json not found: $CLEANUP_JSON"
  exit 1
fi

PYTHON=""
if [[ -x "$SCRIPT_DIR/env/bin/python" ]]; then
  PYTHON="$SCRIPT_DIR/env/bin/python"
elif command -v python3 &>/dev/null; then
  PYTHON="$(command -v python3)"
elif command -v python &>/dev/null; then
  PYTHON="$(command -v python)"
fi

if [[ -z "$PYTHON" ]]; then
  echo "ERROR: Python 3 is required to parse cleanup.json"
  exit 1
fi

# ---------------------------------------------------------------------------
# Helpers — avoid mapfile < <(...) which can fail with /dev/fd errors under
# set -e and abort the script BEFORE nifi-connectors is wiped.
# ---------------------------------------------------------------------------

json_list() {
  local key="$1"
  "$PYTHON" -c "
import json, sys
key = sys.argv[2]
with open(sys.argv[1], encoding='utf-8') as f:
    data = json.load(f)
for item in data.get(key) or []:
    s = str(item).strip()
    if s:
        print(s)
" "$CLEANUP_JSON" "$key"
}

json_opt_bool() {
  local key="$1"
  local default="${2:-true}"
  "$PYTHON" -c "
import json, sys
key, default = sys.argv[2], sys.argv[3].lower() in ('1', 'true', 'yes')
with open(sys.argv[1], encoding='utf-8') as f:
    data = json.load(f)
opts = data.get('Options') or {}
val = opts.get(key, default)
print('1' if val else '0')
" "$CLEANUP_JSON" "$key" "$default"
}

# Resolve BasePath: CLI wins, else cleanup.json, else my/default config
if [[ -z "$BASEPATH" ]]; then
  BASEPATH="$("$PYTHON" -c "
import json, sys
with open(sys.argv[1], encoding='utf-8') as f:
    data = json.load(f)
print((data.get('BasePath') or '').strip())
" "$CLEANUP_JSON" 2>/dev/null || true)"
fi
if [[ -z "$BASEPATH" ]]; then
  for cfg in "$SCRIPT_DIR/config/my-config.json" "$SCRIPT_DIR/config/default-config.json"; do
    if [[ -f "$cfg" ]]; then
      BASEPATH="$("$PYTHON" -c "
import json, sys
with open(sys.argv[1], encoding='utf-8') as f:
    data = json.load(f)
print((data.get('BasePath') or '').strip())
" "$cfg" 2>/dev/null || true)"
      [[ -n "$BASEPATH" ]] && break
    fi
  done
fi

echo "BasePath: ${BASEPATH:-<not set>}"

clean_path() {
  local p="$1"
  if [[ ! -e "$p" && ! -L "$p" ]]; then
    return 0
  fi
  if [[ $DRY_RUN -eq 1 ]]; then
    echo "[DryRun] would remove: $p"
    return 0
  fi
  echo "Removing: $p"
  if rm -rf -- "$p" 2>/dev/null; then
    return 0
  fi
  # Root-owned files (dashboard ran under sudo) — retry elevated
  if [[ ${EUID:-0} -ne 0 ]] && command -v sudo &>/dev/null; then
    echo "  (permission denied — retrying with sudo)"
    if sudo rm -rf -- "$p" 2>/dev/null; then
      echo "  Removed via sudo: $p"
      return 0
    fi
  fi
  echo "  FAILED to remove: $p (check ownership/permissions)"
  return 1
}

# Wipe every file/dir inside nifi/nifi-connectors (keep the folder itself).
wipe_nifi_connectors() {
  local NAR_DIR="$SCRIPT_DIR/nifi/nifi-connectors"
  if [[ ! -d "$NAR_DIR" ]]; then
    echo "--- Staged NiFi connectors: $NAR_DIR (folder not present, skip) ---"
    return 0
  fi
  echo "--- Staged NiFi connectors (all files) under $NAR_DIR ---"

  if [[ $DRY_RUN -eq 1 ]]; then
    shopt -s nullglob dotglob
    local item
    for item in "$NAR_DIR"/*; do
      echo "[DryRun] would remove: $item"
    done
    shopt -u nullglob dotglob
    return 0
  fi

  # Pass 1: delete each entry
  shopt -s nullglob dotglob
  local item
  for item in "$NAR_DIR"/*; do
    clean_path "$item" || true
  done
  shopt -u nullglob dotglob

  # Pass 2: force residual (root-owned NARs)
  local left=0
  shopt -s nullglob dotglob
  for item in "$NAR_DIR"/*; do
    left=1
    break
  done
  shopt -u nullglob dotglob

  if [[ $left -eq 1 ]]; then
    echo "  Retrying residual files under $NAR_DIR..."
    if [[ ${EUID:-0} -eq 0 ]]; then
      rm -rf -- "$NAR_DIR"/* "$NAR_DIR"/.[!.]* "$NAR_DIR"/..?* 2>/dev/null || true
    elif command -v sudo &>/dev/null; then
      sudo rm -rf -- "$NAR_DIR"/* "$NAR_DIR"/.[!.]* "$NAR_DIR"/..?* 2>/dev/null || true
    fi
  fi

  left=0
  shopt -s nullglob dotglob
  for item in "$NAR_DIR"/*; do
    echo "  still present: $item"
    left=1
  done
  shopt -u nullglob dotglob

  if [[ $left -eq 1 ]]; then
    echo "  WARNING: some files under $NAR_DIR could not be deleted."
    echo "  Run:  sudo rm -rf '$NAR_DIR'/*"
    return 1
  fi
  echo "  $NAR_DIR is empty."
  return 0
}

# Read JSON lists into temp files (no process-substitution /dev/fd dependency)
TMPDIR_CLEAN="$(mktemp -d)"
trap 'rm -rf "$TMPDIR_CLEAN"' EXIT

json_list ToolkitFolders  > "$TMPDIR_CLEAN/toolkit_folders"  || true
json_list ToolkitFiles    > "$TMPDIR_CLEAN/toolkit_files"    || true
json_list ToolkitGlobs    > "$TMPDIR_CLEAN/toolkit_globs"    || true
json_list BasePathFolders > "$TMPDIR_CLEAN/bp_folders"       || true
json_list BasePathFiles   > "$TMPDIR_CLEAN/bp_files"         || true
json_list BasePathGlobs   > "$TMPDIR_CLEAN/bp_globs"         || true

OPTS_DELETE_TOOLKIT="$(json_opt_bool DeleteToolkitTargets true)"
OPTS_DELETE_BASE="$(json_opt_bool DeleteBasePathTargets true)"

# ---------------------------------------------------------------------------
# ALWAYS wipe nifi-connectors first (hard guarantee; do not skip on errors)
# ---------------------------------------------------------------------------
wipe_nifi_connectors || true

# ---------------------------------------------------------------------------
# Toolkit cleanup
# ---------------------------------------------------------------------------
if [[ "$OPTS_DELETE_TOOLKIT" == "1" ]]; then
  echo "--- Toolkit folders ---"
  while IFS= read -r rel || [[ -n "${rel:-}" ]]; do
    [[ -z "${rel:-}" ]] && continue
    clean_path "$SCRIPT_DIR/$rel" || true
  done < "$TMPDIR_CLEAN/toolkit_folders"

  echo "--- Toolkit files ---"
  while IFS= read -r rel || [[ -n "${rel:-}" ]]; do
    [[ -z "${rel:-}" ]] && continue
    clean_path "$SCRIPT_DIR/$rel" || true
  done < "$TMPDIR_CLEAN/toolkit_files"

  echo "--- Toolkit globs ---"
  while IFS= read -r pattern || [[ -n "${pattern:-}" ]]; do
    [[ -z "${pattern:-}" ]] && continue
    if [[ "$pattern" == *"**"* ]]; then
      if [[ "$pattern" == "**/__pycache__" ]]; then
        find "$SCRIPT_DIR" -type d -name "__pycache__" 2>/dev/null | while IFS= read -r p; do
          clean_path "$p" || true
        done
      elif [[ "$pattern" == "**/"* ]]; then
        suffix="${pattern#**/}"
        find "$SCRIPT_DIR" -type f -name "$suffix" 2>/dev/null | while IFS= read -r p; do
          clean_path "$p" || true
        done
      else
        shopt -s globstar nullglob
        for p in $SCRIPT_DIR/$pattern; do
          clean_path "$p" || true
        done
        shopt -u globstar nullglob
      fi
    else
      shopt -s nullglob
      for p in $SCRIPT_DIR/$pattern; do
        clean_path "$p" || true
      done
      shopt -u nullglob
    fi
  done < "$TMPDIR_CLEAN/toolkit_globs"
else
  echo "DeleteToolkitTargets=false — skipping other toolkit cleanup"
fi

# ---------------------------------------------------------------------------
# BasePath cleanup
# ---------------------------------------------------------------------------
if [[ "$OPTS_DELETE_BASE" == "1" && -n "$BASEPATH" && -d "$BASEPATH" ]]; then
  echo "--- BasePath folders under $BASEPATH ---"
  while IFS= read -r rel || [[ -n "${rel:-}" ]]; do
    [[ -z "${rel:-}" ]] && continue
    clean_path "$BASEPATH/$rel" || true
  done < "$TMPDIR_CLEAN/bp_folders"

  echo "--- BasePath files ---"
  while IFS= read -r rel || [[ -n "${rel:-}" ]]; do
    [[ -z "${rel:-}" ]] && continue
    clean_path "$BASEPATH/$rel" || true
  done < "$TMPDIR_CLEAN/bp_files"

  echo "--- BasePath globs ---"
  while IFS= read -r pattern || [[ -n "${pattern:-}" ]]; do
    [[ -z "${pattern:-}" ]] && continue
    shopt -s nullglob
    for p in $BASEPATH/$pattern; do
      clean_path "$p" || true
    done
    shopt -u nullglob
  done < "$TMPDIR_CLEAN/bp_globs"
elif [[ -z "$BASEPATH" ]]; then
  echo "BasePath not set — skipping BasePath cleanup (pass --basepath or set BasePath in config)"
elif [[ ! -d "$BASEPATH" ]]; then
  echo "BasePath does not exist: $BASEPATH — skipping"
else
  echo "DeleteBasePathTargets=false — skipping BasePath cleanup"
fi

if [[ $FORCE -eq 1 ]]; then
  echo "NOTE: --force given; systemd unit deletion is controlled by Options.DeleteSystemdUnits in cleanup.json (default false)."
fi

echo "Done."
exit 0
