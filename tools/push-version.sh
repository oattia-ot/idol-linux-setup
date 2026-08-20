#!/usr/bin/env bash
# ==============================================================================
#  push-version.sh
#  Write VERSION.txt, ensure git/remote, commit, and push to GitHub (Linux).
#
#  All interactive input uses: read -r
#
#  Usage:
#    ./tools/push-version.sh --version ver-0.2r1m0
#    ./tools/push-version.sh --version ver-0.2r1m0 --message "Fix units"
#    ./tools/push-version.sh ver-0.2r1m0
#    ./tools/push-version.sh              # prompts for version via read -r
#    KD_VERSION=ver-0.2r1m0 ./tools/push-version.sh
#    ./tools/push-version.sh --version ver-0.2r1m0 -y
#    ./tools/push-version.sh --version ver-0.2r1m0 --dry-run
# ==============================================================================

set -euo pipefail

YELLOW='\033[1;33m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Repo root: prefer a directory that already has .git
if [[ -d "$SCRIPT_DIR/.git" ]]; then
  REPO_ROOT="$SCRIPT_DIR"
elif [[ -d "$SCRIPT_DIR/../.git" ]]; then
  REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
elif [[ "$(basename "$SCRIPT_DIR")" == "tools" ]]; then
  REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
else
  REPO_ROOT="$SCRIPT_DIR"
fi

cd "$REPO_ROOT"

DEFAULT_REMOTE_URL="https://github.com/oattia-ot/idol-linux-setup.git"
VERSION="${KD_VERSION:-${VERSION:-}}"
MESSAGE=""
REMOTE_NAME="origin"
BRANCH=""
REMOTE_URL="$DEFAULT_REMOTE_URL"
YES_ALL=false
DRY_RUN=false

usage() {
  cat <<EOF
Usage: $0 [--version <ver>] [options]

Options:
  -Version,  --version <ver>    Version written to VERSION.txt
  -Message,  --message <msg>    Commit message (default: "Bump version to <ver>")
  -Remote,   --remote <name>    Git remote name (default: origin)
  -Branch,   --branch <name>    Branch to push (default: current / main)
  -Url,      --url <url>        Remote URL (default: $DEFAULT_REMOTE_URL)
  -y, --yes                     Auto-confirm all prompts
  --dry-run                     Show plan only; make no changes
  -h, --help                    Show this help

If --version is omitted, the script prompts with read -r.

Environment:
  KD_VERSION / VERSION          Fallback if --version is omitted
EOF
}

# --------------------------------------------------------------------------
# Interactive helpers — all use read -r
# --------------------------------------------------------------------------
confirm() {
  local prompt="$1"
  local reply=""
  if $YES_ALL; then
    echo "  [yes] $prompt"
    return 0
  fi
  if $DRY_RUN; then
    echo "  [dry-run] would ask: $prompt"
    return 0
  fi
  while true; do
    printf "  ${YELLOW}%s [y/N] ${NC}" "$prompt"
    read -r reply || reply="n"
    case "${reply}" in
      [yY]|[yY][eE][sS]) return 0 ;;
      [nN]|[nN][oO]|"")  return 1 ;;
      *) echo "    Please answer y or n." ;;
    esac
  done
}

prompt_value() {
  # prompt_value "Prompt text" "default"  -> echoes answer to stdout
  local prompt="$1"
  local default="${2:-}"
  local reply=""
  if [[ -n "$default" ]]; then
    printf "  ${YELLOW}%s [%s]: ${NC}" "$prompt" "$default"
  else
    printf "  ${YELLOW}%s: ${NC}" "$prompt"
  fi
  read -r reply || reply=""
  if [[ -z "$reply" && -n "$default" ]]; then
    reply="$default"
  fi
  printf '%s' "$reply"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -Version|--version)  VERSION="${2:-}"; shift 2 ;;
    -Message|--message)  MESSAGE="${2:-}"; shift 2 ;;
    -Remote|--remote)    REMOTE_NAME="${2:-}"; shift 2 ;;
    -Branch|--branch)    BRANCH="${2:-}"; shift 2 ;;
    -Url|--url)          REMOTE_URL="${2:-}"; shift 2 ;;
    -y|--yes)            YES_ALL=true; shift ;;
    --dry-run)           DRY_RUN=true; shift ;;
    -h|--help)           usage; exit 0 ;;
    *)
      if [[ -z "$VERSION" && "$1" != -* ]]; then
        VERSION="$1"; shift
      else
        echo "Unknown argument: $1" >&2
        usage
        exit 1
      fi
      ;;
  esac
done

# Prompt for version if still empty
if [[ -z "$VERSION" ]]; then
  if $YES_ALL || $DRY_RUN; then
    echo "ERROR: version is required (use --version or KD_VERSION)." >&2
    usage
    exit 1
  fi
  echo "Version not specified."
  VERSION="$(prompt_value "Enter version" "")"
  echo
  if [[ -z "$VERSION" ]]; then
    echo "ERROR: version is required." >&2
    exit 1
  fi
fi

if [[ -z "$MESSAGE" ]]; then
  MESSAGE="Bump version to $VERSION"
fi

echo "============================================================"
echo "  push-version.sh"
echo "============================================================"
echo "  Repo root : $REPO_ROOT"
echo "  Version   : $VERSION"
echo "  Message   : $MESSAGE"
echo "  Remote    : $REMOTE_NAME → $REMOTE_URL"
$DRY_RUN && echo "  Mode      : DRY-RUN"
$YES_ALL && echo "  Mode      : auto-confirm (-y)"
echo "============================================================"
echo

# --------------------------------------------------------------------------
# [1] git installed?
# --------------------------------------------------------------------------
echo "[1/6] Checking git ..."
if ! command -v git >/dev/null 2>&1; then
  echo "  [MISSING] git is not installed."
  if confirm "Install git now (apt/dnf/zypper)?"; then
    if $DRY_RUN; then
      echo "  [dry-run] would install git"
    else
      if command -v apt-get >/dev/null 2>&1; then
        sudo apt-get update -y && sudo apt-get install -y git
      elif command -v dnf >/dev/null 2>&1; then
        sudo dnf install -y git
      elif command -v zypper >/dev/null 2>&1; then
        sudo zypper install -y git
      else
        echo "  ERROR: install git manually, then re-run." >&2
        exit 1
      fi
    fi
  else
    echo "  Aborted — git is required."
    exit 1
  fi
else
  echo "  [OK] $(git --version)"
fi
echo

# --------------------------------------------------------------------------
# [2] git repository
# --------------------------------------------------------------------------
echo "[2/6] Checking git repository ..."
if [[ ! -d "$REPO_ROOT/.git" ]]; then
  echo "  [MISSING] No .git in: $REPO_ROOT"
  echo "  (Old script printed: Not a git repository; VERSION.txt updated only.)"
  if confirm "Run 'git init' and create branch 'main' here?"; then
    if $DRY_RUN; then
      echo "  [dry-run] git init && git branch -M main"
    else
      git init
      git branch -M main
      echo "  [OK] Repository initialized at $REPO_ROOT"
    fi
  else
    echo "  Aborted — cannot commit/push without a git repository."
    exit 1
  fi
else
  echo "  [OK] Git repository present."
fi
echo

# --------------------------------------------------------------------------
# [3] remote
# --------------------------------------------------------------------------
echo "[3/6] Checking remote '$REMOTE_NAME' ..."
if ! git remote get-url "$REMOTE_NAME" &>/dev/null; then
  echo "  [MISSING] Remote '$REMOTE_NAME' is not configured."
  if confirm "Add remote '$REMOTE_NAME' → $REMOTE_URL ?"; then
    if $DRY_RUN; then
      echo "  [dry-run] git remote add $REMOTE_NAME $REMOTE_URL"
    else
      git remote add "$REMOTE_NAME" "$REMOTE_URL"
      echo "  [OK] Remote added."
    fi
  else
    custom_url=""
    printf "  ${YELLOW}Enter a different remote URL (empty = abort): ${NC}"
    read -r custom_url || custom_url=""
    if [[ -z "${custom_url}" ]]; then
      echo "  Aborted — remote required to push."
      exit 1
    fi
    if ! $DRY_RUN; then
      git remote add "$REMOTE_NAME" "$custom_url"
      REMOTE_URL="$custom_url"
      echo "  [OK] Remote added → $custom_url"
    fi
  fi
else
  current_url="$(git remote get-url "$REMOTE_NAME")"
  echo "  [OK] $REMOTE_NAME → $current_url"
  if [[ "$current_url" != "$REMOTE_URL" ]]; then
    if confirm "Update '$REMOTE_NAME' URL to $REMOTE_URL ?"; then
      if ! $DRY_RUN; then
        git remote set-url "$REMOTE_NAME" "$REMOTE_URL"
        echo "  [OK] Remote URL updated."
      fi
    fi
  fi
fi
echo

# --------------------------------------------------------------------------
# [4] .gitignore
# --------------------------------------------------------------------------
echo "[4/6] Checking .gitignore ..."
GITIGNORE="$REPO_ROOT/.gitignore"
if [[ ! -f "$GITIGNORE" ]]; then
  echo "  [MISSING] .gitignore not found."
  if confirm "Create default .gitignore (skips env/, logs/, *.zip, …)?"; then
    if ! $DRY_RUN; then
      cat > "$GITIGNORE" <<'EOF'
# Local / generated
env/
.venv/
__pycache__/
*.pyc
logs/
ssl/
*.log

# Large vendor packages
*.zip
*.msi
*.exe
*.nar
nifi/nifi-connectors/*.nar

# Editor / OS
.DS_Store
.idea/
.vscode/
*.swp
EOF
      echo "  [OK] .gitignore created."
    fi
  else
    echo "  [SKIP] Continuing without .gitignore."
  fi
else
  echo "  [OK] .gitignore exists."
fi
echo

# --------------------------------------------------------------------------
# [5] VERSION.txt + commit
# --------------------------------------------------------------------------
echo "[5/6] VERSION.txt and commit ..."
if [[ -f "$REPO_ROOT/VERSION.txt" ]]; then
  echo "  Current: $(tr -d '\r\n' < "$REPO_ROOT/VERSION.txt" 2>/dev/null || echo "(empty)")"
fi
if confirm "Write VERSION.txt = $VERSION ?"; then
  if $DRY_RUN; then
    echo "  [dry-run] would write VERSION.txt = $VERSION"
  else
    printf '%s\n' "$VERSION" > "$REPO_ROOT/VERSION.txt"
    echo "  [OK] Wrote VERSION.txt = $VERSION"
  fi
else
  echo "  [SKIP] VERSION.txt not changed."
fi

if $DRY_RUN; then
  echo "  [dry-run] git add -A && git commit"
else
  git add -A
  git status --short || true
  if git diff --cached --quiet; then
    echo "  [OK] Nothing new to commit."
  else
    if confirm "Commit with message: \"$MESSAGE\" ?"; then
      git commit -m "$MESSAGE"
      echo "  [OK] Commit created."
    else
      echo "  [SKIP] Commit cancelled."
      exit 0
    fi
  fi
fi
echo

# --------------------------------------------------------------------------
# [6] push
# --------------------------------------------------------------------------
echo "[6/6] Push ..."
if [[ -z "$BRANCH" ]]; then
  BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo main)"
fi
echo "  Branch: $BRANCH"
echo "  Remote: $REMOTE_NAME ($(git remote get-url "$REMOTE_NAME" 2>/dev/null || echo "$REMOTE_URL"))"

if confirm "Push '$BRANCH' to '$REMOTE_NAME'?"; then
  if $DRY_RUN; then
    echo "  [dry-run] git push -u $REMOTE_NAME $BRANCH"
  else
    if git push -u "$REMOTE_NAME" "$BRANCH"; then
      echo "  [OK] Push succeeded → $(git remote get-url "$REMOTE_NAME")"
    else
      echo "  [FAILED] Push failed." >&2
      echo "  HTTPS: use a GitHub Personal Access Token as the password." >&2
      echo "  SSH:   git remote set-url $REMOTE_NAME git@github.com:oattia-ot/idol-linux-setup.git" >&2
      exit 1
    fi
  fi
else
  echo "  [SKIP] Push cancelled."
fi

echo
echo "Done."
