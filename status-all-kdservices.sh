#!/usr/bin/env bash
# Show status of every deployed KD-* service.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "=== Status of all deployed KD-* services ==="
bash "$SCRIPT_DIR/manage-kdservices.sh" status --all-deployed
exit $?
