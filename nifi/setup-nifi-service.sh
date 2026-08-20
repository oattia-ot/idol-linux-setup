#!/usr/bin/env bash
# ==============================================================================
#  setup-nifi-service.sh
#  Linux helper to run Apache NiFi under systemd.
#
#  On Linux, prefer NiFi's native bin/nifi.sh or a systemd unit.
#  This script provides a minimal registration helper / status.
#
#  Usage:
#    ./setup-nifi-service.sh --nifi-home /opt/KnowledgeDiscovery/NiFi
#    ./setup-nifi-service.sh --nifi-home /path/to/NiFi --start
# ==============================================================================

set -euo pipefail

NIFI_HOME=""
SERVICE_NAME="nifi"
START=0
NON_INTERACTIVE=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --nifi-home|-NifiHome) NIFI_HOME="$2"; shift 2 ;;
    --service-name|-ServiceName) SERVICE_NAME="$2"; shift 2 ;;
    --start|-Start) START=1; shift ;;
    --non-interactive|-NonInteractive) NON_INTERACTIVE=1; shift ;;
    *) shift ;;
  esac
done

if [[ -z "$NIFI_HOME" ]]; then
  # Resolve from config BasePath when possible; never hardcode version folders
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
  BASE=""
  for cfg in "$ROOT/config/my-config.json" "$ROOT/config/default-config.json"; do
    if [[ -f "$cfg" ]] && command -v python3 &>/dev/null; then
      BASE="$(python3 -c "
import json, sys
with open(sys.argv[1], encoding='utf-8') as f:
    data = json.load(f)
print((data.get('BasePath') or '').strip())
" "$cfg" 2>/dev/null || true)"
      [[ -n "$BASE" ]] && break
    fi
  done
  if [[ -n "$BASE" ]]; then
    NIFI_HOME="${BASE%/}/NiFi"
  else
    NIFI_HOME="/opt/KnowledgeDiscovery/NiFi"
  fi
fi

if [[ ! -d "$NIFI_HOME" ]]; then
  echo "ERROR: NiFi home not found: $NIFI_HOME"
  exit 1
fi

NIFI_SH="$NIFI_HOME/bin/nifi.sh"
if [[ ! -x "$NIFI_SH" && -f "$NIFI_SH" ]]; then
  chmod +x "$NIFI_SH" || true
fi

if [[ ! -f "$NIFI_SH" ]]; then
  echo "ERROR: $NIFI_SH not found. Is this a full NiFi distribution?"
  exit 1
fi

echo "NiFi home: $NIFI_HOME"
echo "Service name (logical): $SERVICE_NAME"
echo
echo "On Linux, manage NiFi with:"
echo "  $NIFI_SH start"
echo "  $NIFI_SH stop"
echo "  $NIFI_SH status"
echo
echo "Optionally create a systemd unit (example):"
echo "  [Unit]"
echo "  Description=OpenText KD Apache NiFi"
echo "  After=network.target"
echo "  [Service]"
echo "  Type=forking"
echo "  ExecStart=$NIFI_SH start"
echo "  ExecStop=$NIFI_SH stop"
echo "  User=nifi"
echo "  Restart=on-failure"
echo "  [Install]"
echo "  WantedBy=multi-user.target"
echo

if [[ $START -eq 1 ]]; then
  echo "Starting NiFi..."
  "$NIFI_SH" start
fi

exit 0
