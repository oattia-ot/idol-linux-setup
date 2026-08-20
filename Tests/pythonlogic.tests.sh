#!/usr/bin/env bash
# pythonlogic.tests.sh - run the Python test suite (pytest) instead of Pester
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT"

PYTHON=""
for c in python3 python; do
  if command -v "$c" &>/dev/null; then
    PYTHON=$(command -v "$c")
    break
  fi
done
if [[ -z "$PYTHON" ]]; then
  echo "Python not found"
  exit 1
fi

echo "Running pytest under Tests/ ..."
$PYTHON -m pytest "$SCRIPT_DIR" -v "$@"
