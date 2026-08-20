#!/usr/bin/env bash
# ==============================================================================
#  install-kd-menu.sh
#  Native Linux interactive main setup menu for the KD Python installer.
#
#  This replaces the interactive menu that, on Windows, lived only in the
#  PowerShell wrapper (Install-KD.ps1) - that wrapper was never ported to
#  Linux, which is why running ./install-kd.sh with no arguments previously
#  just printed a usage hint instead of a menu. This script is that menu,
#  implemented natively in bash, calling the same Python backend
#  (install_kd.py / manage_kd_services.py) that Windows uses.
#
#  Run with:  ./install-kd-menu.sh
#  (it will re-exec itself under sudo if not already root, since installing
#  systemd units and writing under /opt normally requires it)
# ==============================================================================

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ---------------------------------------------------------------------------
# Colors (mirrors the orange/blue/yellow/red scheme documented in README.md
# section 5, "Interactive menu (all modes)")
# ---------------------------------------------------------------------------
ORANGE='\033[0;33m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
GREEN='\033[0;32m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

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
  echo "ERROR: Python 3 not found. Run ./initialize-environment.sh first."
  exit 1
fi

CONFIG_PATH="${KD_CONFIG_PATH:-$SCRIPT_DIR/config/my-config.json}"

# Re-exec under sudo if not root - systemd unit installs and /opt writes need it.
# (Same behavior as setup.sh; kept consistent across entry points.)
if [[ $EUID -ne 0 ]]; then
  echo -e "${YELLOW}NOTE:${NC} Re-running under sudo (systemd unit / /opt writes need root)..."
  exec sudo -E "$0" "$@"
fi

pause() {
  echo
  echo -ne "${YELLOW}Press Enter to return to the menu... ${NC}"; read -r _
}

run_install_kd() {
  # $@ = extra args appended to install_kd.py
  echo -e "${CYAN}>> $PYTHON install_kd.py $* --config \"$CONFIG_PATH\"${NC}"
  echo
  "$PYTHON" "$SCRIPT_DIR/install_kd.py" "$@" --config "$CONFIG_PATH"
  local rc=$?
  echo
  if [[ $rc -eq 0 ]]; then
    echo -e "${GREEN}Done (exit 0).${NC}"
  else
    echo -e "${RED}Finished with exit code $rc.${NC}"
  fi
  return $rc
}

confirm() {
  # $1 = prompt text; returns 0 for yes
  # Prompt text is shown in yellow
  local reply
  echo -ne "${YELLOW}$1 [y/N]: ${NC}"
  read -r reply
  [[ "$reply" =~ ^[Yy]$ ]]
}

# Return 0 if TCP port $1 is currently in use (something is listening).
port_in_use() {
  local port="$1"
  if command -v ss &>/dev/null; then
    ss -ltn "( sport = :$port )" 2>/dev/null | grep -q ":$port"
    return $?
  fi
  if command -v lsof &>/dev/null; then
    lsof -iTCP:"$port" -sTCP:LISTEN -t &>/dev/null
    return $?
  fi
  if command -v fuser &>/dev/null; then
    fuser "$port/tcp" &>/dev/null
    return $?
  fi
  # Fallback: try binding with python
  "$PYTHON" - "$port" <<'PY'
import socket, sys
port = int(sys.argv[1])
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
try:
    s.bind(("127.0.0.1", port))
except OSError:
    sys.exit(0)  # in use
else:
    sys.exit(1)  # free
finally:
    s.close()
PY
}

# Print PIDs (and optional command) listening on TCP port $1.
port_listeners() {
  local port="$1"
  if command -v ss &>/dev/null; then
    ss -ltnp "( sport = :$port )" 2>/dev/null | sed -n '2,$p'
    return
  fi
  if command -v lsof &>/dev/null; then
    lsof -iTCP:"$port" -sTCP:LISTEN -n -P 2>/dev/null
    return
  fi
  if command -v fuser &>/dev/null; then
    fuser -v "$port/tcp" 2>&1
    return
  fi
  echo "(unable to list process details — install ss/lsof/fuser for more info)"
}

# Kill processes listening on TCP port $1. Returns 0 on success.
kill_port() {
  local port="$1"
  local pids=""
  if command -v lsof &>/dev/null; then
    pids=$(lsof -iTCP:"$port" -sTCP:LISTEN -t 2>/dev/null | tr '\n' ' ')
  elif command -v fuser &>/dev/null; then
    pids=$(fuser "$port/tcp" 2>/dev/null | tr -cd '0-9 \n' | tr '\n' ' ')
  elif command -v ss &>/dev/null; then
    pids=$(ss -ltnp "( sport = :$port )" 2>/dev/null | grep -oE 'pid=[0-9]+' | cut -d= -f2 | sort -u | tr '\n' ' ')
  fi
  pids=$(echo "$pids" | xargs)  # trim
  if [[ -z "$pids" ]]; then
    echo -e "${YELLOW}No PID found for port $port; cannot kill automatically.${NC}"
    return 1
  fi
  echo -e "${CYAN}Killing PID(s) on port $port: $pids${NC}"
  # shellcheck disable=SC2086
  kill $pids 2>/dev/null || true
  sleep 1
  # force if still there
  if port_in_use "$port"; then
    # shellcheck disable=SC2086
    kill -9 $pids 2>/dev/null || true
    sleep 1
  fi
  if port_in_use "$port"; then
    echo -e "${RED}Port $port is still in use after kill attempt.${NC}"
    return 1
  fi
  echo -e "${GREEN}Port $port is now free.${NC}"
  return 0
}

opt_00_prerequisite() {
  echo -e "${ORANGE}${BOLD}00) Prerequisite - setup configurations${NC}"
  echo "Opens the config dashboard (web UI) to edit Ports / Components / BrowserUrls."
  echo "Exports to: $CONFIG_PATH"
  echo

  local dash_port=5000
  if port_in_use "$dash_port"; then
    echo -e "${YELLOW}Port $dash_port is already in use.${NC}"
    echo "Listener(s):"
    port_listeners "$dash_port"
    echo
    echo -ne "${YELLOW}Kill the process using port $dash_port before starting the UI dashboard? [y/N]: ${NC}"
    read -r _kill_reply
    if [[ "$_kill_reply" =~ ^[Yy]$ ]]; then
      if ! kill_port "$dash_port"; then
        echo -e "${RED}Could not free port $dash_port. Aborting dashboard start.${NC}"
        pause
        return 1
      fi
    else
      echo -e "${YELLOW}Leaving port $dash_port in use. Dashboard may fail to start.${NC}"
      if ! confirm "Start the dashboard anyway?"; then
        pause
        return 1
      fi
    fi
    echo
  fi

  bash "$SCRIPT_DIR/config/ui-config/start-kd-config-dashboard.sh"
  pause
}

opt_01_install() {
  echo -e "${ORANGE}${BOLD}01) Install${NC}"
  echo "Extract, configure, register and start services (systemd units)."
  local extra=()
  confirm "Run non-interactively (no prompts, use config/env values only)?" && extra+=(--non-interactive)
  confirm "Dry run first (show what would happen, no changes)?" && extra+=(--dry-run)
  run_install_kd --mode Install "${extra[@]}"
  pause
}

opt_02_configure() {
  echo -e "${BLUE}${BOLD}02) Configure${NC}"
  echo "Re-apply config to an existing install."
  run_install_kd --mode Configure --non-interactive
  pause
}

opt_03_upgrade() {
  echo -e "${BLUE}${BOLD}03) Upgrade${NC}"
  echo "Re-run install; existing services are left alone (asks to regenerate SSL)."
  run_install_kd --mode Upgrade
  pause
}

opt_04_repair() {
  echo -e "${BLUE}${BOLD}04) Repair${NC}"
  echo "Uninstall then reinstall (asks to regenerate SSL)."
  confirm "This will uninstall and reinstall KD components. Continue?" && run_install_kd --mode Repair
  pause
}

opt_05_uninstall() {
  echo -e "${BLUE}${BOLD}05) Uninstall${NC}"
  echo "Stop services, delete systemd units, delete folders."
  if confirm "This will stop and remove KD services and data. Are you sure?"; then
    run_install_kd --mode Uninstall --non-interactive
  fi
  pause
}

opt_06_extract_only() {
  echo -e "${BLUE}${BOLD}06) Extract-Only${NC}"
  echo "Extract (if needed) and verify folders - no config / services."
  run_install_kd --mode Install --extract-only --non-interactive
  pause
}


opt_07_set_nifi_credentials() {
  echo -e "${BLUE}${BOLD}07) Set NiFi UI credentials${NC}"
  echo -ne "${YELLOW}NiFi username: ${NC}"; read -r nifi_user
  echo -ne "${YELLOW}NiFi password: ${NC}"; read -rs nifi_pass
  echo
  run_install_kd --mode SetNiFiCredentials --non-interactive \
    --nifi-username "$nifi_user" --nifi-password "$nifi_pass"
  pause
}

# Read Components list from my-config.json (falls back to empty).
read_config_components() {
  local cfg="$CONFIG_PATH"
  if [[ ! -f "$cfg" ]]; then
    return 0
  fi
  "$PYTHON" - "$cfg" <<'PY'
import json, sys
path = sys.argv[1]
try:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
except Exception:
    sys.exit(0)
comps = data.get("Components") or []
if isinstance(comps, list):
    for c in comps:
        c = str(c).strip()
        if c:
            print(c)
PY
}

# Map component name → systemd unit (kd-<lowercase>.service), matching service_manager.
component_unit_name() {
  local comp="$1"
  # Strip optional KD- prefix, lower-case
  local base="${comp#KD-}"
  base="${base#kd-}"
  base=$(echo "$base" | tr '[:upper:]' '[:lower:]')
  echo "kd-${base}.service"
}

# Show journalctl logs for one component (or all) from my-config.json Components.
opt_08_journal_logs() {
  echo -e "${BLUE}${BOLD}Journal logs (journalctl)${NC}"
  echo "Source: $CONFIG_PATH  (Components)"
  echo

  local -a comps=()
  mapfile -t comps < <(read_config_components)
  if [[ ${#comps[@]} -eq 0 ]]; then
    echo -e "${YELLOW}No Components found in config. Export config from option 00 first.${NC}"
    return
  fi

  echo -e "${CYAN}Select a component to view the last 50 journal lines:${NC}"
  echo
  local i
  for i in "${!comps[@]}"; do
    local unit
    unit=$(component_unit_name "${comps[$i]}")
    printf "  ${GREEN}%2d)${NC} %-28s ${YELLOW}%s${NC}\n" "$((i + 1))" "${comps[$i]}" "$unit"
  done
  echo -e "  ${GREEN} a)${NC} ${BOLD}all components${NC}"
  echo -e "  ${RED} 0)${NC} Back"
  echo
  local pick
  echo -ne "${YELLOW}Choice: ${NC}"; read -r pick
  pick="${pick:-0}"

  local -a targets=()
  if [[ "$pick" == "0" ]]; then
    return
  elif [[ "$pick" =~ ^[Aa]$ ]]; then
    targets=("${comps[@]}")
  elif [[ "$pick" =~ ^[0-9]+$ ]] && (( pick >= 1 && pick <= ${#comps[@]} )); then
    targets=("${comps[$((pick - 1))]}")
  else
    echo -e "${RED}Invalid choice.${NC}"
    return
  fi

  local lines=50
  local comp unit
  for comp in "${targets[@]}"; do
    unit=$(component_unit_name "$comp")
    echo
    echo -e "${CYAN}${BOLD}── $comp  ($unit)  — last $lines lines ──${NC}"
    echo -e "${CYAN}>> journalctl -u $unit -n $lines --no-pager${NC}"
    if command -v journalctl &>/dev/null; then
      journalctl -u "$unit" -n "$lines" --no-pager 2>&1 || true
    else
      echo -e "${RED}journalctl not found on this system.${NC}"
    fi
  done
}

opt_08_manage_services() {
  echo -e "${ORANGE}${BOLD}08) Manage services (systemd)${NC}"
  echo
  echo -e "  ${BLUE}1) ${BOLD}status${BLUE}   - config Components + systemctl list-units --all 'kd-*' (color table)${NC}"
  echo -e "  ${BLUE}2) ${BOLD}start${BLUE}    - start KD systemd units${NC}"
  echo -e "  ${BLUE}3) ${BOLD}stop${BLUE}     - stop KD systemd units${NC}"
  echo -e "  ${BLUE}4) ${BOLD}restart${BLUE}  - restart KD systemd units${NC}"
  echo -e "  ${BLUE}5) ${BOLD}delete${BLUE}   - stop + remove systemd units under /lib/systemd/system${NC}"
  echo -e "  ${BLUE}6) ${BOLD}create${BLUE}   - vendor init/systemd templates → /lib/systemd/system + enable${NC}"
  echo -e "  ${BLUE}7) ${BOLD}journal${BLUE}  - journalctl -u kd-<component>.service -n 50 (from my-config.json Components)${NC}"
  echo -e "  ${RED}0) Back${NC}"
  echo -e "  ${YELLOW}"
  local sub
  echo -ne "${YELLOW}Choice [1]: ${NC}"; read -r sub
  sub="${sub:-1}"
  local action=""
  case "$sub" in
    1) action=status ;;
    2) action=start ;;
    3) action=stop ;;
    4) action=restart ;;
    5) action=delete ;;
    6) action=create ;;
    7) opt_08_journal_logs; pause; return ;;
    0) return ;;
    *) echo -e "${RED}Invalid choice.${NC}"; pause; return ;;
  esac
  local extra=()
  if [[ "$action" == "delete" ]]; then
    confirm "Confirm destructive action '$action'?" || { pause; return; }
    extra+=(--force)
  fi
  echo -e "${CYAN}>> $PYTHON manage_kd_services.py $action --config \"$CONFIG_PATH\" ${extra[*]}${NC}"
  "$PYTHON" "$SCRIPT_DIR/manage_kd_services.py" "$action" --config "$CONFIG_PATH" "${extra[@]}"
  pause
}


opt_09_browser_urls() {
  echo -e "${BLUE}${BOLD}09) Generate Browser URLs${NC}"
  run_install_kd --mode ShowBrowserUrls --non-interactive
  pause
}

opt_10_update_config_files() {
  echo -e "${BLUE}${BOLD}10) Update config files${NC}"
  echo "Applies ports / API key from tools/replacements.json (syncs my-config.json)."
  local extra=()
  confirm "Dry run first?" && extra+=(--dry-run)
  bash "$SCRIPT_DIR/tools/update-configfiles.sh" "${extra[@]}"
  pause
}

show_help() {
  "$PYTHON" "$SCRIPT_DIR/install_kd.py" --help
  pause
}

print_menu() {
  clear 2>/dev/null || true
  echo -e "${CYAN}${BOLD}KD Installer (Linux) - Main Setup Menu${NC}"
  echo "Toolkit root: $SCRIPT_DIR"
  echo "Config file:  $CONFIG_PATH"
  echo
  echo "[3/4] Select operation mode"
  echo
  echo -e "  ${YELLOW}Required for a new deployment:${NC}"
  echo -e "  ${RED}${BOLD}00) Prerequisite - setup configurations - open web UI to edit Ports/Components/BrowserUrls (do this first)${NC}"
  echo -e "  ${RED}${BOLD}01) Install                 - extract, configure, register and start services (asks to regenerate SSL)${NC}"
  echo
  echo -e "  ${BLUE}Other modes:${NC}"
  echo -e "  ${BLUE}02) Configure               - re-apply config to an existing install${NC}"
  echo -e "  ${BLUE}03) Upgrade                 - re-run install (existing services left alone; asks to regenerate SSL)${NC}"
  echo -e "  ${BLUE}04) Repair                  - uninstall then reinstall (asks to regenerate SSL)${NC}"
  echo -e "  ${BLUE}05) Uninstall               - stop services, delete services, delete folders (asks before deleting SSL)${NC}"
  echo -e "  ${BLUE}06) Extract-Only            - extract (if needed) and verify folders (no config / services)${NC}"
  echo -e "  ${BLUE}07) Set NiFi UI credentials - apply username/password (NiFi already extracted)${NC}"
  echo -e "  ${BLUE}08) Manage services (systemd) - start/stop/restart/status/delete/create (vendor unit templates)${NC}"
  echo -e "  ${BLUE}09) Generate Browser URLs   - print Admin/Status/Log URLs for all components from config Ports${NC}"
  echo -e "  ${BLUE}10) Update config files     - apply ports / API key from tools/replacements.json${NC}"
  echo -e "   ${YELLOW}?) Help${NC}"
  echo -e "   ${RED}0) Exit${NC}"
  echo -e "   ${YELLOW}"
}

main_loop() {
  while true; do
    print_menu
    echo -ne "${YELLOW}Enter choice [00]: ${NC}"; read -r choice
    choice="${choice:-00}"
    # Accept single-digit entries (1-9) as shorthand for the zero-padded
    # menu options (01-09) - e.g. "1" and "01" both select Install.
    if [[ "$choice" =~ ^[1-9]$ ]]; then
      choice="0$choice"
    fi
    case "$choice" in
      00) opt_00_prerequisite ;;
      01) opt_01_install ;;
      02) opt_02_configure ;;
      03) opt_03_upgrade ;;
      04) opt_04_repair ;;
      05) opt_05_uninstall ;;
      06) opt_06_extract_only ;;
      07) opt_07_set_nifi_credentials ;;
      08) opt_08_manage_services ;;
      09) opt_09_browser_urls ;;
      10) opt_10_update_config_files ;;
      "?") show_help ;;
      0) echo "Exiting."; exit 0 ;;
      *) echo -e "${RED}Invalid choice: $choice${NC}"; pause ;;
    esac
  done
  echo -e "   ${NC}"
}

main_loop
