#!/usr/bin/env python3
"""
Manage Knowledge Discovery (KD) systemd services on Linux.

By default reads Components from config/my-config.json (or --config) and maps
each entry to the logical service name KD-<Component> (e.g. Content →
KD-Content, NiFi → KD-NiFi). The on-disk unit is always installed as
kd-<componentname>.service (e.g. kd-content.service).

With --all-deployed (or when Components is empty), discovers every registered
systemd unit whose name starts with ``kd-`` (or the logical KD- prefix) that
is present on this machine and operates on those — so you can start / stop /
status any of the deployed services even if they are not listed in the config
JSON.

Actions:
  status     Show Running / Stopped / Missing for each service
  start      Start services (LicenseServer first, then the rest; NiFi waits
             for Flow Controller in nifi-app.log)
  stop       Stop services (others first, LicenseServer last). Always runs
             an orphan-process sweep (pgrep + default ports) after systemctl.
  restart    stop then start
  delete     Force-stop (including orphan sweep) and remove the systemd unit
             file (/lib/systemd/system/kd-<componentname>.service). Install
             folders under BasePath are left alone.
  uninstall  Alias for delete (kept so existing scripts keep working). Does
             not delete install folders (use install-kd.sh --mode Uninstall
             for that).
  create     Register systemd units for extracted components under BasePath.
             Standard components use the OpenText vendor unit template under
             <InstallDir>/init/systemd/<componentname>.service (placeholders
             substituted, adapted for the real binary/start-*.sh, written as
             /lib/systemd/system/kd-<name>.service, chmod 755, root:root,
             systemctl enable). NiFi/Find fall back to generated units when no
             vendor template exists. LicenseServer first. Re-creates if present.

Examples:
  python manage_kd_services.py status
  python manage_kd_services.py start --all-deployed
  python manage_kd_services.py start --components Content,NiFi
  python manage_kd_services.py start --components KD-Content,KD-Community
  python manage_kd_services.py stop --config config/my-config.json
  python manage_kd_services.py restart --all-deployed --non-interactive
  python manage_kd_services.py delete --components Content,Community
  python manage_kd_services.py uninstall --force
  python manage_kd_services.py create --non-interactive
  python manage_kd_services.py delete --all-deployed --force

Requires Administrator for start/stop/delete/uninstall (status works without).
"""

from __future__ import annotations

def _is_package_only(component: str) -> bool:
    return (component or "").strip().lower() in {"nifiingest"}


_ANSI_YELLOW = "\033[33m"
_ANSI_RESET = "\033[0m"

import argparse
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

from kd.config import get_kd_config
from kd import discovery, service_manager
from kd.logging import log


ACTIONS = ("status", "start", "stop", "restart", "delete", "uninstall", "create")


def _is_elevated() -> bool:
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _service_status(service_name: str) -> Dict[str, Any]:
    """Return {Name, Exists, State, Detail} via systemctl (Linux)."""
    import subprocess

    unit = service_manager._unit_name(service_name)
    try:
        # is-active: active | inactive | failed | activating | ...
        active_r = subprocess.run(
            ["systemctl", "is-active", unit],
            capture_output=True, text=True, timeout=15,
        )
        active = (active_r.stdout or "").strip() or "unknown"

        show_r = subprocess.run(
            ["systemctl", "show", unit, "-p", "LoadState", "-p", "ActiveState", "-p", "SubState", "-p", "Description", "--value"],
            capture_output=True, text=True, timeout=15,
        )
        show_lines = [ln.strip() for ln in (show_r.stdout or "").splitlines()]
        # --value prints values only, in order of -p
        load_state = show_lines[0] if len(show_lines) > 0 else ""
        active_state = show_lines[1] if len(show_lines) > 1 else active
        sub_state = show_lines[2] if len(show_lines) > 2 else ""
        description = show_lines[3] if len(show_lines) > 3 else ""

        if load_state in ("not-found", "masked") or active_r.returncode != 0 and active in ("unknown", "inactive") and load_state == "not-found":
            # Fall back to unit file presence
            path = service_manager._unit_path(service_name)
            if not path.is_file() and not any(p.is_file() for p in service_manager._unit_paths_for_cleanup(service_name)):
                return {
                    "Name": service_name,
                    "Exists": False,
                    "State": "Missing",
                    "Detail": f"unit not found ({unit})",
                }

        # Map to friendly state
        if active in ("active",) or active_state == "active":
            state = "Running"
        elif active in ("failed",) or active_state == "failed":
            state = "Failed"
        elif active in ("activating", "reloading") or active_state in ("activating", "reloading"):
            state = "Starting"
        elif active in ("deactivating",) or active_state == "deactivating":
            state = "Stopping"
        else:
            state = "Stopped"

        detail_parts = []
        if description:
            detail_parts.append(description)
        if sub_state:
            detail_parts.append(f"sub={sub_state}")
        detail_parts.append(unit)
        return {
            "Name": service_name,
            "Exists": True,
            "State": state,
            "Detail": " | ".join(detail_parts),
        }
    except FileNotFoundError:
        return {
            "Name": service_name,
            "Exists": False,
            "State": "Unavailable",
            "Detail": "systemctl not found",
        }
    except Exception as e:
        return {
            "Name": service_name,
            "Exists": False,
            "State": "Error",
            "Detail": str(e),
        }


def _normalize_component(token: str) -> str:
    """
    Accept either a component name (Content) or a full service name (KD-Content).
    Returns the component form used by the rest of the toolkit.
    """
    token = (token or "").strip()
    if not token:
        return token
    return service_manager.service_name_to_component(token)


def _ordered_components(components: Sequence[str], *, for_start: bool) -> List[str]:
    """
    Start: LicenseServer first, then others (stable order).
    Stop/delete/uninstall: others first, LicenseServer last.
    """
    comps = list(components)
    license_keys = [c for c in comps if c.lower() == "licenseserver"]
    rest = [c for c in comps if c.lower() != "licenseserver"]
    if for_start:
        return license_keys + rest
    return rest + license_keys


def _components_from_deployed_services() -> List[str]:
    """
    Discover every registered KD systemd unit (kd-*.service / logical KD-*)
    and map them back to component names (KD-Content → Content).
    """
    services = service_manager.list_kd_services()
    components: List[str] = []
    seen = set()
    for svc in services:
        comp = service_manager.service_name_to_component(svc)
        key = comp.lower()
        if key in seen:
            continue
        seen.add(key)
        components.append(comp)
    return components


def _resolve_components(
    explicit: Optional[str],
    config_components: Sequence[str],
    *,
    all_deployed: bool,
) -> List[str]:
    """
    Resolve the component list for this run.

    Priority:
      1. --components LIST (comma-separated; accepts Content or KD-Content)
      2. --all-deployed → every KD-* service registered on this machine
      3. config Components
      4. fallback: every KD-* service registered on this machine
    """
    if explicit:
        return [
            _normalize_component(c)
            for c in explicit.split(",")
            if c.strip()
        ]

    if all_deployed:
        discovered = _components_from_deployed_services()
        if discovered:
            return discovered
        # Fall through to config if nothing is registered yet
        return list(config_components)

    if config_components:
        return list(config_components)

    # No config Components and no --components → operate on whatever is deployed
    return _components_from_deployed_services()


def _find_component_executable(base_path: Path, component: str) -> Optional[Path]:
    comp_dir = base_path / component
    if not comp_dir.is_dir():
        # Case-insensitive folder match (path casing may differ from the
        # component name used in service registration).
        parent = base_path
        if parent.is_dir():
            for child in parent.iterdir():
                if child.is_dir() and child.name.lower() == component.lower():
                    comp_dir = child
                    break
        if not comp_dir.is_dir():
            return None
    result = discovery.find_kd_component_executable(comp_dir, component)
    if result.get("Success") and result.get("Executable"):
        return Path(result["Executable"])
    return None


def _ansi(enabled: bool) -> Dict[str, str]:
    if not enabled or not sys.stdout.isatty():
        return {k: "" for k in ("reset", "bold", "dim", "green", "red", "yellow", "cyan", "white")}
    return {
        "reset": "\033[0m",
        "bold": "\033[1m",
        "dim": "\033[2m",
        "green": "\033[32m",
        "red": "\033[31m",
        "yellow": "\033[33m",
        "cyan": "\033[36m",
        "white": "\033[37m",
    }


def _color_state(state: str, c: Dict[str, str]) -> str:
    s = (state or "").strip().lower()
    if s in ("running", "active"):
        return f"{c['green']}{c['bold']}{state}{c['reset']}"
    if s in ("stopped", "inactive", "dead", "missing"):
        return f"{c['red']}{state}{c['reset']}"
    if s in ("failed", "error"):
        return f"{c['red']}{c['bold']}{state}{c['reset']}"
    if s in ("activating", "deactivating", "startpending", "stoppending", "reloading"):
        return f"{c['yellow']}{state}{c['reset']}"
    return f"{c['dim']}{state}{c['reset']}"


def _systemctl_list_kd_units() -> List[Dict[str, str]]:
    """
    Merge systemctl list-units and list-unit-files for kd-* (and known vendor
    unit names) into a list of dicts: unit, load, active, sub, description.
    """
    import subprocess

    rows: Dict[str, Dict[str, str]] = {}

    def _run(args: List[str]) -> str:
        try:
            r = subprocess.run(
                args, capture_output=True, text=True, timeout=30
            )
            return r.stdout or ""
        except Exception:
            return ""

    # Active/loaded units matching kd-*
    out = _run(
        ["systemctl", "list-units", "--type=service", "--all", "--no-legend", "--no-pager", "--plain", "kd-*"]
    )
    for line in out.splitlines():
        parts = line.split(None, 4)
        if len(parts) < 4:
            continue
        unit, load, active, sub = parts[0], parts[1], parts[2], parts[3]
        desc = parts[4] if len(parts) > 4 else ""
        if not unit.endswith(".service"):
            continue
        rows[unit] = {
            "unit": unit,
            "load": load,
            "active": active,
            "sub": sub,
            "description": desc,
            "source": "kd-*",
        }

    # Unit files for kd-* (captures disabled / not-found load states)
    out_files = _run(
        ["systemctl", "list-unit-files", "--type=service", "--no-legend", "--no-pager", "kd-*"]
    )
    for line in out_files.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        unit, state = parts[0], parts[1]
        if not unit.endswith(".service"):
            continue
        if unit not in rows:
            rows[unit] = {
                "unit": unit,
                "load": "loaded" if state not in ("not-found",) else "not-found",
                "active": "inactive",
                "sub": "dead",
                "description": f"unit-file state={state}",
                "source": "kd-*",
            }
        else:
            rows[unit]["description"] = rows[unit].get("description") or f"unit-file state={state}"

    return list(rows.values())


def action_status(components: Sequence[str]) -> int:
    """
    Show a single aligned, colorized table that merges:
      - configured Components (logical KD-* names → vendor/kd unit)
      - every systemd unit matching kd-* (systemctl list-units --all "kd-*")
    """
    c = _ansi(True)
    # Collect rows keyed by unit file name to de-dupe
    table: Dict[str, Dict[str, str]] = {}

    for comp in components:
        svc = service_manager.get_kd_service_name(comp)
        unit = service_manager._unit_name(svc)
        st = _service_status(svc)
        # Prefer live systemctl show when available
        active = st["State"]
        table[unit] = {
            "component": comp,
            "service": svc,
            "unit": unit,
            "state": active,
            "detail": st.get("Detail") or "",
            "source": "config",
        }

    for row in _systemctl_list_kd_units():
        unit = row["unit"]
        state = row.get("active") or "unknown"
        # Prefer sub-state when active is generic
        if row.get("sub") and state.lower() in ("active", "inactive"):
            display_state = row["sub"] if state.lower() == "inactive" else state
        else:
            display_state = state
        if unit in table:
            table[unit]["state"] = display_state if display_state else table[unit]["state"]
            if row.get("description"):
                table[unit]["detail"] = row["description"]
            table[unit]["source"] = "config+kd-*"
        else:
            # Derive component-ish label from unit name
            stem = unit[: -len(".service")] if unit.endswith(".service") else unit
            if stem.startswith("kd-"):
                label = stem[3:]
            else:
                label = stem
            table[unit] = {
                "component": label,
                "service": service_manager.get_kd_service_name(label),
                "unit": unit,
                "state": display_state,
                "detail": row.get("description") or "",
                "source": "kd-*",
            }

    # Column widths
    headers = ("Component", "Unit", "State", "Source", "Detail")
    rows_list = sorted(table.values(), key=lambda r: (r["unit"] or "").lower())
    col_w = [len(h) for h in headers]
    for r in rows_list:
        col_w[0] = max(col_w[0], len(r["component"]))
        col_w[1] = max(col_w[1], len(r["unit"]))
        col_w[2] = max(col_w[2], len(r["state"]))
        col_w[3] = max(col_w[3], len(r["source"]))
        # detail truncated later

    def fmt_row(vals, colorize_state=False):
        cells = []
        for i, v in enumerate(vals):
            if i == 2 and colorize_state:
                # pad then color (pad based on plain length)
                plain = f"{v:<{col_w[i]}}"
                colored = _color_state(v, c)
                # re-pad: colored string has ansi codes so use plain width spacing
                pad = col_w[i] - len(v)
                cells.append(colored + (" " * max(0, pad)))
            else:
                width = col_w[i] if i < 4 else None
                if width is not None:
                    cells.append(f"{v:<{width}}")
                else:
                    cells.append(v[:48])
        return "  ".join(cells)

    print()
    print(f"{c['bold']}{c['cyan']}KD systemd services{c['reset']}  "
          f"{c['dim']}(config Components + systemctl list-units --all 'kd-*'){c['reset']}")
    print(f"{c['bold']}{fmt_row(headers)}{c['reset']}")
    print(f"{c['dim']}{'-' * (sum(col_w[:4]) + 8 + 48)}{c['reset']}")
    if not rows_list:
        print(f"{c['yellow']}  (no services found){c['reset']}")
    for r in rows_list:
        print(fmt_row(
            [r["component"], r["unit"], r["state"], r["source"], r["detail"]],
            colorize_state=True,
        ))
    print()
    running = sum(1 for r in rows_list if (r["state"] or "").lower() in ("running", "active"))
    total = len(rows_list)
    print(
        f"{c['dim']}Total: {total}  "
        f"{c['green']}running/active: {running}{c['reset']}{c['dim']}  "
        f"other: {total - running}{c['reset']}"
    )
    print()
    return 0


def action_start(
    components: Sequence[str],
    dry_run: bool,
    base_path: Optional[Path] = None,
) -> int:
    ordered = _ordered_components(components, for_start=True)
    failed: List[str] = []
    for comp in ordered:
        svc = service_manager.get_kd_service_name(comp)
        st = _service_status(svc)
        if not st["Exists"]:
            log.warn(f"{svc} not installed — skip start")
            failed.append(comp)
            continue
        if dry_run:
            log.info(f"[DryRun] Would start {svc}")
            continue

        if comp.lower() == "nifi":
            nifi_home = (base_path / "NiFi") if base_path else None
            log.info(
                f"Starting {svc} (waiting for Flow Controller in nifi-app.log)..."
            )
            result = service_manager.start_kd_nifi_and_wait_for_flow(
                service_name=svc,
                nifi_home=nifi_home,
                timeout_seconds=180,
            )
            if result.get("Success"):
                elapsed = result.get("ElapsedSeconds", 0)
                marker = "yes" if result.get("MarkerFound") else "no"
                svc_run = "yes" if result.get("ServiceRunning", True) else "no"
                msg = (
                    f"  {svc} → ready (elapsed {elapsed}s, "
                    f"Flow Controller marker={marker}, service RUNNING={svc_run})"
                )
                if result.get("Warning"):
                    # Marker seen / soft timeout — orange WARNING, not ERROR
                    log.warn(msg + "  [WARNING]")
                else:
                    log.info(msg)
            else:
                log.error(f"  {svc} failed: {result.get('Detail')}")
                failed.append(comp)
            continue

        if st["State"] == "Running":
            log.info(f"{svc} already Running")
            continue
        log.info(f"Starting {svc}...")
        ok = service_manager.start_kd_service(svc, timeout_seconds=45)
        if ok:
            log.info(f"  {svc} → Running")
        else:
            log.error(f"  {svc} failed to reach Running")
            failed.append(comp)
    return 1 if failed else 0


def action_stop(components: Sequence[str], dry_run: bool) -> int:
    ordered = _ordered_components(components, for_start=False)
    failed: List[str] = []
    for comp in ordered:
        svc = service_manager.get_kd_service_name(comp)
        st = _service_status(svc)
        if not st["Exists"]:
            log.info(f"{svc} not present — nothing to stop")
            continue
        if st["State"] == "Stopped":
            log.info(f"{svc} already Stopped")
            continue
        if dry_run:
            log.info(f"[DryRun] Would stop {svc}")
            continue
        ok = service_manager.stop_kd_service_force(svc, timeout_seconds=45)
        if ok:
            log.info(f"  {svc} → Stopped")
        else:
            log.error(f"  {svc} could not be stopped")
            failed.append(comp)
    return 1 if failed else 0


def action_restart(
    components: Sequence[str],
    dry_run: bool,
    base_path: Optional[Path] = None,
) -> int:
    rc = action_stop(components, dry_run)
    rc2 = action_start(components, dry_run, base_path=base_path)
    return rc or rc2


def action_delete(components: Sequence[str], dry_run: bool) -> int:
    """
    Stop (if running) and remove the systemd unit for each component.
    Install folders under BasePath are left alone.
    """
    ordered = _ordered_components(components, for_start=False)
    failed: List[str] = []
    for comp in ordered:
        svc = service_manager.get_kd_service_name(comp)
        st = _service_status(svc)
        if not st["Exists"]:
            log.info(f"{svc} not present — nothing to delete")
            continue
        if dry_run:
            log.info(f"[DryRun] Would stop-if-running and delete {svc}")
            continue
        result = service_manager.remove_kd_service_if_present(svc, timeout_seconds=45)
        if result.get("Success"):
            log.info(f"  {result.get('Detail', f'Deleted {svc}')}")
        else:
            log.error(f"  {result.get('Detail', f'Failed to delete {svc}')}")
            failed.append(comp)
    return 1 if failed else 0


def action_uninstall(
    components: Sequence[str],
    base_path: Path,
    uninstall_template: Optional[List[str]],
    dry_run: bool,
) -> int:
    """
    Alias of action_delete on Linux.

    Unit removal is the only operation (the old Windows binary -remove path
    does not apply), so uninstall delegates to delete. Does not remove
    BasePath folders (use install-kd.sh --mode Uninstall).
    """
    # uninstall_template / base_path retained for CLI compatibility only
    _ = (base_path, uninstall_template)
    return action_delete(components, dry_run)


def action_create(
    components: Sequence[str],
    base_path: Path,
    *,
    install_template: Optional[List[str]],
    start_mode: str,
    nifi_install_service: bool,
    find_install_service: bool = True,
    find_cfg: Optional[Dict[str, Any]] = None,
    dry_run: bool,
) -> int:
    """
    Create / re-register systemd units for already-extracted components under
    BasePath. Does not extract ZIPs or edit .cfg files — use Install or
    Configure for that.

    Order: LicenseServer first, then remaining (same as start).
    NiFi uses install_kd_nifi_service (bin/nifi.sh run);
    Find uses install_kd_find_service (java -jar find.war);
    every other component uses install_kd_service (vendor template or
    generated unit, always written as kd-<name>.service).
    """
    ordered = _ordered_components(components, for_start=True)
    if not ordered:
        log.error("No components to create services for")
        return 1

    if not base_path.is_dir():
        log.error(f"BasePath does not exist: {base_path}")
        log.error("  Extract components first (Install / Extract only), then re-run create.")
        return 1

    log.info(f"Creating services under BasePath={base_path}")
    log.info(f"  Order: {', '.join(ordered)}")
    failed: List[str] = []
    find_cfg = find_cfg or {}

    for comp in ordered:
        svc = service_manager.get_kd_service_name(comp)
        comp_dir = base_path / comp
        if not comp_dir.is_dir():
            # case-insensitive
            if base_path.is_dir():
                for child in base_path.iterdir():
                    if child.is_dir() and child.name.lower() == comp.lower():
                        comp_dir = child
                        break
        if not comp_dir.is_dir():
            log.warn(f"  {comp}: folder not found under {base_path} — skip create")
            failed.append(comp)
            continue

        if comp.lower() == "nifi":
            if not nifi_install_service:
                log.info(f"  {svc}: skipped (NiFi.InstallService is false)")
                continue
            exe = _find_component_executable(base_path, comp)
            if exe is None:
                # Prefer bin/nifi.sh (Linux); fall back to nifi.cmd only if needed
                for name in ("nifi.sh", "nifi.cmd"):
                    candidate = comp_dir / "bin" / name
                    if candidate.is_file():
                        exe = candidate
                        break
            if exe is None:
                log.error(f"  {svc}: nifi.sh not found under {comp_dir}/bin")
                failed.append(comp)
                continue
            log.info(f"  Creating {svc} via systemd ({exe.name}) ...")
            result = service_manager.install_kd_nifi_service(
                component=comp,
                nifi_cmd_path=exe,
                start_mode=start_mode,
                dry_run=dry_run,
            )
        elif comp.lower() == "find":
            if not find_install_service:
                log.info(f"  {svc}: skipped (Find.InstallService is false)")
                continue
            exe = _find_component_executable(base_path, comp)
            if exe is None:
                # Prefer find.war at component root
                war = comp_dir / "find.war"
                if war.is_file():
                    exe = war
            if exe is None:
                log.error(f"  {svc}: find.war not found under {comp_dir}")
                failed.append(comp)
                continue
            port = str(
                find_cfg.get("ServerPort")
                or (find_cfg.get("Ports") or {}).get("Find")
                or "8080"
            )
            log.info(f"  Creating {svc} (java -jar {exe.name}) ...")
            result = service_manager.install_kd_find_service(
                component=comp,
                find_war_path=exe,
                start_mode=start_mode,
                server_port=port,
                heap_xms=str(find_cfg.get("HeapXms") or "1g"),
                heap_xmx=str(find_cfg.get("HeapXmx") or "2g"),
                home_dir_name=str(find_cfg.get("HomeDir") or "home"),
                dry_run=dry_run,
            )
        else:
            exe = _find_component_executable(base_path, comp)
            if exe is None:
                # Surface the discovery reason (wrong-arch ZIP, missing +x, etc.)
                from kd import discovery as _disc
                detail = _disc.find_kd_component_executable(comp_dir, comp).get("Reason") or "not found"
                log.error(f"  {svc}: component executable not found under {comp_dir} — {detail}")
                failed.append(comp)
                continue
            log.info(f"  Creating {svc} via systemd ({exe.name}) ...")
            result = service_manager.install_kd_service(
                component=comp,
                executable_path=exe,
                start_mode=start_mode,
                args_template=install_template,
                dry_run=dry_run,
                component_path=comp_dir,
            )

        if result.get("Success"):
            detail = result.get("Detail") or "OK"
            log.info(f"  {svc}: {detail}")
        else:
            log.error(f"  {svc}: {result.get('Detail') or 'create failed'}")
            failed.append(comp)

    if failed:
        log.warn(f"Create finished with failures: {', '.join(failed)}")
        return 1
    log.info("All requested services created successfully.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="manage_kd_services.py",
        description=(
            "Start / stop / delete / uninstall KD systemd services on Linux. "
            "Targets config Components by default, or every deployed kd-* "
            "unit with --all-deployed."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  python manage_kd_services.py status
  python manage_kd_services.py start --all-deployed
  python manage_kd_services.py stop
  python manage_kd_services.py start --config config/my-config.json
  python manage_kd_services.py restart --components Content,NiFi
  python manage_kd_services.py start --components KD-Content,KD-Community
  python manage_kd_services.py delete --force
  python manage_kd_services.py uninstall --non-interactive
  python manage_kd_services.py create --non-interactive
  python manage_kd_services.py delete --all-deployed --force

service names:
  Logical name: KD-<Name>  (NiFi → KD-NiFi).
  On-disk unit: kd-<name>.service  (kd-nifi.service, kd-content.service, …).
  --components accepts either form: Content  or  KD-Content.

discovery:
  --all-deployed  operate on every kd-*.service unit registered on this
                  machine (not limited to config Components).
  If Components is empty and --components is omitted, discovery is used
  automatically so any deployed service can still be started/stopped.

order:
  start     : LicenseServer first, then remaining order
  stop/delete/uninstall : remaining first, LicenseServer last
  create   : LicenseServer first, then remaining (vendor template or
             generated unit; NiFi via bin/nifi.sh)
""",
    )
    p.add_argument(
        "action",
        choices=ACTIONS,
        help="Operation to perform",
    )
    p.add_argument(
        "--config",
        dest="config_path",
        metavar="PATH",
        help="JSON config (default: config/my-config.json or config/default-config.json)",
    )
    p.add_argument(
        "--components",
        metavar="LIST",
        help=(
            "Override target list (comma-separated). Accepts component names "
            "or full service names, e.g. Content,NiFi or KD-Content,KD-NiFi"
        ),
    )
    p.add_argument(
        "--all-deployed",
        "-a",
        action="store_true",
        help=(
            "Operate on every kd-*.service / KD-* unit registered on this "
            "machine (ignores config Components unless none are found)"
        ),
    )
    p.add_argument("--dry-run", action="store_true", help="Print actions only")
    p.add_argument(
        "--force",
        action="store_true",
        help="Skip confirmation for delete/uninstall",
    )
    p.add_argument(
        "--non-interactive",
        action="store_true",
        help="No prompts (implies --force for destructive actions)",
    )
    return p


def _resolve_config_path(explicit: Optional[str]) -> Path:
    root = Path(__file__).resolve().parent
    if explicit:
        return Path(explicit)
    for rel in ("config/my-config.json", "config/default-config.json"):
        candidate = root / rel
        if candidate.is_file():
            return candidate
    return root / "config/default-config.json"


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    action = args.action

    needs_admin = action in ("start", "stop", "restart", "delete", "uninstall", "create")
    # On Linux these actions normally need root (or passwordless sudo) for
    # systemctl / writing to /lib/systemd/system. We do not hard-fail here;
    # the underlying helpers will surface permission errors clearly.
    if needs_admin and sys.platform == "win32":
        # Legacy guard kept only for any accidental Windows run of this script.
        print(
            "ERROR: This toolkit is Linux-only. Use the Linux manage-kdservices.sh wrapper.",
            file=sys.stderr,
        )
        return 1

    config_path = _resolve_config_path(args.config_path)
    config: Dict[str, Any] = {}
    try:
        config = get_kd_config(config_path)
    except Exception as e:
        # Config is optional when --all-deployed or --components is given
        if not args.all_deployed and not args.components:
            print(f"FATAL: cannot load config ({config_path}): {e}", file=sys.stderr)
            return 1
        print(f"WARNING: cannot load config ({config_path}): {e}", file=sys.stderr)
        print("         Continuing with discovered / explicit service list only.", file=sys.stderr)

    components = _resolve_components(
        args.components,
        [c for c in (config.get("Components") or []) if not _is_package_only(c)],
        all_deployed=bool(args.all_deployed),
    )

    if not components:
        print(
            "FATAL: No services to manage.\n"
            "  - Set Components in the config JSON, or\n"
            "  - Pass --components Content,NiFi, or\n"
            "  - Pass --all-deployed (requires at least one KD-* service installed).",
            file=sys.stderr,
        )
        return 1

    base_path = Path(config.get("BasePath") or ".")
    uninstall_template = config.get("ServiceUninstallArgsTemplate")

    # Prefer BasePath\logs when it exists; otherwise temp (avoids failing on
    # a not-yet-created install path during status checks).
    log_dir = base_path / "logs"
    if not log_dir.parent.is_dir():
        log_dir = Path(os.environ.get("TEMP") or os.environ.get("TMP") or "/tmp")
    try:
        log.initialize(log_dir)
    except Exception:
        try:
            log.initialize(Path(os.environ.get("TEMP") or "/tmp"))
        except Exception:
            pass

    source = (
        "explicit --components"
        if args.components
        else ("all deployed KD-* services" if args.all_deployed else "config Components")
    )
    # If we fell back to discovery because config Components was empty:
    if not args.components and not args.all_deployed and not (config.get("Components") or []):
        source = "all deployed KD-* services (config Components empty)"

    print("=" * 60)
    print(f"  KD Service Manager  |  action={action}")
    print(f"  Config : {config_path}")
    print(f"  BasePath: {base_path}")
    print(f"  Source : {source}")
    print(f"  Targets ({len(components)}): {', '.join(components)}")
    print("=" * 60)

    destructive = action in ("delete", "uninstall")
    auto_yes = args.force or args.non_interactive or args.dry_run
    if destructive and not auto_yes:
        print()
        print(f"This will {action} these KD systemd services:")
        for c in components:
            print(f"  - {service_manager.get_kd_service_name(c)}")
        try:
            answer = input(f"{_ANSI_YELLOW}Type YES to continue: {_ANSI_RESET}").strip()
        except EOFError:
            answer = ""
        if answer != "YES":
            print("Aborted.")
            return 0

    if action == "status":
        return action_status(components)
    if action == "start":
        return action_start(components, args.dry_run, base_path=base_path)
    if action == "stop":
        return action_stop(components, args.dry_run)
    if action == "restart":
        return action_restart(components, args.dry_run, base_path=base_path)
    if action == "delete":
        return action_delete(components, args.dry_run)
    if action == "uninstall":
        return action_uninstall(
            components,
            base_path=base_path,
            uninstall_template=uninstall_template,
            dry_run=args.dry_run,
        )
    if action == "create":
        # Prefer config Components (folders must already exist under BasePath).
        # --all-deployed is ignored for create — you cannot create from SCM alone.
        create_list = [c for c in (config.get("Components") or []) if not _is_package_only(c)]
        if args.components:
            create_list = [
                _normalize_component(c)
                for c in args.components.split(",")
                if c.strip()
            ]
        if not create_list:
            print(
                "FATAL: create requires Components in the config JSON "
                "or --components Content,NiFi,...",
                file=sys.stderr,
            )
            return 1
        nifi_cfg = config.get("NiFi") or {}
        nifi_install = bool(nifi_cfg.get("InstallService", True))
        find_cfg = config.get("Find") or {}
        # Merge Ports.Find into find_cfg if ServerPort not set
        ports = config.get("Ports") or {}
        if "ServerPort" not in find_cfg and ports.get("Find"):
            find_cfg = dict(find_cfg)
            find_cfg["ServerPort"] = ports["Find"]
        find_install = bool(find_cfg.get("InstallService", True))
        if config.get("InstallService") is False:
            # Global switch still allows explicit create via this tool
            pass
        return action_create(
            create_list,
            base_path=base_path,
            install_template=config.get("ServiceInstallArgsTemplate"),
            start_mode=str(config.get("StartMode") or "Auto"),
            nifi_install_service=nifi_install,
            find_install_service=find_install,
            find_cfg=find_cfg,
            dry_run=args.dry_run,
        )
    return 1


if __name__ == "__main__":
    sys.exit(main())
