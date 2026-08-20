#!/usr/bin/env bash
# extract-all-zips.sh - thin wrapper around unzip-all.sh
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec bash "$SCRIPT_DIR/unzip-all.sh" "$@"
