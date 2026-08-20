#!/usr/bin/env bash
YELLOW='\033[1;33m'
NC='\033[0m'
# cleanup-kd.sh - full uninstall via Python backend then optional BasePath removal
# BasePath is taken only from --basepath / config; no hardcoded version folders.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

BASEPATH=""
CONFIG=""
DRY_RUN=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --basepath|-BasePath) BASEPATH="$2"; shift 2 ;;
    --config|-ConfigPath) CONFIG="$2"; shift 2 ;;
    --dry-run|-DryRun) DRY_RUN=1; shift ;;
    *)
      # Positional BasePath for backward compatibility
      if [[ -z "$BASEPATH" && "$1" == /* ]]; then
        BASEPATH="$1"
      fi
      shift
      ;;
  esac
done

# Resolve BasePath from config when not provided on the CLI
if [[ -z "$BASEPATH" ]]; then
  PYTHON=""
  if [[ -x "$SCRIPT_DIR/env/bin/python" ]]; then
    PYTHON="$SCRIPT_DIR/env/bin/python"
  elif command -v python3 &>/dev/null; then
    PYTHON="$(command -v python3)"
  fi
  CFG="${CONFIG:-}"
  if [[ -z "$CFG" ]]; then
    for c in "$SCRIPT_DIR/config/my-config.json" "$SCRIPT_DIR/config/default-config.json"; do
      [[ -f "$c" ]] && CFG="$c" && break
    done
  fi
  if [[ -n "$PYTHON" && -n "$CFG" && -f "$CFG" ]]; then
    BASEPATH="$("$PYTHON" -c "
import json, sys
with open(sys.argv[1], encoding='utf-8') as f:
    data = json.load(f)
print((data.get('BasePath') or '').strip())
" "$CFG" 2>/dev/null || true)"
  fi
fi

if [[ -z "$BASEPATH" ]]; then
  BASEPATH="/opt/KnowledgeDiscovery"
fi

echo "=== KD Cleanup Utility (Python backend) ==="
echo "This will STOP and DELETE services (where supported)"
echo "and then DELETE component folders under: $BASEPATH"
echo

if [[ $DRY_RUN -eq 0 ]]; then
  echo -ne "${YELLOW}Are you sure? Type YES to continue: ${NC}"; read -r confirm
  if [[ "$confirm" != "YES" ]]; then
    echo "Aborted."
    exit 0
  fi
fi

ARGS=(--mode Uninstall --non-interactive)
[[ -n "$CONFIG" ]] && ARGS+=(--config "$CONFIG")
[[ $DRY_RUN -eq 1 ]] && ARGS+=(--dry-run)
[[ -n "$BASEPATH" ]] && ARGS+=(--basepath "$BASEPATH")

bash "$SCRIPT_DIR/install-kd.sh" "${ARGS[@]}"
echo "Python uninstall finished. You may still need to remove $BASEPATH manually if desired."
