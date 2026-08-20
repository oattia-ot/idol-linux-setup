#!/usr/bin/env bash
# ==============================================================================
#  setup.sh - bootstrap launcher for initialize-environment.sh
#
#  Linux equivalent of Setup.bat / Initialize-Environment.ps1 entry point.
#  Run with: sudo ./setup.sh   or   ./setup.sh (will prompt for sudo if needed)
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [[ $EUID -ne 0 ]]; then
  echo "NOTE: Some steps work better with root privileges. Re-running with sudo..."
  exec sudo "$0" "$@"
fi

bash "$SCRIPT_DIR/initialize-environment.sh" "$@"
EXITCODE=$?

if [[ $EXITCODE -ne 0 ]]; then
  echo
  echo "============================================================"
  echo "  initialize-environment.sh exited with code $EXITCODE."
  echo "============================================================"
fi

exit $EXITCODE
