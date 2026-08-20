"""
Locates component ZIP packages and the correct main service executable.
Strong ranking + exclusion list tuned for real OpenText KD package layouts.
Also extracts Knowledge Discovery major.minor version from ZIP file names
(e.g. Content_26.3.1_LINUX_X86_64.zip → 26.3) for informational display only; BasePath is never versioned.
"""

from __future__ import annotations

import os
import re
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# Packages that use independent versioning (not KD major.minor)
_NON_KD_VERSION_NAMES = re.compile(
    r"(?i)(?:^|[^a-z])(?:nifi|jdk|jre|temurin|adoptium)(?:[^a-z]|$)"
)

# OpenText KD-style: Content_26.3.1_LINUX_X86_64.zip / Community_26.3.0_...
_KD_UNDERSCORE_VER = re.compile(
    r"(?i)(?:^|_)(?:content|community|category|agentstore|licenseserver|view|qms|"
    r"answerserver|statsserver|dah|dih|cfs|eduction|keyview|"
    r"answerbankagentstore|conversationagentstore|qmsagentstore)"
    r"[^0-9]*_(\d+)\.(\d+)(?:\.(\d+))?_"
)

# Generic OpenText component_MAJOR.MINOR.PATCH_PLATFORM.zip
_GENERIC_UNDERSCORE_VER = re.compile(r"(?i)_(\d+)\.(\d+)(?:\.(\d+))?_(?:windows|linux|win64|x86)")

# Find-style: find-26.2.0.zip
_FIND_DASH_VER = re.compile(r"(?i)(?:^|[^0-9])find[_-](\d+)\.(\d+)(?:\.(\d+))?")

# Fallback: any _MAJOR.MINOR.PATCH_ token in a platform package name
_FALLBACK_VER = re.compile(r"(?i)(?:^|_)(\d{2})\.(\d+)(?:\.(\d+))?(?:_|$)")


def parse_kd_version_from_filename(filename: str) -> Optional[Dict[str, Any]]:
    """
    Extract Knowledge Discovery version parts from a package file name.

    Examples:
      Content_26.3.1_LINUX_X86_64.zip    → major=26, minor=3, patch=1, major_minor="26.3"
      Content_26.3.1_WINDOWS_X86_64.zip  → major=26, minor=3, patch=1, major_minor="26.3"
      find-26.2.0.zip                    → major=26, minor=2, patch=0, major_minor="26.2"
      nifi-2.10.0-bin.zip                → None (not a KD product version)
    """
    name = Path(filename).name
    if not name.lower().endswith(".zip"):
        return None
    if _NON_KD_VERSION_NAMES.search(name):
        return None

    m = _KD_UNDERSCORE_VER.search(name)
    if not m:
        m = _GENERIC_UNDERSCORE_VER.search(name)
    if not m:
        m = _FIND_DASH_VER.search(name)
    if not m:
        # Accept fallback when name looks like an OpenText platform package
        if re.search(r"(?i)windows|win64|linux|x86_64", name):
            m = _FALLBACK_VER.search(name)
    if not m:
        return None

    major = int(m.group(1))
    minor = int(m.group(2))
    patch = int(m.group(3)) if m.lastindex and m.lastindex >= 3 and m.group(3) is not None else None
    # KD product versions are typically 12.x–30.x range; reject absurd values
    if major < 10 or major > 99:
        return None
    return {
        "Major": major,
        "Minor": minor,
        "Patch": patch,
        "MajorMinor": f"{major}.{minor}",
        "Full": f"{major}.{minor}.{patch}" if patch is not None else f"{major}.{minor}",
        "FileName": name,
    }


def suggest_base_path(major_minor: str = "", root: str = "/opt/KnowledgeDiscovery") -> str:
    """
    Return the install root BasePath.

    Version numbers (26.2 / 26.3 / …) are intentionally NOT appended. The
    install root is always the configured BasePath value only, e.g.
    /opt/KnowledgeDiscovery — never /opt/KnowledgeDiscovery/26.3.
    ``major_minor`` is accepted for call-site compatibility but ignored.
    """
    _ = major_minor  # intentionally unused
    root = str(root or "/opt/KnowledgeDiscovery").rstrip("\\/")
    return root.replace("\\", "/")



def analyze_kd_zip_versions(zip_directory: str | Path) -> Dict[str, Any]:
    """
    Scan ZipPath for KD package versions and detect consensus / mismatches.

    Returns:
      Success, MajorMinor (consensus), SuggestedBasePath, Packages (list),
      Mismatches (list of {FileName, MajorMinor}), Warnings (list of str),
      Unversioned (KD-looking names without a parseable version).
    """
    zip_dir = Path(zip_directory)
    empty: Dict[str, Any] = {
        "Success": False,
        "MajorMinor": None,
        "SuggestedBasePath": None,
        "Packages": [],
        "Mismatches": [],
        "Unversioned": [],
        "Warnings": [],
        "Detail": f"ZipPath not found or not a directory: {zip_dir}",
    }
    if not zip_dir.is_dir():
        return empty

    packages: List[Dict[str, Any]] = []
    unversioned: List[str] = []
    try:
        zips = [p for p in zip_dir.iterdir() if p.is_file() and p.suffix.lower() == ".zip"]
    except Exception as e:
        empty["Detail"] = f"Cannot list ZipPath: {e}"
        return empty

    for p in zips:
        if _NON_KD_VERSION_NAMES.search(p.name):
            continue
        # Skip obvious non-KD archives
        if re.search(r"(?i)jdk|jre|temurin", p.name):
            continue
        parsed = parse_kd_version_from_filename(p.name)
        if parsed:
            packages.append(parsed)
        elif re.search(
            r"(?i)content|community|category|agentstore|licenseserver|view|qms|"
            r"answerserver|statsserver|find|windows",
            p.name,
        ):
            unversioned.append(p.name)

    if not packages:
        return {
            "Success": False,
            "MajorMinor": None,
            "SuggestedBasePath": None,
            "Packages": [],
            "Mismatches": [],
            "Unversioned": unversioned,
            "Warnings": (
                [
                    "No Knowledge Discovery version could be parsed from ZIP names in ZipPath. "
                    "Expected names like Content_26.3.1_LINUX_X86_64.zip."
                ]
                if zips
                else ["No .zip files found in ZipPath."]
            ),
            "Detail": "No parseable KD package versions",
        }

    counts = Counter(p["MajorMinor"] for p in packages)
    consensus, _ = counts.most_common(1)[0]
    mismatches = [
        {"FileName": p["FileName"], "MajorMinor": p["MajorMinor"], "Full": p["Full"]}
        for p in packages
        if p["MajorMinor"] != consensus
    ]

    warnings: List[str] = []
    if mismatches:
        details = ", ".join(f"{m['FileName']} ({m['MajorMinor']})" for m in mismatches)
        warnings.append(
            f"ZIP version mismatch: consensus is {consensus}, but these packages differ: {details}. "
            f"BasePath should use major.minor from the package set (e.g. C:\\KnowledgeDiscovery\\{consensus})."
        )
    if unversioned:
        warnings.append(
            "Some KD-looking ZIP names have no parseable major.minor version: "
            + ", ".join(unversioned[:8])
            + ("…" if len(unversioned) > 8 else "")
        )
    if len(counts) > 1:
        summary = ", ".join(f"{v}×{c}" for v, c in counts.most_common())
        warnings.append(f"Multiple KD versions present in ZipPath: {summary}. Prefer a single major.minor set.")

    return {
        "Success": True,
        "MajorMinor": consensus,
        "SuggestedBasePath": suggest_base_path(consensus),
        "Packages": packages,
        "Mismatches": mismatches,
        "Unversioned": unversioned,
        "Warnings": warnings,
        "Detail": f"Consensus KD version {consensus} from {len(packages)} package(s)",
        "Counts": dict(counts),
    }


def apply_versioned_base_path(
    config: Dict[str, Any],
    analysis: Optional[Dict[str, Any]] = None,
    *,
    only_if_default: bool = True,
) -> Dict[str, Any]:
    """
    Normalize BasePath / IndexPath so they never contain a version leaf
    (26.2, 26.3, …).

    Historically the Windows toolkit rewrote BasePath to
    ``…/KnowledgeDiscovery/<major.minor>``. On Linux the install root must
    stay at the configured BasePath only (e.g. ``/opt/KnowledgeDiscovery``)
    and IndexPath at ``<BasePath>/Indexes``.

    If the current BasePath ends with a ``\\d+.\\d+`` version segment under
    KnowledgeDiscovery, that segment is stripped. IndexPath is aligned to
    ``<BasePath>/Indexes`` when it was empty, still under the old versioned
    path, or looked like a stock template.
    """
    result = {
        "Changed": False,
        "BasePath": config.get("BasePath"),
        "IndexPath": config.get("IndexPath"),
        "MajorMinor": None,
        "Detail": "",
    }
    if analysis is None:
        zp = config.get("ZipPath") or ""
        analysis = analyze_kd_zip_versions(zp)
    mm = analysis.get("MajorMinor") if analysis else None
    result["MajorMinor"] = mm

    current = str(config.get("BasePath") or "").strip()

    def _strip_version_leaf(path: str) -> str:
        if not path:
            return path
        # Normalize separators for matching, then restore with forward slashes
        norm = path.replace("\\", "/").rstrip("/")
        # …/KnowledgeDiscovery/26.3  →  …/KnowledgeDiscovery
        m = re.match(r"(?i)^(.*/KnowledgeDiscovery)/\d+\.\d+$", norm)
        if m:
            return m.group(1)
        # bare trailing /<digits>.<digits> after KnowledgeDiscovery-style roots
        m2 = re.match(r"(?i)^(.+)/\d+\.\d+$", norm)
        if m2 and re.search(r"(?i)KnowledgeDiscovery$", m2.group(1)):
            return m2.group(1)
        return norm

    def _is_versioned_kd_path(path: str) -> bool:
        if not path:
            return False
        norm = path.replace("\\", "/").rstrip("/")
        return bool(re.search(r"(?i)KnowledgeDiscovery/\d+\.\d+$", norm))

    cleaned = _strip_version_leaf(current) if current else current
    # Default when empty
    if not cleaned:
        cleaned = "/opt/KnowledgeDiscovery"

    # Prefer forward slashes on Linux
    cleaned = cleaned.replace("\\", "/")

    changed = False
    if cleaned != current.replace("\\", "/").rstrip("/"):
        config["BasePath"] = cleaned
        changed = True
    elif current and current != cleaned:
        config["BasePath"] = cleaned
        changed = True
    else:
        # Ensure stored value uses forward slashes
        if current and config.get("BasePath") != cleaned:
            config["BasePath"] = cleaned
            changed = True
        elif not current:
            config["BasePath"] = cleaned
            changed = True

    # IndexPath: only normalize when the user has explicitly set one.
    # Do NOT invent <BasePath>/Indexes when IndexPath is absent — the default
    # OpenText relative layout (MainPath=./index/main, etc.) is preferred and
    # avoids Admin UI "Missing data file" errors from injected absolute paths.
    old_index = str(config.get("IndexPath") or "").strip()
    if old_index:
        base = str(config.get("BasePath") or cleaned).rstrip("/\\")
        desired_index = f"{base}/Indexes"
        old_index_norm = old_index.replace("\\", "/").rstrip("/")
        should_fix_index = (
            _is_versioned_kd_path(str(Path(old_index_norm).parent) if old_index_norm else "")
            or bool(re.search(r"(?i)KnowledgeDiscovery/\d+\.\d+/Indexes$", old_index_norm))
            or (current and old_index_norm.lower().startswith(
                current.replace("\\", "/").rstrip("/").lower() + "/"))
            or (old_index_norm.lower().endswith("/indexes")
                and _is_versioned_kd_path(old_index_norm.rsplit("/", 1)[0]))
            or bool(re.search(r"(?i)/\d+\.\d+/Indexes$", old_index_norm))
        )
        if should_fix_index and old_index_norm != desired_index.rstrip("/"):
            config["IndexPath"] = desired_index
            changed = True

    result["Changed"] = changed
    result["BasePath"] = config.get("BasePath")
    result["IndexPath"] = config.get("IndexPath")
    if changed:
        result["Detail"] = (
            f"Normalized BasePath to {config.get('BasePath')} "
            f"(version leaf removed if present); IndexPath={config.get('IndexPath')}"
        )
    else:
        result["Detail"] = f"BasePath unchanged: {config.get('BasePath')}"
    return result



def test_kd_windows_executable(exe_path: str | Path) -> bool:
    """PE header check (MZ). Kept for any legacy Windows-path callers."""
    path = Path(exe_path)
    if not path.is_file():
        return False
    try:
        with open(path, "rb") as f:
            header = f.read(2)
        return header == b"MZ"
    except Exception:
        return False


def test_kd_linux_executable(exe_path: str | Path) -> bool:
    """
    ELF header check (0x7F 'E' 'L' 'F') - the native Linux equivalent of the
    Windows `test_kd_windows_executable()` PE/MZ check above. OpenText's
    Linux IDOL binaries ship as extension-less ELF executables, so this is
    what actually validates a candidate on this platform.
    """
    path = Path(exe_path)
    if not path.is_file():
        return False
    try:
        with open(path, "rb") as f:
            header = f.read(4)
        return header == b"\x7fELF"
    except Exception:
        return False


def test_kd_linux_launcher(path: str | Path, component: str = "") -> bool:
    """
    True for a usable Linux process entrypoint:
      - native ELF binary, or
      - shebang script (#!/...) whose name matches the component
        or the official OpenText start-<component>.sh wrapper
    """
    p = Path(path)
    if not p.is_file():
        return False
    if test_kd_linux_executable(p):
        return True
    try:
        with open(p, "rb") as f:
            head = f.read(128)
        if not head.startswith(b"#!"):
            return False
        name = p.name.lower()
        stem = p.stem.lower()
        comp = (component or "").lower().replace(" ", "")
        if not comp:
            return False
        # Official OpenText start/stop scripts at package root
        if name in (f"start-{comp}.sh", f"start-{comp}server.sh", f"start{comp}.sh"):
            return True
        if stem == comp or stem == f"{comp}server" or stem == f"idol{comp}":
            return True
        if comp == "licenseserver" and stem in ("licenseserver", "license", "idollicenseserver"):
            return True
        if comp == "licenseserver" and name in ("start-licenseserver.sh", "start-license.sh"):
            return True
        if comp.endswith("agentstore") and stem in ("agentstore", "content"):
            return True
        if comp.endswith("agentstore") and name in ("start-agentstore.sh", "start-content.sh"):
            return True
        return False
    except Exception:
        return False



def find_kd_component_zip(zip_directory: str | Path, component: str) -> Dict[str, Any]:
    """
    Find the best Linux ZIP for a component.
    Agentstore-like components (Agentstore, QMSAgentStore, AnswerBankAgentStore,
    ConversationAgentStore) re-use the Content package.
    NiFi is special-cased: looks for Apache NiFi bin ZIPs (*nifi*-bin*.zip).
    """
    zip_dir = Path(zip_directory)

    # --- Apache NiFi (optional component) ---
    if component.lower() == "nifi":
        # Prefer official bin distributions: nifi-2.10.0-bin.zip etc.
        candidates = list(zip_dir.glob("*nifi*-bin*.zip")) + list(zip_dir.glob("*nifi*.zip"))
        # de-dupe; never treat NiFiIngest (connector NARs only) as the Apache binary
        seen = set()
        unique = []
        for c in candidates:
            if c.name not in seen:
                seen.add(c.name)
                if "nifiingest" in c.name.lower():
                    continue  # package-only NAR source — not nifi-*-bin.zip
                unique.append(c)
        candidates = unique
        if not candidates:
            return {
                "Success": False,
                "Reason": f"No NiFi binary ZIP found in {zip_dir} (expected *nifi*-bin*.zip or *nifi*.zip)",
                "Zip": None,
            }
        # Prefer higher version-looking names and those containing -bin
        def nifi_score(p: Path) -> tuple:
            name = p.name.lower()
            s = 0
            if "-bin" in name:
                s -= 100000  # strongly prefer official Apache bin distributions
            else:
                s += 50000   # deprioritise non-bin (e.g. accidental matches)
            m = re.search(r"nifi-(\d+)\.(\d+)\.(\d+)", name)
            if m:
                s -= int(m.group(1)) * 10000 + int(m.group(2)) * 100 + int(m.group(3))
            try:
                size = p.stat().st_size
            except OSError:
                size = 0
            return (s, -size)
        ranked = sorted(candidates, key=nifi_score)
        chosen = ranked[0]
        return {
            "Success": True,
            "Reason": f"OK (NiFi binary: {chosen.name})",
            "Zip": chosen,
        }

    # --- OpenText Find (optional Java component; sample: find-26.2.0.zip / find-xx.x.x.zip) ---
    if component.lower() == "find":
        candidates = list(zip_dir.glob("*find*.zip"))
        # de-dupe
        seen = set()
        unique = []
        for c in candidates:
            if c.name not in seen:
                seen.add(c.name)
                unique.append(c)
        candidates = unique
        if not candidates:
            return {
                "Success": False,
                "Reason": f"No Find ZIP found in {zip_dir} (expected *find*.zip e.g. find-26.2.0.zip)",
                "Zip": None,
            }
        def find_score(p: Path) -> tuple:
            name = p.name.lower()
            s = 0
            m = re.search(r"find-(\d+)\.(\d+)\.(\d+)", name)
            if m:
                s -= int(m.group(1)) * 10000 + int(m.group(2)) * 100 + int(m.group(3))
            # Prefer Linux-named packages when both exist; deprioritise Windows
            if re.search(r"(?i)windows|win64", name):
                s += 5000
            if re.search(r"(?i)linux", name):
                s -= 2000
            return (s, -p.stat().st_size)
        ranked = sorted(candidates, key=find_score)
        return {"Success": True, "Reason": "OK (Find binary)", "Zip": ranked[0]}

    search_component = (
        "Content"
        if component
        in (
            "Agentstore",
            "QMSAgentStore",
            "AnswerBankAgentStore",
            "ConversationAgentStore",
        )
        else component
    )

    # Case-insensitive match: Linux filesystems are case-sensitive, but OpenText
    # package names vary (LicenseServer vs licenseserver vs LICENCE...).
    # Glob with the exact component name first, then fall back to a full scan.
    candidates = list(zip_dir.glob(f"*{search_component}*.zip"))
    if not candidates and zip_dir.is_dir():
        needle = search_component.lower()
        candidates = [
            c for c in zip_dir.glob("*.zip")
            if needle in c.name.lower()
        ]
    if not candidates:
        return {
            "Success": False,
            "Reason": f"No file matching *{search_component}*.zip found in {zip_dir} (needed for {component})",
            "Zip": None,
        }

    def _zip_rank(c: Path) -> tuple:
        """Prefer LINUX_X86_64, then higher version, then larger file."""
        name = c.name.lower()
        s = 0
        if re.search(r"linux", name) and not re.search(r"windows|win64", name):
            s -= 100000
        if re.search(r"linux_x86_64|linux-x86_64|x86_64.*linux", name):
            s -= 50000
        if re.search(r"windows|win64", name) and not re.search(r"linux", name):
            s += 100000
        m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", name)
        if m:
            major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
            s -= major * 10000 + minor * 100 + patch
        try:
            size = c.stat().st_size
        except OSError:
            size = 0
        return (s, -size, c.name.lower())

    ranked = sorted(candidates, key=_zip_rank)
    best = ranked[0]
    name_l = best.name.lower()
    if re.search(r"windows|win64", name_l) and not re.search(r"linux", name_l):
        return {
            "Success": False,
            "Reason": (
                f"Only Windows packages found for {search_component} "
                f"(need *_LINUX_X86_64.zip). Candidates: "
                + ", ".join(c.name for c in ranked[:5])
            ),
            "Zip": None,
        }
    reason = "OK"
    if re.search(r"linux", name_l):
        reason = f"OK (Linux package: {best.name})"
    elif len(ranked) > 1:
        reason = f"OK (best of {len(ranked)} candidates: {best.name})"
    return {"Success": True, "Reason": reason, "Zip": best}


def find_kd_component_executable(component_path: str | Path, component: str) -> Dict[str, Any]:
    """
    Locate the main service binary, applying a strong exclusion list and ranking.
    NiFi is special: returns the bin/nifi.sh launcher (not an ELF binary).
    """
    root = Path(component_path)

    # --- Apache NiFi ---
    if component.lower() == "nifi":
        # Prefer bin/nifi.sh (the Linux launcher NiFi's binary distribution
        # ships); nifi.cmd (Windows) is bundled in the same distribution too,
        # so fall back to it only if nifi.sh is somehow missing.
        candidates = list(root.rglob("nifi.sh")) or list(root.rglob("nifi.cmd"))
        if not candidates:
            return {
                "Success": False,
                "Reason": "nifi.sh not found under component path (is the ZIP extracted correctly?)",
                "Executable": None,
                "AllCandidates": [],
            }
        # Prefer the one closest to root / under a bin/ folder
        def nifi_launcher_score(p: Path) -> tuple:
            parts = [x.lower() for x in p.parts]
            s = 0
            if "bin" in parts:
                s -= 100
            try:
                s += len(p.relative_to(root).parts) * 10
            except ValueError:
                s += 50
            return (s,)
        ranked = sorted(candidates, key=nifi_launcher_score)
        return {
            "Success": True,
            "Reason": "OK (NiFi launcher)",
            "Executable": ranked[0],
            "AllCandidates": ranked,
        }

    # --- OpenText Find (Java executable .war) ---
    if component.lower() == "find":
        candidates = list(root.rglob("find.war")) + list(root.rglob("*.war"))
        # Prefer exact find.war
        exact = [c for c in candidates if c.name.lower() == "find.war"]
        if exact:
            candidates = exact
        if not candidates:
            return {
                "Success": False,
                "Reason": "find.war not found under component path (is the ZIP extracted correctly?)",
                "Executable": None,
                "AllCandidates": [],
            }
        def find_war_score(p: Path) -> tuple:
            s = 0
            if p.name.lower() == "find.war":
                s -= 1000
            try:
                s += len(p.relative_to(root).parts) * 10
            except ValueError:
                s += 50
            return (s,)
        ranked = sorted(candidates, key=find_war_score)
        return {
            "Success": True,
            "Reason": "OK (Find Java war)",
            "Executable": ranked[0],
            "AllCandidates": ranked,
        }

    exclude_re = re.compile(
        r"(?i)uninstall|setup|remove|delete|java|jre|jdk|"
        r"vc_?redist|vcruntime|msvcp|msvcr|dotnet|ndp\d|"
        r"prereq|redist|crashpad|directx|dxsetup|langfiles|jpn|cha|"
        r"tools|nistrdstool|autpassword|makeda|kvoop|extract|filter|lua|"
        # Bundled toolchain / runtime bits that are ELF but are never the
        # main ACI server binary (seen in AnswerServer / View packages):
        r"ptxas|nvcc|cuda|cudnn|nvidia|cublas|curand|cusolver|cusparse|"
        r"^python(\d+(\.\d+)?)?$|^pip(\d+)?$|^perl$|^ruby$|"
        r"^node$|^npm$|^busybox$|^openssl$|^sqlite3?$"
    )
    path_exclude_re = re.compile(
        r"(?i)[\\/](langfiles|tools|jpn|cha|passwordlib|filters|"
        r"cuda|nvidia|python|jre|jdk|runtime|runtimes|"
        r"init|systemv|sysv|systemd|rc\.d)[\\/]"
    )

    # Linux IDOL binaries are normally extension-less ELF executables.
    # We deliberately do NOT require the executable bit up front: some unzip
    # builds drop Unix permission bits. Discover by ELF magic, rank, then
    # chmod +x the winner. Also accept a small set of known suffixes (.bin)
    # used by some OpenText packages.
    candidates: List[Path] = []
    for candidate in root.rglob("*"):
        if not candidate.is_file():
            continue
        # Extension-less (normal IDOL Linux binary), .bin, or official start-*.sh
        suf = candidate.suffix.lower()
        if suf not in ("", ".bin", ".sh"):
            continue
        if exclude_re.search(candidate.name):
            continue
        if path_exclude_re.search(str(candidate)):
            continue
        # Accept native ELF or a component-named shebang launcher
        if not test_kd_linux_launcher(candidate, component):
            continue
        candidates.append(candidate)

    if not candidates:
        # Diagnostics: count ELFs / EXEs and list top-level names so the log
        # shows what was actually extracted (wrong ZIP vs filter vs empty).
        any_elf = 0
        any_exe = 0
        elf_names: List[str] = []
        top_level: List[str] = []
        try:
            if root.is_dir():
                for child in sorted(root.iterdir())[:25]:
                    mark = "/" if child.is_dir() else ""
                    top_level.append(child.name + mark)
            for pth in root.rglob("*"):
                if not pth.is_file():
                    continue
                if pth.suffix.lower() == ".exe":
                    any_exe += 1
                elif test_kd_linux_executable(pth):
                    any_elf += 1
                    if len(elf_names) < 8:
                        try:
                            elf_names.append(str(pth.relative_to(root)))
                        except ValueError:
                            elf_names.append(pth.name)
        except Exception:
            pass
        top_s = ", ".join(top_level) if top_level else "(empty)"
        hint = f" | top-level: [{top_s}]"
        if any_elf:
            hint += (
                f" | {any_elf} ELF file(s) present but excluded by name/path filters: "
                f"{', '.join(elf_names)}"
            )
        elif any_exe and not any_elf:
            hint += (
                f" | found {any_exe} .exe and 0 ELF – extracted tree looks like a "
                f"Windows package (or extract did not unpack the Linux binaries). "
                f"Confirm ZipPath has *_LINUX_X86_64.zip and re-run with force extract."
            )
        elif not root.is_dir() or not top_level:
            hint += " | component folder is missing or empty – re-run extract"
        else:
            hint += (
                " | no ELF binaries found under the component folder. "
                "If you extracted the LINUX_X86_64 ZIP manually and it works, "
                "delete this folder and re-run Install so the toolkit re-extracts it."
            )
        return {
            "Success": False,
            "Reason": (
                f"No suitable native Linux (ELF) executable found for {component} "
                f"after exclusions{hint}"
            ),
            "Executable": None,
            "AllCandidates": [],
        }

    comp_lower = component.lower()

    def score(p: Path) -> tuple:
        name = p.stem.lower()
        full = str(p).lower()
        s = 0

        # Extremely strong bonus for the real main binary
        if (
            name == comp_lower
            or name == f"{comp_lower}server"
            or name == f"idol{comp_lower}"
            or name in ("cfs", "content", "community", "category")
            # Agentstore-like components run agentstore (copied from content)
            or (
                comp_lower
                in (
                    "agentstore",
                    "qmsagentstore",
                    "answerbankagentstore",
                    "conversationagentstore",
                )
                and name in ("agentstore", "content")
            )
            or (
                comp_lower == "qms"
                and name in ("qms", "querymanipulation", "querymanipulationserver")
            )
            or (
                comp_lower == "answerserver"
                and name in ("answerserver", "answer")
            )
            or (
                comp_lower == "licenseserver"
                and name in ("licenseserver", "license", "idollicenseserver")
            )
            or (
                comp_lower == "statsserver"
                and name in ("statsserver", "stats", "statistics", "statisticsserver")
            )
            or (
                comp_lower == "view"
                and name in ("view", "idolview", "viewserver")
            )
            or name in (f"start-{comp_lower}", f"start-{comp_lower}server")
        ):
            s -= 15000
        # Prefer package-root start-*.sh over anything under init/
        if name.startswith("start-") and "init" not in full:
            s -= 8000
        elif comp_lower in name and not path_exclude_re.search(full):
            s -= 5000

        # Heavy penalties
        if path_exclude_re.search(full):
            s += 12000
        if re.search(r"(?i)^(makeda|autpassword|nistrdstool|passwordlib|kvoop|extract|filter|lua)", name):
            s += 10000
        # NOTE: no penalty for a "linux" path segment here - on this platform
        # that's the binary we *want*. (The old Windows-only version of this
        # scorer penalized it and then hard-rejected the result below.)

        # Depth penalty
        try:
            depth = len(p.relative_to(root).parts)
            if depth > 2:
                s += 3000
        except ValueError:
            s += 3000

        # Prefer larger files as tie-breaker (main binaries tend to be bigger)
        return (s, -p.stat().st_size)

    ranked = sorted(candidates, key=score)
    best = ranked[0]

    if not test_kd_linux_launcher(best, component):
        return {
            "Success": False,
            "Reason": "Not a valid Linux ELF executable or component launcher script",
            "Executable": None,
            "AllCandidates": ranked,
        }

    # Ensure the chosen binary is executable (ZIP extract sometimes drops +x)
    try:
        mode = best.stat().st_mode
        if not (mode & 0o111):
            best.chmod(mode | 0o755)
    except OSError:
        pass

    return {
        "Success": True,
        "Reason": "OK",
        "Executable": best,
        "AllCandidates": ranked,
    }
