#!/usr/bin/env bash
# ---------------------------------------------------------------------------
#  start-kd-config-dashboard.sh
#  Location: <setup>/config/ui-config/
#  Config is written to: <setup>/config/my-config.json
#
#  Behaviour (aligned with Windows launcher for external client access):
#    1. Verify whether TCP port 5000 (or $KD_DASHBOARD_PORT) is listening.
#    2. If it is already listening → report the URLs and exit.
#    3. If not → open the firewall (best-effort) and install + start a
#       systemd service that binds 0.0.0.0 so external clients can reach
#       the dashboard. Falls back to a foreground start when systemd /
#       root privileges are unavailable.
# ---------------------------------------------------------------------------

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [[ ! -f "$SCRIPT_DIR/kd-config-server.py" ]]; then
  echo
  echo "  ERROR: kd-config-server.py not found in:"
  echo "    $SCRIPT_DIR"
  echo
  exit 1
fi
if [[ ! -f "$SCRIPT_DIR/kd-config-dashboard.html" ]]; then
  echo
  echo "  ERROR: kd-config-dashboard.html not found in:"
  echo "    $SCRIPT_DIR"
  echo
  exit 1
fi

PYTHON=""
for c in python3 python; do
  if command -v "$c" &>/dev/null; then
    PYTHON=$(command -v "$c")
    break
  fi
done
if [[ -z "$PYTHON" ]]; then
  echo "ERROR: Python 3 not found."
  exit 1
fi

DASH_PORT="${KD_DASHBOARD_PORT:-5000}"
# Bind all interfaces by default so external clients can reach the dashboard
# (same idea as the Windows Start-KD-Config-Dashboard.bat --host 0.0.0.0).
DASH_HOST="${KD_DASHBOARD_HOST:-0.0.0.0}"

# ---------------------------------------------------------------------------
# Best-effort "is port listening?" check (ss / lsof / fuser / python bind).
# ---------------------------------------------------------------------------
_port_in_use() {
  local port="$1"
  if command -v ss &>/dev/null; then
    ss -ltn "( sport = :$port )" 2>/dev/null | grep -q ":$port" && return 0
    return 1
  fi
  if command -v lsof &>/dev/null; then
    lsof -iTCP:"$port" -sTCP:LISTEN -t &>/dev/null && return 0
    return 1
  fi
  if command -v fuser &>/dev/null; then
    fuser "$port/tcp" &>/dev/null && return 0
    return 1
  fi
  "$PYTHON" - "$port" <<'PY' 2>/dev/null
import socket, sys
port = int(sys.argv[1])
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
try:
    s.bind(("127.0.0.1", port))
except OSError:
    sys.exit(0)
else:
    sys.exit(1)
finally:
    s.close()
PY
}

# ---------------------------------------------------------------------------
# Best-effort open firewall for the dashboard port (ufw / firewalld).
# Mirrors the Windows batch "Opening Windows Firewall for dashboard port".
# ---------------------------------------------------------------------------
_open_firewall() {
  local port="$1"
  echo "  Opening firewall for TCP port $port (best-effort) ..."

  if command -v ufw &>/dev/null; then
    if ufw status 2>/dev/null | grep -qi "Status: active"; then
      if ufw status 2>/dev/null | grep -qE "(^|[[:space:]])${port}/tcp"; then
        echo "  ufw rule already present for ${port}/tcp"
      else
        if [[ $EUID -eq 0 ]]; then
          ufw allow "${port}/tcp" comment "KD Config Dashboard" >/dev/null 2>&1 \
            && echo "  ufw: allowed ${port}/tcp" \
            || echo "  WARNING: could not add ufw rule for ${port}/tcp"
        elif command -v sudo &>/dev/null; then
          sudo ufw allow "${port}/tcp" comment "KD Config Dashboard" >/dev/null 2>&1 \
            && echo "  ufw: allowed ${port}/tcp" \
            || echo "  WARNING: could not add ufw rule for ${port}/tcp (need sudo)"
        else
          echo "  WARNING: ufw is active but root/sudo is required to open port $port"
        fi
      fi
      return 0
    fi
  fi

  if command -v firewall-cmd &>/dev/null && systemctl is-active --quiet firewalld 2>/dev/null; then
    if firewall-cmd --list-ports 2>/dev/null | grep -q "${port}/tcp"; then
      echo "  firewalld rule already present for ${port}/tcp"
    else
      local cmd_prefix=()
      if [[ $EUID -ne 0 ]]; then
        if command -v sudo &>/dev/null; then
          cmd_prefix=(sudo)
        else
          echo "  WARNING: firewalld is active but root/sudo is required to open port $port"
          return 0
        fi
      fi
      "${cmd_prefix[@]}" firewall-cmd --permanent --add-port="${port}/tcp" >/dev/null 2>&1 \
        && "${cmd_prefix[@]}" firewall-cmd --reload >/dev/null 2>&1 \
        && echo "  firewalld: allowed ${port}/tcp" \
        || echo "  WARNING: could not add firewalld rule for ${port}/tcp"
    fi
    return 0
  fi

  echo "  No active ufw/firewalld detected; skipping firewall change."
}

# ---------------------------------------------------------------------------
# Install + enable + start a systemd unit so the dashboard stays up and is
# reachable from external clients (bind 0.0.0.0).
# ---------------------------------------------------------------------------
_install_and_start_systemd() {
  local unit_name="kd-config-dashboard.service"
  local unit_path="/etc/systemd/system/${unit_name}"
  local python="$1"
  local script_dir="$2"
  local host="$3"
  local port="$4"

  if ! command -v systemctl &>/dev/null; then
    echo "  systemd / systemctl not available — cannot install service."
    return 1
  fi

  local SUDO=()
  if [[ $EUID -ne 0 ]]; then
    if command -v sudo &>/dev/null; then
      SUDO=(sudo)
    else
      echo "  ERROR: root or sudo is required to install a system systemd unit."
      return 1
    fi
  fi

  # Prefer the invoking non-root user so the service owns the config files.
  local run_user run_group
  if [[ -n "${SUDO_USER:-}" ]]; then
    run_user="$SUDO_USER"
    run_group="$(id -gn "$SUDO_USER" 2>/dev/null || id -gn)"
  else
    run_user="$(id -un)"
    run_group="$(id -gn)"
  fi

  echo "  Writing systemd unit: $unit_path"
  cat <<EOF | "${SUDO[@]}" tee "$unit_path" >/dev/null
[Unit]
Description=KD Configuration Dashboard
Documentation=file://${script_dir}/README-KD-Config-Dashboard.txt
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${run_user}
Group=${run_group}
WorkingDirectory=${script_dir}
ExecStart=${python} ${script_dir}/kd-config-server.py --host ${host} --port ${port} --no-browser
Restart=on-failure
RestartSec=5
# Give the process a clean environment; override with Environment= if needed.
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

  echo "  Reloading systemd, enabling and starting ${unit_name} ..."
  "${SUDO[@]}" systemctl daemon-reload
  "${SUDO[@]}" systemctl enable "${unit_name}" >/dev/null 2>&1 || true
  "${SUDO[@]}" systemctl restart "${unit_name}"

  # Brief wait then verify
  sleep 1
  if "${SUDO[@]}" systemctl is-active --quiet "${unit_name}"; then
    echo "  systemd: ${unit_name} is active."
    return 0
  fi
  echo "  WARNING: ${unit_name} did not become active. Check status with:"
  echo "    systemctl status ${unit_name}"
  echo "    journalctl -u ${unit_name} -e"
  return 1
}

# ---------------------------------------------------------------------------
# Detect a reasonable setup-root (two levels up from config/ui-config/) so
# the server can write my-config.json to the expected place.
# ---------------------------------------------------------------------------
_detect_setup_root() {
  local candidate
  candidate="$(cd "$SCRIPT_DIR/../.." 2>/dev/null && pwd)" || return 0
  if [[ -d "$candidate/config" ]]; then
    echo "$candidate"
  fi
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
echo
echo "  ================================================================"
echo "   KD Configuration Dashboard  (pre-install step)"
echo "   Edit Ports / Components / Browser URLs, then Save / Export JSON"
echo "   before running the installer."
echo "  ================================================================"
echo

SETUP_ROOT="${KD_SETUP_ROOT:-$(_detect_setup_root)}"
if [[ -n "$SETUP_ROOT" ]]; then
  echo "  Setup root  : $SETUP_ROOT"
  echo "  Export to   : $SETUP_ROOT/config/my-config.json"
else
  echo "  WARNING: could not resolve setup root; export path may be wrong."
  echo "  Set KD_SETUP_ROOT if needed, e.g.:"
  echo "    export KD_SETUP_ROOT=/opt/kd-setup/idol-linux-setup"
fi
echo "  Local URL   : http://127.0.0.1:${DASH_PORT}/kd-config-dashboard.html"
echo "  Bind        : ${DASH_HOST}:${DASH_PORT}  (external clients allowed)"
echo

if _port_in_use "$DASH_PORT"; then
  echo "  Port ${DASH_PORT} is already listening — dashboard appears to be running."
  echo "  Local URL   : http://127.0.0.1:${DASH_PORT}/kd-config-dashboard.html"
  if [[ "$DASH_HOST" == "0.0.0.0" || "$DASH_HOST" == "::" ]]; then
    echo "  External    : http://<this-host-ip>:${DASH_PORT}/kd-config-dashboard.html"
  fi
  echo
  echo "  Tip: to stop a systemd-managed instance:"
  echo "    sudo systemctl stop kd-config-dashboard.service"
  echo "  Or free the port manually, e.g.:"
  echo "    sudo ss -ltnp 'sport = :${DASH_PORT}'"
  echo "    sudo fuser -k ${DASH_PORT}/tcp"
  echo
  exit 0
fi

echo "  Port ${DASH_PORT} is not listening."
echo "  Installing systemd service for persistent external client access ..."
echo

_open_firewall "$DASH_PORT"

if _install_and_start_systemd "$PYTHON" "$SCRIPT_DIR" "$DASH_HOST" "$DASH_PORT"; then
  echo
  echo "  Dashboard should now be reachable at:"
  echo "    http://127.0.0.1:${DASH_PORT}/kd-config-dashboard.html"
  echo "    http://<this-host-ip>:${DASH_PORT}/kd-config-dashboard.html"
  echo
  echo "  Manage the service with:"
  echo "    sudo systemctl status  kd-config-dashboard.service"
  echo "    sudo systemctl stop    kd-config-dashboard.service"
  echo "    sudo systemctl restart kd-config-dashboard.service"
  echo "    sudo systemctl disable kd-config-dashboard.service"
  echo
  exit 0
fi

# Fallback: no systemd / no privileges → start in the foreground (original behaviour)
echo
echo "  Falling back to foreground start (Ctrl+C to stop) ..."
echo "  Open http://127.0.0.1:${DASH_PORT}/kd-config-dashboard.html in your browser"
echo

EXTRA_ARGS=()
if [[ -n "$SETUP_ROOT" ]]; then
  EXTRA_ARGS+=(--setup-root "$SETUP_ROOT")
fi

exec "$PYTHON" "$SCRIPT_DIR/kd-config-server.py" \
  --host "$DASH_HOST" \
  --port "$DASH_PORT" \
  "${EXTRA_ARGS[@]}"
