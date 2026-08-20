#!/usr/bin/env bash
# ==============================================================================
#  install-kd.sh
#  Knowledge Discovery Linux installer wrapper (Python backend).
#
#  Examples:
#    ./install-kd.sh --mode Install --non-interactive --config config/my-config.json
#    ./install-kd.sh --mode Uninstall --non-interactive
#    ./install-kd.sh --help
#
#  Interactive menu-style use is limited; prefer passing flags or run the
#  Python entry point directly. Configuration UI is available via the
#  dashboard scripts under config/ui-config/.
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Prefer the toolkit local venv created by initialize-environment.sh (PEP 668 safe).
# Fall back to system python3 only if the venv is absent.
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
  echo "ERROR: Python 3 not found. Run ./initialize-environment.sh first."
  exit 1
fi

# If no arguments, launch the interactive main setup menu
if [[ $# -eq 0 ]]; then
  exec bash "$SCRIPT_DIR/install-kd-menu.sh"
fi

# Pass through all args
exec "$PYTHON" "$SCRIPT_DIR/install_kd.py" "$@"
