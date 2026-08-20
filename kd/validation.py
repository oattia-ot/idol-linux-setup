"""
Pre-flight environment validation.
"""

from __future__ import annotations

import hashlib
import platform
import re
import socket
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .logging import log

# Optional rich console for colored SHA-256 report (same stack as kd.logging)
try:
    from rich.console import Console
    from rich.text import Text

    _rich_console = Console(highlight=False)
    _HAS_RICH = True
except ImportError:
    _rich_console = None
    _HAS_RICH = False

# ANSI fallbacks
_ANSI_BLUE = "\033[94m"
_ANSI_CYAN = "\033[96m"
_ANSI_PURPLE = "\033[95m"
_ANSI_GREEN = "\033[92m"
_ANSI_RED = "\033[91m"
_ANSI_YELLOW = "\033[93m"
_ANSI_BOLD = "\033[1m"
_ANSI_RESET = "\033[0m"
_ANSI_DIM = "\033[2m"

# Linux / cross-platform tip to generate sha256.txt next to the ZIP packages.
# Prefer sha256sum (coreutils); fall back to openssl.
_LINUX_SHA256_HINT_LINES = (
    "Command to verify ZIP file integrity (run on the download machine)",
    "",
    "Run this inside the folder that contains the downloaded ZIP files.",
    "Before running:",
    "  1. cd into the folder containing the downloaded ZIP files.",
    "  2. Make sure all component ZIP files are in this folder.",
    "  3. Run ONE of the commands below (prefer sha256sum).",
    "  4. Compare the generated checksums with the vendor-published values.",
    "",
    "Preferred — Linux / macOS (sha256sum, coreutils):",
    "  sha256sum -- *.zip > sha256.txt",
    "",
    "Alternative — openssl (when sha256sum is unavailable):",
    "  # Use a subshell + printf so VS Code / shell-integration OSC sequences",
    "  # never pollute the first line of sha256.txt.",
    "  ( for f in *.zip; do",
    "      [ -f \"$f\" ] || continue",
    "      h=$(openssl dgst -sha256 \"$f\" 2>/dev/null | awk '{print $NF}')",
    "      [ -n \"$h\" ] && printf '%s  %s\n' \"$h\" \"$f\"",
    "    done",
    "  ) > sha256.txt",
    "",
    "PowerShell (if generating on Windows before copying to Linux):",
    "  Get-ChildItem -File -Filter *.zip |",
    "    Get-FileHash -Algorithm SHA256 |",
    "    ForEach-Object { \"$($_.Hash)  $($_.Path | Split-Path -Leaf)\" } |",
    "    Set-Content -Encoding utf8 sha256.txt",
)



def test_kd_admin_rights() -> Dict[str, Any]:
    is_admin = False
    detail = "Unable to determine elevation status"
    try:
        import ctypes
        is_admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
        detail = "Running elevated" if is_admin else "Script must be run as Administrator"
    except Exception:
        # Non-Windows or restricted
        detail = "Admin check only meaningful on Windows"
        is_admin = platform.system() != "Windows"  # allow non-Windows for dry-run testing
    return {
        "Name": "Administrator privileges",
        "Pass": is_admin,
        "Detail": detail,
    }


def test_kd_operating_system() -> Dict[str, Any]:
    is_win64 = platform.system() == "Windows" and platform.machine().endswith("64")
    detail = f"{platform.system()} {platform.release()} ({platform.machine()})"
    return {
        "Name": "Operating system (Windows x64)",
        "Pass": is_win64 or platform.system() != "Windows",  # allow non-Windows for development
        "Detail": detail,
    }


def test_kd_powershell_version() -> Dict[str, Any]:
    # Python equivalent: just check Python version
    ver = sys.version_info
    pass_ = ver >= (3, 8)
    return {
        "Name": "Python version",
        "Pass": pass_,
        "Detail": f"Current: {ver.major}.{ver.minor}.{ver.micro}, Required: >= 3.8",
    }


def test_kd_memory(minimum_gb: int = 4) -> Dict[str, Any]:
    total_gb = 0.0
    try:
        import psutil
        total_gb = round(psutil.virtual_memory().total / (1024 ** 3), 1)
    except ImportError:
        # Fallback rough estimate via ctypes on Windows
        try:
            import ctypes
            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]
            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(stat)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
            total_gb = round(stat.ullTotalPhys / (1024 ** 3), 1)
        except Exception:
            return {
                "Name": "System memory",
                "Pass": True,  # don't block if we can't query
                "Detail": "Unable to query memory (skipped)",
            }
    pass_ = total_gb >= minimum_gb
    return {
        "Name": "System memory",
        "Pass": pass_,
        "Detail": f"Total: {total_gb}GB, Required: {minimum_gb}GB",
    }


def test_kd_disk_space(path: str, minimum_gb: int = 10) -> Dict[str, Any]:
    try:
        import shutil
        # Use the drive of the path even if the folder does not exist yet
        p = Path(path)
        # On Windows Path.drive works; on Linux we just use /
        target = p.drive + "\\" if p.drive else "/"
        usage = shutil.disk_usage(target if Path(target).exists() else str(p.parent or "/"))
        free_gb = round(usage.free / (1024 ** 3), 1)
        pass_ = free_gb >= minimum_gb
        return {
            "Name": "Disk space",
            "Pass": pass_,
            "Detail": f"Free: {free_gb}GB, Required: {minimum_gb}GB",
        }
    except Exception as e:
        return {
            "Name": "Disk space",
            "Pass": False,
            "Detail": f"Could not determine free space: {e}",
        }


def test_kd_port_available(port: int) -> Dict[str, Any]:
    in_use = False
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("0.0.0.0", port))
    except OSError:
        in_use = True
    return {
        "Name": f"Port {port} available",
        "Pass": not in_use,
        "Detail": f"Port {port} is {'already in use' if in_use else 'free'}",
    }


def test_kd_zip_source_reachable(zip_path: str) -> Dict[str, Any]:
    p = Path(zip_path)
    exists = p.is_dir()
    has_zips = False
    if exists:
        has_zips = any(p.glob("*.zip"))
    if not exists:
        detail = f"Path not found: {zip_path}"
    elif not has_zips:
        detail = f"No .zip files found in {zip_path}"
    else:
        detail = f"Found packages in {zip_path}"
    return {
        "Name": "ZIP package source",
        "Pass": exists and has_zips,
        "Detail": detail,
    }


def test_kd_package_versions(zip_path: str, base_path: str = "") -> Dict[str, Any]:
    """
    Parse major.minor from KD ZIP names and warn on mismatches / BasePath drift.

    Pass is True when ZipPath is empty/missing (skipped) or when a consensus
    version exists. Version mismatches are reported as Warning=True but still
    Pass so install can continue with operator awareness.
    """
    from . import discovery

    if not zip_path:
        return {
            "Name": "KD package version (ZIP names)",
            "Pass": True,
            "Warning": False,
            "Detail": "ZipPath not set — skipped version scan",
            "Analysis": None,
        }

    analysis = discovery.analyze_kd_zip_versions(zip_path)
    warnings = list(analysis.get("Warnings") or [])
    mm = analysis.get("MajorMinor")
    suggested = analysis.get("SuggestedBasePath")

    # BasePath must NOT include a version leaf (26.2 / 26.3). Flag if it still does.
    if base_path:
        norm_base = str(base_path).replace("\\", "/").rstrip("/")
        if re.search(r"(?i)KnowledgeDiscovery/\d+\.\d+$", norm_base):
            warnings.append(
                f"BasePath '{base_path}' includes a version folder; "
                f"use the install root only (e.g. /opt/KnowledgeDiscovery), not …/26.x"
            )

    if not analysis.get("Success"):
        # No parseable versions: soft warning, do not fail the install
        detail = "; ".join(warnings) if warnings else (analysis.get("Detail") or "No version info")
        return {
            "Name": "KD package version (ZIP names)",
            "Pass": True,
            "Warning": bool(warnings),
            "Detail": detail,
            "Analysis": analysis,
        }

    detail_parts = [analysis.get("Detail") or f"Consensus {mm}"]
    if suggested:
        detail_parts.append(f"Suggested BasePath: {suggested}")
    if warnings:
        detail_parts.extend(warnings)

    return {
        "Name": "KD package version (ZIP names)",
        "Pass": True,
        "Warning": bool(warnings),
        "Detail": " | ".join(detail_parts),
        "Analysis": analysis,
    }


def test_kd_environment(config: Dict[str, Any], required_ports: List[int] | None = None) -> Dict[str, Any]:
    results: List[Dict[str, Any]] = []
    results.append(test_kd_admin_rights())
    results.append(test_kd_operating_system())
    results.append(test_kd_powershell_version())  # actually Python version
    results.append(test_kd_memory(4))
    results.append(test_kd_disk_space(config.get("BasePath", "C:\\"), 10))
    results.append(test_kd_zip_source_reachable(config.get("ZipPath", "")))
    results.append(
        test_kd_package_versions(
            config.get("ZipPath", ""),
            base_path=str(config.get("BasePath") or ""),
        )
    )
    if required_ports:
        for p in required_ports:
            results.append(test_kd_port_available(int(p)))
    failed = [r for r in results if not r["Pass"]]
    return {
        "AllPassed": len(failed) == 0,
        "Results": results,
        "Failed": failed,
    }


# ---------------------------------------------------------------------------
# Pre-extraction ZIP SHA-256 integrity gate
# ---------------------------------------------------------------------------

def compute_file_sha256(path: Path, chunk_size: int = 1024 * 1024) -> str:
    """
    Compute the SHA-256 hex digest of a file using buffered reads.
    Suitable for multi-GB OpenText component ZIP packages.
    """
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def generate_zip_sha_report(
    zip_path: str | Path,
) -> Dict[str, Any]:
    """
    Generate SHA-256 hashes for every .zip under ZipPath.

    Returns a dict with:
      Success, Hashes (list of {FileName, Path, SizeMB, SHA256}), Detail
    """
    zip_dir = Path(zip_path) if zip_path else Path()
    if not zip_dir.is_dir():
        return {
            "Success": False,
            "Hashes": [],
            "Detail": f"ZipPath not found or not a directory: {zip_path}",
        }

    zips = sorted(zip_dir.glob("*.zip"))
    if not zips:
        return {
            "Success": True,
            "Hashes": [],
            "Detail": f"No .zip files found in {zip_dir}",
        }

    hashes: List[Dict[str, Any]] = []
    log.info(f"Computing SHA-256 for {len(zips)} ZIP package(s) in {zip_dir} ...")
    for z in zips:
        try:
            size_mb = z.stat().st_size / (1024 * 1024)
            log.info(f"  Hashing {z.name} ({size_mb:.1f} MB) ...")
            sha = compute_file_sha256(z)
            entry = {
                "FileName": z.name,
                "Path": str(z.resolve()),
                "SizeMB": round(size_mb, 2),
                "SHA256": sha,
            }
            hashes.append(entry)
            # Print hash via ANSI/rich console — do NOT put markup in log.info
            # (KDLogger escapes Rich tags so they would appear literally).
            _print_hash_line(z.name, sha)
        except Exception as e:
            log.error(f"  Failed to hash {z.name}: {e}")
            hashes.append(
                {
                    "FileName": z.name,
                    "Path": str(z),
                    "SizeMB": 0.0,
                    "SHA256": None,
                    "Error": str(e),
                }
            )

    ok = all(h.get("SHA256") for h in hashes)
    return {
        "Success": ok,
        "Hashes": hashes,
        "Detail": f"Hashed {len(hashes)} package(s)" + ("" if ok else " (some failures)"),
    }


def _print_blue(msg: str) -> None:
    if _HAS_RICH and _rich_console is not None:
        _rich_console.print(f"[bold blue]{msg}[/bold blue]")
    else:
        print(f"{_ANSI_BLUE}{_ANSI_BOLD}{msg}{_ANSI_RESET}")


def _print_purple(msg: str) -> None:
    if _HAS_RICH and _rich_console is not None:
        _rich_console.print(f"[bold magenta]{msg}[/bold magenta]")
    else:
        print(f"{_ANSI_PURPLE}{_ANSI_BOLD}{msg}{_ANSI_RESET}")


def _print_green(msg: str) -> None:
    if _HAS_RICH and _rich_console is not None:
        _rich_console.print(f"[bold green]{msg}[/bold green]")
    else:
        print(f"{_ANSI_GREEN}{_ANSI_BOLD}{msg}{_ANSI_RESET}")


def _print_red(msg: str) -> None:
    if _HAS_RICH and _rich_console is not None:
        _rich_console.print(f"[bold red]{msg}[/bold red]")
    else:
        print(f"{_ANSI_RED}{_ANSI_BOLD}{msg}{_ANSI_RESET}")


def _print_hash_line(filename: str, sha: str) -> None:
    """Print filename + bold cyan SHA-256 (ANSI or rich console, never log markup)."""
    if _HAS_RICH and _rich_console is not None:
        _rich_console.print(f"    {filename}: [bold cyan]{sha}[/bold cyan]")
    else:
        print(f"    {filename}: {_ANSI_CYAN}{_ANSI_BOLD}{sha}{_ANSI_RESET}")


def _print_sha256_hint() -> None:
    """Show purple instructions + the command to generate sha256.txt."""
    if _HAS_RICH and _rich_console is not None:
        _rich_console.print()
        for line in _LINUX_SHA256_HINT_LINES:
            if line == "":
                _rich_console.print()
            else:
                _rich_console.print(f"[bold magenta]{line}[/bold magenta]")
        _rich_console.print()
    else:
        print()
        for line in _LINUX_SHA256_HINT_LINES:
            print(f"{_ANSI_PURPLE}{_ANSI_BOLD}{line}{_ANSI_RESET}" if line else "")
        print()


def _print_sha_report(zip_dir: Path, hashes: List[Dict[str, Any]]) -> None:
    """Colored report of computed SHA-256 values."""
    sep = "=" * 64
    if _HAS_RICH and _rich_console is not None:
        _rich_console.print()
        _rich_console.print(f"[bold blue]{sep}[/bold blue]")
        _rich_console.print(f"[bold blue]  SHA-256 report — {zip_dir}[/bold blue]")
        _rich_console.print(f"[bold blue]{sep}[/bold blue]")
        for h in hashes:
            name = h.get("FileName") or "?"
            sha = h.get("SHA256") or (h.get("Error") or "FAILED")
            size = h.get("SizeMB")
            size_s = f"  ({size:.1f} MB)" if isinstance(size, (int, float)) else ""
            _rich_console.print(f"  [cyan]{name}[/cyan]{size_s}")
            _rich_console.print(f"    [bold cyan]{sha}[/bold cyan]")
        _rich_console.print(f"[bold blue]{sep}[/bold blue]")
        _rich_console.print(
            "[blue]  Compare these hashes with the values published by the download[/blue]"
        )
        _rich_console.print(
            "[blue]  source (OpenText / vendor portal) to confirm package integrity.[/blue]"
        )
        _rich_console.print(f"[bold blue]{sep}[/bold blue]")
        _rich_console.print()
    else:
        print()
        print(f"{_ANSI_BLUE}{_ANSI_BOLD}{sep}{_ANSI_RESET}")
        print(f"{_ANSI_BLUE}{_ANSI_BOLD}  SHA-256 report — {zip_dir}{_ANSI_RESET}")
        print(f"{_ANSI_BLUE}{_ANSI_BOLD}{sep}{_ANSI_RESET}")
        for h in hashes:
            name = h.get("FileName") or "?"
            sha = h.get("SHA256") or (h.get("Error") or "FAILED")
            size = h.get("SizeMB")
            size_s = f"  ({size:.1f} MB)" if isinstance(size, (int, float)) else ""
            print(f"  {_ANSI_CYAN}{name}{_ANSI_RESET}{size_s}")
            print(f"    {_ANSI_CYAN}{_ANSI_BOLD}{sha}{_ANSI_RESET}")
        print(f"{_ANSI_BLUE}{_ANSI_BOLD}{sep}{_ANSI_RESET}")
        print(
            f"{_ANSI_BLUE}  Compare these hashes with the values published by the download{_ANSI_RESET}"
        )
        print(
            f"{_ANSI_BLUE}  source (OpenText / vendor portal) to confirm package integrity.{_ANSI_RESET}"
        )
        print(f"{_ANSI_BLUE}{_ANSI_BOLD}{sep}{_ANSI_RESET}")
        print()


def load_sha256_txt(zip_dir: Path) -> Dict[str, Any]:
    """
    Load expected hashes from ``sha256.txt`` in *zip_dir*.

    Accepted line formats (case-insensitive hash):
      HASH  filename
      HASH *filename          (GNU coreutils style)
      HASH  full/path/file    (basename is used for matching)
    """
    candidates = [
        zip_dir / "sha256.txt",
        zip_dir / "SHA256.txt",
        zip_dir / "sha256sums.txt",
    ]
    path = next((p for p in candidates if p.is_file()), None)
    if path is None:
        return {
            "Found": False,
            "Path": None,
            "Expected": {},
            "Detail": f"No sha256.txt found in {zip_dir}",
        }

    expected: Dict[str, str] = {}
    try:
        text_content = path.read_text(encoding="utf-8-sig", errors="replace")
    except Exception as e:
        return {
            "Found": True,
            "Path": path,
            "Expected": {},
            "Detail": f"Could not read {path.name}: {e}",
        }

    # Strip terminal/VS Code shell-integration noise (OSC 633, ANSI CSI, etc.)
    # that can corrupt the first line when the generate command is pasted
    # into an integrated terminal.
    _noise_re = re.compile(
        r"\x1b\][0-9];.*?\x07"   # OSC ... BEL
        r"|\x1b\[[0-9;]*[A-Za-z]"  # CSI sequences
        r"|\x1b\].*?(\x07|\x1b\\)"  # other OSC
    )

    for raw in text_content.splitlines():
        line = _noise_re.sub("", raw).strip()
        if not line or line.startswith("#"):
            continue
        # Prefer a 64-char hex digest anywhere on the line (survives leading junk)
        m = re.search(
            r"(?i)\b([0-9a-f]{64})\b(?:\s+\*?)\s*(\S+\.(?:zip|ZIP))\s*$",
            line,
        )
        if m:
            digest, name = m.group(1), Path(m.group(2)).name
            expected[name.lower()] = digest.lower()
            continue
        parts = line.split(None, 1)
        if len(parts) < 2:
            continue
        digest, name = parts[0].strip(), parts[1].strip()
        if name.startswith("*"):
            name = name[1:]
        name = Path(name).name
        if len(digest) == 64 and all(c in "0123456789abcdefABCDEF" for c in digest):
            expected[name.lower()] = digest.lower()

    return {
        "Found": True,
        "Path": path,
        "Expected": expected,
        "Detail": f"Loaded {len(expected)} expected hash(es) from {path.name}",
    }


def compare_zip_hashes(
    hashes: List[Dict[str, Any]],
    expected: Dict[str, str],
) -> Dict[str, Any]:
    """
    Compare computed hashes against the expected map from sha256.txt.
    """
    matches: List[Dict[str, Any]] = []
    mismatches: List[Dict[str, Any]] = []
    missing_in_file: List[Dict[str, Any]] = []

    seen_lower: set = set()
    for h in hashes:
        name = h.get("FileName") or "?"
        key = name.lower()
        seen_lower.add(key)
        computed = (h.get("SHA256") or "").lower()
        exp = expected.get(key)

        if exp is None:
            missing_in_file.append({"FileName": name, "Computed": computed or None})
        elif not computed:
            mismatches.append(
                {
                    "FileName": name,
                    "Computed": None,
                    "Expected": exp,
                    "Error": h.get("Error") or "hash failed",
                }
            )
        elif computed == exp:
            matches.append({"FileName": name, "SHA256": computed})
        else:
            mismatches.append(
                {
                    "FileName": name,
                    "Computed": computed,
                    "Expected": exp,
                }
            )

    extra_in_file = [
        {"FileName": name, "Expected": dig}
        for name, dig in expected.items()
        if name not in seen_lower
    ]

    all_match = (
        len(mismatches) == 0
        and len(missing_in_file) == 0
        and len(extra_in_file) == 0
        and len(matches) > 0
    )
    strict_ok = len(mismatches) == 0 and len(matches) + len(missing_in_file) == len(hashes)

    return {
        "AllMatch": all_match,
        "StrictOk": strict_ok and len(mismatches) == 0,
        "Matches": matches,
        "Mismatches": mismatches,
        "MissingInFile": missing_in_file,
        "ExtraInFile": extra_in_file,
    }


def _print_comparison_report(
    zip_dir: Path,
    sha_path: Optional[Path],
    comparison: Dict[str, Any],
) -> None:
    """Print MATCHED (green) / DIFFERENT (red) / missing / extra summary."""
    sep = "-" * 56
    matches = comparison.get("Matches") or []
    mismatches = comparison.get("Mismatches") or []
    missing = comparison.get("MissingInFile") or []
    extra = comparison.get("ExtraInFile") or []
    src = sha_path.name if sha_path else "sha256.txt"

    _print_blue(sep)
    _print_blue(f"  Comparison vs {src}")
    _print_blue(sep)

    if _HAS_RICH and _rich_console is not None:
        _rich_console.print(
            f"  [green]Matches: {len(matches)}[/green]  |  "
            f"[red]Mismatches: {len(mismatches)}[/red]  |  "
            f"[yellow]Missing in file: {len(missing)}[/yellow]  |  "
            f"[yellow]Extra in file: {len(extra)}[/yellow]"
        )
        _rich_console.print()
        if matches:
            _rich_console.print("[bold green]  ✓ MATCHED[/bold green]")
            for m in matches:
                sha = m.get("SHA256") or ""
                _rich_console.print(
                    f"    [green]{m.get('FileName')}[/green]  "
                    f"[bold cyan]{sha}[/bold cyan]"
                )
            _rich_console.print()
        if mismatches:
            _rich_console.print("[bold red]  ✗ DIFFERENT HASHES[/bold red]")
            for m in mismatches:
                _rich_console.print(f"    [red]{m.get('FileName')}[/red]")
                _rich_console.print(
                    f"      computed=[bold cyan]{m.get('Computed')}[/bold cyan]"
                )
                _rich_console.print(
                    f"      expected=[bold cyan]{m.get('Expected')}[/bold cyan]"
                )
            _rich_console.print()
        if missing:
            _rich_console.print("[bold yellow]  ○ missing-in-file (ZIP present, not listed)[/bold yellow]")
            for m in missing:
                _rich_console.print(
                    f"    [yellow]{m.get('FileName')}[/yellow]  "
                    f"[bold cyan]{m.get('Computed') or ''}[/bold cyan]"
                )
            _rich_console.print()
        if extra:
            _rich_console.print("[bold yellow]  ○ extra-in-file (listed, no ZIP)[/bold yellow]")
            for m in extra:
                _rich_console.print(
                    f"    [yellow]{m.get('FileName')}[/yellow]  "
                    f"[bold cyan]{m.get('Expected') or ''}[/bold cyan]"
                )
            _rich_console.print()
    else:
        print(
            f"  {_ANSI_GREEN}Matches: {len(matches)}{_ANSI_RESET}  |  "
            f"{_ANSI_RED}Mismatches: {len(mismatches)}{_ANSI_RESET}  |  "
            f"{_ANSI_YELLOW}Missing in file: {len(missing)}{_ANSI_RESET}  |  "
            f"{_ANSI_YELLOW}Extra in file: {len(extra)}{_ANSI_RESET}"
        )
        print()
        if matches:
            print(f"{_ANSI_GREEN}{_ANSI_BOLD}  ✓ MATCHED{_ANSI_RESET}")
            for m in matches:
                sha = m.get("SHA256") or ""
                print(
                    f"    {_ANSI_GREEN}{m.get('FileName')}{_ANSI_RESET}  "
                    f"{_ANSI_CYAN}{_ANSI_BOLD}{sha}{_ANSI_RESET}"
                )
            print()
        if mismatches:
            print(f"{_ANSI_RED}{_ANSI_BOLD}  ✗ DIFFERENT HASHES{_ANSI_RESET}")
            for m in mismatches:
                print(f"    {_ANSI_RED}{m.get('FileName')}{_ANSI_RESET}")
                print(
                    f"      computed={_ANSI_CYAN}{_ANSI_BOLD}{m.get('Computed')}{_ANSI_RESET}"
                )
                print(
                    f"      expected={_ANSI_CYAN}{_ANSI_BOLD}{m.get('Expected')}{_ANSI_RESET}"
                )
            print()
        if missing:
            print(f"{_ANSI_YELLOW}{_ANSI_BOLD}  ○ missing-in-file (ZIP present, not listed){_ANSI_RESET}")
            for m in missing:
                print(
                    f"    {_ANSI_YELLOW}{m.get('FileName')}{_ANSI_RESET}  "
                    f"{_ANSI_CYAN}{_ANSI_BOLD}{m.get('Computed') or ''}{_ANSI_RESET}"
                )
            print()
        if extra:
            print(f"{_ANSI_YELLOW}{_ANSI_BOLD}  ○ extra-in-file (listed, no ZIP){_ANSI_RESET}")
            for m in extra:
                print(
                    f"    {_ANSI_YELLOW}{m.get('FileName')}{_ANSI_RESET}  "
                    f"{_ANSI_CYAN}{_ANSI_BOLD}{m.get('Expected') or ''}{_ANSI_RESET}"
                )
            print()

    _print_blue(sep)
    print()


def confirm_zip_hashes(
    zip_path: str | Path,
    *,
    non_interactive: bool = False,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Pre-extraction ZIP SHA-256 verification gate.

    1. Compute SHA-256 for every ``*.zip`` under ZipPath.
    2. Load ``sha256.txt`` from the same folder (if present).
    3. Compare computed hashes to the file contents.
    4. Interactive: require yes/y to continue; mismatches listed first.
    5. Non-interactive: abort on hard hash mismatches; continue if all match
       or if sha256.txt is absent (warning only).
    """
    zip_dir = Path(zip_path) if zip_path else Path()
    report = generate_zip_sha_report(zip_dir)
    hashes = report.get("Hashes") or []

    if not hashes:
        log.info("No ZIP packages present — skipping SHA-256 confirmation.")
        return {
            "Success": True,
            "Aborted": False,
            "Hashes": [],
            "Comparison": None,
            "Detail": report.get("Detail") or "No ZIPs to verify",
        }

    _print_sha_report(zip_dir.resolve(), hashes)
    _print_sha256_hint()

    loaded = load_sha256_txt(zip_dir)
    comparison: Optional[Dict[str, Any]] = None

    if not loaded["Found"]:
        log.warn(
            f"No sha256.txt found in {zip_dir}. "
            "Place a downloaded/generated sha256.txt next to the ZIP packages "
            "for automatic comparison."
        )
        _print_blue(
            "  No sha256.txt found — cannot auto-compare. "
            "Generate one with the purple command above, then re-run."
        )
    else:
        log.info(loaded["Detail"])
        comparison = compare_zip_hashes(hashes, loaded["Expected"])
        _print_comparison_report(zip_dir, loaded.get("Path"), comparison)

        if comparison["Mismatches"]:
            log.error(
                f"{len(comparison['Mismatches'])} ZIP(s) have a DIFFERENT SHA-256 "
                "than listed in sha256.txt:"
            )
            for m in comparison["Mismatches"]:
                log.error(
                    f"  {m['FileName']}: computed={m.get('Computed')} "
                    f"expected={m.get('Expected')}"
                )
        if comparison.get("AllMatch"):
            log.info("All ZIP SHA-256 hashes match sha256.txt — integrity OK.")
        elif comparison.get("StrictOk") and not comparison["Mismatches"]:
            log.info(
                "All listed ZIPs match sha256.txt "
                f"({len(comparison.get('MissingInFile') or [])} ZIP(s) not in file)."
            )

    if dry_run:
        log.info("[DryRun] ZIP SHA-256 comparison finished; skipping confirmation.")
        return {
            "Success": True,
            "Aborted": False,
            "Hashes": hashes,
            "Comparison": comparison,
            "Detail": "Dry-run: comparison done, confirmation skipped",
        }

    if non_interactive:
        if comparison and comparison.get("Mismatches"):
            log.error(
                "Non-interactive mode: hash mismatches detected — aborting setup."
            )
            return {
                "Success": False,
                "Aborted": True,
                "Hashes": hashes,
                "Comparison": comparison,
                "Detail": "Non-interactive: mismatched SHA-256 vs sha256.txt",
            }
        log.info(
            "Non-interactive mode: continuing "
            + (
                "(all hashes match sha256.txt)."
                if comparison and comparison.get("AllMatch")
                else "(no hard mismatches, or no sha256.txt)."
            )
        )
        return {
            "Success": True,
            "Aborted": False,
            "Hashes": hashes,
            "Comparison": comparison,
            "Detail": "Non-interactive: comparison done",
        }

    has_mismatches = bool(comparison and comparison.get("Mismatches"))
    all_match = bool(comparison and comparison.get("AllMatch"))

    if all_match:
        _print_blue(
            "All SHA-256 hashes are identical to sha256.txt. "
            "Type YES (or Y) to continue setup."
        )
    elif has_mismatches:
        _print_blue(
            "WARNING: one or more files have DIFFERENT SHA-256 codes (listed above)."
        )
        _print_blue(
            "Type YES (or Y) to continue anyway, or anything else to abort."
        )
    else:
        _print_blue(
            "Type YES (or Y) to confirm the hashes and continue setup."
        )
        _print_blue(
            "Anything else will abort the install before any extraction begins."
        )

    try:
        answer = input(f"{_ANSI_YELLOW}Confirm ZIP hashes / continue? [yes/no]: {_ANSI_RESET}").strip().lower()
    except EOFError:
        answer = "no"

    if answer in ("y", "yes"):
        if has_mismatches:
            log.warn(
                "Operator continued despite SHA-256 mismatches. Proceeding with setup."
            )
        else:
            log.info("Operator confirmed ZIP SHA-256 hashes. Continuing with setup.")
        return {
            "Success": True,
            "Aborted": False,
            "Hashes": hashes,
            "Comparison": comparison,
            "Detail": (
                "Operator continued despite mismatches"
                if has_mismatches
                else "Operator confirmed hashes"
            ),
        }

    log.error("Operator declined ZIP hash confirmation — aborting setup.")
    return {
        "Success": False,
        "Aborted": True,
        "Hashes": hashes,
        "Comparison": comparison,
        "Detail": "Operator aborted: ZIP hashes not confirmed",
    }
