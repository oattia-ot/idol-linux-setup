#!/usr/bin/env bash
# Ensure unzip / tar are available (Linux equivalent of Ensure-ExtractTools.bat)
set -euo pipefail

QUIET=0
if [[ "${1:-}" == "--quiet" || "${1:-}" == "-q" ]]; then
  QUIET=1
fi

has_tool() { command -v "$1" &>/dev/null; }

if has_tool unzip || has_tool tar; then
  [[ $QUIET -eq 0 ]] && echo "[OK] Extraction tools available (unzip/tar)"
  exit 0
fi

[[ $QUIET -eq 0 ]] && echo "No unzip/tar found. Attempting to install unzip..."

if has_tool apt-get; then
  sudo apt-get update -qq && sudo apt-get install -y unzip
elif has_tool dnf; then
  sudo dnf install -y unzip
elif has_tool yum; then
  sudo yum install -y unzip
elif has_tool zypper; then
  sudo zypper install -y unzip
elif has_tool pacman; then
  sudo pacman -S --noconfirm unzip
else
  echo "[FAIL] Could not determine package manager. Install 'unzip' manually."
  exit 1
fi

if has_tool unzip || has_tool tar; then
  [[ $QUIET -eq 0 ]] && echo "[OK] unzip installed"
  exit 0
fi
echo "[FAIL] unzip still not found after install attempt."
exit 1
