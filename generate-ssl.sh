#!/usr/bin/env bash
# ==============================================================================
#  generate-ssl.sh
#  Preferred path: call the Python SSL generator (tools/generate_ssl.py).
#
#  Usage:
#    ./generate-ssl.sh --auto
#    ./generate-ssl.sh --help
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

if [[ ! -f "$SCRIPT_DIR/tools/generate_ssl.py" ]]; then
  echo "ERROR: tools/generate_ssl.py not found."
  exit 1
fi

# Forward all args to the Python generator
exec "$PYTHON" "$SCRIPT_DIR/tools/generate_ssl.py" "$@"
