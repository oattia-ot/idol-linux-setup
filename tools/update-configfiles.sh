#!/usr/bin/env bash
# update-configfiles.sh - apply replacements from tools/replacements.json
# Section-aware (optional "section" key restricts replace to INI [Section] body).
# Mirrors tools/Update-ConfigFiles.ps1 behaviour from the Windows toolkit.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
REPL="$SCRIPT_DIR/replacements.json"
DRY_RUN=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run|-DryRun) DRY_RUN=1; shift ;;
    --replacements|-ReplacementsPath) REPL="$2"; shift 2 ;;
    --setup-root|-SetupRoot) REPO_ROOT="$2"; shift 2 ;;
    *) shift ;;
  esac
done

if [[ ! -f "$REPL" ]]; then
  echo "ERROR: replacements.json not found: $REPL"
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
  echo "ERROR: Python required for JSON replacements"
  exit 1
fi

echo ""
echo "  Update configuration files (ports, API key, etc.)"
echo "  Setup root : $REPO_ROOT"
echo "  JSON       : $REPL"
if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "  Mode       : DRY-RUN (no writes)"
fi
echo ""

$PYTHON - "$REPL" "$REPO_ROOT" "$DRY_RUN" <<'PY'
import json
import re
import sys
import shutil
from pathlib import Path

repl_path = Path(sys.argv[1])
root = Path(sys.argv[2])
dry = sys.argv[3] == "1"

data = json.loads(repl_path.read_text(encoding="utf-8"))
entries = data if isinstance(data, list) else data.get("replacements", data.get("items", []))

def section_body_span(text: str, section: str):
    """Return (start, end) of the body of [section], or None.
    Body starts after the header line and ends at the next [Header] or EOF.
    """
    if not section:
        return None
    sec_re = re.compile(r"(?m)^\s*\[" + re.escape(section) + r"\]\s*")
    m = sec_re.search(text)
    if not m:
        return None
    body_start = m.end()
    next_hdr = re.search(r"(?m)^\s*\[", text[body_start:])
    body_end = body_start + next_hdr.start() if next_hdr else len(text)
    return body_start, body_end

ok_count = 0
change_count = 0
print(f"  Loaded {len(entries)} file entries\n")

for entry in entries:
    if not isinstance(entry, dict):
        continue
    rel = entry.get("file") or entry.get("path") or entry.get("File")
    if not rel:
        continue
    path = root / rel
    if not path.is_file():
        print(f"  [ERROR] File not found: {path}")
        continue

    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        print(f"  [ERROR] Cannot read {path}: {e}")
        continue

    original = content
    total = 0
    skipped_reps = 0

    for rep in entry.get("replacements") or []:
        if not isinstance(rep, dict):
            continue
        frm = str(rep.get("from") or rep.get("From") or "")
        to = str(rep.get("to") or rep.get("To") or "")
        if not frm:
            continue
        # Skip no-op (from == to)
        if frm == to:
            skipped_reps += 1
            continue

        section = str(rep.get("section") or "").strip() or None
        n = 0

        if section:
            span = section_body_span(content, section)
            if span is None:
                skipped_reps += 1
                continue
            before = content[: span[0]]
            sec_text = content[span[0] : span[1]]
            after = content[span[1] :]
            if frm not in sec_text:
                skipped_reps += 1
                continue
            new_sec = sec_text.replace(frm, to)
            total += 1
            content = before + new_sec + after
        else:
            if frm not in content:
                skipped_reps += 1
                continue
            content = content.replace(frm, to)
            total += 1

    if content == original:
        extra = f"  [{skipped_reps} skipped]" if skipped_reps else ""
        print(f"  [SKIP]  {rel}  (no changes){extra}")
        ok_count += 1
        continue

    if dry:
        print(f"  [DryRun] would update: {rel}  ({total} replacement(s))")
        ok_count += 1
        change_count += 1
        continue

    try:
        bak = path.with_suffix(path.suffix + ".bak")
        if not bak.exists():
            shutil.copy2(path, bak)
        path.write_text(content, encoding="utf-8")
        extra = f"  [{skipped_reps} skipped]" if skipped_reps else ""
        print(f"  [OK]    {rel}  ({total} replacement(s)){extra}")
        ok_count += 1
        change_count += 1
    except Exception as e:
        print(f"  [ERROR] Cannot write {path}: {e}")

print(f"\n  File replacements finished. {ok_count} / {len(entries)} entries OK  ({change_count} changed).")
PY
