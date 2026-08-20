"""
Idempotent systemd service install / uninstall / health-check for KD components.

This is a NATIVE LINUX replacement for the original Windows implementation,
which used Windows sc.exe / systemd; this module uses systemd only.

OpenText Linux packages ship vendor unit templates under:

    <InstallDir>/init/systemd/<componentname>.service

Installation follows the official OpenText procedure (adapted for toolkit naming):

  1. Copy / adapt the vendor template (or generate a unit) to
     /lib/systemd/system/kd-<componentname>.service
  2. Replace placeholders (__COMPONENT_INSTALL_DIR__, __USER__, __GROUP__, ...)
  3. chmod 755 + chown/chgrp root on the unit file
  4. systemctl enable kd-<componentname>
  5. daemon-reload

Logical API names remain KD-<Component> (Windows parity for callers). The
actual systemd unit file always uses the toolkit prefix kd- + vendor
componentname (lowercase executable name), e.g. kd-licenseserver.service,
kd-content.service, kd-nifi.service. This makes ``systemctl ... "kd*"`` and
the manage_kd_services status views consistent.

NiFi and Find fall back to generated units when no vendor template exists.
"""

from __future__ import annotations

import os
import re
import sys
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .logging import (
    log,
    status_line,
    status_line_clear,
    status_line_finish,
    chown_to_invoking_user,
    _status_supports_color,
    _ANSI_RESET,
    _ANSI_CYAN,
    _ANSI_GREEN,
    _ANSI_YELLOW,
    _ANSI_BOLD,
    _ANSI_DIM,
)

DEFAULT_INSTALL_TEMPLATE: List[str] = []   # unused on Linux (no self-install switch needed)
DEFAULT_UNINSTALL_TEMPLATE: List[str] = []  # unused on Linux

# Official OpenText target for Debian/Ubuntu; also accepted by systemd on most distros.
SYSTEMD_UNIT_DIR = Path("/lib/systemd/system")
# Legacy location used by earlier toolkit revisions (cleaned on uninstall).
SYSTEMD_UNIT_DIR_LEGACY = Path("/etc/systemd/system")
KD_UNIT_PREFIX = "kd-"  # installed unit files: kd-content.service, kd-nifi.service, ...


# --------------------------------------------------------------------------
# Naming helpers (unchanged behaviour from the Windows module)
# --------------------------------------------------------------------------

def get_kd_service_name(component: str) -> str:
    """
    Map a component name (or an already-qualified service name) to the
    logical KD service name.

    Examples:
      Content      → KD-Content
      NiFi         → KD-NiFi
      KD-Content   → KD-Content   (idempotent)
    """
    name = (component or "").strip()
    if not name:
        return "KD-"
    if name[:3].upper() == "KD-":
        return "KD-" + name[3:]
    return f"KD-{name}"


def service_name_to_component(service_name: str) -> str:
    """Inverse of get_kd_service_name: KD-Content → Content, KD-NiFi → NiFi."""
    name = (service_name or "").strip()
    if name[:3].upper() == "KD-":
        return name[3:]
    return name


def vendor_unit_basename(component: str) -> str:
    """
    Map a component / logical service name to the OpenText vendor unit basename
    (no .service suffix). Matches the component executable name.

    Examples:
      LicenseServer / KD-LicenseServer -> licenseserver
      Content       / KD-Content       -> content
      QMSAgentStore / KD-QMSAgentStore -> qmsagentstore
      NiFi          / KD-NiFi          -> nifi
    """
    comp = service_name_to_component(component)
    return (comp or "").strip().lower()


def _unit_name(service_name: str) -> str:
    """
    systemd unit file name for a logical service.

    Always use the toolkit kd- prefix (kd-content.service, kd-nifi.service, ...)
    so that ``systemctl list-units --type=service --all "kd*"`` and the
    manage_kd_services status views discover every KD unit. Vendor unit
    templates are still adapted and written under this name. Older plain
    vendor names (content.service) and previous kd-* files are cleaned up
    via _unit_paths_for_cleanup.
    """
    return f"{KD_UNIT_PREFIX}{vendor_unit_basename(service_name)}.service"


def _unit_path(service_name: str) -> Path:
    """Primary unit path under /lib/systemd/system."""
    return SYSTEMD_UNIT_DIR / _unit_name(service_name)


def _unit_paths_for_cleanup(service_name: str) -> List[Path]:
    """All locations a unit for this service might live (current + legacy)."""
    base = vendor_unit_basename(service_name)
    names = [
        f"{base}.service",
        f"{KD_UNIT_PREFIX}{base}.service",
        f"{KD_UNIT_PREFIX}{service_name_to_component(service_name).lower()}.service",
    ]
    # de-dupe while preserving order
    seen = set()
    ordered: List[str] = []
    for n in names:
        if n not in seen:
            seen.add(n)
            ordered.append(n)
    paths: List[Path] = []
    for directory in (SYSTEMD_UNIT_DIR, SYSTEMD_UNIT_DIR_LEGACY):
        for n in ordered:
            paths.append(directory / n)
    return paths


# Components assembled from Content share the agentstore binary / unit family
_AGENTSTORE_LIKE_COMPONENTS = {
    "agentstore",
    "qmsagentstore",
    "answerbankagentstore",
    "conversationagentstore",
}


def _find_vendor_unit_template(component_path: Path, component: str) -> Optional[Path]:
    """
    Locate OpenText's shipped unit template under:
      <component_path>/init/systemd/<name>.service

    Search order:
      1. Exact match for this component (e.g. licenseserver.service, content.service)
      2. For Agentstore-like components: agentstore.service, then content.service
         (these folders are assembled from Content and often only ship content.service)
      3. Any *.service in init/systemd (or scripts/init/systemd)
    """
    base = vendor_unit_basename(component)
    search_roots = [
        component_path / "init" / "systemd",
        component_path / "scripts" / "init" / "systemd",
    ]

    preferred_names = [f"{base}.service"]
    if base in _AGENTSTORE_LIKE_COMPONENTS or base.endswith("agentstore"):
        for alt in ("agentstore.service", "content.service"):
            if alt not in preferred_names:
                preferred_names.append(alt)

    for name in preferred_names:
        for root in search_roots:
            candidate = root / name
            if candidate.is_file():
                return candidate

    for root in search_roots:
        if root.is_dir():
            matches = sorted(root.glob("*.service"))
            if matches:
                return matches[0]
    return None


def _adapt_unit_template_for_component(
    unit_text: str,
    *,
    component: str,
    install_dir: Path,
    executable: Optional[Path] = None,
) -> str:
    """
    Adjust a vendor unit template for a specific component instance.

    Agentstore-like components are copied from Content, so the template may
    still reference the ``content`` binary and ``content.cfg``. Rewrite:

      * the first path token after the install-dir placeholder (executable /
        start-*.sh), and
      * ``-configfile …/content.cfg`` → ``…/agentstore.cfg`` for every
        Agentstore-family component (Agentstore, QMSAgentStore, …).

    Example transformation:

        ExecStart=…/Agentstore/start-content.sh -configfile …/Agentstore/content.cfg
        →
        ExecStart=…/Agentstore/start-content.sh -configfile …/Agentstore/agentstore.cfg
    """
    import re

    out = unit_text
    base = vendor_unit_basename(component)
    is_agentstore_like = (
        base in _AGENTSTORE_LIKE_COMPONENTS or base.endswith("agentstore")
    )
    exe = executable
    if exe is None:
        # Prefer official start-*.sh launchers when present (they set up
        # LD_LIBRARY_PATH / env and daemonise correctly for many IDOL packages).
        candidates = [
            f"start-{base}.sh",
            f"start-{component.lower()}.sh",
            f"start-{base}server.sh",
            base,
            f"{base}.sh",
            "agentstore",
            "start-content.sh",
            "start-agentstore.sh",
            "content",
            component.lower(),
        ]
        for name in candidates:
            cand = install_dir / name
            if cand.is_file():
                exe = cand
                break
            # Some packages put launchers one level deeper
            cand2 = install_dir / "bin" / name
            if cand2.is_file():
                exe = cand2
                break
    if exe is not None and exe.is_file():
        exe_name = exe.name

        def _fix_exec(match: "re.Match[str]") -> str:
            line = match.group(0)
            # Only the *first* path after a COMPONENT_INSTALL_DIR placeholder is
            # the executable. Using count=1 prevents corrupting the -configfile
            # …/xxx.cfg argument.
            line2 = re.sub(
                r"(__COMPONENT_INSTALL_DIR__|/COMPONENT_INSTALL_DIR__|COMPONENT_INSTALL_DIR)/[A-Za-z0-9_.-]+",
                r"\1/" + exe_name,
                line,
                count=1,
            )
            # Also handle already-substituted absolute paths that still name
            # the generic content/agentstore binary (Agentstore-from-Content case).
            # Again only the first occurrence (the ExecStart binary).
            line2 = re.sub(
                r"(?<![A-Za-z0-9_./])(?:content|agentstore)(?:\.exe)?(?=[\s\"']|$)",
                exe_name,
                line2,
                count=1,
            )
            return line2

        out = re.sub(r"(?im)^ExecStart=.*$", _fix_exec, out)

    # Agentstore-family: OpenText's start-content.sh is interactive (read DUMMY)
    # and ignores -configfile — it always launches content.exe with no cfg arg,
    # so Port defaults to content.cfg (9100). Force a non-interactive ExecStart
    # that passes agentstore.cfg explicitly.
    if is_agentstore_like:
        # Prefer a real binary in the install dir
        bin_name = "content.exe"
        for candidate in ("content.exe", "content", "agentstore.exe", "agentstore"):
            if (install_dir / candidate).is_file():
                bin_name = candidate
                break
        # Absolute paths after placeholder substitution; placeholders before it
        new_exec = (
            f"ExecStart=__COMPONENT_INSTALL_DIR__/{bin_name} "
            f"-configfile __COMPONENT_INSTALL_DIR__/agentstore.cfg"
        )
        # If install_dir is already an absolute path in the unit, use it
        if "__COMPONENT_INSTALL_DIR__" not in out and str(install_dir) in out:
            new_exec = (
                f"ExecStart={install_dir}/{bin_name} "
                f"-configfile {install_dir}/agentstore.cfg"
            )
        out = re.sub(r"(?im)^ExecStart=.*$", new_exec, out)
        # Binary stays in foreground → Type=simple (not forking via nohup script)
        if re.search(r"(?im)^Type\s*=", out):
            out = re.sub(r"(?im)^Type\s*=\s*\S+", "Type=simple", out)
        else:
            out = re.sub(r"(?im)^(\[Service\])", r"\1\nType=simple", out)
        if component.lower() != "content":
            out = re.sub(
                r"(?im)^Description=.*$",
                f"Description=IDOL {component}",
                out,
                count=1,
            )

    return out



def _default_service_user_group() -> Tuple[str, str]:
    """
    USER/GROUP for placeholder substitution.
    Prefer SUDO_USER when the toolkit is elevated; otherwise the current user.
    """
    sudo_user = (os.environ.get("SUDO_USER") or "").strip()
    if sudo_user and sudo_user != "root":
        try:
            import grp
            import pwd
            pw = pwd.getpwnam(sudo_user)
            gr = grp.getgrgid(pw.pw_gid)
            return sudo_user, gr.gr_name
        except Exception:
            return sudo_user, sudo_user
    try:
        import grp
        import pwd
        pw = pwd.getpwuid(os.getuid())
        gr = grp.getgrgid(pw.pw_gid)
        if pw.pw_name != "root":
            return pw.pw_name, gr.gr_name
    except Exception:
        pass
    return "root", "root"


def _substitute_unit_placeholders(
    unit_text: str,
    *,
    install_dir: Path,
    user: str,
    group: str,
    ld_library_path: str = "",
    environment_file: str = "",
) -> str:
    """
    Replace OpenText service-file placeholders.

    Official OpenText unit templates use double-underscore delimiters, e.g.:

        __COMPONENT_INSTALL_DIR__   Full path where the component lives
                                    e.g. /opt/KnowledgeDiscovery/LicenseServer
        __USER__                    Linux user that runs the process
        __GROUP__                   Linux group for the process
        __COMPONENT_LD_LIBRARY_PATH__  Extra library path (optional)
        __ENVIRONMENT_FILE__        Absolute path to a systemd environment file (optional)

    Replacing only the bare name (COMPONENT_INSTALL_DIR) leaves the surrounding
    underscores and produces broken lines such as:

        ExecStart=__/opt/KnowledgeDiscovery/LicenseServer__/licenseserver

    so the full ``__NAME__`` token is always substituted first.
    """
    install = str(install_dir.resolve()) if install_dir else ""
    # Prefer forward slashes in unit files on Linux
    install = install.replace("\\", "/")

    if not ld_library_path:
        candidates = [
            install_dir / "common" / "lib",
            install_dir / "common",
            install_dir,
        ]
        ld_parts = [str(c.resolve()).replace("\\", "/") for c in candidates if c.is_dir()]
        ld_library_path = ":".join(ld_parts) if ld_parts else install

    if not environment_file:
        for name in ("environ", "environment", ".env"):
            cand = install_dir / name
            if cand.is_file():
                environment_file = str(cand.resolve()).replace("\\", "/")
                break

    # Map bare names -> values; we expand both __NAME__ and bare NAME forms.
    values = {
        "COMPONENT_INSTALL_DIR": install,
        "USER": user,
        "GROUP": group,
        "COMPONENT_LD_LIBRARY_PATH": ld_library_path,
        "ENVIRONMENT_FILE": environment_file or "",
    }

    out = unit_text
    # 1) Official form with double underscores on both sides (must run first)
    for key, value in values.items():
        out = out.replace(f"__{key}__", value)
    # 2) Single-underscore form sometimes seen in older packages: _NAME_
    for key, value in values.items():
        out = out.replace(f"_{key}_", value)
    # 3) Bare token only when it still appears as a whole placeholder-like token
    #    (avoid rewriting unrelated prose). Skip short tokens USER/GROUP bare
    #    unless they appear in assignment context — handled via __ form above.
    for key, value in values.items():
        if key in ("USER", "GROUP"):
            continue
        out = out.replace(key, value)

    # Drop optional EnvironmentFile= lines left empty (invalid for systemd)
    cleaned_lines = []
    for line in out.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.lower().startswith("environmentfile=") and (
            stripped.endswith("=") or stripped.lower() in ("environmentfile=", "environmentfile=-")
        ):
            continue
        cleaned_lines.append(line)
    out = "".join(cleaned_lines)

    # Decide Type= based on the ExecStart target.
    #
    # OpenText start-*.sh launchers typically start the real daemon and then
    # exit. With Type=simple systemd treats that exit as "service stopped"
    # (Duration ~ a few ms, Active: inactive (dead)). Force Type=forking so
    # the unit stays active after the script exits.
    #
    # Conversely, when the vendor template already says Type=forking but there
    # is no PIDFile= and the ExecStart is a long-running binary (not a .sh
    # launcher), prefer Type=simple so systemd supervises the main process
    # directly.
    exec_start_m = re.search(r"(?im)^ExecStart\s*=\s*(.+)$", out)
    exec_start_val = (exec_start_m.group(1).strip() if exec_start_m else "")
    # First token of ExecStart (may be quoted or absolute path)
    first_token = re.split(r"\s+", exec_start_val, maxsplit=1)[0].strip("\"'")
    is_sh_launcher = first_token.lower().endswith(".sh") or "/start-" in first_token.lower()

    has_type = bool(re.search(r"(?im)^Type\s*=", out))
    has_forking = bool(re.search(r"(?im)^Type\s*=\s*forking\s*$", out))
    has_simple = bool(re.search(r"(?im)^Type\s*=\s*simple\s*$", out))
    has_pidfile = bool(re.search(r"(?im)^PIDFile\s*=", out))

    if is_sh_launcher and (not has_type or has_simple):
        if has_simple:
            out = re.sub(r"(?im)^Type\s*=\s*simple\s*$", "Type=forking", out)
        else:
            out = re.sub(
                r"(?im)^(\[Service\]\s*)$",
                r"\1\nType=forking",
                out,
                count=1,
            )
    elif has_forking and not has_pidfile and not is_sh_launcher:
        out = re.sub(r"(?im)^Type\s*=\s*forking\s*$", "Type=simple", out)

    # Ensure a modest restart policy so transient startup failures recover
    if not re.search(r"(?im)^Restart\s*=", out):
        # Insert after [Service] header
        out = re.sub(
            r"(?im)^(\[Service\]\s*)$",
            r"\1\nRestart=on-failure\nRestartSec=5",
            out,
            count=1,
        )

    return out


def _install_unit_file_from_text(
    unit_basename: str,
    unit_text: str,
    *,
    enable: bool = True,
) -> Dict[str, Any]:
    """
    Write unit_text to /lib/systemd/system/<unit_basename>.service with
    chmod 755 + root:root ownership, daemon-reload, and optional enable.
    """
    if not unit_basename.endswith(".service"):
        unit_file = f"{unit_basename}.service"
    else:
        unit_file = unit_basename
    path = SYSTEMD_UNIT_DIR / unit_file
    try:
        prefix = _systemctl_needs_sudo()
        # Ensure directory exists
        if _is_root():
            SYSTEMD_UNIT_DIR.mkdir(parents=True, exist_ok=True)
            path.write_text(unit_text, encoding="utf-8")
            os.chmod(path, 0o755)
            try:
                shutil.chown(path, user="root", group="root")
            except Exception:
                # chown may require root; ignore if already root-owned
                pass
        else:
            # Write via sudo tee, then chmod/chown
            proc = subprocess.run(
                ["sudo", "tee", str(path)],
                input=unit_text,
                capture_output=True,
                text=True,
                timeout=15,
            )
            if proc.returncode != 0:
                return {"Success": False, "Detail": f"Failed to write unit file: {proc.stderr.strip()}"}
            subprocess.run(["sudo", "chmod", "755", str(path)], capture_output=True, text=True, timeout=10)
            subprocess.run(["sudo", "chown", "root", str(path)], capture_output=True, text=True, timeout=10)
            subprocess.run(["sudo", "chgrp", "root", str(path)], capture_output=True, text=True, timeout=10)

        reload_r = subprocess.run(
            prefix + ["systemctl", "daemon-reload"],
            capture_output=True, text=True, timeout=15,
        )
        if reload_r.returncode != 0:
            return {"Success": False, "Detail": f"daemon-reload failed: {reload_r.stderr.strip()}"}

        if enable:
            enable_r = subprocess.run(
                prefix + ["systemctl", "enable", unit_file],
                capture_output=True, text=True, timeout=15,
            )
            if enable_r.returncode != 0:
                return {
                    "Success": False,
                    "Detail": f"systemctl enable failed: {enable_r.stderr.strip()}",
                }

        return {"Success": True, "Detail": f"Unit installed: {path}", "UnitPath": str(path)}
    except Exception as e:
        return {"Success": False, "Detail": f"Exception installing unit file: {e}"}


def expand_kd_args_template(template: List[str], values: Dict[str, str]) -> List[str]:
    result = []
    for token in template:
        val = token
        for k, v in values.items():
            val = val.replace(f"{{{k}}}", v)
        result.append(val)
    return result


def _run_captured(cmd: List[str], cwd: Optional[str] = None, timeout: int = 120) -> Dict[str, Any]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd, timeout=timeout)
        return {"ExitCode": proc.returncode, "StdOut": proc.stdout or "", "StdErr": proc.stderr or ""}
    except subprocess.TimeoutExpired:
        return {"ExitCode": -1, "StdOut": "", "StdErr": "Timeout"}
    except Exception as e:
        return {"ExitCode": -1, "StdOut": "", "StdErr": str(e)}


def _systemctl(*args: str, timeout: int = 30) -> Dict[str, Any]:
    return _run_captured(["systemctl", *args], timeout=timeout)


def _is_root() -> bool:
    try:
        return os.geteuid() == 0
    except AttributeError:
        return False


def _systemctl_needs_sudo() -> List[str]:
    """Prefix a systemctl invocation with sudo if we're not root."""
    return [] if _is_root() else ["sudo"]


# --------------------------------------------------------------------------
# _sc() compatibility shim
# --------------------------------------------------------------------------
#
# Callers elsewhere in the toolkit historically parsed sc.exe-style text
# ("STATE : 4 RUNNING", "STOPPED", returncode 1060 if missing) out of this
# function's .stdout. We preserve that shape here so those call sites keep
# working unmodified, while the real work is done by systemctl.

def _sc(*args: str) -> subprocess.CompletedProcess:
    if not args:
        return subprocess.CompletedProcess(args=[], returncode=-1, stdout="", stderr="No subcommand")

    verb = args[0].lower()
    svc = args[1] if len(args) > 1 else ""
    unit = _unit_name(svc) if svc else ""

    try:
        if verb == "query" or verb == "queryex":
            r = subprocess.run(
                ["systemctl", "show", unit, "--property=LoadState,ActiveState,SubState,MainPID", "--no-pager"],
                capture_output=True, text=True, timeout=15,
            )
            props = dict(
                line.split("=", 1) for line in (r.stdout or "").splitlines() if "=" in line
            )
            load_state = props.get("LoadState", "")
            active_state = props.get("ActiveState", "")
            unit_file_exists = _unit_path(svc).is_file()
            # Unit is truly absent when: LoadState=not-found, or no ActiveState,
            # or (inactive and the unit file itself is gone). Checking the file
            # prevents a race after daemon-reload where systemctl still briefly
            # reports the old unit.
            if (
                load_state == "not-found"
                or not active_state
                or (active_state == "inactive" and not unit_file_exists)
            ):
                return subprocess.CompletedProcess(
                    args=list(args), returncode=1060, stdout="", stderr="Unit not found"
                )
            state_word = "RUNNING" if active_state == "active" else "STOPPED"
            pid = props.get("MainPID", "0")
            stdout = (
                f"SERVICE_NAME: {svc}\n"
                f"        STATE              : {state_word}\n"
                f"        PID                : {pid}\n"
            )
            return subprocess.CompletedProcess(args=list(args), returncode=0, stdout=stdout, stderr="")

        if verb == "start":
            cmd = _systemctl_needs_sudo() + ["systemctl", "start", unit]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            return subprocess.CompletedProcess(args=list(args), returncode=r.returncode, stdout=r.stdout, stderr=r.stderr)

        if verb == "stop":
            cmd = _systemctl_needs_sudo() + ["systemctl", "stop", unit]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            return subprocess.CompletedProcess(args=list(args), returncode=r.returncode, stdout=r.stdout, stderr=r.stderr)

        if verb == "delete":
            # Thorough, idempotent removal of a KD systemd unit:
            # 1. disable + stop (current + legacy unit names)
            # 2. reset-failed
            # 3. remove unit files under /lib/systemd/system and /etc/systemd/system
            # 4. remove leftover enable symlinks under *.wants/
            # 5. daemon-reload
            prefix = _systemctl_needs_sudo()
            paths = _unit_paths_for_cleanup(svc)
            unit_names = sorted({pth.name for pth in paths})

            for uname in unit_names:
                subprocess.run(
                    prefix + ["systemctl", "disable", "--now", uname],
                    capture_output=True, text=True, timeout=30,
                )
                subprocess.run(
                    prefix + ["systemctl", "reset-failed", uname],
                    capture_output=True, text=True, timeout=15,
                )

            def _rm(p: Path) -> None:
                if p.is_file() or p.is_symlink():
                    subprocess.run(
                        prefix + ["rm", "-f", str(p)],
                        capture_output=True, text=True, timeout=15,
                    )

            for path in paths:
                _rm(path)
            for directory in (SYSTEMD_UNIT_DIR, SYSTEMD_UNIT_DIR_LEGACY):
                if directory.is_dir():
                    for wants in directory.glob("*.wants"):
                        for uname in unit_names:
                            _rm(wants / uname)

            subprocess.run(
                prefix + ["systemctl", "daemon-reload"],
                capture_output=True, text=True, timeout=15,
            )

            time.sleep(0.3)
            still = [path for path in paths if path.is_file()]
            if still:
                for path in still:
                    _rm(path)
                subprocess.run(
                    prefix + ["systemctl", "daemon-reload"],
                    capture_output=True, text=True, timeout=15,
                )
                time.sleep(0.2)
                still = [path for path in paths if path.is_file()]

            still_present = bool(still)
            return subprocess.CompletedProcess(
                args=list(args),
                returncode=0 if not still_present else 1,
                stdout="",
                stderr="" if not still_present else f"Unit file still present after delete: {still}",
            )

        if verb == "config":
            # sc config <name> DisplayName=... has no systemd equivalent worth
            # doing (unit Description= would require a rewrite); no-op.
            return subprocess.CompletedProcess(args=list(args), returncode=0, stdout="", stderr="")

        return subprocess.CompletedProcess(args=list(args), returncode=-1, stdout="", stderr=f"Unsupported verb: {verb}")
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(args=list(args), returncode=-1, stdout="", stderr="Timeout")
    except Exception as e:
        return subprocess.CompletedProcess(args=list(args), returncode=-1, stdout="", stderr=str(e))


def set_kd_service_display_name(component: str) -> None:
    """No-op on Linux: unit Description= is set once at unit-file creation time."""
    return


# --------------------------------------------------------------------------
# Unit file writer
# --------------------------------------------------------------------------

def _write_unit_file(
    service_name: str,
    description: str,
    exec_start: str,
    working_directory: str,
    environment: Optional[Dict[str, str]] = None,
    start_mode: str = "Auto",
    user: str = "root",
    group: str = "root",
    restart: str = "on-failure",
) -> Dict[str, Any]:
    """
    Generate a systemd unit (fallback when no vendor template exists) and
    install it under /lib/systemd/system with chmod 755 + root ownership.
    """
    unit = _unit_name(service_name)

    env_lines = ""
    if environment:
        for k, v in environment.items():
            env_lines += f'Environment="{k}={v}"\n'

    wanted_by = "[Install]\nWantedBy=multi-user.target\n" if str(start_mode).lower() in ("auto", "automatic") else ""

    unit_text = (
        f"[Unit]\n"
        f"Description={description}\n"
        f"After=network.target\n"
        f"\n"
        f"[Service]\n"
        f"Type=simple\n"
        f"User={user}\n"
        f"Group={group}\n"
        f"WorkingDirectory={working_directory}\n"
        f"{env_lines}"
        f"ExecStart={exec_start}\n"
        f"Restart={restart}\n"
        f"RestartSec=5\n"
        f"\n"
        f"{wanted_by}"
    )

    enable = str(start_mode).lower() in ("auto", "automatic")
    return _install_unit_file_from_text(unit, unit_text, enable=enable)


# --------------------------------------------------------------------------
# Stop / remove
# --------------------------------------------------------------------------

# Default ACI ports (mirrors config/default-config.json Ports section).
# Used only for orphan-process cleanup when systemctl alone is not enough.
_COMPONENT_DEFAULT_PORTS: Dict[str, List[int]] = {
    "licenseserver": [20000],
    "content": [9100],
    "agentstore": [9050],
    "community": [9030],
    "category": [9020],
    "view": [9080],
    "qms": [16000],
    "qmsagentstore": [9150],
    "answerbankagentstore": [9450],
    "conversationagentstore": [9550],
    "answerserver": [12000],
    "statsserver": [19870],
    "nifi": [8443, 8080],
    "find": [8080],
}


def _component_basename_from_service(service_name: str) -> str:
    """
    Map KD-LicenseServer / kd-licenseserver / licenseserver → licenseserver.
    """
    name = (service_name or "").strip()
    # Strip logical KD- prefix and unit kd- prefix / .service
    for prefix in ("KD-", "kd-", "KD", "kd"):
        if name.lower().startswith(prefix.lower()):
            name = name[len(prefix) :]
            break
    if name.lower().endswith(".service"):
        name = name[: -len(".service")]
    return name.lower().replace(" ", "").replace("_", "")


def _kill_orphan_processes(service_name: str) -> None:
    """
    After systemctl stop/kill, sweep leftover processes that may have been
    started manually or survived a partial uninstall:

      1. pgrep -fa <component-basename>
      2. ss -tulpn looking at the component's default port(s)
      3. kill -9 any matching PIDs (never the toolkit's own python/manage process)
    """
    base = _component_basename_from_service(service_name)
    if not base or base in ("manage", "python", "kdservices"):
        return

    my_pid = os.getpid()
    pids: set[int] = set()

    # 1) pgrep by basename / start- script name
    patterns = [base, f"start-{base}", f"start-{base}.sh"]
    if base.endswith("agentstore"):
        patterns.extend(["agentstore", "start-agentstore", "content", "start-content"])
    for pat in patterns:
        try:
            r = subprocess.run(
                ["pgrep", "-fa", pat],
                capture_output=True, text=True, timeout=8,
            )
            for line in (r.stdout or "").splitlines():
                line = line.strip()
                if not line:
                    continue
                # Skip our own process tree and pure systemctl helpers
                if "manage_kd_services" in line or "manage-kdservices" in line:
                    continue
                if "systemctl" in line and "kd-" in line:
                    continue
                try:
                    pid = int(line.split(None, 1)[0])
                except (ValueError, IndexError):
                    continue
                if pid != my_pid and pid > 1:
                    pids.add(pid)
        except Exception:
            pass

    # 2) Listeners on known ports
    ports = _COMPONENT_DEFAULT_PORTS.get(base, [])
    if ports:
        try:
            # Prefer ss; fall back silently if unavailable
            r = subprocess.run(
                ["ss", "-tulpn"],
                capture_output=True, text=True, timeout=10,
            )
            out = r.stdout or ""
            for port in ports:
                # Match :PORT and extract pid=NNN
                for m in re.finditer(
                    rf":{port}\b.*?pid=(\d+)", out, flags=re.IGNORECASE
                ):
                    try:
                        pid = int(m.group(1))
                        if pid != my_pid and pid > 1:
                            pids.add(pid)
                    except ValueError:
                        pass
        except Exception:
            pass

    if not pids:
        return

    log.warn(
        f"  Orphan process sweep for {service_name}: killing PIDs {sorted(pids)}"
    )
    prefix = ["sudo"] if not _is_root() else []
    for pid in sorted(pids):
        try:
            subprocess.run(
                prefix + ["kill", "-9", str(pid)],
                capture_output=True, text=True, timeout=5,
            )
        except Exception:
            pass


def stop_kd_service_force(service_name: str, timeout_seconds: int = 30) -> bool:
    """
    Aggressively stop a service and wait until inactive (or SIGKILL it),
    then sweep any leftover orphan processes (pgrep + port listeners).
    """
    r = _sc("query", service_name)
    if r.returncode != 0:
        # Even if the unit is gone, still sweep orphans from a previous manual start
        _kill_orphan_processes(service_name)
        return True  # already gone

    if "RUNNING" not in (r.stdout or "").upper():
        # Already stopped – still run orphan sweep (manual starts / partial uninstalls)
        _kill_orphan_processes(service_name)
        return True

    log.info(f"Stopping service {service_name}...")
    _sc("stop", service_name)

    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        r = _sc("query", service_name)
        if "STOPPED" in (r.stdout or "").upper() or r.returncode != 0:
            _kill_orphan_processes(service_name)
            return True
        time.sleep(0.5)

    # Still running after graceful stop - SIGKILL via systemctl kill
    unit = _unit_name(service_name)
    log.warn(f"Service {service_name} still active after {timeout_seconds}s; sending SIGKILL")
    subprocess.run(
        _systemctl_needs_sudo() + ["systemctl", "kill", "-s", "SIGKILL", unit],
        capture_output=True, text=True, timeout=15,
    )
    time.sleep(1)

    _kill_orphan_processes(service_name)

    r = _sc("query", service_name)
    return "STOPPED" in (r.stdout or "").upper() or r.returncode != 0


def remove_kd_service_if_present(service_name: str, timeout_seconds: int = 30) -> Dict[str, Any]:
    """
    Check whether a unit exists and is active; if active, stop it first,
    then delete the unit file. No-op success if it isn't present at all.
    Retries the delete once and verifies both the unit file and systemctl
    LoadState so residual units (common after a previous incomplete uninstall)
    are cleaned reliably.
    """
    r = _sc("query", service_name)
    if r.returncode != 0:
        # Double-check unit files (current + legacy paths) are gone.
        leftover = [path for path in _unit_paths_for_cleanup(service_name) if path.is_file()]
        if leftover:
            log.info(f"  {service_name} not reported by systemctl but unit file(s) exist - removing")
            _sc("delete", service_name)
            time.sleep(0.3)
            leftover = [path for path in _unit_paths_for_cleanup(service_name) if path.is_file()]
            if leftover:
                return {"Success": False, "Detail": f"{service_name} unit file still present at {leftover}"}
            return {"Success": True, "Detail": f"Removed leftover unit file for {service_name}"}
        return {"Success": True, "Detail": f"{service_name} not present; nothing to remove"}

    is_running = "RUNNING" in (r.stdout or "").upper()
    if is_running:
        log.info(f"  {service_name} is running - stopping before delete...")
        stop_kd_service_force(service_name, timeout_seconds)
    else:
        log.info(f"  {service_name} exists but is already stopped - deleting directly")

    del_result = _sc("delete", service_name)
    time.sleep(0.5)

    r2 = _sc("query", service_name)
    leftover = [path for path in _unit_paths_for_cleanup(service_name) if path.is_file()]
    still_there = r2.returncode == 0 or bool(leftover)

    if still_there:
        # One more aggressive attempt (covers timing races right after daemon-reload)
        log.info(f"  {service_name} still visible after first delete - retrying removal...")
        _sc("delete", service_name)
        time.sleep(0.8)
        r2 = _sc("query", service_name)
        leftover = [path for path in _unit_paths_for_cleanup(service_name) if path.is_file()]
        still_there = r2.returncode == 0 or bool(leftover)

    if still_there:
        detail = f"{service_name} still present"
        leftover = [path for path in _unit_paths_for_cleanup(service_name) if path.is_file()]
        if leftover:
            detail += f" (unit file: {leftover[0]})"
        if del_result.stderr:
            detail += f" ({del_result.stderr.strip()})"
        return {"Success": False, "Detail": detail}

    return {"Success": True, "Detail": f"Removed {service_name}"}


# Known vendor unit basenames that belong to KD components
_VENDOR_UNIT_BASENAMES = {
    "licenseserver", "content", "community", "category", "agentstore",
    "qmsagentstore", "answerbankagentstore", "conversationagentstore",
    "qms", "answerserver", "statsserver", "view", "nifi", "find",
}


def list_kd_services() -> List[str]:
    """
    Return all logical KD-* service names (running or not).

    Discovers toolkit units (kd-content.service, kd-nifi.service, ...) and
    any leftover plain vendor-style units (content.service, licenseserver.service)
    from older installs.
    """
    names: List[str] = []

    def _add_from_unit_token(token: str) -> None:
        if not token.endswith(".service"):
            return
        stem = token[:-len(".service")]
        if stem.startswith(KD_UNIT_PREFIX):
            names.append(get_kd_service_name(stem[len(KD_UNIT_PREFIX):]))
        elif stem in _VENDOR_UNIT_BASENAMES:
            names.append(get_kd_service_name(stem))

    try:
        proc = subprocess.run(
            ["systemctl", "list-unit-files", "--type=service", "--no-legend", "--plain"],
            capture_output=True, text=True, timeout=30,
        )
        for line in (proc.stdout or "").splitlines():
            token = line.strip().split()[0] if line.strip() else ""
            _add_from_unit_token(token)
    except Exception:
        pass

    # Also scan unit directories directly
    for directory in (SYSTEMD_UNIT_DIR, SYSTEMD_UNIT_DIR_LEGACY):
        if not directory.is_dir():
            continue
        for path in directory.glob("*.service"):
            _add_from_unit_token(path.name)

    return sorted(set(names))


# --------------------------------------------------------------------------
# Install: generic KD component (Content, Category, Community, ...)
# --------------------------------------------------------------------------

def install_kd_service(
    component: str,
    executable_path: str | Path,
    start_mode: str = "Auto",
    args_template: Optional[List[str]] = None,
    dry_run: bool = False,
    service_user: Optional[str] = None,
    service_group: Optional[str] = None,
    component_path: Optional[str | Path] = None,
) -> Dict[str, Any]:
    """
    Register a native IDOL ACI server binary as a systemd unit using the
    OpenText vendor template when present:

        <InstallDir>/init/systemd/<componentname>.service
          -> /lib/systemd/system/kd-<componentname>.service
        chmod 755; chown/chgrp root; systemctl enable kd-<componentname>

    Placeholders substituted:
      COMPONENT_INSTALL_DIR, USER, GROUP,
      COMPONENT_LD_LIBRARY_PATH, ENVIRONMENT_FILE

    Falls back to a generated Type=simple unit when no vendor template exists.
    Unit names always carry the kd- prefix so they appear under
    ``systemctl list-units --type=service --all "kd*"``.
    """
    svc_name = get_kd_service_name(component)
    exe = Path(executable_path)
    unit_base = vendor_unit_basename(component)

    # Resolve component install directory (COMPONENT_INSTALL_DIR)
    if component_path:
        install_dir = Path(component_path)
    else:
        install_dir = exe.parent
        for marker in ("init", "systemv", "sysv", "systemd", "bin"):
            if install_dir.name.lower() == marker:
                install_dir = install_dir.parent
        if not list(install_dir.glob("*.cfg")) and (install_dir.parent / f"{component.lower()}.cfg").is_file():
            install_dir = install_dir.parent

    user, group = _default_service_user_group()
    if service_user:
        user = service_user
    if service_group:
        group = service_group

    r = _sc("query", svc_name)
    if r.returncode == 0:
        if dry_run:
            return {
                "Success": True, "Skipped": True, "ServiceName": svc_name,
                "Detail": "[DryRun] Unit already exists; would stop-if-running and reinstall",
            }
        cleanup = remove_kd_service_if_present(svc_name)
        if not cleanup["Success"]:
            return {
                "Success": False, "Skipped": False, "ServiceName": svc_name,
                "Detail": f"Could not remove existing unit before reinstall: {cleanup['Detail']}",
            }
        log.info(f"  {cleanup['Detail']}; proceeding with fresh install")

    # Prefer OpenText vendor unit template under <InstallDir>/init/systemd/
    template = _find_vendor_unit_template(install_dir, component)
    if template is not None:
        log.info(f"  Using vendor unit template: {template} -> {_unit_name(svc_name)}")
        try:
            raw = template.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return {
                "Success": False, "Skipped": False, "ServiceName": svc_name,
                "Detail": f"Failed to read vendor unit template {template}: {e}",
            }
        # Agentstore-like: template may still say "content" — point at real binary
        raw = _adapt_unit_template_for_component(
            raw,
            component=component,
            install_dir=install_dir,
            executable=exe if exe.is_file() else None,
        )
        unit_text = _substitute_unit_placeholders(
            raw,
            install_dir=install_dir,
            user=user,
            group=group,
        )
        if dry_run:
            return {
                "Success": True, "Skipped": True, "ServiceName": svc_name,
                "Detail": (
                    f"[DryRun] Would copy {template.name} -> {SYSTEMD_UNIT_DIR / _unit_name(svc_name)} "
                    f"with COMPONENT_INSTALL_DIR={install_dir} USER={user} GROUP={group}"
                ),
            }
        enable = str(start_mode).lower() in ("auto", "automatic")
        result = _install_unit_file_from_text(_unit_name(svc_name), unit_text, enable=enable)
        if not result["Success"]:
            return {"Success": False, "Skipped": False, "ServiceName": svc_name, "Detail": result["Detail"]}
        return {
            "Success": True, "Skipped": False, "ServiceName": svc_name,
            "Detail": f"Installed vendor unit {_unit_name(svc_name)} from {template} (InstallDir={install_dir})",
            "UnitPath": result.get("UnitPath"),
        }

    # Fallback: generated unit (no vendor template)
    values = {"ServiceName": svc_name, "DisplayName": f"OpenText KD {component}", "StartMode": start_mode}
    extra_args: List[str] = []
    if args_template and sys.platform.startswith("win"):
        extra_args = expand_kd_args_template(args_template, values)
    elif args_template and not sys.platform.startswith("win"):
        win_only = {"-install", "-remove", "-servicename", "-displayname", "-start"}
        expanded = expand_kd_args_template(args_template, values)
        skip_next = False
        for tok in expanded:
            if skip_next:
                skip_next = False
                continue
            if tok.lower() in win_only or tok.lower().startswith("-servicename") or tok.lower().startswith("-displayname"):
                if tok.lower() in ("-servicename", "-displayname", "-start", "-install", "-remove"):
                    skip_next = True
                continue
            extra_args.append(tok)
    exec_start = " ".join([str(exe), *extra_args]) if extra_args else str(exe)

    work_dir = install_dir
    env = {}
    lib_candidates = [work_dir, work_dir / "common", work_dir / "common" / "runtimes"]
    ld_parts = [str(d) for d in lib_candidates if d.is_dir()]
    if ld_parts:
        existing = os.environ.get("LD_LIBRARY_PATH", "")
        env["LD_LIBRARY_PATH"] = ":".join(ld_parts + ([existing] if existing else []))

    if dry_run:
        return {
            "Success": True, "Skipped": True, "ServiceName": svc_name,
            "Detail": f"[DryRun] Would write systemd unit ExecStart={exec_start} WorkDir={work_dir}",
        }

    result = _write_unit_file(
        service_name=svc_name,
        description=f"OpenText KD {component}",
        exec_start=exec_start,
        working_directory=str(work_dir),
        environment=env or None,
        start_mode=start_mode,
        user=user,
        group=group,
    )
    if not result["Success"]:
        return {"Success": False, "Skipped": False, "ServiceName": svc_name, "Detail": result["Detail"]}

    return {
        "Success": True, "Skipped": False, "ServiceName": svc_name,
        "Detail": f"Installed generated systemd unit {_unit_name(svc_name)} (no vendor template under {install_dir}/init/systemd)",
    }


# --------------------------------------------------------------------------
# Install: Apache NiFi
# --------------------------------------------------------------------------

def install_kd_nifi_service(
    component: str,
    nifi_cmd_path: str | Path,
    start_mode: str = "Auto",
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Register Apache NiFi as a systemd unit that runs `bin/nifi.sh run` in
    the foreground. NiFi ships an official `nifi.sh` init-style script, so
    this needs no third-party service wrapper -
    it's exactly what upstream Apache NiFi recommends for Linux.

    `nifi_cmd_path` may point at either the legacy `nifi.cmd` toolkit
    launcher or the native `nifi.sh`; either way we resolve NIFI_HOME from
    its parent `bin/` directory and always exec `bin/nifi.sh run`.
    """
    svc_name = get_kd_service_name(component)  # KD-NiFi
    launcher = Path(nifi_cmd_path).resolve()
    bin_dir = launcher.parent
    nifi_home = bin_dir.parent
    nifi_sh = bin_dir / "nifi.sh"
    logs_dir = nifi_home / "logs"

    if dry_run:
        return {
            "Success": True, "Skipped": True, "ServiceName": svc_name,
            "Detail": f"[DryRun] Would write systemd unit: ExecStart={nifi_sh} run",
        }

    if not nifi_sh.is_file():
        return {
            "Success": False, "Skipped": False, "ServiceName": svc_name,
            "Detail": f"nifi.sh not found at {nifi_sh}. Ensure NiFi was extracted correctly under {nifi_home}.",
        }

    # Legacy unit names from earlier toolkit versions
    for legacy in ("KD-Apache-NiFi", "ApacheNifiService"):
        remove_kd_service_if_present(legacy)

    try:
        from . import prerequisites  # local import to avoid circularity
        java_home = os.environ.get("JAVA_HOME") or os.environ.get("JDK_HOME") or prerequisites.get_permanent_env_var("JAVA_HOME")
    except Exception:
        java_home = os.environ.get("JAVA_HOME") or os.environ.get("JDK_HOME")

    env = {"NIFI_HOME": str(nifi_home)}
    if java_home:
        env["JAVA_HOME"] = java_home
    else:
        log.warn("  JAVA_HOME not set — unit may fail to start if java is not on PATH")

    logs_dir.mkdir(parents=True, exist_ok=True)
    chown_to_invoking_user(logs_dir, recursive=False)

    result = _write_unit_file(
        service_name=svc_name,
        description="Apache NiFi Data Flow - OpenText Knowledge Discovery",
        exec_start=f"{nifi_sh} run",
        working_directory=str(nifi_home),
        environment=env,
        start_mode=start_mode,
    )
    if not result["Success"]:
        return {"Success": False, "Skipped": False, "ServiceName": svc_name, "Detail": result["Detail"]}

    enable_r = subprocess.run(
        _systemctl_needs_sudo() + ["systemctl", "enable", _unit_name(svc_name)],
        capture_output=True, text=True, timeout=15,
    )
    if enable_r.returncode != 0:
        return {"Success": False, "Skipped": False, "ServiceName": svc_name, "Detail": f"systemctl enable failed: {enable_r.stderr.strip()}"}

    return {"Success": True, "Skipped": False, "ServiceName": svc_name, "Detail": f"Installed as systemd unit -> {nifi_sh} run"}


# --------------------------------------------------------------------------
# Install: OpenText Find
# --------------------------------------------------------------------------

def install_kd_find_service(
    component: str,
    find_war_path: str | Path,
    start_mode: str = "Auto",
    server_port: str = "8080",
    heap_xms: str = "1g",
    heap_xmx: str = "2g",
    home_dir_name: str = "home",
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Register OpenText Find as a systemd unit running `java -jar find.war`
    directly - no extra service wrapper needed on Linux.
    """
    svc_name = get_kd_service_name(component)  # KD-Find
    find_war = Path(find_war_path).resolve()
    find_home = find_war.parent
    home_dir = find_home / (home_dir_name or "home")

    if dry_run:
        return {
            "Success": True, "Skipped": True, "ServiceName": svc_name,
            "Detail": f"[DryRun] Would install KD-Find via systemd: java -jar {find_war.name} (port={server_port}, home={home_dir})",
        }

    java_exe: Optional[Path] = None
    java_home = (os.environ.get("JAVA_HOME") or os.environ.get("JDK_HOME") or "").strip().strip('"')
    if java_home:
        candidate = Path(java_home) / "bin" / "java"
        if candidate.is_file():
            java_exe = candidate
    if java_exe is None:
        which = shutil.which("java")
        if which:
            java_exe = Path(which)
    if java_exe is None or not java_exe.is_file():
        return {
            "Success": False, "Skipped": False, "ServiceName": svc_name,
            "Detail": "java not found (set JAVA_HOME to a JDK install, or put java on PATH). "
                      "Find requires Java to run the .war. Recommended: `sudo apt install openjdk-21-jdk`.",
        }

    remove_kd_service_if_present(svc_name, timeout_seconds=30)
    home_dir.mkdir(parents=True, exist_ok=True)
    (find_home / "logs").mkdir(parents=True, exist_ok=True)
    chown_to_invoking_user(home_dir, recursive=False)
    chown_to_invoking_user(find_home / "logs", recursive=False)

    exec_start = (
        f'{java_exe} -Xms{heap_xms} -Xmx{heap_xmx} '
        f'-Didol.find.home="{home_dir}" '
        f'-Dserver.port={server_port} '
        f'-jar "{find_war}" '
        f'-uriEncoding utf-8'
    )

    env = {}
    if java_home:
        env["JAVA_HOME"] = java_home

    result = _write_unit_file(
        service_name=svc_name,
        description="OpenText Knowledge Discovery Find (Java search UI)",
        exec_start=exec_start,
        working_directory=str(find_home),
        environment=env,
        start_mode=start_mode,
    )
    if not result["Success"]:
        return {"Success": False, "Skipped": False, "ServiceName": svc_name, "Detail": result["Detail"]}

    enable_r = subprocess.run(
        _systemctl_needs_sudo() + ["systemctl", "enable", _unit_name(svc_name)],
        capture_output=True, text=True, timeout=15,
    )
    if enable_r.returncode != 0:
        return {"Success": False, "Skipped": False, "ServiceName": svc_name, "Detail": f"systemctl enable failed: {enable_r.stderr.strip()}"}

    return {
        "Success": True, "Skipped": False, "ServiceName": svc_name,
        "Detail": f"Installed as systemd unit -> java -jar {find_war.name} (port={server_port}, home={home_dir})",
    }


# --------------------------------------------------------------------------
# Uninstall
# --------------------------------------------------------------------------

def uninstall_kd_service(
    component: str,
    executable_path: Optional[str | Path] = None,
    args_template: Optional[List[str]] = None,
    dry_run: bool = False,
    skip_stop: bool = False,
) -> Dict[str, Any]:
    """
    Remove the systemd unit for a component.

    On Linux the Windows-style binary `-remove` switch does not apply; unit
    removal is identical to `remove_kd_service_if_present` / the manage-services
    `delete` action. This function is kept as the public uninstall entry point
    so callers (installer Uninstall mode, manage_kd_services.py uninstall)
    continue to work — it simply delegates to the same delete path.
    """
    svc_name = get_kd_service_name(component)

    r = _sc("query", svc_name)
    if r.returncode != 0:
        # Also sweep legacy unit names that may still be present
        cleaned = []
        legacy_candidates = [svc_name]
        if component.lower() == "nifi":
            legacy_candidates.extend(["KD-Apache-NiFi", "ApacheNifiService", "KD-NiFi"])
        for legacy_name in legacy_candidates:
            legacy = remove_kd_service_if_present(legacy_name)
            if legacy.get("Success") and "not present" not in (legacy.get("Detail") or ""):
                cleaned.append(legacy_name)
        if cleaned:
            return {"Success": True, "Detail": f"Unit {svc_name} not present; cleaned legacy {', '.join(cleaned)}"}
        return {"Success": True, "Detail": f"Unit {svc_name} not present; nothing to do"}

    if dry_run:
        return {"Success": True, "Detail": f"[DryRun] Would stop and remove unit {svc_name}"}

    try:
        if not skip_stop:
            stopped = stop_kd_service_force(svc_name, 30)
            if not stopped:
                log.warn(f"Could not fully stop {svc_name} before removal; continuing")

        cleanup = remove_kd_service_if_present(svc_name)
        if not cleanup["Success"]:
            cleanup = remove_kd_service_if_present(svc_name)

        r = _sc("query", svc_name)
        still_there = r.returncode == 0
        return {
            "Success": not still_there,
            "Detail": (f"Unit {svc_name} still present" if still_there else f"Removed {svc_name}"),
        }
    except Exception as e:
        return {"Success": False, "Detail": f"Exception during uninstall: {e}"}


def _systemd_failure_detail(service_name: str, max_journal_lines: int = 12) -> str:
    """
    Collect a concise, human-readable failure summary for a unit that is not
    running: Result/ExecMainStatus from systemctl show + last journal lines.
    Best-effort; never raises.
    """
    unit = _unit_name(service_name)
    parts: List[str] = []
    try:
        r = subprocess.run(
            ["systemctl", "show", unit,
             "--property=Result,ExecMainStatus,ExecMainCode,ActiveState,SubState,NRestarts",
             "--no-pager"],
            capture_output=True, text=True, timeout=10,
        )
        props = dict(
            line.split("=", 1) for line in (r.stdout or "").splitlines() if "=" in line
        )
        result = (props.get("Result") or "").strip()
        status = (props.get("ExecMainStatus") or "").strip()
        code = (props.get("ExecMainCode") or "").strip()
        nrestarts = (props.get("NRestarts") or "").strip()
        if result and result not in ("success",):
            parts.append(f"Result={result}")
        if status and status not in ("0",):
            parts.append(f"exit={status}")
        if code and code not in ("0", "exited"):
            parts.append(f"code={code}")
        if nrestarts and nrestarts not in ("0",):
            parts.append(f"restarts={nrestarts}")
    except Exception:
        pass

    try:
        jr = subprocess.run(
            ["journalctl", "-u", unit, "-n", str(max_journal_lines),
             "--no-pager", "-o", "cat"],
            capture_output=True, text=True, timeout=10,
        )
        lines = [
            ln.strip() for ln in (jr.stdout or "").splitlines()
            if ln.strip() and not ln.strip().startswith("--")
        ]
        # Prefer error-ish lines; fall back to last few
        interesting = [
            ln for ln in lines
            if any(k in ln.lower() for k in (
                "error", "fail", "cannot", "unable", "denied", "license",
                "permission", "no such", "not found", "refused", "abort",
                "segfault", "killed", "library", "cfg",
            ))
        ]
        pick = interesting[-6:] if interesting else lines[-4:]
        if pick:
            # Collapse whitespace and keep short
            snippet = " | ".join(
                " ".join(ln.split())[:140] for ln in pick
            )
            parts.append(f"journal: {snippet}")
    except Exception:
        pass

    if not parts:
        parts.append(f"see: journalctl -u {unit} -n 50 --no-pager")
    return "; ".join(parts)


def test_kd_service_healthy(component: str) -> Dict[str, Any]:
    svc_name = get_kd_service_name(component)
    r = _sc("query", svc_name)
    if r.returncode != 0:
        unit = _unit_name(svc_name)
        return {
            "Success": False,
            "Detail": (
                f"Service {svc_name} not found "
                f"(unit {unit} missing or not loaded). "
                f"Re-run Install or: ./manage-kdservices.sh create --components {component}"
            ),
        }
    running = "RUNNING" in (r.stdout or "").upper()
    if running:
        return {"Success": True, "Detail": "Status: Running"}
    # Surface why it is inactive so HEALTH CHECK is actionable
    why = _systemd_failure_detail(svc_name)
    return {
        "Success": False,
        "Detail": f"Status: Not running — {why}",
    }


def start_kd_service(service_name: str, timeout_seconds: int = 30) -> bool:
    """Start a systemd unit and wait until active."""
    result = start_kd_service_detailed(service_name, timeout_seconds)
    return bool(result.get("Success"))


def start_kd_service_detailed(service_name: str, timeout_seconds: int = 30) -> Dict[str, Any]:
    """
    Start a systemd unit and wait until active.
    Returns Success/Detail/State/Win32ExitCode (the latter kept for call-site
    compatibility; always None here since it's a Windows-only concept - use
    `journalctl -u <unit>` for real diagnostics, surfaced in Detail).
    """
    r0 = _sc("query", service_name)
    if r0.returncode != 0:
        return {
            "Success": False, "State": "MISSING", "Win32ExitCode": None,
            "Detail": f"{service_name} is not registered. Run Manage services -> Create all services, or re-run Install.",
        }

    if "RUNNING" in (r0.stdout or "").upper():
        return {"Success": True, "State": "RUNNING", "Win32ExitCode": 0, "Detail": "Already Running"}

    start_result = _sc("start", service_name)

    deadline = time.time() + timeout_seconds
    last_state = "UNKNOWN"
    while time.time() < deadline:
        r = _sc("query", service_name)
        out_u = (r.stdout or "").upper()
        if "RUNNING" in out_u:
            return {"Success": True, "State": "RUNNING", "Win32ExitCode": 0, "Detail": "Running"}
        if "STOPPED" in out_u:
            last_state = "STOPPED"
            break
        last_state = "STARTING"
        time.sleep(1)

    unit = _unit_name(service_name)
    detail_parts = [f"Status: {last_state}"]
    if start_result.returncode != 0 and (start_result.stderr or "").strip():
        detail_parts.append(f"systemctl start: {start_result.stderr.strip()[:200]}")
    # Pull real reason from systemd + journal so operators do not need a second hop
    detail_parts.append(_systemd_failure_detail(service_name))
    detail_parts.append(f"Full log: journalctl -u {unit} -n 80 --no-pager")
    return {"Success": False, "State": last_state, "Win32ExitCode": None, "Detail": " | ".join(detail_parts)}


# --------------------------------------------------------------------------
# NiFi flow-controller readiness polling (log-tail logic unchanged)
# --------------------------------------------------------------------------

NIFI_FLOW_STARTED_MARKER = "Flow Controller started successfully."


def resolve_nifi_home(nifi_home: Optional[str | Path] = None) -> Optional[Path]:
    """Best-effort NIFI_HOME for log tailing."""
    if nifi_home:
        p = Path(str(nifi_home))
        if p.is_dir():
            return p.resolve()
    for key in ("NIFI_HOME", "KD_NIFI_HOME"):
        val = os.environ.get(key)
        if val and Path(val).is_dir():
            return Path(val).resolve()
    try:
        from . import prerequisites
        val = prerequisites.get_permanent_env_var("NIFI_HOME")
        if val and Path(val).is_dir():
            return Path(val).resolve()
    except Exception:
        pass
    for candidate in (Path("/opt/KnowledgeDiscovery/NiFi"),):
        if candidate.is_dir():
            return candidate.resolve()
    return None


def _nifi_app_log_path(nifi_home: Path) -> Path:
    return nifi_home / "logs" / "nifi-app.log"


def _log_contains_marker(log_path: Path, marker: str, from_offset: int = 0) -> bool:
    try:
        if not log_path.is_file():
            return False
        with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
            if from_offset:
                fh.seek(max(0, from_offset))
            while True:
                chunk = fh.read(256 * 1024)
                if not chunk:
                    break
                if marker in chunk:
                    return True
        return False
    except OSError:
        return False


def start_kd_nifi_and_wait_for_flow(
    service_name: str = "KD-NiFi",
    nifi_home: Optional[str | Path] = None,
    timeout_seconds: int = 180,
) -> Dict[str, Any]:
    """
    Start the NiFi systemd unit, then poll `logs/nifi-app.log` until
    "Flow Controller started successfully." appears.
    """
    home = resolve_nifi_home(nifi_home)
    log_path = _nifi_app_log_path(home) if home else None
    marker = NIFI_FLOW_STARTED_MARKER

    start_offset = 0
    if log_path and log_path.is_file():
        try:
            start_offset = log_path.stat().st_size
        except OSError:
            start_offset = 0

    r0 = _sc("query", service_name)
    already_running = r0.returncode == 0 and "RUNNING" in (r0.stdout or "").upper()
    if already_running and log_path and _log_contains_marker(log_path, marker, 0):
        detail = f"{service_name} already Running and log already contains '{marker}' ({log_path})"
        log.info(f"  {detail}")
        return {"Success": True, "ElapsedSeconds": 0.0, "Detail": detail, "LogPath": str(log_path), "MarkerFound": True}

    t0 = time.time()
    if not already_running:
        log.info(f"  Issuing systemctl start {_unit_name(service_name)} ...")
        _sc("start", service_name)
    else:
        log.info(f"  {service_name} is already Running — waiting for Flow Controller marker in log")

    if log_path:
        log.info(f"  Watching: {log_path}")
        log.info(f"  Waiting for: \"{marker}\"  (timeout {timeout_seconds}s)")
    else:
        log.warn("  NIFI_HOME not resolved — cannot tail nifi-app.log; falling back to unit ActiveState only")

    deadline = t0 + timeout_seconds
    marker_found = False
    while time.time() < deadline:
        elapsed = time.time() - t0
        if log_path and _log_contains_marker(log_path, marker, start_offset):
            marker_found = True
            break
        if not log_path:
            r = _sc("query", service_name)
            if "RUNNING" in (r.stdout or "").upper() and elapsed >= 15:
                break
        mins, secs = divmod(int(elapsed), 60)
        # Colored live status on a single overwriting row (see logging.status_line)
        if _status_supports_color():
            msg = (
                f"  {_ANSI_BOLD}{_ANSI_CYAN}NiFi starting ...{_ANSI_RESET} "
                f"{_ANSI_GREEN}{mins:02d}:{secs:02d}{_ANSI_RESET} elapsed "
                f"{_ANSI_DIM}(waiting for Flow Controller){_ANSI_RESET}"
            )
        else:
            msg = f"  NiFi starting ... {mins:02d}:{secs:02d} elapsed (waiting for Flow Controller)"
        status_line(msg)
        time.sleep(1)

    elapsed_total = time.time() - t0
    mins, secs = divmod(int(elapsed_total), 60)
    elapsed_str = f"{mins:02d}:{secs:02d}" if mins else f"{secs}s"
    elapsed_precise = f"{elapsed_total:.1f}s"

    r = _sc("query", service_name)
    running = r.returncode == 0 and "RUNNING" in (r.stdout or "").upper()

    if marker_found:
        detail = f"Flow Controller started successfully after {elapsed_precise} (wall {elapsed_str}). Log: {log_path}"
        if running:
            if _status_supports_color():
                status_line_finish(
                    f"  {_ANSI_BOLD}{_ANSI_GREEN}NiFi Flow Controller ready{_ANSI_RESET} - "
                    f"total start time: {_ANSI_GREEN}{elapsed_precise}{_ANSI_RESET} ({elapsed_str})"
                )
            else:
                status_line_finish(f"  NiFi Flow Controller ready - total start time: {elapsed_precise} ({elapsed_str})")
            log.info(f"  [OK] {detail}")
            return {"Success": True, "ElapsedSeconds": round(elapsed_total, 1), "Detail": detail,
                     "LogPath": str(log_path) if log_path else None, "MarkerFound": True, "ServiceRunning": True}
        detail_warn = f"{detail} - however systemd unit {_unit_name(service_name)} is not active (check `systemctl status` and nifi-app.log)."
        if _status_supports_color():
            status_line_finish(
                f"  {_ANSI_YELLOW}NiFi Flow Controller marker seen after {elapsed_precise}, "
                f"but unit is not active (WARNING){_ANSI_RESET}"
            )
        else:
            status_line_finish(f"  NiFi Flow Controller marker seen after {elapsed_precise}, but unit is not active (WARNING)")
        log.warn(f"  {detail_warn}")
        return {"Success": True, "ElapsedSeconds": round(elapsed_total, 1), "Detail": detail_warn,
                 "LogPath": str(log_path) if log_path else None, "MarkerFound": True, "ServiceRunning": False, "Warning": True}

    if running:
        detail = (f"Timeout after {elapsed_precise}: unit is active but '{marker}' not seen in "
                   f"{log_path or '(no log path)'}. NiFi may still be bootstrapping - check nifi-app.log.")
        if _status_supports_color():
            status_line_finish(
                f"  {_ANSI_YELLOW}NiFi unit active but Flow Controller marker not seen after {elapsed_precise}{_ANSI_RESET}"
            )
        else:
            status_line_finish(f"  NiFi unit active but Flow Controller marker not seen after {elapsed_precise}")
        log.warn(f"  {detail}")
        return {"Success": True, "ElapsedSeconds": round(elapsed_total, 1), "Detail": detail,
                 "LogPath": str(log_path) if log_path else None, "MarkerFound": False, "ServiceRunning": True, "Warning": True}

    older_marker = bool(log_path and _log_contains_marker(log_path, marker, 0))
    if older_marker:
        detail = (f"Timeout after {elapsed_precise}: unit not active, but log already contains '{marker}' "
                   f"(possibly from a previous start). Check {log_path or 'nifi-app.log'} and unit status.")
        if _status_supports_color():
            status_line_finish(
                f"  {_ANSI_YELLOW}NiFi unit not active after {elapsed_precise} "
                f"(Flow Controller marker present in log - WARNING){_ANSI_RESET}"
            )
        else:
            status_line_finish(f"  NiFi unit not active after {elapsed_precise} (Flow Controller marker present in log - WARNING)")
        log.warn(f"  {detail}")
        return {"Success": True, "ElapsedSeconds": round(elapsed_total, 1), "Detail": detail,
                 "LogPath": str(log_path) if log_path else None, "MarkerFound": True, "ServiceRunning": False, "Warning": True}

    detail = f"Timeout after {elapsed_precise}: unit not active and '{marker}' not found in {log_path or '(no log path)'}"
    if _status_supports_color():
        status_line_finish(f"  {_ANSI_BOLD}\033[31mNiFi failed to start within {elapsed_precise}{_ANSI_RESET}")
    else:
        status_line_finish(f"  NiFi failed to start within {elapsed_precise}")
    log.error(f"  {detail}")
    return {"Success": False, "ElapsedSeconds": round(elapsed_total, 1), "Detail": detail,
             "LogPath": str(log_path) if log_path else None, "MarkerFound": False, "ServiceRunning": False}
