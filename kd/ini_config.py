"""
Safe .cfg / .ini editing for OpenText KD component configs.

Design goals:
- Section-scoped key updates (never touch the same key name in another section)
- Never glue a value onto the next ``[Section]`` header (classic corruption:
  ``LicenseServerHost=localhost[License]``)
- Preserve the file's dominant line ending (CRLF on Windows packages)
- Backup before write; atomic temp + replace
- Escape section/key for any residual regex use (original PS bug fixed)
"""

from __future__ import annotations

import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple


def backup_kd_file(file_path: str | Path) -> Optional[Path]:
    path = Path(file_path)
    if not path.is_file():
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = path.with_suffix(path.suffix + f".bak-{stamp}")
    shutil.copy2(path, backup)
    return backup


def restore_kd_file_from_backup(file_path: str | Path, backup_path: str | Path) -> bool:
    try:
        shutil.copy2(backup_path, file_path)
        return True
    except Exception:
        return False


def _detect_newline(content: str) -> str:
    """Prefer CRLF when the file already uses it (OpenText Windows packages)."""
    crlf = content.count("\r\n")
    # Count lone LF (not part of CRLF)
    lf_only = content.count("\n") - crlf
    if crlf >= lf_only and crlf > 0:
        return "\r\n"
    if content.count("\r") > 0 and crlf == 0 and lf_only == 0:
        return "\r"
    return "\n"


def _split_lines(content: str) -> Tuple[List[str], str]:
    """
    Split into logical lines **without** line-ending characters.
    Returns (lines, newline) where newline is the dominant ending to use on write.
    """
    newline = _detect_newline(content)
    # Normalize to \n for splitting, then drop trailing empty line caused by
    # a final terminator so we can re-join cleanly.
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    ends_with_nl = normalized.endswith("\n")
    if ends_with_nl:
        normalized = normalized[:-1]
    lines = normalized.split("\n") if normalized or content else []
    return lines, newline


def _is_section_header(line: str) -> bool:
    s = line.strip()
    return len(s) >= 2 and s.startswith("[") and s.endswith("]")


def _section_name(line: str) -> str:
    return line.strip()[1:-1].strip()


def _key_match(line: str, key: str) -> bool:
    """True if line is ``Key=...`` or ``Key = ...`` (case-sensitive, KD style)."""
    s = line.lstrip()
    if s.startswith("#") or s.startswith(";"):
        return False
    eq = s.find("=")
    if eq < 0:
        return False
    return s[:eq].rstrip() == key


def _set_key_value(line: str, key: str, value: str) -> str:
    """Preserve indentation and spacing around ``=`` when replacing a value."""
    # Match leading whitespace + key + optional spaces around =
    m = re.match(rf"^(\s*{re.escape(key)}\s*=\s*)(.*)$", line)
    if m:
        return m.group(1) + value
    return f"{key}={value}"


def update_kd_ini_file(
    file_path: str | Path,
    section: str,
    key: str,
    value: str,
    no_backup: bool = False,
) -> bool:
    """
    Set ``key=value`` under ``[section]`` in an INI-style .cfg file.

    - Creates the section at EOF if missing.
    - Updates the key only inside that section (other sections untouched).
    - Inserts the key before the next section header when adding.
    - Guarantees a newline between a value and any following ``[Section]``.
    """
    path = Path(file_path)
    if not path.is_file():
        return False

    try:
        # newline="" is required: default universal-newline mode turns CRLF
        # into "\n" on read, so we would rewrite OpenText Windows packages
        # with Unix LF and can corrupt values against the next [Section].
        # Use open() — Path.read_text() does not accept newline= on all Pythons.
        with path.open("r", encoding="utf-8", errors="replace", newline="") as fh:
            raw = fh.read()
        lines, newline = _split_lines(raw)

        if not no_backup:
            backup_kd_file(path)

        # Locate [section] block: [start_index, end_index)
        section_start: Optional[int] = None
        section_end: Optional[int] = None
        for i, line in enumerate(lines):
            if _is_section_header(line) and _section_name(line) == section:
                section_start = i
                section_end = len(lines)
                for j in range(i + 1, len(lines)):
                    if _is_section_header(lines[j]):
                        section_end = j
                        break
                break

        if section_start is None:
            # Append a new section at EOF (with a blank line separator if needed)
            if lines and lines[-1].strip() != "":
                lines.append("")
            lines.append(f"[{section}]")
            lines.append(f"{key}={value}")
        else:
            # Search for key only inside this section
            key_index: Optional[int] = None
            for i in range(section_start + 1, section_end or len(lines)):
                if _key_match(lines[i], key):
                    key_index = i
                    break

            if key_index is not None:
                lines[key_index] = _set_key_value(lines[key_index], key, value)
            else:
                # Insert before the next section (or at EOF of this section).
                # Skip trailing blank lines inside the section so the new key
                # sits with the other keys, not after a blank gap.
                insert_at = section_end if section_end is not None else len(lines)
                while insert_at > section_start + 1 and lines[insert_at - 1].strip() == "":
                    insert_at -= 1
                lines.insert(insert_at, f"{key}={value}")

        # Re-join; always terminate the file with a newline (INI convention)
        new_content = newline.join(lines) + newline

        # Safety net: if any value was previously glued to a section header
        # (e.g. "localhost[License]"), split them back apart.
        new_content = re.sub(
            r"(=)([^\r\n\[]+)\[([^\]]+)\]",
            rf"\1\2{newline}[\3]",
            new_content,
        )

        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8", newline="") as fh:
            fh.write(new_content)
        tmp.replace(path)
        return True
    except Exception:
        return False
