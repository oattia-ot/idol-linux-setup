#!/usr/bin/env bash
# ==============================================================================
#  manage-kdservices.sh
#  Linux wrapper around manage_kd_services.py
#
#  Native systemd service management (kd/service_manager.py manages
#  KD-* components as real systemd units - no Windows sc.exe / PowerShell — Linux systemd only).
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Prefer the toolkit local venv created by initialize-environment.sh (PEP 668 safe).
PYTHON=""
if [[ -x "$SCRIPT_DIR/env/bin/python" ]]; then
  PYTHON="$SCRIPT_DIR/env/bin/python"
else
  for c in python3 python; do
    if command -v "$c" &>/dev/null; then
      PYTHON=$(command -v "$c")
      break
    fi
  done
fi
if [[ -z "$PYTHON" ]]; then
  echo "ERROR: Python 3 not found."
  exit 1
fi

# Forward all arguments to the Python manager
exec "$PYTHON" "$SCRIPT_DIR/manage_kd_services.py" "$@"
