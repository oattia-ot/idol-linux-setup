#!/usr/bin/env bash
# Start every deployed KD-* service (LicenseServer first, then others).
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [[ $EUID -ne 0 ]]; then
  echo "NOTE: Prefer running as root (sudo) for service control."
fi

echo "=== Starting all deployed KD-* services ==="
bash "$SCRIPT_DIR/manage-kdservices.sh" start --all-deployed --non-interactive
EXITCODE=$?
echo
if [[ $EXITCODE -ne 0 ]]; then
  echo "Finished with errors (exit $EXITCODE)."
else
  echo "All requested starts completed."
fi
exit $EXITCODE
