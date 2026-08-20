"""
Main orchestration: Install / Uninstall / Repair / Configure / HealthCheck.
Extraction is delegated entirely to unzip-one.sh (native unzip/tar, strips the
OpenText root folder). There is no Python-side zip extraction here; the
installer just shells out to the .bat file and checks the result.
"""

from __future__ import annotations

_ANSI_YELLOW = "\033[33m"
_ANSI_RESET = "\033[0m"

import os
import sys
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import discovery, ini_config, service_manager, state
from .logging import ElapsedClock, format_elapsed, log, chown_to_invoking_user


def _ensure_nifi_launcher(component_path: Path) -> Optional[Path]:
    """
    Locate the extracted NiFi's bin/nifi.sh (the official Apache launcher -
    NiFi's binary distribution ships it directly, there is nothing to
    deploy/overwrite) and make sure it's executable.
    """
    nifi_sh = None
    for cand in component_path.rglob("nifi.sh"):
        nifi_sh = cand
        break
    if nifi_sh is None or not nifi_sh.is_file():
        log.warn(f"  bin/nifi.sh not found under {component_path}")
        return None
    try:
        if not os.access(nifi_sh, os.X_OK):
            nifi_sh.chmod(nifi_sh.stat().st_mode | 0o111)
    except OSError as e:
        log.warn(f"  Could not make {nifi_sh} executable: {e}")
    return nifi_sh



def _deploy_nifi_properties_template(component_path: Path) -> Optional[Path]:
    """
    Overwrite target NiFi conf/nifi.properties with the toolkit template:

      <SETUP>/nifi/conf/nifi.properties  →  <TARGET>/NiFi/conf/nifi.properties

    Called immediately after nifi-*-bin.zip extraction (and again during Configure)
    so Apache defaults are replaced by the KD template before value substitution.
    """
    toolkit = Path(__file__).resolve().parent.parent
    src = toolkit / "nifi" / "conf" / "nifi.properties"
    if not src.is_file():
        log.warn(f"  Toolkit nifi.properties template missing: {src}")
        return None

    # Prefer standard layout <TARGET>/NiFi/conf/
    conf_dir = component_path / "conf"
    dest = conf_dir / "nifi.properties"
    if not conf_dir.is_dir():
        # Nested Apache layout still present
        found = None
        for candidate in component_path.rglob("nifi.properties"):
            found = candidate
            break
        if found is not None:
            dest = found
            conf_dir = found.parent
        else:
            conf_dir.mkdir(parents=True, exist_ok=True)
            dest = conf_dir / "nifi.properties"
    try:
        conf_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        log.info(f"  Overwrote nifi.properties: {src} -> {dest}")
        return dest
    except Exception as e:
        log.warn(f"  Failed to overwrite nifi.properties from toolkit template: {e}")
        return None



_PACKAGE_ONLY_COMPONENTS = frozenset({"nifiingest"})


def _is_package_only_component(component: str) -> bool:
    """NiFiIngest (and similar) supply ZIPs/NARs only — never a systemd service."""
    return (component or "").strip().lower() in _PACKAGE_ONLY_COMPONENTS


def _find_ssl_root(setup_path=None):
    """Locate ssl/ produced by tools/generate_ssl.py (prefers intermediate/nifi/keystore.p12)."""
    toolkit = Path(__file__).resolve().parent.parent
    candidates = []
    if setup_path is not None:
        sp = Path(str(setup_path).strip().strip('"'))
        if sp.name.lower() == "ssl":
            candidates.append(sp)
        else:
            candidates.append(sp / "ssl")
            parent = sp.parent
            for sibling in ("idol-linux-setup", "idol-linux-setup-main", "idol-linux-setup-synced"):
                candidates.append(parent / sibling / "ssl")
    candidates.append(toolkit / "ssl")
    candidates.append(Path("/opt/kd-setup/idol-linux-setup/ssl"))
    candidates.append(Path.home() / "idol-linux-setup" / "ssl")

    seen = set()
    ranked = []
    for c in candidates:
        try:
            key = str(c.resolve()) if c.exists() else str(c)
        except Exception:
            key = str(c)
        if key in seen:
            continue
        seen.add(key)
        ranked.append(c)

    for c in ranked:
        if (c / "intermediate" / "nifi" / "keystore.p12").is_file():
            return c
    for c in ranked:
        if (c / "intermediate" / "nifi").is_dir():
            return c
    for c in ranked:
        if c.is_dir():
            return c
    return None


def _load_ssl_passwords(ssl_root=None, setup_path=None):
    """Read keystore/truststore passwords from generate_ssl.py output files."""
    result = {"keystore_pass": "", "truststore_pass": ""}
    roots = []
    if ssl_root is not None:
        roots.append(Path(ssl_root))
    found = _find_ssl_root(setup_path)
    if found is not None:
        roots.append(found)
    env_files = []
    for root in roots:
        env_files.extend([
            root / "ssl-passwords.txt",           # preferred human-readable form
            root / ".idol-ssl-passwords.env",
            root / "env" / ".idol-ssl-passwords.env",
            root / "env" / ".idol-ssl-passwords.sh",
        ])
    # Also check toolkit env/
    toolkit = Path(__file__).resolve().parent.parent
    env_files.append(toolkit / "env" / ".idol-ssl-passwords.env")
    env_files.append(toolkit / "ssl-passwords.txt")

    seen = set()
    for ef in env_files:
        try:
            key = str(ef.resolve()) if ef.exists() else str(ef)
        except Exception:
            key = str(ef)
        if key in seen or not ef.is_file():
            continue
        seen.add(key)
        try:
            for line in ef.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                low = line.lower()
                if "=" in line:
                    k, _, v = line.partition("=")
                    k = k.strip()
                    if k.lower().startswith("$env:"):
                        k = k[5:].strip()
                    v = v.strip().strip("'").strip('"')
                    kl = k.lower().replace(" ", "")
                    if kl in ("idol_cert_keystore_pass", "keystorepassword", "keystorepasswd") and v and not result["keystore_pass"]:
                        result["keystore_pass"] = v
                    if kl in ("idol_cert_truststore_pass", "truststorepassword", "truststorepasswd") and v and not result["truststore_pass"]:
                        result["truststore_pass"] = v
                if low.startswith("keystore password:"):
                    v = line.split(":", 1)[1].strip().strip("'").strip('"')
                    if v and not result["keystore_pass"]:
                        result["keystore_pass"] = v
                elif low.startswith("truststore password:"):
                    v = line.split(":", 1)[1].strip().strip("'").strip('"')
                    if v and not result["truststore_pass"]:
                        result["truststore_pass"] = v
        except Exception:
            continue
        if result["keystore_pass"] and result["truststore_pass"]:
            break
    return result




def _regenerate_ssl_for_nifi(setup_path: Optional[Path], extra_ips: list, external_hostname: str = "idol-docker-host") -> Dict[str, Any]:
    """
    Force-regenerate SSL via tools/generate_ssl.py with --extra-ip so NiFi
    keystore SANs include the public IP (fixes HTTP 400 Invalid SNI).
    """
    toolkit = Path(__file__).resolve().parent.parent
    gen = toolkit / "tools" / "generate_ssl.py"
    if not gen.is_file():
        return {"Success": False, "Detail": f"generate_ssl.py not found at {gen}"}
    out_dir = None
    if setup_path:
        cand = Path(setup_path) / "ssl"
        out_dir = cand
    if out_dir is None:
        out_dir = toolkit / "ssl"
    ips = [str(x).strip() for x in (extra_ips or []) if str(x).strip()]
    if not ips:
        return {"Success": False, "Detail": "No extra IP provided for SSL regeneration"}
    cmd = [
        sys.executable, str(gen),
        "--auto", "--kd-services", "--force", "--no-trust-store",
        "--output-dir", str(out_dir),
        "--external-hostname", external_hostname or "idol-docker-host",
    ]
    for ip in ips:
        cmd.extend(["--extra-ip", ip])
    log.info(f"  Regenerating SSL with IP SANs: {', '.join(ips)}")
    log.info(f"  Running: {' '.join(cmd)}")
    try:
        proc = subprocess.run(cmd, cwd=str(toolkit), timeout=300, capture_output=False)
        if proc.returncode != 0:
            return {"Success": False, "Detail": f"generate_ssl.py exited {proc.returncode}"}
        ks = out_dir / "intermediate" / "nifi" / "keystore.p12"
        if not ks.is_file():
            return {"Success": False, "Detail": f"keystore.p12 still missing after regen under {out_dir}"}
        return {"Success": True, "Detail": f"SSL regenerated under {out_dir}", "SslRoot": str(out_dir)}
    except Exception as e:
        return {"Success": False, "Detail": str(e)}


def _verify_nifi_keystore_sans(keystore: Path, password: str, required_ips=None) -> Dict[str, Any]:
    """openssl pkcs12 | openssl x509 SAN check; fail if required IPs missing."""
    import shutil as _shutil
    openssl = _shutil.which("openssl")
    if not openssl:
        return {"Success": False, "Detail": "openssl not found on PATH"}
    if not keystore or not Path(keystore).is_file():
        return {"Success": False, "Detail": f"keystore not found: {keystore}"}
    try:
        proc = subprocess.run(
            [openssl, "pkcs12", "-in", str(keystore), "-nokeys", "-passin", f"pass:{password}"],
            capture_output=True, timeout=30,
        )
        if proc.returncode != 0:
            err = (proc.stderr or b"").decode("utf-8", "replace")[:300]
            return {"Success": False, "Detail": f"openssl pkcs12 failed: {err}"}
        x509 = subprocess.run(
            [openssl, "x509", "-noout", "-text"],
            input=proc.stdout, capture_output=True, timeout=30,
        )
        if x509.returncode != 0:
            return {"Success": False, "Detail": "openssl x509 failed"}
        out = (x509.stdout or b"").decode("utf-8", "replace")
        san = []
        capture = False
        for line in out.splitlines():
            if "Subject Alternative Name" in line:
                capture = True
                san.append(line.strip())
                continue
            if capture:
                stripped = line.strip()
                # SAN values are indented continuation lines (DNS:/IP Address:)
                if "DNS:" in stripped or "IP Address:" in stripped or stripped.startswith("IP:"):
                    san.append(stripped)
                    # Usually one continuation line holds all SANs
                    break
                if stripped and not line.startswith(" ") and not line.startswith("\t"):
                    break
        blob = " ".join(san)
        log.info("  openssl SAN verification:")
        for s in san:
            log.info(f"    {s}")
        if "DNS:" not in blob and "IP Address:" not in blob and "IP:" not in blob:
            return {"Success": False, "Detail": "Subject Alternative Name missing/empty", "SAN": blob}
        missing = []
        for ip in (required_ips or []):
            ip = str(ip).strip()
            if ip and ip not in blob:
                missing.append(ip)
        if missing:
            return {
                "Success": False,
                "Detail": f"Required IP SAN(s) missing: {', '.join(missing)}",
                "SAN": blob,
            }
        return {"Success": True, "Detail": "SAN verification passed", "SAN": blob}
    except Exception as e:
        return {"Success": False, "Detail": str(e)}


def _deploy_nifi_ssl_material(component_path, setup_path=None):
    """
    Copy generated keystore.p12 / truststore.p12 into NiFi conf/ and return
    paths + passwords for nifi.properties updates.
    """
    empty = {
        "ok": False, "keystore": None, "truststore": None,
        "keystore_pass": "", "truststore_pass": "", "ssl_root": None, "detail": "",
    }
    ssl_root = _find_ssl_root(setup_path)
    if ssl_root is None:
        empty["detail"] = (
            "SSL output folder not found under SetupPath/ssl or toolkit/ssl "
            "(run tools/generate_ssl.py first)"
        )
        return empty

    src_dir = ssl_root / "intermediate" / "nifi"
    src_ks = src_dir / "keystore.p12"
    src_ts = src_dir / "truststore.p12"
    if not src_ks.is_file() or not src_ts.is_file():
        empty["ssl_root"] = ssl_root
        empty["detail"] = (
            f"NiFi PKCS12 files missing under {src_dir} "
            f"(expected keystore.p12 + truststore.p12 from generate_ssl.py)"
        )
        return empty

    conf_dir = component_path / "conf"
    if not conf_dir.is_dir():
        for props in component_path.rglob("nifi.properties"):
            conf_dir = props.parent
            break
    try:
        conf_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        empty["ssl_root"] = ssl_root
        empty["detail"] = f"Cannot create conf dir {conf_dir}: {e}"
        return empty

    dest_ks = conf_dir / "keystore.p12"
    dest_ts = conf_dir / "truststore.p12"
    try:
        import shutil as _shutil
        _shutil.copy2(src_ks, dest_ks)
        _shutil.copy2(src_ts, dest_ts)
    except Exception as e:
        empty["ssl_root"] = ssl_root
        empty["detail"] = f"Failed to copy PKCS12 files to {conf_dir}: {e}"
        return empty

    passwords = _load_ssl_passwords(ssl_root=ssl_root, setup_path=setup_path)
    return {
        "ok": True,
        "keystore": dest_ks,
        "truststore": dest_ts,
        "keystore_pass": passwords.get("keystore_pass") or "",
        "truststore_pass": passwords.get("truststore_pass") or "",
        "ssl_root": ssl_root,
        "detail": (
            f"keystore+truststore copied from {src_dir} -> {conf_dir} "
            f"(passwords from {ssl_root / '.idol-ssl-passwords.env'})"
        ),
    }


def _nifi_security_property_updates(ssl_material):
    """Build nifi.security.* property updates from deployed SSL material."""
    updates = {
        "nifi.security.keystore": "./conf/keystore.p12",
        "nifi.security.keystoreType": "PKCS12",
        "nifi.security.truststore": "./conf/truststore.p12",
        "nifi.security.truststoreType": "PKCS12",
    }
    ks_pass = (ssl_material.get("keystore_pass") or "").strip()
    ts_pass = (ssl_material.get("truststore_pass") or "").strip()
    if ks_pass:
        updates["nifi.security.keystorePasswd"] = ks_pass
        updates["nifi.security.keyPasswd"] = ks_pass
    if ts_pass:
        updates["nifi.security.truststorePasswd"] = ts_pass
    return updates


def _stage_nars_from_nifiingest_zip(zip_path, dry_run=False):
    """Extract *.nar from NiFiIngest_*.zip into nifi/nifi-connectors (no service)."""
    import zipfile as _zipfile
    import shutil as _shutil
    zip_dir = Path(zip_path) if zip_path else Path()
    dest = Path(__file__).resolve().parent.parent / "nifi" / "nifi-connectors"
    if not zip_dir.is_dir():
        return {"Success": False, "Count": 0, "Detail": f"ZipPath not found: {zip_dir}"}
    candidates = sorted(
        [p for p in zip_dir.iterdir()
         if p.is_file() and p.suffix.lower() == ".zip" and "nifiingest" in p.name.lower()],
        key=lambda p: p.stat().st_mtime, reverse=True,
    )
    if not candidates:
        return {"Success": True, "Count": 0, "Skipped": True,
                "Detail": f"No NiFiIngest_*.zip found under {zip_dir} (optional)"}
    zip_file = candidates[0]
    if dry_run:
        return {"Success": True, "Count": 0,
                "Detail": f"[DryRun] Would extract *.nar from {zip_file.name} -> {dest}"}
    try:
        dest.mkdir(parents=True, exist_ok=True)
        extracted = 0
        with _zipfile.ZipFile(zip_file, "r") as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                name = info.filename.replace("\\", "/")
                base = Path(name).name
                if not base.lower().endswith(".nar"):
                    continue
                target = dest / base
                with zf.open(info) as src, open(target, "wb") as out:
                    _shutil.copyfileobj(src, out)
                extracted += 1
        return {"Success": True, "Count": extracted,
                "Detail": f"Staged {extracted} .nar file(s) from {zip_file.name} -> {dest}",
                "Zip": str(zip_file)}
    except Exception as e:
        return {"Success": False, "Count": 0, "Detail": str(e)}


def _set_nifi_single_user_credentials(
    component_path: Path,
    username: str,
    password: str,
) -> Dict[str, Any]:
    """
    Apply fixed UI login credentials for NiFi single-user mode.

    Prefer calling the Java class **directly** via bin/nifi.sh's own
    classpath resolution logic isn't reusable here, so we replicate the
    classpath NiFi's launcher builds (bootstrap + lib jars + conf), using
    ':' as the classpath separator (Linux/POSIX; Windows uses ';', which is
    what this code used before being ported and is why it always failed
    here with a silent ClassNotFoundException):

        java -cp "lib/bootstrap/*:lib/*:conf" \
             -Dnifi.properties.file.path=conf/nifi.properties \
             org.apache.nifi.authentication.single.user.command.SetSingleUserCredentials \
             <username> <password>

    Falls back to NiFi's own `bin/nifi.sh set-single-user-credentials`
    subcommand (the official entry point covering both Unix and Windows) if
    the direct invocation can't load the class.

    Working directory is NIFI_HOME (parent of bin/), not bin/.
    Password should be at least 12 characters (NiFi requirement).
    """
    username = (username or "").strip()
    password = (password or "").strip()
    if not username or not password:
        return {
            "Success": True,
            "Skipped": True,
            "Detail": "NiFi.Username / NiFi.Password not set - leaving single-user credentials unchanged",
        }

    if len(password) < 12:
        return {
            "Success": False,
            "Skipped": False,
            "Detail": (
                f"NiFi password must be at least 12 characters (got {len(password)}). "
                "Update NiFi.Password in config and retry."
            ),
        }

    # Locate nifi.sh to resolve NIFI_HOME / bin layout
    nifi_sh = None
    for cand in component_path.rglob("nifi.sh"):
        nifi_sh = cand
        break
    if nifi_sh is None or not nifi_sh.is_file():
        return {
            "Success": False,
            "Skipped": False,
            "Detail": f"bin/nifi.sh not found under {component_path}",
        }

    nifi_home = nifi_sh.parent.parent  # .../NiFi/bin/nifi.sh -> .../NiFi
    conf_dir = nifi_home / "conf"
    props = conf_dir / "nifi.properties"
    bootstrap_lib = nifi_home / "lib" / "bootstrap"

    if not props.is_file():
        return {
            "Success": False,
            "Skipped": False,
            "Detail": f"nifi.properties not found at {props}",
        }

    # Resolve java
    java_exe = None
    java_home = (
        os.environ.get("JAVA_HOME")
        or os.environ.get("JDK_HOME")
        or ""
    ).strip().strip('"')
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
            "Success": False,
            "Skipped": False,
            "Detail": "java not found (set JAVA_HOME to a JDK 21 install)",
        }

    # Classpath: bootstrap jars + conf (matches Apache workaround / nifi.sh),
    # using the platform's classpath separator (':' on Linux).
    cp_parts = []
    if bootstrap_lib.is_dir():
        cp_parts.append(str(bootstrap_lib / "*"))
    # Broader fallback used by some NiFi 2.x layouts
    lib_dir = nifi_home / "lib"
    if lib_dir.is_dir():
        cp_parts.append(str(lib_dir / "*"))
    cp_parts.append(str(conf_dir))
    classpath = os.pathsep.join(cp_parts)

    main_class = (
        "org.apache.nifi.authentication.single.user.command.SetSingleUserCredentials"
    )
    cmd = [
        str(java_exe),
        "-cp", classpath,
        f"-Dnifi.properties.file.path={props}",
        main_class,
        username,
        password,
    ]

    log.info(
        f"  Setting NiFi single-user UI credentials before service start "
        f"(user='{username}') ..."
    )
    log.info(
        f"  Command: java -cp <nifi lib> "
        f"-Dnifi.properties.file.path=conf/nifi.properties "
        f"SetSingleUserCredentials \"{username}\" \"***\""
    )
    log.info(f"  Working directory (NIFI_HOME): {nifi_home}")
    log.info(f"  JAVA: {java_exe}")

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(nifi_home),
            capture_output=True,
            text=True,
            timeout=120,
            shell=False,
        )
        out = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
        out_lower = out.lower()
        usage_failure = any(
            marker in out_lower
            for marker in (
                "unexpected number of arguments",
                "usage: setsingleusercredentials",
                "could not find or load main class",
                "classnotfoundexception",
            )
        )
        if proc.returncode == 0 and not usage_failure:
            log.info(
                f"  NiFi single-user credentials applied for user '{username}' "
                f"(login-identity-providers.xml updated)"
            )
            if out:
                for line in out.splitlines()[-8:]:
                    if line.strip():
                        log.info(f"    {line.rstrip()}")
            return {
                "Success": True,
                "Skipped": False,
                "Detail": f"Single-user credentials set for user '{username}'",
            }

        # Fallback: NiFi's own launcher subcommand (official entry point,
        # builds its own classpath correctly - unlike the hand-rolled one
        # above, this doesn't need us to guess the right jars).
        log.warn(
            "  Direct Java invocation could not load SetSingleUserCredentials; "
            "trying bin/nifi.sh set-single-user-credentials..."
        )
        try:
            proc2 = subprocess.run(
                [str(nifi_sh), "set-single-user-credentials", username, password],
                cwd=str(nifi_home),
                capture_output=True,
                text=True,
                timeout=120,
                shell=False,
            )
            out2 = ((proc2.stdout or "") + "\n" + (proc2.stderr or "")).strip()
            out2_lower = out2.lower()
            usage2 = any(
                m in out2_lower
                for m in (
                    "unexpected number of arguments",
                    "usage: setsingleusercredentials",
                )
            )
            if proc2.returncode == 0 and not usage2:
                log.info(
                    f"  NiFi single-user credentials applied for user '{username}' "
                    f"(via bin/nifi.sh)"
                )
                return {
                    "Success": True,
                    "Skipped": False,
                    "Detail": f"Single-user credentials set for user '{username}'",
                }
            out = out + "\n--- nifi.sh helper ---\n" + out2
            proc = proc2
            usage_failure = usage2 or usage_failure
        except (OSError, subprocess.TimeoutExpired) as e:
            out = out + f"\n--- nifi.sh helper failed to run: {e} ---"

        detail = f"set-single-user-credentials exit {proc.returncode}"
        if usage_failure:
            detail = "set-single-user-credentials failed (classpath/argument issue)"
        if out:
            detail += f": {out[-500:]}"
        log.warn(f"  NiFi set-single-user-credentials failed: {detail}")
        return {"Success": False, "Skipped": False, "Detail": detail}
    except subprocess.TimeoutExpired:
        log.warn("  NiFi set-single-user-credentials timed out after 120s")
        return {
            "Success": False,
            "Skipped": False,
            "Detail": "set-single-user-credentials timed out after 120s",
        }
    except Exception as e:
        log.warn(f"  NiFi set-single-user-credentials failed: {e}")
        return {
            "Success": False,
            "Skipped": False,
            "Detail": f"set-single-user-credentials failed: {e}",
        }



def _nifi_install_service_enabled(config: Dict[str, Any]) -> bool:
    """
    Whether to register KD-NiFi as a Windows service.
    Global InstallService is the master switch; NiFi.InstallService can
    opt out of service registration for NiFi only (still extracts/configures).
    """
    if not config.get("InstallService", True):
        return False
    nifi_cfg = config.get("NiFi") or {}
    # Default True when key omitted - installing NiFi as a service is the
    # normal path when InstallService is true and NiFi is in Components.
    return bool(nifi_cfg.get("InstallService", True))


def _update_properties_file(
    file_path: Path,
    updates: Dict[str, str],
    no_backup: bool = False,
) -> bool:
    """
    Update key=value lines in a Java-style properties file (nifi.properties,
    bootstrap.conf, etc.). Creates a timestamped backup by default.
    Keys that do not exist are appended at the end.
    """
    if not file_path.is_file():
        return False
    try:
        if not no_backup:
            ini_config.backup_kd_file(file_path)
        content = file_path.read_text(encoding="utf-8", errors="replace")
        lines = content.splitlines(keepends=True)
        found = set()
        new_lines: List[str] = []
        for line in lines:
            stripped = line.lstrip()
            if stripped.startswith("#") or "=" not in line:
                new_lines.append(line)
                continue
            key = line.split("=", 1)[0].strip()
            if key in updates:
                nl = "\r\n" if line.endswith("\r\n") else ("\n" if line.endswith("\n") else "")
                prefix = line[: len(line) - len(line.lstrip())]
                new_lines.append(f"{prefix}{key}={updates[key]}{nl}")
                found.add(key)
            else:
                new_lines.append(line)
        for key, val in updates.items():
            if key not in found:
                new_lines.append(f"{key}={val}\r\n")
        tmp = file_path.with_suffix(file_path.suffix + ".tmp")
        tmp.write_text("".join(new_lines), encoding="utf-8")
        tmp.replace(file_path)
        return True
    except Exception as e:
        log.warn(f"Failed to update properties file {file_path}: {e}")
        return False


def _unblock_file(path: Path) -> None:
    """
    No-op on Linux.

    "Mark-of-the-Web" (an NTFS Zone.Identifier alternate data stream that
    Windows attaches to files downloaded from the internet, requiring an
    Unblock-File / PowerShell call to clear before execution) is an
    NTFS/Windows-only concept. Linux filesystems (ext4, xfs, etc.) have no
    equivalent, and nothing here needs "unblocking" to run - kept as a
    stub so any leftover call sites remain harmless.
    """
    return


def _unblock_tree(root: Path) -> None:
    """No-op on Linux - see _unblock_file()."""
    return


def _force_rmtree(path: Path, retries: int = 6, delay: float = 1.5) -> None:
    """
    Robust recursive delete for both Linux and (legacy) Windows.

    On Linux the common failure mode is files still held open by a leftover
    process (NiFi / Find Java, AnswerServer, ACI binaries). We therefore:

      1. chmod +u+w the tree and try shutil.rmtree
      2. walk + unlink/rmdir one-by-one
      3. fuser -k / lsof-based kill of processes holding the path (Linux)
      4. ``rm -rf`` as a last-resort shell delete
      5. retry the whole sequence a few times with backoff

    Always raises with a *meaningful* reason when the path still exists
    (never ``: None``).
    """
    import os
    import stat

    def _onerror(func, p, exc_info):
        try:
            os.chmod(p, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
            func(p)
        except Exception:
            pass

    def _chmod_tree(root: Path) -> None:
        try:
            for dirpath, dirnames, filenames in os.walk(str(root), topdown=False):
                for name in filenames + dirnames:
                    fp = Path(dirpath) / name
                    try:
                        os.chmod(fp, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
                    except Exception:
                        pass
            try:
                os.chmod(root, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
            except Exception:
                pass
        except Exception:
            pass

    def _kill_holders(target: Path) -> None:
        """Best-effort: kill processes that still have files open under target (Linux)."""
        if os.name == "nt":
            return
        target_s = str(target.resolve()) if target.exists() else str(target)
        # fuser -k is the simplest reliable approach when available
        for tool in (
            ["fuser", "-k", "-9", f"{target_s}"],
            ["fuser", "-k", "-9", f"{target_s}/"],
        ):
            try:
                subprocess.run(tool, capture_output=True, text=True, timeout=15)
            except Exception:
                pass
        # Also try lsof to collect PIDs and kill them explicitly
        try:
            r = subprocess.run(
                ["lsof", "+D", target_s],
                capture_output=True, text=True, timeout=20,
            )
            pids = set()
            for line in (r.stdout or "").splitlines()[1:]:
                parts = line.split()
                if len(parts) >= 2 and parts[1].isdigit():
                    pids.add(int(parts[1]))
            my_pid = os.getpid()
            for pid in pids:
                if pid > 1 and pid != my_pid:
                    try:
                        os.kill(pid, 9)
                    except Exception:
                        try:
                            subprocess.run(
                                ["sudo", "kill", "-9", str(pid)],
                                capture_output=True, timeout=5,
                            )
                        except Exception:
                            pass
        except Exception:
            pass

    path = Path(path)
    if not path.exists():
        return

    # Clear attributes on the whole tree first (best-effort) – Windows only
    try:
        if os.name == "nt":
            subprocess.run(
                ["attrib", "-R", "-S", "-H", str(path / "*"), "/S", "/D"],
                capture_output=True,
                timeout=90,
            )
            subprocess.run(
                ["attrib", "-R", "-S", "-H", str(path)],
                capture_output=True,
                timeout=15,
            )
    except Exception:
        pass

    last_err: Optional[BaseException | str] = None
    for attempt in range(1, retries + 1):
        _chmod_tree(path)

        # 1) shutil.rmtree
        try:
            shutil.rmtree(path, onerror=_onerror)
            if not path.exists():
                return
            last_err = last_err or "shutil.rmtree left path present"
        except Exception as e:
            last_err = e

        # 2) Manual walk + unlink
        try:
            for root, dirs, files in os.walk(str(path), topdown=False):
                for name in files:
                    fp = Path(root) / name
                    try:
                        os.chmod(fp, stat.S_IWRITE | stat.S_IREAD)
                        fp.unlink(missing_ok=True)
                    except Exception as e:
                        last_err = e
                for name in dirs:
                    dp = Path(root) / name
                    try:
                        os.chmod(dp, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
                        dp.rmdir()
                    except Exception as e:
                        last_err = e
            try:
                path.rmdir()
            except Exception as e:
                last_err = e
            if not path.exists():
                return
        except Exception as e:
            last_err = e

        # 3) Kill processes still holding the tree (Linux)
        if os.name != "nt" and path.exists():
            _kill_holders(path)
            time.sleep(0.8)

        # 4a) Linux native: rm -rf
        if os.name != "nt" and path.exists():
            try:
                cmd = ["rm", "-rf", "--", str(path)]
                if os.geteuid() != 0:
                    cmd = ["sudo"] + cmd
                proc = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=180,
                )
                if not path.exists():
                    return
                err_txt = (proc.stderr or proc.stdout or "").strip()
                if err_txt:
                    last_err = err_txt
                elif proc.returncode != 0:
                    last_err = f"rm -rf exited {proc.returncode}"
            except Exception as e:
                last_err = e

        # 4b) Windows native recursive delete
        if os.name == "nt" and path.exists():
            try:
                proc = subprocess.run(
                    ["cmd.exe", "/c", "rmdir", "/s", "/q", str(path)],
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
                if not path.exists():
                    return
                if proc.returncode != 0 and (proc.stderr or proc.stdout):
                    last_err = (proc.stderr or proc.stdout or "").strip() or last_err
            except Exception as e:
                last_err = e

        if attempt < retries:
            time.sleep(delay * attempt)  # mild backoff

    if path.exists():
        # Collect a short diagnostic of what is still left
        leftover = []
        try:
            for i, p in enumerate(path.rglob("*")):
                if i >= 8:
                    leftover.append("…")
                    break
                leftover.append(str(p.relative_to(path)))
        except Exception:
            pass
        detail = last_err if last_err is not None else "unknown reason (path still present)"
        raise OSError(
            f"Could not fully delete {path}: {detail}"
            + (f" | still contains: {', '.join(leftover)}" if leftover else "")
        )


def _resolve_component_folder(base_path: Path, component: str) -> Optional[Path]:
    """
    Locate the on-disk folder for a configured component under BasePath.
    Prefer the exact name; fall back to case-insensitive match so Uninstall
    still finds folders created with different casing.
    """
    exact = base_path / component
    if exact.exists():
        return exact
    if not base_path.is_dir():
        return exact  # caller treats non-existent as already gone
    target = component.lower()
    try:
        for child in base_path.iterdir():
            if child.is_dir() and child.name.lower() == target:
                return child
    except Exception:
        pass
    return exact

def _extract_timeout_seconds(zip_path: Path) -> int:
    """
    Size-based extract timeout. Large OpenText packages (1–4+ GB) can take
    20–40+ minutes on slow disks; a fixed 600s caused false timeouts / 'stuck'.
    Estimate ~3 MB/s worst-case + 5 min headroom; clamp 10 min .. 2 hours.
    """
    try:
        size_mb = zip_path.stat().st_size / (1024 * 1024)
    except OSError:
        size_mb = 500.0
    # seconds ≈ size_mb / 3 + 300, clamped
    estimated = int(size_mb / 3.0) + 300
    return max(600, min(7200, estimated))


def expand_kd_zip_native(zip_path: str | Path, destination_path: str | Path) -> None:
    """
    Extract a single ZIP by shelling out to unzip-one.sh (native `unzip`/`tar`,
    strips the OpenText root folder). All extraction logic lives in the
    shell script itself; this is just a thin, checked subprocess call.

    Live stdout/stderr is streamed to the log so large ZIPs do not appear
    'stuck'. Timeout scales with ZIP size (up to 2 hours).
    """
    zip_path = Path(zip_path).resolve()
    dest = Path(destination_path).resolve()

    if not zip_path.is_file():
        raise FileNotFoundError(f"ZIP not found: {zip_path}")

    script_root = Path(__file__).resolve().parent.parent
    unzip_bat = script_root / "unzip-one.sh"
    if not unzip_bat.is_file():
        raise FileNotFoundError(f"unzip-one.sh not found in {script_root}")

    try:
        size_mb = zip_path.stat().st_size / (1024 * 1024)
    except OSError:
        size_mb = 0.0
    timeout = _extract_timeout_seconds(zip_path)
    log.info(
        f"  Extracting via unzip-one.sh (native unzip/tar) -> {dest} "
        f"[{size_mb:.1f} MB, timeout {timeout}s / {timeout // 60} min]"
    )
    log.info("  (large packages can take several minutes - progress lines appear below)")

    # Nested clock so extract alone shows its own elapsed ticks
    with ElapsedClock(f"Extract {zip_path.name}", interval=20.0):
        lines: list[str] = []
        try:
            proc = subprocess.Popen(
                ["bash", str(unzip_bat), str(zip_path), str(dest)],
                cwd=str(script_root),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            assert proc.stdout is not None
            deadline = time.time() + timeout
            while True:
                if time.time() > deadline:
                    proc.kill()
                    try:
                        proc.wait(timeout=30)
                    except Exception:
                        pass
                    raise TimeoutError(
                        f"Extraction timed out after {timeout}s for '{zip_path.name}' "
                        f"({size_mb:.1f} MB). Re-run Install (resume) or extract manually "
                        f"with unzip-one.sh, then continue."
                    )
                line = proc.stdout.readline()
                if line:
                    text = line.rstrip()
                    if text:
                        lines.append(text)
                        log.info(f"  | {text}")
                elif proc.poll() is not None:
                    rest = proc.stdout.read()
                    if rest:
                        for part in rest.splitlines():
                            part = part.rstrip()
                            if part:
                                lines.append(part)
                                log.info(f"  | {part}")
                    break
                else:
                    time.sleep(0.2)
            returncode = proc.returncode if proc.returncode is not None else -1
        except TimeoutError:
            raise
        except Exception as e:
            raise RuntimeError(
                f"Failed to run unzip-one.sh for '{zip_path.name}': {e}"
            ) from e

        if returncode != 0:
            tail = "\n".join(lines[-30:]) if lines else "(no output)"
            raise RuntimeError(
                f"unzip-one.sh exited with code {returncode} for '{zip_path.name}'.\n{tail}"
            )

        if not dest.is_dir():
            raise RuntimeError(
                f"unzip-one.sh reported success but destination missing: {dest}"
            )
        has_any = False
        try:
            for child in dest.iterdir():
                if child.is_file() or (
                    child.is_dir() and child.name.lower() != "_tmp_extract"
                ):
                    has_any = True
                    break
        except Exception:
            has_any = False
        if not has_any:
            raise RuntimeError(
                f"unzip-one.sh reported success but no files were found in {dest}."
            )
        log.info(f"  unzip-one.sh completed for {zip_path.name}")


def _folder_has_content(path: Path) -> bool:
    """
    True if path exists and contains at least one file.
    Prefer shallow checks first so multi-GB trees do not block on full rglob.
    """
    if not path.is_dir():
        return False
    try:
        for child in path.iterdir():
            if child.is_file():
                return True
            if child.is_dir() and child.name.lower() not in ("_tmp_extract",):
                # one level down is enough for OpenText layout
                try:
                    for grand in child.iterdir():
                        if grand.is_file() or grand.is_dir():
                            return True
                except Exception:
                    continue
        # last resort (small trees / odd layouts)
        return any(p.is_file() for p in path.rglob("*"))
    except Exception:
        return False


def _script_root() -> Path:
    return Path(__file__).resolve().parent.parent


# Components that ship toolkit .cfg templates under config/cfg/<name>/
_CFG_TEMPLATE_COMPONENTS = frozenset(
    {
        "content",
        "community",
        "agentstore",
        "category",
        "qms",
        "qmsagentstore",
        "statsserver",
        "answerbankagentstore",
        "conversationagentstore",
        "answerserver",
        "licenseserver",
        "view",
    }
)

# Components assembled from Content (no own ZIP) - Agentstore-style engines
_AGENTSTORE_LIKE = frozenset(
    {
        "Agentstore",
        "QMSAgentStore",
        "AnswerBankAgentStore",
        "ConversationAgentStore",
    }
)


def _ensure_agentstore_writable(
    component: str,
    component_path: Path,
    config: Optional[Dict[str, Any]] = None,
) -> None:
    """
    Ensure Agentstore-family trees are writable by the service user.

    IDOL Content/Agentstore refuses to start with:
      Could not initialize database structure ... read only
    when index/logs were created as root (common under sudo install) while the
    systemd unit runs as SUDO_USER / azureuser.

    Creates standard runtime dirs and chowns the component tree (and optional
    shared IndexPath) to the invoking user.
    """
    if not component_path:
        return
    try:
        component_path = Path(component_path)
        component_path.mkdir(parents=True, exist_ok=True)
        for sub in ("index", "logs", "actions", "logs/archive"):
            (component_path / sub).mkdir(parents=True, exist_ok=True)
        chown_to_invoking_user(component_path, recursive=True)

        # Shared custom IndexPath (if configured)
        if config:
            raw_index = str(config.get("IndexPath") or "").strip()
            if raw_index:
                idx = Path(raw_index)
                idx.mkdir(parents=True, exist_ok=True)
                chown_to_invoking_user(idx, recursive=True)

        log.info(
            f"  Ensured writable data dirs for {component} "
            f"(owner=invoking user) under {component_path}"
        )
    except Exception as e:
        log.warn(f"  Could not ensure writable dirs for {component}: {e}")



def _ensure_answerserver_writable(component_path: Path) -> None:
    """
    AnswerServer AnswerBank needs write access under the component tree.

    Failure mode (application.log):
      Failed to configure answer system: Could not write to file/structure
      because it is read only.

    Creates Topics / logs / templates / rag runtime dirs and chowns the
    whole AnswerServer tree to the invoking user (systemd unit runs as that user).
    """
    if not component_path:
        return
    try:
        component_path = Path(component_path)
        component_path.mkdir(parents=True, exist_ok=True)
        for sub in (
            "logs",
            "logs/archive",
            "Topics",
            "templates",
            "rag",
            "actions",
        ):
            (component_path / sub).mkdir(parents=True, exist_ok=True)
        # Ensure nested package dirs are writable if present
        for nested in component_path.rglob("*"):
            if nested.is_dir():
                try:
                    # no-op mkdir; just ensure we can chown later
                    pass
                except Exception:
                    pass
        chown_to_invoking_user(component_path, recursive=True)
        log.info(
            f"  Ensured writable AnswerServer dirs (Topics/logs/templates) "
            f"under {component_path}"
        )
    except Exception as e:
        log.warn(f"  Could not ensure writable dirs for AnswerServer: {e}")


def _nifi_is_extracted(component_path: Path) -> bool:
    """
    True only when a real Apache NiFi binary tree is present.

    Requires bin/nifi.sh (or nested .../bin/nifi.sh). Do NOT treat
    conf/nifi.properties alone as extracted — the toolkit template is often
    copied into conf/ before the Apache nifi-*-bin.zip is unpacked, which
    previously caused extraction to be skipped and later:
      Locate launcher for NiFi - nifi.sh not found under component path
    """
    if not component_path.is_dir():
        return False
    # Standard layout: <BasePath>/NiFi/bin/nifi.sh
    if (component_path / "bin" / "nifi.sh").is_file():
        return True
    if (component_path / "bin" / "nifi.cmd").is_file():
        return True
    # Nested layout (top-level folder not stripped by unzip)
    try:
        for cmd in component_path.rglob("nifi.sh"):
            if cmd.is_file() and cmd.parent.name.lower() == "bin":
                return True
        for cmd in component_path.rglob("nifi.cmd"):
            if cmd.is_file() and cmd.parent.name.lower() == "bin":
                return True
    except Exception:
        pass
    return False


def _nar_member_under_nifi_folder(member_name: str) -> bool:
    """
    True if the ZIP member path sits under a ``nifi/`` (or ``nifi\\``) folder
    segment, e.g. `` somehow/nifi/foo.nar``, ``nifi/foo.nar``, ``Content/nifi/x.nar``.
    """
    parts = member_name.replace("\\", "/").split("/")
    parts_l = [p.lower() for p in parts]
    if "nifi" not in parts_l:
        return False
    # require a .nar leaf
    return parts_l[-1].endswith(".nar")


def _stage_nars_from_all_zips(zip_path: str | Path) -> Dict[str, Any]:
    """
    Scan **every** ``*.zip`` under ZipPath. If a ZIP contains a ``nifi/``
    subfolder with ``*.nar`` files, copy those NARs into:

        <SetupRoot>/nifi/nifi-connectors/

    Existing staged NARs are never overwritten. Covers NiFiIngest packages,
    Content/other OpenText zips that ship a nested nifi/ connectors folder,
    and any other vendor layout with nifi/*.nar members.
    """
    import zipfile

    zip_dir = Path(zip_path) if zip_path else Path()
    dest = _script_root() / "nifi" / "nifi-connectors"
    if not zip_dir.is_dir():
        return {
            "Success": False,
            "Count": 0,
            "Detail": f"ZipPath not a directory: {zip_dir}",
        }

    zips = sorted(zip_dir.glob("*.zip"))
    # Skip the pure Apache NiFi binary distribution (no connectors inside)
    zips = [
        z
        for z in zips
        if not re.search(r"(?i)nifi-[\d.]+-bin\.zip$", z.name)
        and not re.search(r"(?i)^nifi-.*-bin\.zip$", z.name)
    ]

    if not zips:
        return {
            "Success": True,
            "Count": 0,
            "Detail": f"No component ZIPs to scan under {zip_dir}",
        }

    dest.mkdir(parents=True, exist_ok=True)
    staged: list[str] = []
    skipped_existing: list[str] = []
    scanned = 0
    with_nifi = 0
    errors: list[str] = []

    log.info(f"  Scanning {len(zips)} ZIP(s) under {zip_dir} for nifi/**/*.nar ...")

    for zf in zips:
        try:
            with zipfile.ZipFile(zf, "r") as z:
                nar_members = [
                    info
                    for info in z.infolist()
                    if (not info.is_dir())
                    and _nar_member_under_nifi_folder(info.filename)
                ]
                if not nar_members:
                    # Also accept any *.nar at archive root for *NiFiIngest* packages
                    if re.search(r"(?i)nifiingest", zf.name):
                        nar_members = [
                            info
                            for info in z.infolist()
                            if (not info.is_dir())
                            and Path(info.filename).name.lower().endswith(".nar")
                        ]
                if not nar_members:
                    continue
                with_nifi += 1
                scanned += 1
                log.info(
                    f"  {zf.name}: found {len(nar_members)} .nar under nifi/ (or NiFiIngest layout)"
                )
                for info in nar_members:
                    name = Path(info.filename).name
                    target = dest / name
                    if target.exists():
                        skipped_existing.append(name)
                        continue
                    try:
                        with z.open(info) as src, open(target, "wb") as out:
                            shutil.copyfileobj(src, out)
                        staged.append(name)
                    except Exception as e:
                        errors.append(f"{zf.name}/{name}: {e}")
        except zipfile.BadZipFile as e:
            errors.append(f"{zf.name}: bad zip ({e})")
        except Exception as e:
            errors.append(f"{zf.name}: {e}")

    try:
        chown_to_invoking_user(dest, recursive=True)
    except Exception:
        pass

    detail = (
        f"Scanned {len(zips)} ZIP(s), {with_nifi} with nifi/*.nar; "
        f"staged {len(staged)} new, kept {len(skipped_existing)} existing -> {dest}"
    )
    if errors:
        detail += f"; errors: {'; '.join(errors[:5])}"
    if staged:
        log.info(
            f"  Staged {len(staged)} .nar -> {dest}: "
            + ", ".join(staged[:15])
            + ("…" if len(staged) > 15 else "")
        )
    else:
        log.info(f"  {detail}")

    return {
        "Success": len(errors) == 0,
        "Count": len(staged),
        "SkippedExisting": len(skipped_existing),
        "Detail": detail,
        "Staged": staged,
    }


def _stage_nifi_nars_from_component(
    component_path: Path, dry_run: bool = False
) -> Dict[str, Any]:
    """
    After extracting a component folder, stage every ``*.nar`` under its
    ``nifi/`` subfolder into ``<SetupRoot>/nifi/nifi-connectors``.
    """
    src = Path(component_path) / "nifi"
    dest = _script_root() / "nifi" / "nifi-connectors"

    if not src.is_dir():
        return {
            "Success": True,
            "Count": 0,
            "Detail": f"No nifi subfolder at {src}",
        }

    nars = sorted(src.rglob("*.nar")) + sorted(src.rglob("*.NAR"))
    if not nars:
        return {
            "Success": True,
            "Count": 0,
            "Detail": f"No .nar files under {src}",
        }

    if dry_run:
        return {
            "Success": True,
            "Count": len(nars),
            "Detail": f"[DryRun] Would stage {len(nars)} .nar from {src} -> {dest}",
        }

    try:
        dest.mkdir(parents=True, exist_ok=True)
        copied = []
        skipped = []
        errors = []
        for p in nars:
            target = dest / p.name
            try:
                if target.exists():
                    skipped.append(p.name)
                    continue
                shutil.copy2(p, target)
                copied.append(p.name)
            except Exception as e:
                errors.append(f"{p.name}: {e}")
        try:
            chown_to_invoking_user(dest, recursive=True)
        except Exception:
            pass
        if copied:
            log.info(
                f"  Staged {len(copied)} .nar from {src} -> {dest}: "
                + ", ".join(copied[:12])
                + ("…" if len(copied) > 12 else "")
            )
        return {
            "Success": len(errors) == 0,
            "Count": len(copied),
            "Detail": (
                f"Staged {len(copied)} new, kept {len(skipped)} existing from "
                f"{src} -> {dest}"
                + (f"; errors: {'; '.join(errors)}" if errors else "")
            ),
        }
    except Exception as e:
        return {
            "Success": False,
            "Count": 0,
            "Detail": f"Failed to stage NARs from {src}: {e}",
        }


def _copy_nifi_nar_extensions(
    base_path: Path,
    dry_run: bool = False,
    zip_path: str | Path | None = None,
) -> Dict[str, Any]:
    """
    After extracting ``nifi-*-bin.zip``:

      1. Scan every ZIP under ZipPath for ``nifi/**/*.nar`` and stage into
         ``<SetupRoot>/nifi/nifi-connectors/`` (never overwrite staged files).
      2. Copy **all** ``*.nar`` from ``nifi-connectors`` into
         ``<BasePath>/NiFi/extensions/`` (never overwrite existing dest NARs).
      3. ``chown`` the NiFi tree to the invoking user.
    """
    nifi_root = Path(base_path) / "NiFi"
    if not _nifi_is_extracted(nifi_root):
        log.info(
            f"  NiFi is not extracted under {nifi_root} (no bin/nifi.sh). "
            "Extract nifi-*-bin.zip first, then copy NAR connectors."
        )
        return {
            "Success": False,
            "Copied": 0,
            "Skipped": True,
            "Detail": "NiFi binary tree not present — extract nifi-*-bin.zip before NAR copy",
        }

    connectors = _script_root() / "nifi" / "nifi-connectors"
    dest = nifi_root / "extensions"

    # Step 1: harvest NARs from every component ZIP that has a nifi/ subfolder
    stage_detail = ""
    if zip_path:
        stage_result = _stage_nars_from_all_zips(zip_path)
        stage_detail = stage_result.get("Detail") or ""
        # Loose *.nar sitting directly in ZipPath
        zp = Path(zip_path)
        if zp.is_dir():
            connectors.mkdir(parents=True, exist_ok=True)
            for p in sorted(zp.glob("*.nar")) + sorted(zp.glob("*.NAR")):
                target = connectors / p.name
                if not target.exists():
                    try:
                        shutil.copy2(p, target)
                        log.info(f"  Staged loose NAR {p.name} from ZipPath")
                    except Exception as e:
                        log.warn(f"  Could not stage loose NAR {p.name}: {e}")

    connectors.mkdir(parents=True, exist_ok=True)
    nars = sorted(connectors.glob("*.nar")) + sorted(connectors.glob("*.NAR"))
    # Also accept toolkit nifi/*.nar (legacy layout)
    nifi_root_setup = _script_root() / "nifi"
    if nifi_root_setup.is_dir():
        for p in sorted(nifi_root_setup.glob("*.nar")) + sorted(
            nifi_root_setup.glob("*.NAR")
        ):
            if p.parent.resolve() == connectors.resolve():
                continue
            if p not in nars:
                nars.append(p)

    if not nars:
        existing = []
        if dest.is_dir():
            existing = list(dest.glob("*.nar")) + list(dest.glob("*.NAR"))
        try:
            chown_to_invoking_user(dest if dest.is_dir() else nifi_root, recursive=True)
            chown_to_invoking_user(nifi_root, recursive=True)
        except Exception:
            pass
        detail = (
            f"No .nar in {connectors}"
            + (f" ({stage_detail})" if stage_detail else "")
            + (
                f"; keeping {len(existing)} existing in {dest}"
                if existing
                else f"; {dest} is empty — place connectors in nifi/nifi-connectors or ZIPs with nifi/*.nar"
            )
        )
        return {"Success": True, "Count": 0, "Detail": detail}

    if dry_run:
        return {
            "Success": True,
            "Count": len(nars),
            "Detail": f"[DryRun] Would copy {len(nars)} .nar {connectors} -> {dest}",
        }

    try:
        dest.mkdir(parents=True, exist_ok=True)
        copied = []
        skipped = []
        errors = []
        for p in nars:
            target = dest / p.name
            try:
                if target.exists():
                    skipped.append(p.name)
                    continue
                shutil.copy2(p, target)
                copied.append(p.name)
            except Exception as e:
                errors.append(f"{p.name}: {e}")

        try:
            chown_to_invoking_user(dest, recursive=True)
            chown_to_invoking_user(nifi_root, recursive=True)
            chown_to_invoking_user(connectors, recursive=True)
        except Exception as e:
            log.warn(f"  chown after NAR copy: {e}")

        if copied:
            log.info(
                f"  Copied {len(copied)} .nar {connectors} -> {dest}: "
                + ", ".join(copied[:15])
                + ("…" if len(copied) > 15 else "")
            )
        detail = (
            f"copied {len(copied)}, kept existing {len(skipped)} -> {dest}"
            + (f"; {stage_detail}" if stage_detail else "")
            + (f"; errors: {'; '.join(errors)}" if errors else "")
        )
        return {
            "Success": len(errors) == 0,
            "Count": len(copied),
            "SkippedExisting": len(skipped),
            "Detail": detail,
        }
    except Exception as e:
        return {
            "Success": False,
            "Count": 0,
            "Detail": f"Failed to copy .nar files to {dest}: {e}",
        }


def _deploy_find_config_json(
    component_path: Path,
    home_dir_name: str = "home",
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    After Find ZIP extraction, copy toolkit config/cfg/find/config.json into
    <BasePath>/Find/<home>/config.json (overwriting if present).

    Sets defaultLogin (e.g. admin/admin) and backend host/port defaults before
    the service starts, so operators are not stuck with a random password.
    """
    src = _script_root() / "config" / "cfg" / "find" / "config.json"
    if not src.is_file():
        return {
            "Success": False,
            "Detail": f"Toolkit Find config not found at {src}",
        }

    home_dir = component_path / (home_dir_name or "home")
    dest = home_dir / "config.json"

    if dry_run:
        return {
            "Success": True,
            "Detail": f"[DryRun] Would copy {src} -> {dest}",
        }

    try:
        home_dir.mkdir(parents=True, exist_ok=True)
        chown_to_invoking_user(home_dir, recursive=False)
        shutil.copy2(src, dest)
        log.info(f"  Deployed Find config.json -> {dest}")
        return {
            "Success": True,
            "Detail": str(dest),
        }
    except Exception as e:
        return {
            "Success": False,
            "Detail": f"Failed to copy Find config.json: {e}",
        }


def _deploy_cfg_templates(component: str, component_path: Path, dry_run: bool = False) -> int:
    """
    After ZIP extraction, overwrite the extracted component root with the
    toolkit cfg templates from config/cfg/<component>/ (files + subdirs).

    For AnswerServer this copies answerserver.cfg *and* the entire rag/
    folder (scripts, prompts, tokenizer cache, etc.) into the component root
    so relative paths in the cfg (./rag/...) resolve correctly.

    Returns the number of top-level items (files + dirs) successfully deployed
    (0 if no templates exist for component).
    """
    key = component.lower()
    if key not in _CFG_TEMPLATE_COMPONENTS:
        return 0

    src_dir = _script_root() / "config" / "cfg" / key
    if not src_dir.is_dir():
        log.info(f"  No toolkit cfg templates at {src_dir} (skipping overwrite)")
        return 0

    items = [p for p in src_dir.iterdir() if not p.name.startswith(".")]
    if not items:
        log.info(f"  Toolkit cfg template folder empty: {src_dir}")
        return 0

    if dry_run:
        for src in items:
            kind = "dir" if src.is_dir() else "file"
            log.info(
                f"  [DryRun] Would deploy cfg {kind}: {src.name} -> {component_path / src.name}"
            )
        return len(items)

    component_path.mkdir(parents=True, exist_ok=True)
    chown_to_invoking_user(component_path, recursive=False)
    count = 0
    for src in items:
        dest = component_path / src.name
        try:
            if src.is_dir():
                if dest.exists():
                    shutil.rmtree(dest)
                # Ignore bytecode caches and other non-runtime noise
                shutil.copytree(
                    src,
                    dest,
                    ignore=shutil.ignore_patterns(
                        "__pycache__", "*.pyc", "*.pyo", ".git", ".DS_Store"
                    ),
                )
                log.info(f"  Deployed cfg folder from toolkit: {src.name}/ -> {dest}")
            else:
                shutil.copy2(src, dest)
                log.info(f"  Overwrote cfg from toolkit: {src.name} -> {dest}")
            count += 1
        except Exception as e:
            log.warn(f"  Failed to deploy {src.name} -> {dest}: {e}")

    # Fix ownership of everything we just copied (copy2/copytree run as root)
    if count:
        chown_to_invoking_user(component_path, recursive=True)

    if count:
        log.step_result(
            f"Deploy cfg templates for {component}",
            True,
            f"{count} item(s) from config/cfg/{key}/",
        )
    return count
    

def _find_autpassword(component_path: Path) -> Optional[Path]:
    """Locate the autpassword binary under the Community extract tree (Linux/Windows)."""
    names = ("autpassword", "autpassword.exe")
    for name in names:
        direct = component_path / name
        if direct.is_file():
            return direct
    # Nested OpenText layout: Community/<version>/autpassword
    for name in names:
        for found in component_path.rglob(name):
            if found.is_file() and found.name.lower() in ("autpassword", "autpassword.exe"):
                return found
    return None


def _ensure_community_aes_key(
    component_path: Path,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Ensure Community has aes.key and point community.cfg [Security] SecurityInfoKeys at it.

    OpenText Community will not start without a valid AES key file. Failure mode:
      Could not open security key file / SecurityInfoKeys missing

    If aes.key is missing, run from the Community install directory:

        ./autpassword -x -tAES -oKeyFile=aes.key

    The key is written next to community.cfg (and at component_path root) so the
    relative path SecurityInfoKeys=aes.key resolves under systemd WorkingDirectory.
    """
    component_path = Path(component_path)
    cfg_candidates = [
        component_path / "community.cfg",
        *sorted(component_path.rglob("community.cfg")),
    ]
    cfg_path = next((p for p in cfg_candidates if p.is_file()), component_path / "community.cfg")
    # Prefer key beside community.cfg (matches relative SecurityInfoKeys=aes.key)
    key_dir = cfg_path.parent if cfg_path.is_file() else component_path
    aes_key = key_dir / "aes.key"
    root_key = component_path / "aes.key"

    def _point_cfg_at_key(key_path: Path) -> bool:
        if not cfg_path.is_file():
            log.warn(f"  community.cfg not found under {component_path}")
            return False
        # Relative path when key sits next to cfg (WorkingDirectory = install dir)
        try:
            if key_path.parent.resolve() == cfg_path.parent.resolve():
                value = "aes.key"
            else:
                value = str(key_path.resolve())
        except Exception:
            value = str(key_path)
        ok = ini_config.update_kd_ini_file(cfg_path, "Security", "SecurityInfoKeys", value)
        if ok:
            log.info(f"  [Security] SecurityInfoKeys={value}")
        else:
            log.warn(f"  Could not update SecurityInfoKeys in {cfg_path}")
        return ok

    # Reuse existing key if present anywhere under the tree
    existing = None
    if aes_key.is_file():
        existing = aes_key
    elif root_key.is_file():
        existing = root_key
    else:
        for found in component_path.rglob("aes.key"):
            if found.is_file():
                existing = found
                break

    if existing is not None:
        log.info(f"  aes.key already present: {existing}")
        if not dry_run:
            # Ensure a copy next to community.cfg for relative SecurityInfoKeys
            try:
                if existing.resolve() != aes_key.resolve():
                    import shutil as _shutil
                    aes_key.parent.mkdir(parents=True, exist_ok=True)
                    _shutil.copy2(existing, aes_key)
                    log.info(f"  Copied aes.key -> {aes_key}")
            except Exception as e:
                log.warn(f"  Could not copy aes.key next to cfg: {e}")
            _point_cfg_at_key(aes_key if aes_key.is_file() else existing)
            try:
                chown_to_invoking_user(aes_key if aes_key.is_file() else existing, recursive=False)
            except Exception:
                pass
        return {
            "Success": True,
            "Created": False,
            "KeyPath": str(aes_key if aes_key.is_file() else existing),
            "Detail": "aes.key already present",
        }

    autpassword = _find_autpassword(component_path)
    if not autpassword:
        detail = (
            "aes.key missing and autpassword binary not found under Community; "
            "create manually: cd <Community> && ./autpassword -x -tAES -oKeyFile=aes.key"
        )
        log.warn(f"  {detail}")
        return {"Success": False, "Created": False, "KeyPath": str(aes_key), "Detail": detail}

    # Ensure executable bit on Linux
    try:
        mode = autpassword.stat().st_mode
        if not (mode & 0o111):
            autpassword.chmod(mode | 0o755)
            log.info(f"  chmod +x {autpassword}")
    except Exception as e:
        log.warn(f"  Could not chmod autpassword: {e}")

    if dry_run:
        log.info(
            f"  [DryRun] Would create aes.key: cd {key_dir} && "
            f"{autpassword.name} -x -tAES -oKeyFile=aes.key"
        )
        return {"Success": True, "Created": False, "KeyPath": str(aes_key), "Detail": "[DryRun]"}

    log.info(f"  Creating aes.key via {autpassword.name} (cwd={key_dir})")
    try:
        proc = subprocess.run(
            [str(autpassword), "-x", "-tAES", "-oKeyFile=aes.key"],
            cwd=str(key_dir),
            capture_output=True,
            text=True,
            timeout=90,
            shell=False,
        )
        out = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
        if proc.returncode != 0:
            detail = f"autpassword exit {proc.returncode}"
            if out:
                detail += f": {out[:500]}"
            log.warn(f"  {detail}")
            return {"Success": False, "Created": False, "KeyPath": str(aes_key), "Detail": detail}
        # autpassword may write relative to cwd (key_dir) or next to the binary
        if not aes_key.is_file():
            # Search freshly created key near binary or tree
            for candidate in (
                key_dir / "aes.key",
                autpassword.parent / "aes.key",
                component_path / "aes.key",
            ):
                if candidate.is_file():
                    if candidate.resolve() != aes_key.resolve():
                        import shutil as _shutil
                        aes_key.parent.mkdir(parents=True, exist_ok=True)
                        _shutil.copy2(candidate, aes_key)
                    break
        if not aes_key.is_file():
            for found in component_path.rglob("aes.key"):
                if found.is_file():
                    import shutil as _shutil
                    aes_key.parent.mkdir(parents=True, exist_ok=True)
                    _shutil.copy2(found, aes_key)
                    break
        if not aes_key.is_file():
            detail = "autpassword reported success but aes.key was not created"
            if out:
                detail += f": {out[:300]}"
            log.warn(f"  {detail}")
            return {"Success": False, "Created": False, "KeyPath": str(aes_key), "Detail": detail}
        log.info(f"  aes.key created: {aes_key}")
        try:
            chown_to_invoking_user(aes_key, recursive=False)
        except Exception:
            pass
        # Mirror at component root when cfg lives in a nested folder
        if root_key.resolve() != aes_key.resolve():
            try:
                import shutil as _shutil
                _shutil.copy2(aes_key, root_key)
            except Exception:
                pass
    except subprocess.TimeoutExpired:
        detail = "autpassword timed out while creating aes.key"
        log.warn(f"  {detail}")
        return {"Success": False, "Created": False, "KeyPath": str(aes_key), "Detail": detail}
    except Exception as e:
        detail = f"autpassword failed: {e}"
        log.warn(f"  {detail}")
        return {"Success": False, "Created": False, "KeyPath": str(aes_key), "Detail": detail}

    ok = _point_cfg_at_key(aes_key)
    if not ok and cfg_path.is_file():
        return {
            "Success": False,
            "Created": True,
            "KeyPath": str(aes_key),
            "Detail": f"aes.key created but SecurityInfoKeys update failed in {cfg_path}",
        }
    log.step_result("Community aes.key + SecurityInfoKeys", True, str(aes_key))
    return {"Success": True, "Created": True, "KeyPath": str(aes_key), "Detail": f"Created {aes_key}"}


def _apply_standard_cfg_patches(
    component: str,
    component_path: Path,
    config: Dict[str, Any],
    dry_run: bool = False,
) -> Optional[Path]:
    """
    Apply runtime config values on top of (optional) toolkit cfg templates:
    LicenseHost, [Server] Port from Ports map, IndexPath.
    Returns the primary .cfg path used, or None if none found.
    """
    cfg_files = [
        p
        for p in component_path.rglob("*.cfg")
        if "bak" not in p.name.lower() and p.name.lower() != "idol.common.cfg"
    ]
    # Prefer the component-named cfg at the root when present
    preferred = component_path / f"{component.lower()}.cfg"
    # Agentstore-like engines use agentstore.cfg (not qmsagentstore.cfg)
    agentstore_cfg = component_path / "agentstore.cfg"
    if preferred.is_file():
        cfg = preferred
    elif component in _AGENTSTORE_LIKE and agentstore_cfg.is_file():
        cfg = agentstore_cfg
    elif cfg_files:
        cfg = cfg_files[0]
    else:
        return None

    if dry_run:
        log.info(f"  [DryRun] Would patch {cfg.name} (LicenseHost / Port / IndexPath)")
        return cfg

    ini_config.update_kd_ini_file(cfg, "License", "LicenseServerHost", config["LicenseHost"])
    # Intentionally do NOT write LicenseServerPort
    ports = config.get("Ports") or {}
    if component in ports:
        # Primary ACI port lives under [Server] in OpenText templates
        port_val = str(ports[component])
        ini_config.update_kd_ini_file(cfg, "Server", "Port", port_val)
        log.info(f"  [Server] Port={port_val} in {cfg.name}")
    if config.get("IndexPath") and (
        component in ("Content", "Category", "Community")
        or component in _AGENTSTORE_LIKE
    ):
        ini_config.update_kd_ini_file(cfg, "Index", "IndexPath", config["IndexPath"])
        log.info(f"  [Index] IndexPath={config['IndexPath']} in {cfg.name}")
    return cfg



def _extract_nifi_preserving_extensions(zip_file: Path, dest: Path) -> None:
    """
    Extract nifi-*-bin.zip into *dest* and merge with existing extensions/.

    unzip-one.sh does ``rm -rf DEST``, which would wipe ``NiFi/extensions``.
    Flow:

      1. Backup dest/extensions (if any) outside dest
      2. Extract the Apache ZIP into a staging dir
      3. Flatten nested nifi-X.Y.Z/ if present so stage has bin/nifi.sh at top
      4. Clear dest except extensions/
      5. Move staged tree into dest (except staged extensions/)
      6. Restore backed-up NARs; add any new ZIP extensions without overwrite
      7. Require dest/bin/nifi.sh or raise — never leave conf+extensions only
    """
    import tempfile

    dest = Path(dest).resolve()
    zip_file = Path(zip_file).resolve()
    if not zip_file.is_file():
        raise FileNotFoundError(f"NiFi binary ZIP not found: {zip_file}")
    dest.mkdir(parents=True, exist_ok=True)

    try:
        zsize = zip_file.stat().st_size
        log.info(
            f"  NiFi extract (preserve extensions): {zip_file.name} "
            f"({zsize / (1024*1024):.1f} MB) -> {dest}"
        )
    except OSError:
        log.info(f"  NiFi extract (preserve extensions): {zip_file.name} -> {dest}")

    ext_dir = dest / "extensions"
    backup: Optional[Path] = None
    bak_parent: Optional[Path] = None
    file_count = 0

    if ext_dir.is_dir():
        try:
            file_count = sum(1 for p in ext_dir.rglob("*") if p.is_file())
        except Exception:
            file_count = 0
        if file_count > 0 or any(ext_dir.iterdir()):
            bak_parent = Path(tempfile.mkdtemp(prefix="kd-nifi-ext-bak-"))
            backup = bak_parent / "extensions"
            shutil.copytree(ext_dir, backup, dirs_exist_ok=True)
            log.info(
                f"  Backed up NiFi extensions/ ({file_count} file(s)) — "
                "will merge after Apache bin extract"
            )

    # Also preserve conf/ if it already has operator edits (optional safety)
    conf_backup: Optional[Path] = None
    conf_dir = dest / "conf"
    if conf_dir.is_dir() and any(conf_dir.iterdir()):
        try:
            conf_bak_parent = Path(tempfile.mkdtemp(prefix="kd-nifi-conf-bak-"))
            conf_backup = conf_bak_parent / "conf"
            shutil.copytree(conf_dir, conf_backup, dirs_exist_ok=True)
            log.info("  Backed up existing NiFi conf/ (will re-apply key files after extract)")
        except Exception as e:
            log.warn(f"  Could not backup conf/: {e}")
            conf_backup = None

    stage_parent = Path(tempfile.mkdtemp(prefix="kd-nifi-stage-"))
    stage = stage_parent / "out"
    stage.mkdir(parents=True, exist_ok=True)

    try:
        expand_kd_zip_native(zip_file, stage)

        # Flatten: Apache ZIP is usually nifi-2.10.0/{bin,conf,lib,...}
        # unzip-one strips one level, but if still nested, peel once more.
        def _stage_has_launcher(root: Path) -> bool:
            return (root / "bin" / "nifi.sh").is_file() or (root / "bin" / "nifi.cmd").is_file()

        if not _stage_has_launcher(stage):
            kids = [c for c in stage.iterdir() if c.is_dir()]
            if len(kids) == 1 and _stage_has_launcher(kids[0]):
                log.info(f"  Flattening nested folder {kids[0].name}/ to stage root")
                inner = kids[0]
                for item in list(inner.iterdir()):
                    target = stage / item.name
                    if target.exists():
                        if target.is_dir():
                            shutil.rmtree(target)
                        else:
                            target.unlink()
                    shutil.move(str(item), str(target))
                shutil.rmtree(inner, ignore_errors=True)

        if not _stage_has_launcher(stage):
            top = []
            try:
                top = [c.name + ("/" if c.is_dir() else "") for c in sorted(stage.iterdir())[:20]]
            except Exception:
                pass
            raise RuntimeError(
                f"After extracting {zip_file.name}, bin/nifi.sh is missing under staging. "
                f"Stage top-level: {', '.join(top) or '(empty)'}. "
                f"Confirm the ZIP is Apache nifi-*-bin.zip (not NiFiIngest)."
            )

        log.info(
            "  Staged Apache NiFi OK (bin/nifi.sh present); "
            f"merging into {dest} (keeping extensions/)"
        )

        # Clear dest except extensions/
        for child in list(dest.iterdir()):
            if child.name == "extensions":
                continue
            try:
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink()
            except Exception as e:
                log.warn(f"  Could not clear {child} before NiFi merge: {e}")

        # Move staged content into dest (skip staged extensions for now)
        for child in list(stage.iterdir()):
            if child.name == "extensions":
                continue
            target = dest / child.name
            try:
                if target.exists():
                    if target.is_dir():
                        shutil.rmtree(target)
                    else:
                        target.unlink()
                shutil.move(str(child), str(target))
            except Exception as e:
                # Fallback copy if cross-device move fails
                try:
                    if child.is_dir():
                        shutil.copytree(child, target, dirs_exist_ok=True)
                        shutil.rmtree(child, ignore_errors=True)
                    else:
                        shutil.copy2(child, target)
                        child.unlink()
                except Exception as e2:
                    log.warn(f"  Could not place staged {child.name} -> {target}: {e}; {e2}")

        dest_ext = dest / "extensions"
        dest_ext.mkdir(parents=True, exist_ok=True)

        restored = 0
        if backup is not None and backup.is_dir():
            for src in backup.rglob("*"):
                if not src.is_file():
                    continue
                rel = src.relative_to(backup)
                dst = dest_ext / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                restored += 1
            log.info(f"  Restored {restored} preserved file(s) into NiFi/extensions")

        stage_ext = stage / "extensions"
        added = 0
        if stage_ext.is_dir():
            for src in stage_ext.rglob("*"):
                if not src.is_file():
                    continue
                rel = src.relative_to(stage_ext)
                dst = dest_ext / rel
                if dst.exists():
                    continue
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                added += 1
            if added:
                log.info(f"  Added {added} new file(s) from ZIP extensions/")

        # Re-apply a few safe conf backups only if Apache conf is missing keys
        # (full conf comes from the bin ZIP; template is applied later in Configure)
        if conf_backup is not None and conf_backup.is_dir():
            dest_conf = dest / "conf"
            dest_conf.mkdir(parents=True, exist_ok=True)
            # Do not overwrite Apache defaults wholesale — Configure will deploy template

        if not (dest / "bin" / "nifi.sh").is_file() and not (dest / "bin" / "nifi.cmd").is_file():
            top = []
            try:
                top = [c.name + ("/" if c.is_dir() else "") for c in sorted(dest.iterdir())[:20]]
            except Exception:
                pass
            raise RuntimeError(
                f"NiFi merge finished but bin/nifi.sh is still missing under {dest}. "
                f"Top-level now: {', '.join(top) or '(empty)'}"
            )

        log.info(
            f"  NiFi binary tree ready: {dest}/bin/nifi.sh "
            f"(extensions preserved={restored}, added_from_zip={added})"
        )
        # Immediately replace Apache nifi.properties with SETUP toolkit template
        props = _deploy_nifi_properties_template(dest)
        if props is None:
            log.warn(
                "  Could not overwrite conf/nifi.properties from "
                "<SETUP>/nifi/conf/nifi.properties after extract"
            )
    finally:
        shutil.rmtree(stage_parent, ignore_errors=True)
        if bak_parent is not None:
            shutil.rmtree(bak_parent, ignore_errors=True)
        if conf_backup is not None:
            try:
                shutil.rmtree(conf_backup.parent, ignore_errors=True)
            except Exception:
                pass



def _ensure_component_extracted(
    component: str,
    component_path: Path,
    base_path: Path,
    zip_path: str,
    dry_run: bool = False,
    non_interactive: bool = False,
    force: bool = False,
    nifi_version: str | None = None,
) -> bool:
    """
    Verify the target component folder has content; if missing/empty, extract
    it automatically by calling unzip-one.sh (native unzip/tar, strips the
    OpenText root folder). All extraction logic lives in that .bat file -
    this function just locates the matching ZIP and invokes it.
    Agentstore / QMSAgentStore have no ZIP - they are assembled from Content later.

    force=True skips the "already has content" short-circuit and always
    re-extracts, even if the destination folder already exists with files
    in it. unzip-one.sh itself removes the destination folder before
    extracting (rd /s /q), so this guarantees a clean overwrite of the
    target folder rather than silently reusing whatever was there before -
    used by the standalone Install Java Services (NiFi|Find) menu, since
    that is an explicit (re)install action.
    """
    # Agentstore-like components are derived from Content – only need Content folder ready
    if component in _AGENTSTORE_LIKE:
        content_path = base_path / "Content"
        if _folder_has_content(content_path):
            log.info(f"  {component}: Content folder is present (will copy after check)")
            return True
        log.warn(f"  {component} needs the Content folder extracted first.")
        # Fall through to Content-style extraction using Content component name
        check_component = "Content"
        check_path = content_path
    else:
        check_component = component
        check_path = component_path

    # NiFi binary ZIP is an explicit prerequisite. Check/download it BEFORE
    # the extracted-folder short-circuit. A previous successful extraction
    # must not suppress the download when the ZIP was later removed.
    if check_component.lower() == "nifi" and not dry_run:
        try:
            from . import prerequisites
            nifi_version = str(nifi_version or prerequisites.NIFI_DEFAULT_VERSION)
            zip_dir = Path(zip_path) if zip_path else Path()
            zip_result = discovery.find_kd_component_zip(str(zip_dir), check_component)
            if not (zip_result.get("Success") and zip_result.get("Zip")):
                log.warn(
                    f"  NiFi ZIP is missing from {zip_dir}. "
                    f"Starting automatic download of nifi-{nifi_version}-bin.zip."
                )
                dl = prerequisites.download_nifi_zip(
                    zip_dir,
                    version=nifi_version,
                    dry_run=False,
                )
                if dl.get("Success"):
                    log.info(f"  NiFi automatic download succeeded: {dl.get('Path')}")
                else:
                    log.error(
                        "  NiFi automatic download failed: "
                        f"{dl.get('Detail', 'unknown download error')}"
                    )
                    return False
        except Exception as e:
            log.error(f"  NiFi automatic download could not be started: {e}")
            return False

    # NiFi: only treat as extracted when bin/nifi.sh (or equivalent) exists.
    # A lone NiFi/extensions folder from a prior NAR copy must NOT skip
    # extraction of nifi-*-bin.zip.
    if check_component.lower() == "nifi":
        already_ok = _nifi_is_extracted(check_path)
    else:
        already_ok = _folder_has_content(check_path)

    if not force and already_ok:
        log.info(f"  Found extracted content in {check_path}")
        return True

    if force and already_ok:
        log.info(f"  {check_component}: forcing re-extract - {check_path} will be overwritten")
    elif check_component.lower() == "nifi" and _folder_has_content(check_path) and not already_ok:
        log.warn(
            f"  {check_path} exists but is incomplete (no bin/nifi.sh) — "
            "extracting nifi-*-bin.zip (existing extensions/ will be preserved)"
        )

    if dry_run:
        log.info(f"  [DryRun] Would extract into: {check_path}")
        return True

    # Locate the matching ZIP. For NiFi the prerequisite/download check above
    # has already ensured that it exists (unless this is a dry-run).
    zip_dir = Path(zip_path) if zip_path else Path()
    zip_result = discovery.find_kd_component_zip(str(zip_dir), check_component)

    if not (zip_result.get("Success") and zip_result.get("Zip")):
        log.error(f"  No matching ZIP found for {check_component} in {zip_dir}")
        if zip_result.get("Reason"):
            log.error(f"  Detail: {zip_result['Reason']}")
        return False

    zf = Path(zip_result["Zip"])
    log.info(f"  Selected ZIP: {zf} ({zip_result.get('Reason', 'OK')})")
    log.info(f"  Extracting {zf.name} -> {check_path}")
    try:
        if check_component.lower() == "nifi":
            # Never wipe NiFi/extensions — extract via staging + merge
            _extract_nifi_preserving_extensions(zf, check_path)
        else:
            expand_kd_zip_native(zf, check_path)
    except Exception as e:
        log.error(f"  Extraction failed for {check_component}: {e}")
        return False

    if check_component.lower() == "nifi":
        if not _nifi_is_extracted(check_path):
            log.error(
                f"  Extraction reported success but {check_path} has no bin/nifi.sh "
                f"(nifi-*-bin.zip may be wrong or incomplete)."
            )
            return False
        # Ensure SETUP template is on disk even if extract used a path that
        # did not go through _extract_nifi_preserving_extensions.
        _deploy_nifi_properties_template(check_path)
    elif not _folder_has_content(check_path):
        log.error(f"  Extraction reported success but {check_path} is still empty.")
        return False

    # Quick post-extract sanity: list top-level entries so a wrong/empty
    # extract is obvious in the log before service registration.
    try:
        top = sorted(check_path.iterdir())
        names = [c.name + ("/" if c.is_dir() else "") for c in top[:15]]
        log.info(f"  Extracted content ready in {check_path}  top-level: {', '.join(names)}")
    except Exception:
        log.info(f"  Extracted content ready in {check_path}")

    # Unblock Mark-of-the-Web in one bulk call (not per-file - that looked stuck)
    try:
        _unblock_tree(check_path)
    except Exception:
        pass

    # Prefer the invoking user (SUDO_USER) so extracted component trees are
    # not left root-owned when the installer is run via sudo.
    try:
        chown_to_invoking_user(check_path, recursive=True)
    except Exception:
        pass

    return True



def _ensure_index_path(
    config: Dict[str, Any],
    base_path: Path,
    dry_run: bool = False,
    config_path: Optional[str | Path] = None,
) -> Optional[Path]:
    """
    Ensure a *custom* IDOL IndexPath directory exists (when one is configured).

    By default the toolkit leaves IndexPath unset. In that case the OpenText
    Content / Category / Community / Agentstore templates keep their relative
    paths under [Paths] (e.g. MainPath=./index/main, TemplateDirectory=./templates).
    This is the recommended / default layout and avoids the non-standard
    [Index] IndexPath= key that was previously injected.

    Only when the user explicitly sets a non-empty "IndexPath" in the JSON
    config (or the config dashboard) do we:

    - create that directory, and
    - later write [Index] IndexPath=... into the component .cfg files
      (see _apply_standard_cfg_patches).

    When config_path is supplied and a custom IndexPath is active, the value
    is also persisted back into the JSON so the dashboard and update-configfiles
    stay in sync.
    """
    raw_index_path = str(config.get("IndexPath") or "").strip()

    if not raw_index_path:
        # Default behaviour: keep the relative ./index folders from the
        # OpenText templates. Do not invent a custom IndexPath.
        log.info(
            "  IndexPath not set — using default relative index folders "
            "(./index/...) under each component. Custom IndexPath disabled."
        )
        log.step_result(
            "IndexPath",
            True,
            "default relative (./index) — custom IndexPath disabled",
        )
        return None

    index_path = Path(raw_index_path)

    if dry_run:
        log.info(f"  [DryRun] Would create custom IndexPath: {index_path}")
        return index_path

    if config_path:
        try:
            from .config import update_config_key

            if update_config_key(config_path, "IndexPath", str(index_path)):
                log.info(f"  Synced custom IndexPath -> {config_path}")
            else:
                log.warn(f"  Could not sync IndexPath into {config_path}")
        except Exception as e:
            log.warn(f"  Could not persist IndexPath to {config_path}: {e}")

    try:
        index_path.mkdir(parents=True, exist_ok=True)
        # Prefer invoking user so IndexPath is not root-owned under sudo
        try:
            chown_to_invoking_user(index_path, recursive=False)
        except NameError:
            pass

        if not index_path.is_dir():
            raise RuntimeError(
                f"IndexPath exists but is not a directory: {index_path}"
            )

        log.info(f"  IndexPath ready: {index_path}")
        log.step_result(
            "Create IndexPath",
            True,
            str(index_path),
        )

        return index_path

    except Exception as e:
        log.error(f"  Failed to create IndexPath {index_path}: {e}")
        log.step_result(
            "Create IndexPath",
            False,
            str(e),
        )
        raise



def invoke_kd_install(
    config: Dict[str, Any],
    dry_run: bool = False,
    force: bool = False,
    resume: bool = False,
    extract_only: bool = False,
    non_interactive: bool = False,
) -> Dict[str, Any]:
    """
    Install Knowledge Discovery components.

    Before component processing starts, ensure that the final IndexPath
    directory exists. This must happen before the IDOL services are
    installed and started.
    """
    base_path = Path(config["BasePath"])

    if not dry_run:
        base_path.mkdir(parents=True, exist_ok=True)
        # Prefer the invoking user (SUDO_USER) so BasePath is not left root-owned
        # when the installer is run via sudo.
        chown_to_invoking_user(base_path, recursive=False)

    # ------------------------------------------------------------------
    # Ensure IndexPath exists before any component configuration or
    # Windows service startup.
    #
    # This also guarantees that the same final IndexPath value is used
    # later by _apply_standard_cfg_patches().
    # ------------------------------------------------------------------
    try:
        _ensure_index_path(
            config=config,
            base_path=base_path,
            dry_run=dry_run,
        )
    except Exception as e:
        log.error(f"IndexPath initialization failed: {e}")
        return {
            "Success": False,
            "Completed": [],
            "Failed": ["IndexPath"],
            "Detail": f"Could not create IndexPath: {e}",
        }

    st = state.get_kd_state(base_path)
    completed: List[str] = []
    failed: List[str] = []

    components = list(config.get("Components") or [])
    n_comp = len(components)

    # Phase A: one unit per component
    # Phase B: one unit per component when starting services
    start_phase = bool(
        config.get("InstallService", True)
        and not extract_only
        and not dry_run
    )

    total_units = n_comp + (n_comp if start_phase else 0)

    with ElapsedClock(
        "Install",
        interval=15.0,
        total_units=max(1, total_units),
        unit_label="units",
    ) as clock:
        return _invoke_kd_install_body(
            config=config,
            base_path=base_path,
            st=st,
            completed=completed,
            failed=failed,
            dry_run=dry_run,
            force=force,
            resume=resume,
            extract_only=extract_only,
            non_interactive=non_interactive,
            clock=clock,
            total_components=n_comp,
            start_phase=start_phase,
        )


def _invoke_kd_install_body(
    config: Dict[str, Any],
    base_path: Path,
    st: Any,
    completed: List[str],
    failed: List[str],
    dry_run: bool,
    force: bool,
    resume: bool,
    extract_only: bool,
    non_interactive: bool,
    clock: Optional[ElapsedClock] = None,
    total_components: int = 0,
    start_phase: bool = False,
) -> Dict[str, Any]:
    components = list(config.get("Components") or [])
    if total_components <= 0:
        total_components = len(components)
    units_per_phase = float(total_components) if total_components else 1.0
    total_all = units_per_phase * (2.0 if start_phase else 1.0)

    def _sync_progress(task: str = "") -> None:
        if clock is None:
            return
        # Phase A progress = components finished so far
        done = float(len(completed) + len(failed))
        clock.set_progress(min(done, total_all), total_all)
        if task:
            clock.set_task(task)
        else:
            clock.set_task("")

    for component in components:
        log.info(f"=== Processing component: {component} ===")
        _sync_progress(component)
        component_path = base_path / component
        is_agentstore_like = component in _AGENTSTORE_LIKE

        try:
            # --- Package-only (e.g. NiFiIngest): not a core KD service ---
            # Dashboard may list NiFiIngest under Components because its ZIP
            # is present. It only supplies connector .nar files for NiFi —
            # never .cfg configure or systemd service install.
            if _is_package_only_component(component):
                log.info(
                    f"  {component} is package-only (NiFi connector NARs) — "
                    "not a core KD service; skipping .cfg / service install"
                )
                stage = _stage_nars_from_nifiingest_zip(
                    config.get("ZipPath", ""),
                    dry_run=dry_run,
                )
                log.step_result(
                    f"Stage {component} connector .nar files -> nifi-connectors",
                    bool(stage.get("Success")),
                    stage.get("Detail") or "",
                )
                if not dry_run:
                    st = state.set_kd_component_stage(base_path, st, component, "Complete")
                completed.append(component)
                _sync_progress()
                continue

            if is_agentstore_like and state.test_kd_component_stage_complete(st, component, "Complete"):
                log.info(f"  Skipping {component} (already complete)")
                completed.append(component)
                _sync_progress()
                continue

            # --- Stage: Extract (automatic, via unzip-one.sh) ---
            # Resume trusts the persisted "Extracted" state flag, but that flag
            # can go stale: an interrupted/crashed extraction, a disk-full mid
            # copy, or a folder that was later partially cleared out can all
            # leave the flag set while the folder itself is incomplete. Verify
            # the folder actually has content before trusting the flag - if
            # it doesn't, fall through and let _ensure_component_extracted
            # (which itself checks content first) re-extract and self-heal.
            resolved_check_path = (
                (base_path / "Content")
                if component in _AGENTSTORE_LIKE
                else component_path
            )
            extracted_flag = state.test_kd_component_stage_complete(st, component, "Extracted")
            # NiFi: require real binary tree (bin/nifi.sh); extensions-only is incomplete
            if component.lower() == "nifi":
                folder_ok = _nifi_is_extracted(resolved_check_path)
            else:
                folder_ok = _folder_has_content(resolved_check_path)
            if extracted_flag and folder_ok:
                log.info(f"  Skipping extract check for {component} (resume)")
            else:
                if extracted_flag and not folder_ok:
                    log.warn(
                        f"  State says {component} was Extracted, but "
                        f"{resolved_check_path} is missing/incomplete - re-extracting"
                    )
                # Always force re-extract when NiFi tree is incomplete (conf+extensions only)
                force_extract = bool(extracted_flag and not folder_ok)
                if component.lower() == "nifi" and not folder_ok:
                    force_extract = True
                ready = _ensure_component_extracted(
                    component=component,
                    component_path=component_path,
                    base_path=base_path,
                    zip_path=config.get("ZipPath", ""),
                    dry_run=dry_run,
                    non_interactive=non_interactive,
                    force=force_extract,
                )
                if not ready:
                    log.step_result(
                        f"Extract check for {component}",
                        False,
                        "Extraction failed or no matching ZIP found in ZipPath",
                    )
                    failed.append(component)
                    continue
                log.step_result(f"Extract check for {component}", True, str(component_path))
                if not dry_run:
                    st = state.set_kd_component_stage(base_path, st, component, "Extracted")
                    # Stage nifi/**/*.nar from this component folder into nifi-connectors
                    if component.lower() != "nifi":
                        stage_r = _stage_nifi_nars_from_component(component_path, dry_run=False)
                        if stage_r.get("Count"):
                            log.step_result(
                                f"Stage {component} nifi/*.nar -> nifi-connectors",
                                stage_r.get("Success", False),
                                stage_r.get("Detail", ""),
                            )

            # --- NiFi post-extract: scan all ZIPs for nifi/*.nar, then copy to extensions ---
            if component.lower() == "nifi" and not dry_run:
                nar_result = _copy_nifi_nar_extensions(
                    base_path,
                    dry_run=False,
                    zip_path=config.get("ZipPath") or "",
                )
                # Ensure entire NiFi tree is owned by the invoking user
                try:
                    chown_to_invoking_user(component_path, recursive=True)
                except Exception as e:
                    log.warn(f"  chown {component_path} after NAR restore: {e}")
                log.step_result(
                    "Copy NiFi connector .nar files -> extensions",
                    nar_result.get("Success", False),
                    nar_result.get("Detail", ""),
                )
                if not nar_result.get("Success", False):
                    log.warn(
                        "NiFi was extracted successfully, but one or more "
                        "NAR connectors could not be copied."
                    )

            # --- Agentstore-like post-extract (copy from Content) ---
            if is_agentstore_like and not dry_run:
                content_path = base_path / "Content"
                if content_path.exists():
                    log.info(f"  Assembling {component} from Content -> {component_path}")
                    component_path.mkdir(parents=True, exist_ok=True)
                    for item in content_path.iterdir():
                        dest_item = component_path / item.name
                        if item.is_dir():
                            if dest_item.exists():
                                shutil.rmtree(dest_item, ignore_errors=True)
                            shutil.copytree(item, dest_item)
                        else:
                            shutil.copy2(item, dest_item)
                    try:
                        _unblock_tree(component_path)
                    except Exception:
                        pass

                    # Prefer invoking user so assembled trees are not root-owned
                    try:
                        chown_to_invoking_user(component_path, recursive=True)
                    except Exception:
                        pass

                    # Linux IDOL binaries ship extension-less; fall back to the
                    # .exe names too in case a Windows package ever ends up here.
                    content_exe = next(
                        (p for p in (component_path / "content", component_path / "content.exe") if p.exists()),
                        None,
                    )
                    agent_exe = component_path / ("agentstore" if content_exe and content_exe.suffix == "" else "agentstore.exe")
                    if content_exe is not None and not agent_exe.exists():
                        shutil.copy2(content_exe, agent_exe)
                        os.chmod(agent_exe, os.stat(content_exe).st_mode)
                        log.info(f"  Created {agent_exe.name} from {content_exe.name}")

                    # Toolkit cfg templates overwrite Content copies
                    n_tpl = _deploy_cfg_templates(component, component_path, dry_run=False)
                    if n_tpl == 0:
                        agent_cfg = component_path / "agentstore.cfg"
                        if not agent_cfg.exists():
                            content_cfg = component_path / "content.cfg"
                            if content_cfg.exists():
                                shutil.copy2(content_cfg, agent_cfg)
                                log.info("  Created agentstore.cfg from content.cfg (no toolkit template)")

                    # Ensure init/systemd has a usable unit template for this
                    # Agentstore-like instance. Content packages typically only
                    # ship content.service; copy/adapt it to agentstore.service
                    # and to <component>.service so install_kd_service can find it.
                    try:
                        import re as _re
                        systemd_dir = component_path / "init" / "systemd"
                        systemd_dir.mkdir(parents=True, exist_ok=True)
                        content_unit = systemd_dir / "content.service"
                        agent_unit = systemd_dir / "agentstore.service"
                        comp_unit = systemd_dir / f"{component.lower()}.service"
                        src_unit = None
                        for cand in (agent_unit, content_unit):
                            if cand.is_file():
                                src_unit = cand
                                break
                        if src_unit is None:
                            matches = sorted(systemd_dir.glob("*.service"))
                            if matches:
                                src_unit = matches[0]
                        if src_unit is not None:
                            raw = src_unit.read_text(encoding="utf-8", errors="replace")
                            # OpenText start-content.sh is interactive (read DUMMY) and ignores
                            # -configfile — always runs content.exe with no cfg → Port 9100.
                            # Force binary + agentstore.cfg so ACI binds to 9050/….
                            bin_name = "content.exe"
                            for candidate in ("content.exe", "content", "agentstore.exe", "agentstore"):
                                if (component_path / candidate).is_file():
                                    bin_name = candidate
                                    break
                            raw = _re.sub(
                                r"(?im)^ExecStart=.*$",
                                f"ExecStart=__COMPONENT_INSTALL_DIR__/{bin_name} "
                                f"-configfile __COMPONENT_INSTALL_DIR__/agentstore.cfg",
                                raw,
                            )
                            raw = _re.sub(r"(?im)^Type\s*=\s*\S+", "Type=simple", raw)
                            if component.lower() != "content":
                                raw = _re.sub(
                                    r"(?im)^Description=.*$",
                                    f"Description=IDOL {component}",
                                    raw,
                                    count=1,
                                )
                            for dest in (agent_unit, comp_unit):
                                try:
                                    same = dest.resolve() == src_unit.resolve()
                                except Exception:
                                    same = False
                                if not same or "content" in src_unit.name.lower():
                                    dest.write_text(raw, encoding="utf-8")
                            log.info(
                                f"  Prepared systemd unit templates under {systemd_dir} "
                                f"(from {src_unit.name}; ExecStart={bin_name} -configfile agentstore.cfg)"
                            )
                        else:
                            log.warn(
                                f"  No vendor .service template under {systemd_dir} "
                                f"— service install will generate a unit"
                            )
                    except Exception as e:
                        log.warn(f"  Could not prepare systemd templates for {component}: {e}")

                    # Final ownership pass: templates/unit files written as root
                    # under sudo must be owned by the service user before start,
                    # or ACI fails with "Could not write ... read only".
                    _ensure_agentstore_writable(component, component_path, config)

                    log.step_result(f"Assemble {component} from Content", True, str(component_path))

            # --- License key copy for LicenseServer ---
            if component == "LicenseServer" and not dry_run:
                key_path = config.get("LicenseKeyPath")
                if key_path and Path(key_path).is_file():
                    target_dir = component_path
                    versioned = next(
                        (d for d in component_path.iterdir() if d.is_dir() and "licenseserver" in d.name.lower() and "linux" in d.name.lower()),
                        None,
                    )
                    if versioned:
                        target_dir = versioned
                    else:
                        for p in component_path.rglob("*"):
                            if p.name.lower() in ("licenseserver.exe", "licenseserver") or p.suffix.lower() == ".cfg":
                                target_dir = p.parent
                                break
                    dest_key = target_dir / "licensekey.dat"
                    shutil.copy2(key_path, dest_key)
                    log.info(f"  Copied license key -> {dest_key}")
                    log.step_result("Install license key for LicenseServer", True, str(dest_key))
                else:
                    log.info("  No LicenseKeyPath configured or file missing; skipping license key install")

            # --- Post-extract: always deploy toolkit cfg templates when present ---
            # (runs even under ExtractOnly so BasePath trees get the known-good .cfg files)
            # Agentstore-like templates are applied during assembly from Content above.
            if (
                component.lower() in _CFG_TEMPLATE_COMPONENTS
                and component not in _AGENTSTORE_LIKE
                and not state.test_kd_component_stage_complete(st, component, "Configured")
            ):
                _deploy_cfg_templates(component, component_path, dry_run=dry_run)
                if component.lower() == "answerserver" and not dry_run:
                    _ensure_answerserver_writable(component_path)

            # --- Stage: Configure ---
            if extract_only:
                log.info(f"  [ExtractOnly] Skipping configuration for {component}")
            elif state.test_kd_component_stage_complete(st, component, "Configured"):
                log.info("  Skipping configuration (resume)")
            else:
                if component.lower() == "nifi":
                    # Apache NiFi: edit nifi.properties + bootstrap.conf, ensure bin/nifi.sh is executable
                    nifi_cfg = config.get("NiFi") or {}
                    props_file = None
                    for p in component_path.rglob("nifi.properties"):
                        props_file = p
                        break
                    boot_file = None
                    for p in component_path.rglob("bootstrap.conf"):
                        boot_file = p
                        break

                    if not props_file and not dry_run:
                        log.step_result(f"Configure {component}", False, "nifi.properties not found")
                    else:
                        if not dry_run and props_file:
                            # Prefer KD template (nifi/conf/nifi.properties) so placeholders are present
                            copied = _deploy_nifi_properties_template(component_path)
                            if copied:
                                props_file = copied
                                log.info(f"  Deployed nifi.properties template -> {props_file}")
                            updates = {}
                            key = nifi_cfg.get("SensitivePropsKey") or "ChangeMe-StrongPassword123!"
                            updates["nifi.sensitive.props.key"] = key
                            port = str(nifi_cfg.get("WebHttpsPort") or (config.get("Ports") or {}).get("NiFi") or "8443")
                            updates["nifi.web.https.port"] = port
                            # Allow override via NiFi.WebHttpsHost when set (synced from Windows stable)
                            host = str(nifi_cfg.get("WebHttpsHost") or "0.0.0.0").strip() or "0.0.0.0"
                            updates["nifi.web.https.host"] = host
                            # EXTRA_IP_SANS from NiFi.ExternalIIPSAN (canonical). Legacy ExternalIpAddress migrated.
                            if not str(nifi_cfg.get("ExternalIIPSAN") or "").strip():
                                legacy = str(nifi_cfg.get("ExternalIpAddress") or "").strip()
                                if legacy:
                                    nifi_cfg["ExternalIIPSAN"] = legacy
                                nifi_cfg.pop("ExternalIpAddress", None)
                            extra_ip = str(nifi_cfg.get("ExternalIIPSAN") or "").strip()
                            if not extra_ip:
                                extra_ip = "127.0.0.1"
                            # Public/external IP first — required for browser access via
                            # https://<ExternalIIPSAN>:port/nifi (SNI + Host header).
                            proxy_parts: list = []
                            if extra_ip and extra_ip not in ("0.0.0.0",):
                                proxy_parts.append(f"{extra_ip}:{port}")
                            for candidate in (f"localhost:{port}", f"{host}:{port}", f"127.0.0.1:{port}"):
                                if candidate not in proxy_parts:
                                    proxy_parts.append(candidate)
                            updates["nifi.web.proxy.host"] = ",".join(proxy_parts)
                            # Deploy generated SSL keystore/truststore into conf/
                            ssl_mat = _deploy_nifi_ssl_material(
                                component_path,
                                setup_path=Path(str(config.get("SetupPath") or "")) if config.get("SetupPath") else None,
                            )
                            if ssl_mat.get("ok"):
                                log.info(f"  {ssl_mat.get('detail')}")
                                updates.update(_nifi_security_property_updates(ssl_mat))
                                if not ssl_mat.get("keystore_pass") or not ssl_mat.get("truststore_pass"):
                                    log.warn(
                                        "  SSL PKCS12 files copied but passwords not found "
                                        "(check ssl/.idol-ssl-passwords.env or ssl-passwords.txt)"
                                    )
                                required_ips = []
                                extra = str(nifi_cfg.get("ExternalIIPSAN") or "").strip()
                                if extra:
                                    required_ips = [x.strip() for x in extra.split(",") if x.strip()]
                                v = _verify_nifi_keystore_sans(
                                    ssl_mat.get("keystore"),
                                    ssl_mat.get("keystore_pass") or "",
                                    required_ips=required_ips,
                                )
                                if not v.get("Success") and required_ips:
                                    # Auto-heal: regenerate SSL with ExternalIIPSAN then redeploy
                                    log.warn(f"  NiFi keystore SAN verify failed: {v.get('Detail')}")
                                    log.info("  Attempting SSL regeneration with required IP SANs...")
                                    setup_p = Path(str(config.get("SetupPath") or "")) if config.get("SetupPath") else None
                                    if not setup_p or not setup_p.is_dir():
                                        setup_p = Path(__file__).resolve().parent.parent
                                    regen = _regenerate_ssl_for_nifi(
                                        setup_p,
                                        required_ips,
                                        external_hostname=str(
                                            (config.get("NiFi") or {}).get("WebHttpsHost")
                                            or config.get("ExternalHostname")
                                            or "idol-docker-host"
                                        ),
                                    )
                                    if not regen.get("Success"):
                                        log.error(f"  SSL regeneration failed: {regen.get('Detail')}")
                                        log.step_result(
                                            f"Configure {component}",
                                            False,
                                            f"keystore SAN missing {required_ips}; regen failed: {regen.get('Detail')}",
                                        )
                                        failed.append(component)
                                        continue
                                    # Redeploy material + passwords after regen
                                    ssl_mat = _deploy_nifi_ssl_material(
                                        component_path,
                                        setup_path=setup_p,
                                    )
                                    if not ssl_mat.get("ok"):
                                        log.error(f"  Redeploy after regen failed: {ssl_mat.get('detail')}")
                                        log.step_result(
                                            f"Configure {component}",
                                            False,
                                            f"SSL regen ok but deploy failed: {ssl_mat.get('detail')}",
                                        )
                                        failed.append(component)
                                        continue
                                    log.info(f"  {ssl_mat.get('detail')}")
                                    updates.update(_nifi_security_property_updates(ssl_mat))
                                    v = _verify_nifi_keystore_sans(
                                        ssl_mat.get("keystore"),
                                        ssl_mat.get("keystore_pass") or "",
                                        required_ips=required_ips,
                                    )
                                if not v.get("Success"):
                                    log.error(f"  NiFi keystore SAN verify FAILED: {v.get('Detail')}")
                                    log.step_result(
                                        f"Configure {component}",
                                        False,
                                        f"keystore SAN verify failed: {v.get('Detail')}",
                                    )
                                    failed.append(component)
                                    continue
                                log.info(f"  {v.get('Detail')}")
                            else:
                                log.warn(f"  NiFi SSL material not deployed: {ssl_mat.get('detail')}")
                            ok_props = _update_properties_file(props_file, updates)
                            if ok_props:
                                sec_note = ""
                                if ssl_mat.get("ok") and ssl_mat.get("keystore_pass"):
                                    sec_note = " + security keystore/truststore passwords"
                                log.info(
                                    f"  nifi.properties updated "
                                    f"(sensitive key + https host={host} port={port}"
                                    f" + proxy.host={updates['nifi.web.proxy.host']}"
                                    f"{sec_note})"
                                )
                            else:
                                log.warn(f"  Failed to update {props_file}")
                            ok_boot = True
                            if boot_file:
                                xms = nifi_cfg.get("HeapXms", "8g")
                                xmx = nifi_cfg.get("HeapXmx", "16g")
                                ok_boot = _update_properties_file(
                                    boot_file,
                                    {
                                        "java.arg.2": f"-Xms{xms}",
                                        "java.arg.3": f"-Xmx{xmx}",
                                    },
                                )
                                if ok_boot:
                                    log.info(f"  bootstrap.conf heap set to Xms={xms} Xmx={xmx}")
                                else:
                                    log.warn(f"  Failed to update {boot_file}")
                            # Ensure the extracted bin/nifi.sh is executable
                            nifi_sh_path = _ensure_nifi_launcher(component_path)
                            if nifi_sh_path:
                                log.info(f"  Verified NiFi launcher -> {nifi_sh_path}")
                            # Fixed UI login from config (NiFi.Username / NiFi.Password)
                            creds = _set_nifi_single_user_credentials(
                                component_path,
                                str(nifi_cfg.get("Username") or ""),
                                str(nifi_cfg.get("Password") or ""),
                            )
                            if creds.get("Skipped"):
                                log.info(f"  {creds.get('Detail')}")
                            elif creds.get("Success"):
                                log.info(f"  {creds.get('Detail')}")
                            else:
                                log.warn(f"  NiFi credentials not applied: {creds.get('Detail')}")
                            if ok_props and ok_boot:
                                log.step_result(f"Configure {component}", True, str(props_file))
                            else:
                                log.step_result(
                                    f"Configure {component}", False,
                                    f"Could not write one or more config files under {component_path}",
                                )
                        else:
                            log.step_result(
                                f"Configure {component}", True,
                                "[DryRun] would edit nifi.properties + bootstrap.conf and verify bin/nifi.sh",
                            )
                        if not dry_run:
                            st = state.set_kd_component_stage(base_path, st, component, "Configured")
                elif component.lower() == "find":
                    # OpenText Find: copy toolkit config/cfg/find/config.json into
                    # <BasePath>/Find/home/config.json (defaultLogin admin/admin, etc.)
                    find_cfg = config.get("Find") or {}
                    home_name = str(find_cfg.get("HomeDir") or "home")
                    log.info(f"  Configuring Find (deploy config.json -> {home_name}/)")
                    deploy = _deploy_find_config_json(
                        component_path,
                        home_dir_name=home_name,
                        dry_run=dry_run,
                    )
                    log.step_result(
                        f"Configure {component}",
                        bool(deploy.get("Success")),
                        deploy.get("Detail") or "",
                    )
                    if deploy.get("Success") and not dry_run:
                        st = state.set_kd_component_stage(base_path, st, component, "Configured")
                else:
                    # Standard KD .cfg components:
                    # 1) Overwrite root cfg files from toolkit config/cfg/<component>/
                    # 2) Community: ensure aes.key + SecurityInfoKeys
                    # 3) Patch LicenseHost / [Server] Port / IndexPath from config JSON
                    log.info(f"  Configuring {component} (.cfg templates + runtime patches)")
                    if component not in _AGENTSTORE_LIKE:
                        # Agentstore-like templates already deployed in post-extract assembly
                        _deploy_cfg_templates(component, component_path, dry_run=dry_run)
                    if component.lower() == "answerserver" and not dry_run:
                        _ensure_answerserver_writable(component_path)

                    if component.lower() == "community":
                        aes_result = _ensure_community_aes_key(component_path, dry_run=dry_run)
                        if not aes_result.get("Success"):
                            log.error(f"  Community aes.key step FAILED: {aes_result.get('Detail')}")
                            log.step_result(
                                f"Configure {component}",
                                False,
                                f"aes.key required: {aes_result.get('Detail')}",
                            )
                            failed.append(component)
                            continue
                        log.info(f"  Community aes.key: {aes_result.get('Detail')}")

                    cfg = _apply_standard_cfg_patches(
                        component, component_path, config, dry_run=dry_run
                    )
                    if cfg is None:
                        log.step_result(f"Configure {component}", False, "No .cfg file found")
                        failed.append(component)
                    else:
                        log.step_result(f"Configure {component}", True, str(cfg))
                        if not dry_run:
                            st = state.set_kd_component_stage(base_path, st, component, "Configured")

                    # Agentstore-family: cfg patches may create root-owned files under sudo
                    if (
                        not dry_run
                        and (
                            component in _AGENTSTORE_LIKE
                            or component.lower().endswith("agentstore")
                        )
                    ):
                        _ensure_agentstore_writable(component, component_path, config)
                        if component.lower() == "answerserver":
                            _ensure_answerserver_writable(component_path)

                    # idol.common.cfg only for LicenseServer
                    if component == "LicenseServer" and not dry_run:
                        for idol in component_path.rglob("idol.common.cfg"):
                            ini_config.update_kd_ini_file(idol, "License", "LicenseServerHost", "licenseserver")
                            ini_config.update_kd_ini_file(idol, "License", "Clients", "*.*.*.*")
                            log.info(f"  Updated idol.common.cfg: LicenseServerHost=licenseserver, Clients=*.*.*.* ({idol})")
                            log.step_result("Update idol.common.cfg (LicenseServer)", True, str(idol))

            # --- Stage: Service install ---
            if extract_only:
                log.info(f"  [ExtractOnly] Skipping service install for {component}")
            elif _is_package_only_component(component):
                log.info(f"  Skipping service install for {component} (package-only — no systemd unit)")
            elif config.get("InstallService", True):
                # Resume must not skip registration when the systemd unit is
                # actually missing (common after partial/failed runs). Verify
                # with systemctl; only skip when state says done AND the unit exists.
                svc_name_check = service_manager.get_kd_service_name(component)
                svc_registered = False
                try:
                    q = service_manager._sc("query", svc_name_check)
                    svc_registered = q.returncode == 0
                except Exception:
                    svc_registered = False

                skip_svc = (
                    state.test_kd_component_stage_complete(st, component, "ServiceInstalled")
                    and svc_registered
                )
                if skip_svc:
                    log.info(
                        f"  Skipping service install (resume) - {svc_name_check} is registered"
                    )
                else:
                    if (
                        state.test_kd_component_stage_complete(st, component, "ServiceInstalled")
                        and not svc_registered
                    ):
                        log.warn(
                            f"  State says ServiceInstalled but {svc_name_check} is NOT "
                            "registered - re-creating service"
                        )
                    if component.lower() == "nifi":
                        # Register bin/nifi.sh as a systemd unit (see
                        # service_manager.install_kd_nifi_service) when
                        # NiFi.InstallService is true.
                        if not _nifi_install_service_enabled(config):
                            log.info(
                                "  Skipping NiFi systemd service (NiFi.InstallService is false). "
                                "Use bin/nifi.sh start, or nifi/setup-nifi-service.sh later."
                            )
                            log.step_result(
                                f"Install service for {component}",
                                True,
                                "Skipped (NiFi.InstallService=false); use bin/nifi.sh or nifi/setup-nifi-service.sh later",
                            )
                        else:
                            # Ensure bin/nifi.sh is present/executable even if Configure was resumed/skipped
                            if not dry_run:
                                _ensure_nifi_launcher(component_path)
                            exe_result = discovery.find_kd_component_executable(component_path, component)
                            if not exe_result["Success"]:
                                reason = exe_result.get("Reason") or "nifi.sh not found under component path"
                                if component.lower() == "nifi":
                                    reason = (
                                        f"{reason}. "
                                        "Need Apache nifi-*-bin.zip extracted under BasePath/NiFi "
                                        "(not only conf/ or extensions/). Re-run Install with a valid "
                                        "NiFi binary ZIP in ZipPath, or delete the incomplete folder and retry."
                                    )
                                log.step_result(f"Locate launcher for {component}", False, reason)
                                failed.append(component)
                                continue
                            nifi_cmd = Path(exe_result["Executable"])
                            svc_result = service_manager.install_kd_nifi_service(
                                component=component,
                                nifi_cmd_path=nifi_cmd,
                                start_mode=config.get("StartMode", "Auto"),
                                dry_run=dry_run,
                            )
                            log.step_result(
                                f"Install service for {component}",
                                svc_result["Success"],
                                svc_result["Detail"],
                            )
                            if not svc_result["Success"]:
                                failed.append(component)
                                continue
                            if not dry_run:
                                st = state.set_kd_component_stage(base_path, st, component, "ServiceInstalled")
                    elif component.lower() == "find":
                        # OpenText Find is a Java .war - no native -install switch.
                        # Register via systemd: java -jar find.war
                        find_cfg = config.get("Find") or {}
                        if find_cfg.get("InstallService", True) is False:
                            log.info(
                                "  Skipping Find Windows service (Find.InstallService is false)."
                            )
                            log.step_result(
                                f"Install service for {component}",
                                True,
                                "Skipped (Find.InstallService=false)",
                            )
                        else:
                            exe_result = discovery.find_kd_component_executable(component_path, component)
                            if not exe_result["Success"]:
                                log.step_result(f"Locate find.war for {component}", False, exe_result["Reason"])
                                failed.append(component)
                                continue
                            find_war = Path(exe_result["Executable"])
                            ports = config.get("Ports") or {}
                            server_port = str(
                                find_cfg.get("ServerPort")
                                or ports.get("Find")
                                or "8080"
                            )
                            svc_result = service_manager.install_kd_find_service(
                                component=component,
                                find_war_path=find_war,
                                start_mode=config.get("StartMode", "Auto"),
                                server_port=server_port,
                                heap_xms=str(find_cfg.get("HeapXms") or "1g"),
                                heap_xmx=str(find_cfg.get("HeapXmx") or "2g"),
                                home_dir_name=str(find_cfg.get("HomeDir") or "home"),
                                dry_run=dry_run,
                            )
                            log.step_result(
                                f"Install service for {component}",
                                svc_result["Success"],
                                svc_result["Detail"],
                            )
                            if not svc_result["Success"]:
                                failed.append(component)
                                continue
                            if not dry_run:
                                st = state.set_kd_component_stage(base_path, st, component, "ServiceInstalled")
                    else:
                        exe_result = discovery.find_kd_component_executable(component_path, component)
                        # Self-heal: if resume skipped extract but no Linux ELF is
                        # present (wrong-arch ZIP, permissions dropped, partial
                        # folder), force a re-extract once and try again.
                        if not exe_result["Success"] and extracted_flag and not dry_run:
                            log.warn(
                                f"  No Linux executable in {component_path} after resume – "
                                f"forcing re-extract and retrying locate"
                            )
                            ready = _ensure_component_extracted(
                                component=component,
                                component_path=component_path,
                                base_path=base_path,
                                zip_path=config.get("ZipPath", ""),
                                dry_run=False,
                                non_interactive=non_interactive,
                                force=True,
                            )
                            if ready:
                                exe_result = discovery.find_kd_component_executable(component_path, component)
                        if not exe_result["Success"]:
                            log.step_result(f"Locate executable for {component}", False, exe_result["Reason"])
                            failed.append(component)
                            continue
                        if len(exe_result.get("AllCandidates") or []) > 1:
                            others = ", ".join(p.name for p in exe_result["AllCandidates"][1:4])
                            log.info(f"  Picked {exe_result['Executable'].name} for {component} (other candidates: {others})")

                        template = config.get("ServiceInstallArgsTemplate")
                        svc_result = service_manager.install_kd_service(
                            component=component,
                            executable_path=exe_result["Executable"],
                            start_mode=config.get("StartMode", "Auto"),
                            args_template=template,
                            dry_run=dry_run,
                            component_path=component_path,
                        )
                        log.step_result(f"Install service for {component}", svc_result["Success"], svc_result["Detail"])
                        if not svc_result["Success"]:
                            failed.append(component)
                            continue
                        if not dry_run:
                            st = state.set_kd_component_stage(base_path, st, component, "ServiceInstalled")

            if not dry_run and not extract_only:
                st = state.set_kd_component_stage(base_path, st, component, "Complete")
            completed.append(component)
            _sync_progress()

        except Exception as e:
            log.error(f"Unexpected error processing {component}: {e}")
            failed.append(component)
            _sync_progress()

    # ------------------------------------------------------------------
    # Post-install service start - ONLY components listed in config
    # Order: LicenseServer first when present, then remaining Components.
    # Never require LicenseServer when it is not in Components (e.g. NiFi-only).
    # ------------------------------------------------------------------
    if extract_only:
        log.info("[ExtractOnly] Skipping service start phase")
    elif config.get("InstallService", True) and not dry_run:
        components = list(config.get("Components") or [])
        has_license = any(c.lower() == "licenseserver" for c in components)
        if has_license:
            ordered = [c for c in components if c.lower() == "licenseserver"] + [
                c for c in components if c.lower() != "licenseserver"
            ]
            log.info("=== Starting services (LicenseServer first, then remaining Components) ===")
        else:
            ordered = components
            log.info("=== Starting services (configured Components only) ===")
        log.info(f"  Start order: {', '.join(ordered) if ordered else '(none)'}")

        # Phase B starts after all components are processed
        starts_done = 0
        if clock is not None:
            clock.set_progress(units_per_phase, total_all)
            clock.set_task("starting services")

        for component in ordered:
            if _is_package_only_component(component):
                log.info(f"  Skipping start of {component} (package-only — no service)")
                starts_done += 1
                if clock is not None:
                    clock.set_progress(units_per_phase + starts_done, total_all)
                continue
            if component.lower() == "nifi" and not _nifi_install_service_enabled(config):
                log.info("  Skipping start of KD-NiFi (NiFi.InstallService is false)")
                starts_done += 1
                if clock is not None:
                    clock.set_progress(units_per_phase + starts_done, total_all)
                continue
            if component.lower() == "find":
                find_cfg = config.get("Find") or {}
                if find_cfg.get("InstallService", True) is False:
                    log.info("  Skipping start of KD-Find (Find.InstallService is false)")
                    starts_done += 1
                    if clock is not None:
                        clock.set_progress(units_per_phase + starts_done, total_all)
                    continue
            if clock is not None:
                clock.set_task(f"start {component}")
            svc_name = service_manager.get_kd_service_name(component)

            if component.lower() == "nifi":
                # Start service, then poll nifi-app.log for
                # "Flow Controller started successfully." with a live timer.
                nifi_home = Path(config["BasePath"]) / "NiFi"
                log.info(
                    f"  Starting {svc_name} "
                    f"(waiting for Flow Controller in nifi-app.log, timeout 300s / 5 min)..."
                )
                nifi_result = service_manager.start_kd_nifi_and_wait_for_flow(
                    service_name=svc_name,
                    nifi_home=nifi_home,
                    timeout_seconds=300,
                )
                ok = bool(nifi_result.get("Success"))
                elapsed = nifi_result.get("ElapsedSeconds", 0)
                marker = nifi_result.get("MarkerFound")
                is_warn = bool(nifi_result.get("Warning"))
                detail = nifi_result.get("Detail") or (
                    f"Status: {'Running' if ok else 'Failed'} "
                    f"(elapsed {elapsed}s, marker={'yes' if marker else 'no'})"
                )
                # Soft cases (marker seen but service not RUNNING, or service
                # RUNNING without marker) → orange [WARN], not red ERROR.
                log.step_result(
                    f"Start service {svc_name}",
                    ok,
                    detail,
                    warning=is_warn,
                )
            else:
                # AnswerServer AnswerBank needs writable Topics/logs before start
                if component.lower() == "answerserver":
                    as_path = Path(config["BasePath"]) / "AnswerServer"
                    _ensure_answerserver_writable(as_path)
                if component.lower() == "community":
                    cpath = Path(config["BasePath"]) / "Community"
                    aes = _ensure_community_aes_key(cpath, dry_run=False)
                    if not aes.get("Success"):
                        log.warn(f"  Pre-start Community aes.key: {aes.get('Detail')}")
                # Longer timeouts: native KD binaries often need 60–90s to bind
                # ports and load indexes; Find (Java) needs longer for first JVM start.
                if component.lower() == "licenseserver":
                    timeout = 90
                elif component.lower() in (
                    "content", "community", "category", "agentstore",
                    "qmsagentstore", "answerbankagentstore", "conversationagentstore",
                    "qms", "statsserver", "answerserver",
                ):
                    timeout = 60
                elif component.lower() == "find":
                    timeout = 300
                else:
                    timeout = 45
                log.info(f"  Starting {svc_name} (timeout {timeout}s)...")
                if component.lower() == "find":
                    log.info(
                        "  Find is a Java service (systemd → java -jar find.war); "
                        "first start can take 1–3 minutes."
                    )
                # Agentstore-family: re-assert ownership before start so a prior
                # root-owned index/logs cannot cause "read only" exit.
                if component in _AGENTSTORE_LIKE or component.lower().endswith("agentstore"):
                    _ensure_agentstore_writable(
                        component,
                        Path(config["BasePath"]) / component,
                        config,
                    )
                start_info = service_manager.start_kd_service_detailed(svc_name, timeout)
                ok = bool(start_info.get("Success"))
                detail = start_info.get("Detail") or (
                    f"Status: {'Running' if ok else 'Failed / not yet Running'}"
                )
                # Start failures are soft (WARN): service may still come up later,
                # and Manage → Start / Create remains available. Do not mark the
                # whole Install as failed solely because a process is slow.
                log.step_result(
                    f"Start service {svc_name}",
                    ok,
                    detail,
                    warning=not ok,
                )
                if component.lower() == "find" and not ok:
                    log.warn(
                        "KD-Find did not reach Running within the timeout. "
                        "Check Java/JAVA_HOME and Find/logs/find-service-*.log. "
                        "You can start it later: systemctl start kd-find.service "
                        "or ./manage-kdservices.sh start --components Find"
                    )
                if component.lower() == "licenseserver":
                    if not ok:
                        log.warn(
                            "LicenseServer did not reach Running; continuing with remaining "
                            "Components (check licensekey.dat and LicenseServer logs). "
                            "NiFi/Find do not require it to be up first."
                        )
                    else:
                        # Let LicenseServer finish binding before dependents start
                        log.info("  Waiting 5s for LicenseServer to settle before starting other components...")
                        time.sleep(5)
            starts_done += 1
            if clock is not None:
                clock.set_progress(units_per_phase + starts_done, total_all)
                clock.set_task("")
    elif dry_run and config.get("InstallService", True):
        components = list(config.get("Components") or [])
        has_license = any(c.lower() == "licenseserver" for c in components)
        order_note = "LicenseServer first, then remaining" if has_license else "configured Components only"
        log.info(f"[DryRun] Would start services ({order_note}): {', '.join(components)}")

    if failed and not force:
        log.warn(f"Some components failed: {', '.join(failed)}")

    return {
        "Completed": completed,
        "Failed": failed,
        "StateFile": str(state.get_kd_state_file_path(base_path)),
    }


def invoke_kd_uninstall(
    config: Dict[str, Any],
    dry_run: bool = False,
    non_interactive: bool = False,
) -> Dict[str, Any]:
    """
    Uninstall order is strict:
      Phase 1 – STOP the kd-* systemd units for the Components listed in config
                (never touch services for components that aren't configured)
      Phase 2 – DELETE/unregister each unit (stop + orphan sweep again, then
                remove the unit file from /lib/systemd/system)
      Phase 3 – DELETE component folders (only after services are gone). Uses
                a robust Linux-aware force-rmtree (chmod, fuser/lsof kill of
                holders, rm -rf) so NiFi / Find / AnswerServer trees that
                still have open file handles are cleaned.
      Phase 4 – Optionally DELETE toolkit SSL folder (prompt when interactive)

    Only services corresponding to entries in config["Components"] are ever
    touched. A machine may have other kd-* units registered from a different
    install/config; those are left alone unless the config explicitly opts in
    via "CleanupLeftoverServices": true.
    """
    base_path = Path(config["BasePath"])
    completed: List[str] = []
    failed: List[str] = []
    components = list(reversed(config.get("Components", [])))
    cleanup_leftovers = bool(config.get("CleanupLeftoverServices", False))

    # Service name list is strictly derived from config["Components"] - the
    # installer only ever installs/manages/removes services that are part of
    # the Components list in the config JSON.
    svc_names_ordered: List[str] = []
    for component in components:
        name = service_manager.get_kd_service_name(component)
        if name not in svc_names_ordered:
            svc_names_ordered.append(name)

    if cleanup_leftovers and config.get("InstallService", True) and not dry_run:
        try:
            for extra in service_manager.list_kd_services():
                if extra not in svc_names_ordered:
                    svc_names_ordered.append(extra)
                    log.info(f"  CleanupLeftoverServices=true: found additional service not in config: {extra}")
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Phase 1: for each service, check if it's currently running - if so,
    # stop it first, then continue. Never attempt to delete a running
    # service. (stop_kd_service_force() itself is a no-op / returns True
    # immediately if the service isn't registered or isn't running.)
    # ------------------------------------------------------------------
    if config.get("InstallService", True) and not dry_run:
        log.info("=== Phase 1: Checking/stopping KD-* services (Components in config only) ===")
        for svc_name in svc_names_ordered:
            log.info(f"  Checking whether {svc_name} is running...")
            ok = service_manager.stop_kd_service_force(svc_name, 45)
            log.step_result(
                f"Stop service {svc_name}",
                ok,
                "Stopped (or was not running)" if ok else "Still running after timeout (will retry before delete)",
            )
        # Let SCM and file handles settle
        time.sleep(5)
    elif dry_run and config.get("InstallService", True):
        log.info("[DryRun] Would check each configured service, STOP it if running, then continue with "
                 "DELETE services, then DELETE folders")

    # ------------------------------------------------------------------
    # Phase 2: DELETE services (stop again immediately before each delete)
    # ------------------------------------------------------------------
    if config.get("InstallService", True):
        log.info("=== Phase 2: Removing KD-* services ===")
        for component in components:
            component_path = base_path / component
            exe_path = None
            try:
                exe_result = discovery.find_kd_component_executable(component_path, component)
                if exe_result["Success"]:
                    exe_path = exe_result["Executable"]
            except Exception:
                pass
            template = config.get("ServiceUninstallArgsTemplate")
            # skip_stop=False => stop_kd_service_force runs again inside uninstall
            svc_result = service_manager.uninstall_kd_service(
                component=component,
                executable_path=exe_path,
                args_template=template,
                dry_run=dry_run,
                skip_stop=False,
            )
            log.step_result(
                f"Uninstall service for {component}",
                svc_result["Success"],
                svc_result["Detail"],
            )
            if not svc_result["Success"] and not dry_run:
                failed.append(component)

        # Any leftover KD-* services not mapped to a config component are left
        # alone by default (only Components in the config JSON are managed).
        # Set "CleanupLeftoverServices": true in the config to also sweep
        # these up during uninstall.
        if cleanup_leftovers and not dry_run:
            try:
                leftovers = service_manager.list_kd_services()
            except Exception:
                leftovers = []
            for svc_name in leftovers:
                if svc_name in svc_names_ordered:
                    continue
                log.info(f"  CleanupLeftoverServices=true: removing leftover service {svc_name}...")
                # Check first whether it's running; if so, stop it, then continue with removal.
                service_manager.stop_kd_service_force(svc_name, 30)
                try:
                    service_manager._sc("delete", svc_name)
                    time.sleep(2)
                except Exception:
                    pass
            time.sleep(3)

    # ------------------------------------------------------------------
    # Phase 3: DELETE component folders (services must already be gone)
    # Every name listed in config["Components"] is targeted - including
    # case-insensitive folder matches under BasePath.
    # ------------------------------------------------------------------
    log.info("=== Phase 3: Deleting component folders ===")
    components_delete = list(config.get("Components") or [])
    log.info(
        f"  Components to remove under {base_path}: "
        + (", ".join(components_delete) or "(none)")
    )
    for component in components_delete:
        log.info(f"=== Deleting files for: {component} ===")
        component_path = _resolve_component_folder(base_path, component)
        try:
            if component_path is not None and component_path.exists():
                if dry_run:
                    log.info(f"  [DryRun] Would DELETE folder: {component_path}")
                    log.step_result(f"Delete files for {component}", True, f"[DryRun] {component_path}")
                else:
                    # One more stop pass in case a process respawned
                    svc_name = service_manager.get_kd_service_name(component)
                    try:
                        service_manager.stop_kd_service_force(svc_name, 15)
                    except Exception as stop_err:
                        log.warn(f"  Pre-delete stop of {svc_name}: {stop_err}")
                    # NiFi / Find / AnswerServer (Java or heavy native) often
                    # hold file locks briefly after the unit is stopped.
                    # Kill residual java / component processes that still
                    # reference this folder before we try to delete it.
                    comp_l = component.lower()
                    if os.name == "nt":
                        if comp_l in ("nifi", "find"):
                            try:
                                subprocess.run(
                                    ["taskkill", "/F", "/IM", "java.exe", "/T"],
                                    capture_output=True,
                                    timeout=30,
                                )
                                time.sleep(2)
                            except Exception:
                                pass
                    else:
                        # Linux: kill anything still holding files under the
                        # component folder (fuser) and known process names.
                        try:
                            target = str(component_path)
                            subprocess.run(
                                ["fuser", "-k", "-9", f"{target}/"],
                                capture_output=True, timeout=15,
                            )
                        except Exception:
                            pass
                        # Broad java kill only for the known Java-based components
                        if comp_l in ("nifi", "find"):
                            try:
                                # Prefer pkill of processes whose command line
                                # mentions this component path, not every java.
                                subprocess.run(
                                    ["pkill", "-9", "-f", str(component_path)],
                                    capture_output=True, timeout=10,
                                )
                            except Exception:
                                pass
                        time.sleep(1.5)
                    log.info(f"  Removing folder: {component_path}")
                    _force_rmtree(component_path)
                    if component_path.exists():
                        raise OSError(f"Folder still present after delete: {component_path}")
                    log.step_result(f"Delete files for {component}", True, str(component_path))
            else:
                log.info(f"  Folder not present (already removed): {base_path / component}")
                log.step_result(f"Delete files for {component}", True, "(folder not present)")
            if component not in failed and component not in completed:
                completed.append(component)
        except Exception as e:
            log.error(f"  Failed to delete {component}: {e}")
            log.step_result(f"Delete files for {component}", False, str(e))
            if component not in failed:
                failed.append(component)

    # Also remove IndexPath when it lives under BasePath (indexes are not a Component)
    index_path = config.get("IndexPath")
    if index_path and not dry_run:
        try:
            idx = Path(index_path)
            if idx.exists() and idx.is_dir():
                try:
                    base_resolved = base_path.resolve() if base_path.exists() else base_path
                    under_base = (
                        base_resolved in idx.resolve().parents
                        or idx.resolve() == base_resolved
                    )
                except Exception:
                    under_base = str(idx).lower().startswith(str(base_path).lower())
                if under_base:
                    log.info(f"  Removing IndexPath under BasePath: {idx}")
                    try:
                        _force_rmtree(idx)
                        log.step_result("Delete IndexPath", True, str(idx))
                    except Exception as e:
                        log.warn(f"  Could not delete IndexPath {idx}: {e}")
                        log.step_result("Delete IndexPath", False, str(e))
        except Exception as e:
            log.warn(f"  IndexPath cleanup skipped: {e}")

    # Second pass: any configured component folder still on disk → retry once
    if not dry_run:
        log.info("=== Phase 3b: Retry any remaining component folders ===")
        for component in components_delete:
            left = _resolve_component_folder(base_path, component)
            if left is None or not left.exists():
                continue
            log.warn(f"  Folder still present, retrying: {left}")
            try:
                svc_name = service_manager.get_kd_service_name(component)
                try:
                    service_manager.stop_kd_service_force(svc_name, 10)
                except Exception:
                    pass
                # Extra Linux kill of holders before the retry delete
                if os.name != "nt":
                    try:
                        subprocess.run(
                            ["fuser", "-k", "-9", f"{left}/"],
                            capture_output=True, timeout=15,
                        )
                    except Exception:
                        pass
                    try:
                        subprocess.run(
                            ["pkill", "-9", "-f", str(left)],
                            capture_output=True, timeout=10,
                        )
                    except Exception:
                        pass
                time.sleep(1.5)
                _force_rmtree(left)
                if left.exists():
                    log.error(f"  Still could not delete {left}")
                    log.step_result(f"Retry delete {component}", False, str(left))
                    if component not in failed:
                        failed.append(component)
                    if component in completed:
                        completed.remove(component)
                else:
                    log.info(f"  Retry deleted {left}")
                    log.step_result(f"Retry delete {component}", True, str(left))
                    if component in failed:
                        failed.remove(component)
                    if component not in completed:
                        completed.append(component)
            except Exception as e:
                log.error(f"  Retry failed for {component}: {e}")
                log.step_result(f"Retry delete {component}", False, str(e))
                if component not in failed:
                    failed.append(component)

    # Report what remains under BasePath for configured component names
    if base_path.is_dir() and not dry_run:
        remaining = []
        for component in components_delete:
            p = _resolve_component_folder(base_path, component)
            if p is not None and p.exists():
                remaining.append(str(p))
        if remaining:
            log.warn("  Component folders still present after Uninstall:")
            for r in remaining:
                log.warn(f"    - {r}")
        else:
            log.info("  All configured component folders removed from BasePath.")

    # Clean state file on full success
    state_file = state.get_kd_state_file_path(base_path)
    if not dry_run and state_file.exists() and not failed:
        state_file.unlink(missing_ok=True)
        log.info("Removed install state file.")

    # ------------------------------------------------------------------
    # Phase 4: DELETE toolkit SSL folder(s)
    # Generated certs/keys/passwords live next to the installer scripts.
    # Interactive: ask and confirm before deleting (default Yes).
    # Non-interactive: delete automatically (matches config/cleanup.json).
    # ------------------------------------------------------------------
    toolkit_root = Path(__file__).resolve().parent.parent
    ssl_candidates = [
        toolkit_root / "ssl",
        toolkit_root / "SSL",
        Path("/opt/kd-setup/idol-linux-setup/ssl"),
        Path("/opt/kd-setup/ssl"),
    ]
    cleanup_json = toolkit_root / "config" / "cleanup.json"
    if cleanup_json.is_file():
        try:
            import json as _json
            with open(cleanup_json, encoding="utf-8") as fh:
                cdata = _json.load(fh)
            for rel in cdata.get("ToolkitFolders") or []:
                if rel and str(rel).strip("\\/").lower() == "ssl":
                    ssl_candidates.append(toolkit_root / str(rel))
        except Exception:
            pass

    seen_ssl: set = set()
    existing_ssl: list = []
    for ssl_dir in ssl_candidates:
        try:
            key = str(ssl_dir.resolve()) if ssl_dir.exists() else str(ssl_dir)
        except Exception:
            key = str(ssl_dir)
        if key in seen_ssl:
            continue
        seen_ssl.add(key)
        if ssl_dir.exists():
            existing_ssl.append(ssl_dir)

    log.info("=== Phase 4: Toolkit SSL folder ===")
    if not existing_ssl:
        log.step_result("Delete toolkit SSL folder", True, "(not present)")
    else:
        delete_ssl = False
        if dry_run:
            for ssl_dir in existing_ssl:
                log.info(f"  [DryRun] Would DELETE SSL folder: {ssl_dir}")
            log.step_result(
                "Delete toolkit SSL folder",
                True,
                f"[DryRun] {len(existing_ssl)} folder(s) would be deleted",
            )
        elif non_interactive:
            log.info("Non-interactive uninstall: deleting toolkit SSL folder(s).")
            delete_ssl = True
        else:
            paths_list = ", ".join(str(p) for p in existing_ssl)
            try:
                ans = input(
                    f"{_ANSI_YELLOW}Delete toolkit SSL folder(s)?\n  {paths_list}\n"
                    f"This removes generated certificates, keys and passwords. (Y/N) [Y]: {_ANSI_RESET}"
                ).strip()
            except EOFError:
                ans = "Y"
            if not ans or ans.upper() == "Y":
                try:
                    confirm = input(
                        f"{_ANSI_YELLOW}Confirm deletion of SSL folder(s)? Type YES to proceed: {_ANSI_RESET}"
                    ).strip()
                except EOFError:
                    confirm = ""
                if confirm.upper() == "YES":
                    delete_ssl = True
                else:
                    log.info("SSL folder deletion cancelled (confirmation not YES).")
            else:
                log.info("SSL folder deletion declined by user.")

        if delete_ssl:
            for ssl_dir in existing_ssl:
                try:
                    _force_rmtree(ssl_dir)
                    log.step_result("Delete toolkit SSL folder", True, str(ssl_dir))
                except Exception as e:
                    log.step_result(
                        "Delete toolkit SSL folder", False, f"{ssl_dir}: {e}"
                    )
        elif not dry_run:
            for ssl_dir in existing_ssl:
                log.step_result(
                    "Delete toolkit SSL folder",
                    True,
                    f"(kept) {ssl_dir}",
                )

    return {"Completed": completed, "Failed": failed}


def invoke_kd_install_nifi_service(
    config: Dict[str, Any],
    dry_run: bool = False,
    start_after: bool = True,
) -> Dict[str, Any]:
    """
    Register (or re-register) the KD-NiFi systemd unit.

    Expects NiFi already extracted under BasePath\\NiFi. Callers (menu 07 /
    InstallJavaServices, or its NiFi-only alias InstallNiFiService) should
    ensure the ZIP is present (download if needed) and extract via
    _ensure_component_extracted before invoking this helper. Does not
    install other KD components.
    """
    base_path = Path(config["BasePath"])
    component = "NiFi"
    component_path = base_path / component

    # Case-insensitive folder match
    if not component_path.is_dir():
        if base_path.is_dir():
            for child in base_path.iterdir():
                if child.is_dir() and child.name.lower() == "nifi":
                    component_path = child
                    break

    if not component_path.is_dir():
        detail = (
            f"NiFi folder not found under {base_path}. "
            f"Run Install or Extract-Only first, then re-run "
            f"07) Install Java Services (NiFi|Find)."
        )
        log.error(detail)
        log.step_result("Install NiFi service", False, detail)
        return {"Completed": [], "Failed": [component], "Detail": detail}

    if dry_run:
        log.info(f"[DryRun] Would verify bin/nifi.sh and register KD-NiFi under {component_path}")
        log.step_result("Install NiFi service", True, f"[DryRun] {component_path}")
        return {"Completed": [component], "Failed": [], "Detail": "[DryRun]"}

    log.info(f"=== Install NiFi systemd service under {component_path} ===")
    deployed = _ensure_nifi_launcher(component_path)
    if deployed:
        log.info(f"  Verified NiFi launcher: {deployed}")

    # Always restore connectors into extensions before service registration
    # (synced from Windows stable v0.6r70m13)
    nar_result = _copy_nifi_nar_extensions(base_path, dry_run=False)
    log.step_result(
        "Copy NiFi connector .nar files -> extensions",
        nar_result.get("Success", False),
        nar_result.get("Detail", ""),
    )

    exe_result = discovery.find_kd_component_executable(component_path, component)
    if not exe_result.get("Success"):
        detail = exe_result.get("Reason") or "nifi.sh not found"
        log.step_result("Locate NiFi launcher", False, detail)
        return {"Completed": [], "Failed": [component], "Detail": detail}

    nifi_cmd = Path(exe_result["Executable"])
    svc_result = service_manager.install_kd_nifi_service(
        component=component,
        nifi_cmd_path=nifi_cmd,
        start_mode=config.get("StartMode", "Auto"),
        dry_run=False,
    )
    log.step_result(
        "Install NiFi service",
        svc_result.get("Success", False),
        svc_result.get("Detail", ""),
    )
    if not svc_result.get("Success"):
        return {
            "Completed": [],
            "Failed": [component],
            "Detail": svc_result.get("Detail", "service install failed"),
        }

    if start_after:
        nifi_home = component_path
        log.info("  Starting KD-NiFi (waiting for Flow Controller)...")
        start_result = service_manager.start_kd_nifi_and_wait_for_flow(
            service_name=service_manager.get_kd_service_name(component),
            nifi_home=nifi_home,
            timeout_seconds=300,
        )
        ok = bool(start_result.get("Success"))
        is_warn = bool(start_result.get("Warning"))
        log.step_result(
            "Start service KD-NiFi",
            ok,
            start_result.get("Detail") or "",
            warning=is_warn,
        )
        if not ok:
            return {
                "Completed": [component],
                "Failed": [],
                "Detail": (
                    "Service registered but failed to start: "
                    + str(start_result.get("Detail") or "")
                ),
                "Started": False,
            }
        return {
            "Completed": [component],
            "Failed": [],
            "Detail": start_result.get("Detail") or "Service installed and started",
            "Started": True,
        }

    return {
        "Completed": [component],
        "Failed": [],
        "Detail": svc_result.get("Detail") or "Service installed (not started)",
        "Started": False,
    }


def invoke_kd_install_find_service(
    config: Dict[str, Any],
    dry_run: bool = False,
    start_after: bool = True,
) -> Dict[str, Any]:
    """
    Register (or re-register) the KD-Find systemd unit.

    Expects Find already extracted under BasePath\\Find. Callers (menu 07 /
    InstallJavaServices) should ensure the ZIP is present and extract via
    _ensure_component_extracted before invoking this helper. Does not
    install other KD components.
    """
    base_path = Path(config["BasePath"])
    component = "Find"
    component_path = base_path / component

    # Case-insensitive folder match
    if not component_path.is_dir():
        if base_path.is_dir():
            for child in base_path.iterdir():
                if child.is_dir() and child.name.lower() == "find":
                    component_path = child
                    break

    if not component_path.is_dir():
        detail = (
            f"Find folder not found under {base_path}. "
            f"Run Install or Extract-Only first, then re-run "
            f"07) Install Java Services (NiFi|Find)."
        )
        log.error(detail)
        log.step_result("Install Find service", False, detail)
        return {"Completed": [], "Failed": [component], "Detail": detail}

    if dry_run:
        log.info(f"[DryRun] Would deploy config.json and register KD-Find under {component_path}")
        log.step_result("Install Find service", True, f"[DryRun] {component_path}")
        return {"Completed": [component], "Failed": [], "Detail": "[DryRun]"}

    log.info(f"=== Install Find Windows service under {component_path} ===")

    find_cfg = config.get("Find") or {}
    home_name = str(find_cfg.get("HomeDir") or "home")
    deploy = _deploy_find_config_json(component_path, home_dir_name=home_name, dry_run=False)
    log.step_result(
        "Deploy Find config.json",
        bool(deploy.get("Success")),
        deploy.get("Detail") or "",
    )

    exe_result = discovery.find_kd_component_executable(component_path, component)
    if not exe_result.get("Success"):
        detail = exe_result.get("Reason") or "find.war not found"
        log.step_result("Locate Find launcher", False, detail)
        return {"Completed": [], "Failed": [component], "Detail": detail}

    find_war = Path(exe_result["Executable"])
    ports = config.get("Ports") or {}
    server_port = str(find_cfg.get("ServerPort") or ports.get("Find") or "8080")
    svc_result = service_manager.install_kd_find_service(
        component=component,
        find_war_path=find_war,
        start_mode=config.get("StartMode", "Auto"),
        server_port=server_port,
        heap_xms=str(find_cfg.get("HeapXms") or "1g"),
        heap_xmx=str(find_cfg.get("HeapXmx") or "2g"),
        home_dir_name=home_name,
        dry_run=False,
    )
    log.step_result(
        "Install Find service",
        svc_result.get("Success", False),
        svc_result.get("Detail", ""),
    )
    if not svc_result.get("Success"):
        return {
            "Completed": [],
            "Failed": [component],
            "Detail": svc_result.get("Detail", "service install failed"),
        }

    if start_after:
        svc_name = service_manager.get_kd_service_name(component)
        log.info(f"  Starting {svc_name}...")
        started = service_manager.start_kd_service(svc_name, timeout_seconds=60)
        log.step_result(f"Start service {svc_name}", started, "" if started else "Did not reach Running in time")
        return {
            "Completed": [component],
            "Failed": [],
            "Detail": svc_result.get("Detail") or "Service installed",
            "Started": started,
        }

    return {
        "Completed": [component],
        "Failed": [],
        "Detail": svc_result.get("Detail") or "Service installed (not started)",
        "Started": False,
    }


def invoke_kd_set_nifi_credentials(
    config: Dict[str, Any],
    *,
    username: Optional[str] = None,
    password: Optional[str] = None,
    dry_run: bool = False,
    config_path: Optional[str | Path] = None,
) -> Dict[str, Any]:
    """
    Apply NiFi single-user UI credentials on an already-extracted BasePath\\NiFi tree.

    Username / password come from explicit args when provided, otherwise from
    config NiFi.Username / NiFi.Password. On success, also writes those values
    into the config JSON (NiFi.Username / NiFi.Password) when config_path is set
    so my-config.json stays in sync with the live NiFi login.
    Does not start or stop the service; restart NiFi afterwards if it is already
    running so the new login applies.
    """
    base_path = Path(config["BasePath"])
    component = "NiFi"
    component_path = base_path / component

    if not component_path.is_dir():
        if base_path.is_dir():
            for child in base_path.iterdir():
                if child.is_dir() and child.name.lower() == "nifi":
                    component_path = child
                    break

    if not component_path.is_dir():
        detail = (
            f"NiFi folder not found under {base_path}. "
            f"Run Install or Extract-Only first, then re-run SetNiFiCredentials."
        )
        log.error(detail)
        log.step_result("Set NiFi UI credentials", False, detail)
        return {"Completed": [], "Failed": [component], "Detail": detail}

    nifi_cfg = config.get("NiFi") or {}
    user = (username if username is not None else str(nifi_cfg.get("Username") or "")).strip()
    pwd = (password if password is not None else str(nifi_cfg.get("Password") or "")).strip()

    if not user or not pwd:
        detail = (
            "Username and password are required. Set NiFi.Username / NiFi.Password "
            "in the config JSON, or pass --nifi-username / --nifi-password."
        )
        log.error(detail)
        log.step_result("Set NiFi UI credentials", False, detail)
        return {"Completed": [], "Failed": [component], "Detail": detail}

    if dry_run:
        log.info(
            f"[DryRun] Would set NiFi single-user credentials for user '{user}' "
            f"under {component_path}"
        )
        log.step_result("Set NiFi UI credentials", True, f"[DryRun] user={user}")
        return {"Completed": [component], "Failed": [], "Detail": "[DryRun]"}

    log.info(f"=== Set NiFi UI credentials under {component_path} (user='{user}') ===")
    result = _set_nifi_single_user_credentials(component_path, user, pwd)
    ok = bool(result.get("Success")) and not result.get("Skipped")
    # Skipped only when user/pass empty - we already guard that above
    if result.get("Skipped"):
        ok = False
    log.step_result(
        "Set NiFi UI credentials",
        ok,
        result.get("Detail") or ("OK" if ok else "failed"),
    )
    if ok:
        # Keep in-memory config aligned
        nifi_section = config.setdefault("NiFi", {})
        if not isinstance(nifi_section, dict):
            nifi_section = {}
            config["NiFi"] = nifi_section
        nifi_section["Username"] = user
        nifi_section["Password"] = pwd

        # Persist to my-config.json (or whatever --config points at)
        persisted = False
        if config_path:
            try:
                from .config import update_nifi_credentials_in_config
                persisted = update_nifi_credentials_in_config(config_path, user, pwd)
            except Exception as e:
                log.warn(f"  Could not update config file {config_path}: {e}")
                persisted = False
        if persisted:
            log.info(f"  Updated NiFi.Username / NiFi.Password in {config_path}")
        elif config_path:
            log.warn(f"  Failed to write NiFi credentials into {config_path}")

        log.info(
            "  Credentials updated in login-identity-providers.xml. "
            "If NiFi is running, restart it for the new login to take effect:"
        )
        log.info("    ./manage-kdservices.sh restart --components NiFi")
        log.info("    or:  systemctl restart kd-nifi.service")
        return {
            "Completed": [component],
            "Failed": [],
            "Detail": result.get("Detail"),
            "ConfigUpdated": persisted,
        }
    return {
        "Completed": [],
        "Failed": [component],
        "Detail": result.get("Detail") or "set-single-user-credentials failed",
    }



def invoke_kd_repair(
    config: Dict[str, Any],
    dry_run: bool = False,
    force: bool = False,
    non_interactive: bool = False,
) -> Dict[str, Any]:
    log.info("Repair mode: performing uninstall then fresh install...")
    un = invoke_kd_uninstall(
        config, dry_run=dry_run, non_interactive=non_interactive
    )
    if un["Failed"] and not force:
        log.warn("Uninstall phase had failures; aborting repair (use --force to continue).")
        return {
            "Completed": un["Completed"],
            "Failed": un["Failed"],
            "StateFile": str(state.get_kd_state_file_path(config["BasePath"])),
        }
    inst = invoke_kd_install(
        config, dry_run=dry_run, force=force, resume=False, non_interactive=non_interactive
    )
    return {
        "Completed": inst["Completed"],
        "Failed": list(set(un["Failed"] + inst["Failed"])),
        "StateFile": inst["StateFile"],
    }


def invoke_kd_configure(config: Dict[str, Any], dry_run: bool = False) -> Dict[str, Any]:
    base_path = Path(config["BasePath"])
    completed: List[str] = []
    failed: List[str] = []

    if not base_path.exists():
        log.error(f"BasePath does not exist: {base_path}. Cannot configure.")
        return {"Completed": [], "Failed": list(config.get("Components", []))}

    for component in config.get("Components", []):
        log.info(f"=== Configuring component: {component} ===")
        if _is_package_only_component(component):
            log.info(f"  {component} is package-only — skipping configure")
            completed.append(component)
            continue
        component_path = base_path / component
        if not component_path.exists():
            log.step_result(f"Configure {component}", False, "Component folder not found")
            failed.append(component)
            continue
        try:
            if component.lower() == "nifi":
                nifi_cfg = config.get("NiFi") or {}
                props_file = next(component_path.rglob("nifi.properties"), None)
                boot_file = next(component_path.rglob("bootstrap.conf"), None)
                if not props_file:
                    log.step_result(f"Configure {component}", False, "nifi.properties not found")
                    failed.append(component)
                else:
                    if not dry_run:
                        copied = _deploy_nifi_properties_template(component_path)
                        if copied:
                            props_file = copied
                            log.info(f"  Deployed nifi.properties template -> {props_file}")
                        port = str(nifi_cfg.get("WebHttpsPort") or (config.get("Ports") or {}).get("NiFi") or "8443")
                        host = str(nifi_cfg.get("WebHttpsHost") or "0.0.0.0").strip() or "0.0.0.0"
                        if not str(nifi_cfg.get("ExternalIIPSAN") or "").strip():
                            legacy = str(nifi_cfg.get("ExternalIpAddress") or "").strip()
                            if legacy:
                                nifi_cfg["ExternalIIPSAN"] = legacy
                            nifi_cfg.pop("ExternalIpAddress", None)
                        extra_ip = str(nifi_cfg.get("ExternalIIPSAN") or "").strip() or "127.0.0.1"
                        proxy_parts = [f"localhost:{port}", f"{host}:{port}"]
                        if extra_ip and extra_ip not in (host, "0.0.0.0"):
                            if f"{extra_ip}:{port}" not in proxy_parts:
                                proxy_parts.append(f"{extra_ip}:{port}")
                        updates = {
                            "nifi.sensitive.props.key": nifi_cfg.get("SensitivePropsKey") or "ChangeMe-StrongPassword123!",
                            "nifi.web.https.port": port,
                            "nifi.web.https.host": host,
                            "nifi.web.proxy.host": ",".join(proxy_parts),
                        }
                        ssl_mat = _deploy_nifi_ssl_material(
                            component_path,
                            setup_path=Path(str(config.get("SetupPath") or "")) if config.get("SetupPath") else None,
                        )
                        if ssl_mat.get("ok"):
                            log.info(f"  {ssl_mat.get('detail')}")
                            updates.update(_nifi_security_property_updates(ssl_mat))
                        else:
                            log.warn(f"  NiFi SSL material not deployed: {ssl_mat.get('detail')}")
                        _update_properties_file(props_file, updates)
                        if boot_file:
                            xms = nifi_cfg.get("HeapXms", "8g")
                            xmx = nifi_cfg.get("HeapXmx", "16g")
                            _update_properties_file(
                                boot_file,
                                {"java.arg.2": f"-Xms{xms}", "java.arg.3": f"-Xmx{xmx}"},
                            )
                        nifi_sh_path = _ensure_nifi_launcher(component_path)
                        if nifi_sh_path:
                            log.info(f"  Verified NiFi launcher -> {nifi_sh_path}")
                        creds = _set_nifi_single_user_credentials(
                            component_path,
                            str(nifi_cfg.get("Username") or ""),
                            str(nifi_cfg.get("Password") or ""),
                        )
                        if creds.get("Skipped"):
                            log.info(f"  {creds.get('Detail')}")
                        elif creds.get("Success"):
                            log.info(f"  {creds.get('Detail')}")
                        else:
                            log.warn(f"  NiFi credentials not applied: {creds.get('Detail')}")
                    log.step_result(f"Configure {component}", True, str(props_file))
                    completed.append(component)
            elif component.lower() == "find":
                find_cfg = config.get("Find") or {}
                home_name = str(find_cfg.get("HomeDir") or "home")
                deploy = _deploy_find_config_json(
                    component_path,
                    home_dir_name=home_name,
                    dry_run=dry_run,
                )
                log.step_result(
                    f"Configure {component}",
                    bool(deploy.get("Success")),
                    deploy.get("Detail") or "",
                )
                if deploy.get("Success"):
                    completed.append(component)
                else:
                    failed.append(component)
            else:
                # Overwrite toolkit cfg templates, then apply runtime patches
                log.info(f"  Configuring {component} (.cfg templates + runtime patches)")
                _deploy_cfg_templates(component, component_path, dry_run=dry_run)
                if component.lower() == "answerserver" and not dry_run:
                    _ensure_answerserver_writable(component_path)
                if component.lower() == "answerserver" and not dry_run:
                    _ensure_answerserver_writable(component_path)

                if component.lower() == "community":
                    aes_result = _ensure_community_aes_key(component_path, dry_run=dry_run)
                    if not aes_result.get("Success"):
                        log.error(f"  Community aes.key step FAILED: {aes_result.get('Detail')}")
                        log.step_result(
                            f"Configure {component}",
                            False,
                            f"aes.key required: {aes_result.get('Detail')}",
                        )
                        failed.append(component)
                        continue
                    log.info(f"  Community aes.key: {aes_result.get('Detail')}")

                cfg = _apply_standard_cfg_patches(
                    component, component_path, config, dry_run=dry_run
                )
                if cfg is None:
                    log.step_result(f"Configure {component}", False, "No .cfg file found")
                    failed.append(component)
                else:
                    log.step_result(f"Configure {component}", True, str(cfg))
                    completed.append(component)

                if component == "LicenseServer" and not dry_run:
                    for idol in component_path.rglob("idol.common.cfg"):
                        ini_config.update_kd_ini_file(idol, "License", "LicenseServerHost", "licenseserver")
                        ini_config.update_kd_ini_file(idol, "License", "Clients", "*.*.*.*")
                        log.step_result("Update idol.common.cfg (LicenseServer)", True, str(idol))

                    key_path = config.get("LicenseKeyPath")
                    if key_path and Path(key_path).is_file():
                        target_dir = component_path
                        versioned = next(
                            (d for d in component_path.iterdir() if d.is_dir() and "licenseserver" in d.name.lower() and "windows" in d.name.lower()),
                            None,
                        )
                        if versioned:
                            target_dir = versioned
                        dest_key = target_dir / "licensekey.dat"
                        shutil.copy2(key_path, dest_key)
                        log.info(f"  Copied license key -> {dest_key}")
        except Exception as e:
            log.error(f"Unexpected error configuring {component}: {e}")
            failed.append(component)

    return {"Completed": completed, "Failed": failed}


def invoke_kd_health_check(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Health-check only services that were supposed to be installed for Components."""
    results = []
    for component in config.get("Components", []):
        if _is_package_only_component(component):
            results.append({
                "Component": component,
                "Success": True,
                "Detail": "Skipped (package-only — NiFi connector NARs; no systemd service)",
            })
            continue
        if component.lower() == "nifi" and not _nifi_install_service_enabled(config):
            results.append({
                "Component": component,
                "Success": True,
                "Detail": "Skipped (NiFi.InstallService is false; no systemd service expected)",
            })
            continue
        if not config.get("InstallService", True):
            results.append({
                "Component": component,
                "Success": True,
                "Detail": "Skipped (InstallService is false; no systemd service expected)",
            })
            continue
        try:
            h = service_manager.test_kd_service_healthy(component)
            results.append({"Component": component, "Success": h["Success"], "Detail": h["Detail"]})
        except Exception as e:
            results.append({"Component": component, "Success": False, "Detail": f"Health check error: {e}"})
    return results
