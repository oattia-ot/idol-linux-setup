#!/usr/bin/env bash
# ==============================================================================
#  initialize-environment.sh
#  One-time environment prep for the KD Python Installer toolkit (Linux).
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

GREEN='\033[0;32m'
CYAN='\033[0;36m'
YELLOW='\033[0;33m'
RED='\033[0;31m'
BOLD='\033[1m'
NC='\033[0m'

ok()   { echo -e "  ${GREEN}[OK]${NC} $1"; }
warn() { echo -e "  ${YELLOW}[WARN]${NC} $1"; }
fail() { echo -e "  ${RED}[FAIL]${NC} $1"; }
step() { echo -e "${CYAN}$1${NC}"; }

# Ask the user for confirmation. Returns 0 for yes, 1 for no.
# Non-interactive shells (no TTY) default to "no".
confirm() {
  local prompt="$1"
  local reply
  if [[ ! -t 0 ]]; then
    echo -e "  ${YELLOW}(non-interactive – skipping)${NC} $prompt"
    return 1
  fi
  # Prompt question in yellow
  echo -ne "  ${YELLOW}$prompt [y/N]: ${NC}"
  read -r reply
  [[ "$reply" =~ ^[Yy]$ ]]
}

overall_ok=true
VENV_DIR="$SCRIPT_DIR/env"

echo -e "${CYAN}KD Installer (Python) - Environment Preparation (Linux)${NC}"
echo "Toolkit root: $SCRIPT_DIR"
echo

# 1. Privileges
step "[1/6] Checking privileges..."
if [[ $EUID -eq 0 ]]; then
  ok "Running as root"
else
  warn "Not running as root. Some operations (services, system packages) may require sudo."
fi

# 2. Python
step "[2/6] Checking Python..."
SYSTEM_PYTHON=""
for c in python3 python; do
  if command -v "$c" &>/dev/null; then
    SYSTEM_PYTHON=$(command -v "$c")
    break
  fi
done
if [[ -n "$SYSTEM_PYTHON" ]]; then
  VER=$($SYSTEM_PYTHON -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')" 2>/dev/null || echo "unknown")
  ok "System Python $VER ($SYSTEM_PYTHON)"
  MAJ=$($SYSTEM_PYTHON -c "import sys; print(sys.version_info.major)" 2>/dev/null || echo 0)
  MIN=$($SYSTEM_PYTHON -c "import sys; print(sys.version_info.minor)" 2>/dev/null || echo 0)
  if [[ "$MAJ" -lt 3 ]] || { [[ "$MAJ" -eq 3 ]] && [[ "$MIN" -lt 8 ]]; }; then
    fail "Python 3.8+ required (found $VER)"
    overall_ok=false
  fi
else
  fail "Python 3.8+ not found. Install with: sudo apt install python3 python3-pip python3-venv  (or equivalent)"
  overall_ok=false
fi

# 3. Virtual environment + requirements  (PEP 668 safe)
#    On Ubuntu 24.04+ / Debian the system Python is "externally managed".
#    We therefore install toolkit dependencies into a local venv under ./env
#    and all entry-point scripts prefer that interpreter when present.
step "[3/6] Checking Python packages (local venv)..."
PYTHON=""   # will point at the interpreter we should use going forward

if [[ -n "$SYSTEM_PYTHON" ]]; then
  # Ensure python3-venv is available (needed to create the venv)
  if ! $SYSTEM_PYTHON -c "import venv" 2>/dev/null; then
    warn "python3-venv module not available."
    if confirm "Install python3-venv (and python3-pip) now?"; then
      if command -v apt-get &>/dev/null; then
        apt-get update -qq
        apt-get install -y python3-venv python3-pip python3-full || {
          fail "Failed to install python3-venv"
          overall_ok=false
        }
      elif command -v dnf &>/dev/null; then
        dnf install -y python3-venv python3-pip || { fail "Failed to install python3-venv"; overall_ok=false; }
      else
        warn "Install python3-venv manually, then re-run this script."
      fi
    else
      warn "Skipped. Without a venv, pip installs may fail (PEP 668)."
    fi
  fi

  NEED_VENV=false
  if [[ ! -x "$VENV_DIR/bin/python" ]]; then
    NEED_VENV=true
  fi

  if $NEED_VENV; then
    if confirm "Create local virtual environment at $VENV_DIR and install requirements?"; then
      echo "  Creating venv: $VENV_DIR"
      if $SYSTEM_PYTHON -m venv "$VENV_DIR"; then
        ok "Virtual environment created"
      else
        fail "Failed to create venv at $VENV_DIR"
        overall_ok=false
      fi
    else
      warn "Skipped venv creation. Falling back to system Python (pip may fail on PEP 668 systems)."
    fi
  else
    ok "Local venv already present: $VENV_DIR"
  fi

  # Prefer venv python if it exists
  if [[ -x "$VENV_DIR/bin/python" ]]; then
    PYTHON="$VENV_DIR/bin/python"
    ok "Using toolkit venv: $PYTHON"
  else
    PYTHON="$SYSTEM_PYTHON"
    warn "Using system Python: $PYTHON"
  fi

  # Install / refresh requirements into the chosen interpreter
  if [[ -f "$SCRIPT_DIR/requirements.txt" ]]; then
    echo "  Installing/updating requirements from requirements.txt ..."
    # Ensure pip is present inside the venv
    if ! "$PYTHON" -m pip --version &>/dev/null; then
      "$PYTHON" -m ensurepip --upgrade 2>/dev/null || true
    fi

    if "$PYTHON" -m pip install --upgrade pip setuptools wheel -q 2>/dev/null; then
      :
    fi

    if "$PYTHON" -m pip install -r "$SCRIPT_DIR/requirements.txt"; then
      ok "Requirements installed into $($PYTHON -c 'import sys; print(sys.prefix)')"
    else
      # Last-resort fallback for system Python on PEP 668 hosts
      if [[ "$PYTHON" == "$SYSTEM_PYTHON" ]]; then
        warn "Normal pip install failed (likely PEP 668). Retrying with --break-system-packages ..."
        if confirm "Allow --break-system-packages for system Python? (not recommended)"; then
          if "$PYTHON" -m pip install --break-system-packages -r "$SCRIPT_DIR/requirements.txt"; then
            ok "Requirements installed (with --break-system-packages)"
          else
            fail "pip install failed even with --break-system-packages"
            overall_ok=false
          fi
        else
          warn "Skipped. Create the local venv (re-run this script and answer Y) for a clean install."
        fi
      else
        fail "pip install into venv failed"
        overall_ok=false
      fi
    fi
  fi
else
  warn "Python not available – skipped pip / requirements check"
fi

# 4. Common tools
step "[4/6] Checking common tools..."
MISSING_TOOLS=()
for tool in unzip tar curl openssl; do
  if command -v "$tool" &>/dev/null; then
    ok "$tool found"
  else
    warn "$tool not found"
    MISSING_TOOLS+=("$tool")
  fi
done
if [[ ${#MISSING_TOOLS[@]} -gt 0 ]]; then
  if confirm "Install missing tools (${MISSING_TOOLS[*]}) now?"; then
    if command -v apt-get &>/dev/null; then
      apt-get update -qq
      apt-get install -y "${MISSING_TOOLS[@]}" && ok "Tools installed" || warn "Some tools failed to install"
    elif command -v dnf &>/dev/null; then
      dnf install -y "${MISSING_TOOLS[@]}" && ok "Tools installed" || warn "Some tools failed to install"
    else
      warn "Install manually via your package manager."
    fi
  else
    warn "Skipped. Install later if needed."
  fi
fi

# 5. Writable dirs / permissions
step "[5/6] Basic permissions check..."
if [[ -w "$SCRIPT_DIR" ]]; then
  ok "Toolkit directory is writable"
else
  warn "Toolkit directory not writable by current user"
fi

# 6. Linux OS / WKOOP / GLIBC dependencies (official KD requirements)
step "[6/6] Checking Linux OS dependencies (GLIBC + WKOOP packages)..."
if [[ -n "${PYTHON:-}" ]]; then
  export PYTHONPATH="${SCRIPT_DIR}${PYTHONPATH:+:$PYTHONPATH}"

  CHECK_OUT=$($PYTHON -c "
from kd.prerequisites import test_kd_glibc_requirement, test_kd_wkoop_packages
import json
g = test_kd_glibc_requirement()
w = test_kd_wkoop_packages()
print(json.dumps({'glibc': g, 'wkoop': w}))
" 2>/dev/null || echo '{"glibc":{"Pass":false,"Detail":"import failed"},"wkoop":{"Pass":false,"Missing":[],"Detail":"import failed"}}')

  GLIBC_PASS=$($PYTHON -c "import sys,json; d=json.load(sys.stdin); print('yes' if d.get('glibc',{}).get('Pass') else 'no')" <<<"$CHECK_OUT" 2>/dev/null || echo no)
  GLIBC_DETAIL=$($PYTHON -c "import sys,json; print(json.load(sys.stdin).get('glibc',{}).get('Detail',''))" <<<"$CHECK_OUT" 2>/dev/null || echo "")
  WKOOP_PASS=$($PYTHON -c "import sys,json; d=json.load(sys.stdin); print('yes' if d.get('wkoop',{}).get('Pass') else 'no')" <<<"$CHECK_OUT" 2>/dev/null || echo no)
  WKOOP_DETAIL=$($PYTHON -c "import sys,json; print(json.load(sys.stdin).get('wkoop',{}).get('Detail',''))" <<<"$CHECK_OUT" 2>/dev/null || echo "")
  WKOOP_MISSING=$($PYTHON -c "import sys,json; print(','.join(json.load(sys.stdin).get('wkoop',{}).get('Missing') or []))" <<<"$CHECK_OUT" 2>/dev/null || echo "")

  if [[ "$GLIBC_PASS" == "yes" ]]; then
    ok "GLIBC: $GLIBC_DETAIL"
  else
    warn "GLIBC: $GLIBC_DETAIL"
    warn "  (Cannot be fixed by package install – use a newer OS or the libraries shipped under InstallDir/common)"
  fi

  if [[ "$WKOOP_PASS" == "yes" ]]; then
    ok "WKOOP packages: $WKOOP_DETAIL"
  else
    warn "WKOOP packages: $WKOOP_DETAIL"
    if [[ -n "$WKOOP_MISSING" ]]; then
      echo -e "  Missing: ${YELLOW}${WKOOP_MISSING//,/, }${NC}"
      if confirm "Install the missing WKOOP packages now?"; then
        INST_OUT=$($PYTHON -c "
from kd.prerequisites import install_kd_wkoop_packages
import json
r = install_kd_wkoop_packages(dry_run=False)
print(json.dumps(r))
" 2>/dev/null || echo '{"Success":false,"Detail":"install call failed"}')
        INST_OK=$($PYTHON -c "import sys,json; print('yes' if json.load(sys.stdin).get('Success') else 'no')" <<<"$INST_OUT" 2>/dev/null || echo no)
        INST_DETAIL=$($PYTHON -c "import sys,json; print(json.load(sys.stdin).get('Detail',''))" <<<"$INST_OUT" 2>/dev/null || echo "")
        if [[ "$INST_OK" == "yes" ]]; then
          ok "WKOOP install: $INST_DETAIL"
        else
          warn "WKOOP install: $INST_DETAIL"
          warn "  Components that use WKOOP (Web Connector, NiFi HTML processors, CFS, View) may still work if the core libraries are present."
        fi
      else
        warn "Skipped WKOOP package install. You can re-run this script later or install manually."
        warn "  Example (Ubuntu): sudo apt install libatomic1 libx11-6 libx11-xcb1 libxtst6 libxss1 libxcomposite1 libatk1.0-0t64 at-spi2-core libatk-bridge2.0-0 libcups2 libcairo2 libpango-1.0-0 libpangocairo-1.0-0"
      fi
    fi
  fi
else
  warn "Python not available – skipped OS dependency check"
fi

echo
if $overall_ok; then
  echo -e "${GREEN}Environment preparation completed successfully.${NC}"
  if [[ -x "$VENV_DIR/bin/python" ]]; then
    echo "Toolkit Python: $VENV_DIR/bin/python  (used automatically by install-kd.sh / install-kd-menu.sh)"
  fi
  echo "Next: run ./install-kd.sh  (or python3 install_kd.py --help)"
  exit 0
else
  echo -e "${RED}Environment preparation finished with problems. Fix the [FAIL] items above.${NC}"
  exit 1
fi
