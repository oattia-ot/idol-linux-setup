#!/usr/bin/env bash
# extract-one-zip.sh - thin wrapper around unzip-one.sh
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec bash "$SCRIPT_DIR/unzip-one.sh" "$@"
