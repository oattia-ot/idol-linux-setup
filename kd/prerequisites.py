"""
Visual C++ Redistributable check + optional auto-install.
Missing VC++ is the #1 cause of STATUS_DLL_NOT_FOUND (-1073741515) on service start.
"""

from __future__ import annotations

_ANSI_YELLOW = "\033[33m"
_ANSI_RESET = "\033[0m"

import os
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

from .logging import log

try:
    import winreg
except ImportError:
    winreg = None  # type: ignore

try:
    import certifi
except ImportError:
    certifi = None  # type: ignore


_TLS_OPENER_INSTALLED = False


def _ensure_certifi_installed() -> None:
    """
    If certifi isn't importable (e.g. requirements.txt was never re-installed
    after this feature was added), install it silently via pip so the TLS fix
    below actually works without the user having to remember a manual step.
    """
    global certifi
    if certifi is not None:
        return
    try:
        log.info("  certifi not found - installing it now (pip install certifi)...")
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--quiet", "certifi"],
            capture_output=True,
            timeout=120,
        )
        import certifi as _certifi  # re-import after install
        certifi = _certifi
        log.info("  certifi installed.")
    except Exception as e:
        log.warn(f"  Could not auto-install certifi: {e}")


def _load_windows_store_certs(ctx: ssl.SSLContext) -> int:
    """
    Load certs directly from the Windows CA/ROOT certificate stores into ctx.
    Dependency-free fallback that doesn't need certifi or PyPI access at all -
    covers machines where PyPI isn't reachable but the Windows cert store
    already trusts the relevant root (e.g. via a corporate GPO-pushed CA, or
    the OS's own root program). ctx.load_default_certs() is supposed to do
    this automatically but its behaviour varies across Python builds, so this
    is an explicit belt-and-braces pass.
    """
    if sys.platform != "win32":
        return 0
    loaded = 0
    for store in ("CA", "ROOT"):
        try:
            for cert_der, encoding, _trust in ssl.enum_certificates(store):
                if encoding != "x509_asn":
                    continue
                try:
                    ctx.load_verify_locations(cadata=cert_der)
                    loaded += 1
                except ssl.SSLError:
                    pass
        except Exception:
            continue
    return loaded


def ensure_kd_tls_opener() -> None:
    """
    Install a urllib opener with a working CA bundle for all downloads in this
    module (NiFi ZIP, Temurin/OpenJDK, Linux system packages). Also sets a realistic
    browser-like User-Agent, since Adoptium/GitHub/Apache mirrors commonly
    return HTTP 403 Forbidden for the default "Python-urllib/x.y" User-Agent
    (bot-protection at the CDN/WAF level, unrelated to auth or permissions).

    Fixes CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate,
    which is common on Windows Python installs (official python.org installer,
    imaged machines, embeddable distributions) that don't have a properly
    wired-up system CA bundle. Stacks three independent sources so this works
    even if one is unavailable (e.g. no PyPI access, or certifi never
    installed):
      1. certifi's bundled Mozilla CA set (auto-installed via pip if missing).
      2. ssl's own load_default_certs() (OS trust store, when it works).
      3. An explicit pass over the Windows CA/ROOT stores (dependency-free
         fallback - picks up corporate/internal root CAs pushed via GPO).
    Idempotent - safe to call from every download function.
    """
    global _TLS_OPENER_INSTALLED
    if _TLS_OPENER_INSTALLED:
        return

    _ensure_certifi_installed()

    try:
        ctx = ssl.create_default_context(cafile=certifi.where()) if certifi else ssl.create_default_context()
        try:
            ctx.load_default_certs()
        except Exception:
            pass
        extra = _load_windows_store_certs(ctx)

        opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))
        opener.addheaders = [
            ("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) kd-win-setup-python/1.0"),
            ("Accept", "*/*"),
        ]
        urllib.request.install_opener(opener)
        _TLS_OPENER_INSTALLED = True
        log.debug(f"TLS opener installed (certifi={'yes' if certifi else 'no'}, windows_store_certs={extra})")
    except Exception as e:
        log.warn(f"Could not configure a trusted CA bundle for downloads: {e}")


def test_kd_visual_cpp_redist(architecture: str = "X64", min_version: str = "14.0.0.0") -> Dict[str, Any]:
    """Check the registry key written by the VC++ 2015-2022 redistributable.

    On non-Windows platforms this check is not applicable and always passes.
    """
    if not sys.platform.startswith("win") or winreg is None:
        return {
            "Name": f"Visual C++ Redistributable ({architecture})",
            "Pass": True,
            "Installed": False,
            "Version": None,
            "Detail": "Not required on Linux / non-Windows platforms",
            "Skipped": True,
        }

    reg_path = rf"SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\{architecture}"
    installed = False
    version_string = None

    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, reg_path) as key:
            installed_val, _ = winreg.QueryValueEx(key, "Installed")
            if installed_val == 1:
                installed = True
                try:
                    version_string, _ = winreg.QueryValueEx(key, "Version")
                except OSError:
                    pass
    except OSError:
        pass

    meets = False
    if installed and version_string:
        try:
            clean = version_string.lstrip("v")
            from packaging.version import Version  # optional; fallback below

            meets = Version(clean) >= Version(min_version)
        except Exception:
            # packaging not installed or parse failure – presence is good enough
            meets = True
    elif installed:
        meets = True

    pass_ = installed and meets
    if pass_:
        detail = f"Installed: {version_string}"
    elif installed:
        detail = f"Installed version {version_string} is older than required {min_version}"
    else:
        detail = "Not installed (registry key not found)"

    return {
        "Name": f"Visual C++ Redistributable ({architecture})",
        "Pass": pass_,
        "Installed": installed,
        "Version": version_string,
        "Detail": detail,
    }


def install_kd_visual_cpp_redist(architecture: str = "X64", dry_run: bool = False) -> Dict[str, Any]:
    url = (
        "https://aka.ms/vs/17/release/vc_redist.x64.exe"
        if architecture.upper() == "X64"
        else "https://aka.ms/vs/17/release/vc_redist.x86.exe"
    )
    if dry_run:
        return {
            "Success": True,
            "Detail": f"[DryRun] Would download {url} and run /install /quiet /norestart",
            "RebootRequired": False,
        }

    installer = Path(tempfile.gettempdir()) / f"vc_redist.{architecture.lower()}.exe"
    try:
        ensure_kd_tls_opener()
        log.info(f"Downloading Visual C++ Redistributable ({architecture}) from {url}")
        urllib.request.urlretrieve(url, installer)

        log.info(f"Installing Visual C++ Redistributable ({architecture}) silently...")
        proc = subprocess.run(
            [str(installer), "/install", "/quiet", "/norestart"],
            capture_output=True,
            timeout=300,
        )
        exit_code = proc.returncode
        # 0 = success, 3010 = success + reboot needed, 1638 = newer already present
        success = exit_code in (0, 3010, 1638)
        detail = f"ExitCode: {exit_code}"
        if exit_code == 3010:
            detail += " (installed; a reboot is required)"
        if exit_code == 1638:
            detail += " (compatible or newer version already present)"
        if not success:
            detail += " (install failed)"
        return {
            "Success": success,
            "Detail": detail,
            "RebootRequired": exit_code == 3010,
        }
    except Exception as e:
        return {
            "Success": False,
            "Detail": f"Exception during download/install: {e}",
            "RebootRequired": False,
        }
    finally:
        try:
            installer.unlink(missing_ok=True)
        except Exception:
            pass


def confirm_kd_visual_cpp_redist(
    architecture: str = "X64",
    dry_run: bool = False,
    non_interactive: bool = False,
) -> Dict[str, Any]:
    # Linux / non-Windows: VC++ redistributable is not used — skip entirely
    if not sys.platform.startswith("win"):
        return {
            "Name": f"Visual C++ Redistributable ({architecture})",
            "Pass": True,
            "Detail": "Skipped — not required on Linux",
            "Skipped": True,
        }

    check = test_kd_visual_cpp_redist(architecture)
    if check["Pass"]:
        return {"Name": check["Name"], "Pass": True, "Detail": check["Detail"]}

    log.warn(
        f"{check['Name']} not found - {check['Detail']}. "
        "This will cause KD service installs to fail with STATUS_DLL_NOT_FOUND."
    )

    if dry_run:
        return {
            "Name": check["Name"],
            "Pass": True,
            "Detail": f"[DryRun] Missing ({check['Detail']}); would auto-download and install",
        }

    if not non_interactive:
        try:
            answer = input(f"{_ANSI_YELLOW}{check['Name']} is missing. Download and install it now? (Y/N) [Y]: {_ANSI_RESET}").strip()
            if answer.upper() == "N":
                return {
                    "Name": check["Name"],
                    "Pass": False,
                    "Detail": "Not installed; user declined automatic install",
                }
        except EOFError:
            pass  # non-interactive fallback

    install_result = install_kd_visual_cpp_redist(architecture)
    if not install_result["Success"]:
        return {
            "Name": check["Name"],
            "Pass": False,
            "Detail": (
                f"Automatic install failed - {install_result['Detail']}. "
                f"Download manually from https://aka.ms/vs/17/release/vc_redist.{architecture.lower()}.exe"
            ),
        }

    recheck = test_kd_visual_cpp_redist(architecture)
    detail = recheck["Detail"]
    if install_result.get("RebootRequired"):
        detail += " - a REBOOT is required before KD services will start correctly"
    return {"Name": check["Name"], "Pass": recheck["Pass"], "Detail": detail}


# ---------------------------------------------------------------------------
# Java version check (required by Apache NiFi 2.x / OpenText guidance)
# ---------------------------------------------------------------------------

def _java_bin_name() -> str:
    """Platform-specific Java launcher name."""
    return "java.exe" if sys.platform.startswith("win") else "java"


def _find_java_executable(java_home: Optional[Path] = None) -> Optional[Path]:
    """Locate a java binary under JAVA_HOME or on PATH (cross-platform)."""
    bin_name = _java_bin_name()
    if java_home:
        candidate = java_home / "bin" / bin_name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    # PATH lookup
    which = shutil.which(bin_name)
    if which:
        p = Path(which)
        # Skip Windows Store stubs
        if "WindowsApps" not in str(p):
            return p
    if sys.platform.startswith("win"):
        try:
            r = subprocess.run(
                ["where", "java.exe"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if r.returncode == 0:
                for line in r.stdout.splitlines():
                    p = Path(line.strip())
                    if p.is_file() and "WindowsApps" not in str(p):
                        return p
        except Exception:
            pass
    return None


def test_kd_java_version(min_major: int = 21) -> Dict[str, Any]:
    """
    Best-effort check that a suitable Java runtime is available.
    NiFi 2.x (and therefore the KD optional NiFi component) requires Java 21+.
    Official OpenText NiFi Ingest docs also require a supported JRE/JDK.
    """
    import re

    java_home = None
    version_str = None
    major = 0
    detail_parts = []

    # Prefer JAVA_HOME
    jh = os.environ.get("JAVA_HOME") or os.environ.get("JDK_HOME")
    if jh:
        java_home = Path(jh)
        detail_parts.append(f"JAVA_HOME={jh}")

    java_exe = _find_java_executable(java_home)

    if not java_exe:
        bin_name = _java_bin_name()
        return {
            "Name": f"Java {min_major}+ (required for NiFi 2.x)",
            "Pass": False,
            "Version": None,
            "JavaHome": None,
            "Detail": (
                f"{bin_name} not found on PATH and JAVA_HOME is not set. "
                "Install Eclipse Temurin JDK 21 (or later) from https://adoptium.net/ "
                "and set JAVA_HOME."
            ),
        }

    # Infer JAVA_HOME from bin/java when not set
    if java_home is None:
        try:
            # .../bin/java → parent of bin
            inferred = java_exe.resolve().parent.parent
            if (inferred / "bin" / _java_bin_name()).is_file():
                java_home = inferred
                detail_parts.append(f"JAVA_HOME(inferred)={inferred}")
        except Exception:
            pass

    try:
        proc = subprocess.run(
            [str(java_exe), "-version"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        # java -version writes to stderr
        out = (proc.stderr or "") + (proc.stdout or "")
        m = re.search(r'version\s+"?(\d+)(?:\.(\d+))?', out, re.IGNORECASE)
        if not m:
            m = re.search(r'(\d+)\.(\d+)\.(\d+)', out)
        if m:
            major = int(m.group(1))
            version_str = m.group(0).strip().strip('"')
        detail_parts.append(f"Found: {java_exe}")
        if version_str:
            detail_parts.append(f"Version: {version_str}")
    except Exception as e:
        return {
            "Name": f"Java {min_major}+ (required for NiFi 2.x)",
            "Pass": False,
            "Version": None,
            "JavaHome": str(java_home) if java_home else None,
            "Detail": f"Could not execute java -version: {e}",
        }

    passed = major >= min_major
    if passed:
        detail = "; ".join(detail_parts) if detail_parts else f"Java {major} detected"
    else:
        detail = (
            f"Detected Java major version {major or 'unknown'} "
            f"(need >={min_major}). {'; '.join(detail_parts)}. "
            "Install Eclipse Temurin JDK 21+ from https://adoptium.net/ and set JAVA_HOME."
        )

    return {
        "Name": f"Java {min_major}+ (required for NiFi 2.x)",
        "Pass": passed,
        "Version": version_str,
        "JavaHome": str(java_home) if java_home else None,
        "Detail": detail,
    }


def find_temurin_jdk_install_dir(major: int = 21) -> Optional[Path]:
    """Look for an already-installed Eclipse Temurin / OpenJDK matching major version."""
    bin_name = _java_bin_name()
    search_roots: List[Path] = []

    if sys.platform.startswith("win"):
        program_files = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        search_roots.append(program_files / "Eclipse Adoptium")
        search_roots.append(program_files / "Java")
    else:
        search_roots.extend(
            [
                Path("/opt/java"),
                Path("/opt/jdk"),
                Path("/usr/lib/jvm"),
                Path("/usr/java"),
                Path.home() / ".jdks",
                Path.home() / "java",
            ]
        )

    for base in search_roots:
        if not base.is_dir():
            continue
        candidates = sorted(base.glob(f"jdk-{major}*"), reverse=True)
        candidates += sorted(base.glob(f"temurin-{major}*"), reverse=True)
        candidates += sorted(base.glob(f"java-{major}*"), reverse=True)
        for c in candidates:
            if (c / "bin" / bin_name).is_file():
                return c
        # Some layouts: /usr/lib/jvm/java-21-openjdk-amd64
        for c in sorted(base.iterdir(), reverse=True) if base.is_dir() else []:
            if not c.is_dir():
                continue
            name = c.name.lower()
            if str(major) in name and (c / "bin" / bin_name).is_file():
                return c
    return None


TEMURIN_FEATURE_VERSION = "21"


def _temurin_download_url(version: str = TEMURIN_FEATURE_VERSION) -> str:
    """
    Platform-specific Adoptium download URL.
    Windows: MSI installer. Linux: tar.gz binary archive.
    """
    if sys.platform.startswith("win"):
        # MSI installer API
        return (
            f"https://api.adoptium.net/v3/installer/latest/{version}/ga/"
            f"windows/x64/jdk/hotspot/normal/eclipse"
        )
    # Linux x64 binary tarball
    return (
        f"https://api.adoptium.net/v3/binary/latest/{version}/ga/"
        f"linux/x64/jdk/hotspot/normal/eclipse?project=jdk"
    )


# Back-compat alias (Windows-oriented; prefer _temurin_download_url)
TEMURIN_INSTALLER_URL = (
    f"https://api.adoptium.net/v3/installer/latest/{TEMURIN_FEATURE_VERSION}/ga/"
    f"windows/x64/jdk/hotspot/normal/eclipse"
)


def get_permanent_env_var(name: str) -> Optional[str]:
    """
    Read a permanent, machine-wide environment variable directly from the
    registry (HKLM\\SYSTEM\\CurrentControlSet\\Control\\Session Manager\\Environment),
    independent of what's in the current process's os.environ (which may be
    stale/unset even though the permanent value already exists).
    Returns None if it isn't set there, or if not on Windows.
    """
    if winreg is None:
        return None
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
        ) as key:
            value, _ = winreg.QueryValueEx(key, name)
            return value if value else None
    except (FileNotFoundError, OSError):
        return None
    except Exception:
        return None


def set_permanent_env_var_if_missing(name: str, value: str) -> Dict[str, Any]:
    """
    Set a permanent, machine-wide environment variable.
    Windows: setx <name> <value> /M (registry).
    Linux: write /etc/profile.d/kd-env.sh when running as root; always update
    the current process os.environ so the rest of this run sees the value.
    Never overwrites an operator's existing permanent value on Windows.
    """
    # Always update current process
    if not os.environ.get(name):
        os.environ[name] = value

    if not sys.platform.startswith("win"):
        # Linux: process env is enough for this run; persist via profile.d if root
        written = False
        detail = f"Process {name}={value}"
        if os.geteuid() == 0:
            profile = Path("/etc/profile.d/kd-java.sh")
            try:
                existing_text = profile.read_text() if profile.is_file() else ""
                line = f'export {name}="{value}"\n'
                if f"export {name}=" not in existing_text:
                    with open(profile, "a", encoding="utf-8") as f:
                        if existing_text and not existing_text.endswith("\n"):
                            f.write("\n")
                        f.write(f"# Knowledge Discovery installer\n{line}")
                    try:
                        profile.chmod(0o644)
                    except Exception:
                        pass
                    written = True
                    detail = f"Set {name}={value} (process + {profile})"
                else:
                    detail = f"{name} already in {profile}; process updated"
            except Exception as e:
                detail = f"Process {name}={value}; could not write profile.d: {e}"
        else:
            detail = f"Process {name}={value} (not root — skipped /etc/profile.d)"
        return {
            "Success": True,
            "Written": written,
            "Value": value,
            "Detail": detail,
        }

    existing = get_permanent_env_var(name)
    if existing:
        os.environ[name] = existing
        return {
            "Success": True,
            "Written": False,
            "Value": existing,
            "Detail": f"{name} already permanently set to '{existing}' - left unchanged",
        }

    try:
        proc = subprocess.run(["setx", name, value, "/M"], capture_output=True, text=True, timeout=30)
        if proc.returncode == 0:
            os.environ[name] = value
            return {
                "Success": True,
                "Written": True,
                "Value": value,
                "Detail": f"Set permanent machine environment variable {name}={value}",
            }
        return {
            "Success": False,
            "Written": False,
            "Value": None,
            "Detail": f"setx {name} failed ({(proc.stderr or '').strip()})",
        }
    except Exception as e:
        return {
            "Success": False,
            "Written": False,
            "Value": None,
            "Detail": f"Exception running setx {name}: {e}",
        }


def _path_entry_present(path_value: str, entry: str, java_home: Optional[str] = None) -> bool:
    """
    True if *entry* (or an equivalent expanded form) is already on PATH.
    Treats %JAVA_HOME%\\bin and <java_home>\\bin as the same slot.
    """
    parts = [p.strip() for p in path_value.split(";") if p.strip()]
    entry_norm = entry.rstrip("\\/").lower().replace("/", "\\")
    expanded = None
    if java_home:
        expanded = str(Path(java_home) / "bin").rstrip("\\/").lower().replace("/", "\\")
    for p in parts:
        p_norm = p.rstrip("\\/").lower().replace("/", "\\")
        if p_norm == entry_norm:
            return True
        if expanded and p_norm == expanded:
            return True
        # %JAVA_HOME%\bin already present (any casing / slash style)
        if p_norm.replace("%", "") == "java_home\\bin" or p_norm == "%java_home%\\bin":
            return True
    return False


def ensure_java_bin_on_path(java_home: Optional[str | Path] = None) -> Dict[str, Any]:
    """
    Ensure ``%JAVA_HOME%\\bin`` is on the permanent machine PATH.

    Writes the *unexpanded* entry ``%JAVA_HOME%\\bin`` (REG_EXPAND_SZ) so PATH
    tracks whatever JAVA_HOME is set to later. Also prepends the expanded
    ``<java_home>\\bin`` to the current process PATH so the rest of this
    installer run can find java.exe without a new shell.

    Safe / idempotent: if either form is already present, PATH is left alone.
    Avoids ``setx PATH`` (1024-char truncation risk) and edits the registry
    directly when possible.
    """
    jh = str(java_home or os.environ.get("JAVA_HOME") or get_permanent_env_var("JAVA_HOME") or "").strip()
    entry = r"%JAVA_HOME%\bin"

    if not jh:
        return {
            "Success": False,
            "Written": False,
            "Detail": "JAVA_HOME is not set; cannot add %JAVA_HOME%\\bin to PATH",
        }

    # Always fix up this process so subsequent steps (NiFi service, java -version) work
    expanded_bin = str(Path(jh) / "bin")
    current = os.environ.get("PATH", "")
    if expanded_bin.lower() not in current.lower() and entry.lower() not in current.lower():
        os.environ["PATH"] = expanded_bin + (";" + current if current else "")

    if winreg is None or sys.platform != "win32":
        return {
            "Success": True,
            "Written": False,
            "Detail": f"Non-Windows or no winreg; process PATH updated with {expanded_bin}",
        }

    env_key = r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, env_key, 0, winreg.KEY_READ | winreg.KEY_SET_VALUE) as key:
            try:
                path_val, path_type = winreg.QueryValueEx(key, "Path")
            except FileNotFoundError:
                path_val, path_type = "", winreg.REG_EXPAND_SZ

            if not isinstance(path_val, str):
                path_val = str(path_val or "")

            if _path_entry_present(path_val, entry, jh):
                return {
                    "Success": True,
                    "Written": False,
                    "Detail": f"PATH already contains {entry} (or {expanded_bin}) - left unchanged",
                }

            new_path = entry + (";" + path_val if path_val else "")
            # Prefer REG_EXPAND_SZ so %JAVA_HOME% is expanded at runtime
            write_type = winreg.REG_EXPAND_SZ if path_type in (winreg.REG_EXPAND_SZ, winreg.REG_SZ) else path_type
            winreg.SetValueEx(key, "Path", 0, write_type, new_path)

        # Broadcast WM_SETTINGCHANGE so new processes see the update without reboot
        try:
            import ctypes
            HWND_BROADCAST = 0xFFFF
            WM_SETTINGCHANGE = 0x001A
            SMTO_ABORTIFHUNG = 0x0002
            ctypes.windll.user32.SendMessageTimeoutW(
                HWND_BROADCAST, WM_SETTINGCHANGE, 0, "Environment", SMTO_ABORTIFHUNG, 5000, None
            )
        except Exception:
            pass

        return {
            "Success": True,
            "Written": True,
            "Detail": f"Added {entry} to permanent machine PATH",
        }
    except PermissionError:
        return {
            "Success": False,
            "Written": False,
            "Detail": "Access denied updating machine PATH (need Administrator). "
                      f"Add {entry} manually under System Properties > Environment Variables.",
        }
    except Exception as e:
        return {
            "Success": False,
            "Written": False,
            "Detail": f"Could not update machine PATH: {e}",
        }


def ensure_java_home_and_path(java_home: Optional[str | Path] = None) -> Dict[str, Any]:
    """
    Ensure JAVA_HOME is set permanently (if missing) and ``%JAVA_HOME%\\bin``
    is on the permanent machine PATH. Returns a combined result dict.
    """
    jh = java_home
    if jh is None:
        jh = os.environ.get("JAVA_HOME") or get_permanent_env_var("JAVA_HOME")
    if jh is None:
        found = find_temurin_jdk_install_dir(21)
        if found:
            jh = found

    details: List[str] = []
    success = True

    if jh:
        env_result = set_permanent_env_var_if_missing("JAVA_HOME", str(jh))
        details.append(env_result["Detail"])
        if not env_result["Success"]:
            success = False
        path_result = ensure_java_bin_on_path(jh)
        details.append(path_result["Detail"])
        if not path_result["Success"]:
            success = False
        return {
            "Success": success,
            "JavaHome": str(jh),
            "Detail": "; ".join(details),
        }

    return {
        "Success": False,
        "JavaHome": None,
        "Detail": "No JAVA_HOME value available to set",
    }


def _is_elevated() -> bool:
    """True if the current process has Administrator rights (Windows)."""
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return True  # can't tell - don't block on it


def _pending_reboot() -> bool:
    """
    True if Windows has a reboot pending (PendingFileRenameOperations or
    CBS RebootPending). Very common on freshly-imaged/patched Azure VMs and
    is one of the most frequent causes of MSI error 1603.
    """
    if winreg is None:
        return False
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\Session Manager",
        ) as key:
            try:
                winreg.QueryValueEx(key, "PendingFileRenameOperations")
                return True
            except FileNotFoundError:
                pass
    except Exception:
        pass
    try:
        winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending",
        ).Close()
        return True
    except Exception:
        pass
    return False


def _tail_msi_log(log_path: Path, lines: int = 25) -> str:
    """Return the last N lines of an msiexec /l*v log, filtered to error lines when possible."""
    try:
        text = log_path.read_text(encoding="utf-16", errors="ignore")
        if not text.strip():
            text = log_path.read_text(encoding="utf-8", errors="ignore")
        all_lines = [l for l in text.splitlines() if l.strip()]
        error_lines = [l for l in all_lines if "error" in l.lower() or "return value 3" in l.lower()]
        tail = error_lines[-lines:] if error_lines else all_lines[-lines:]
        return "\n".join(tail)
    except Exception as e:
        return f"(could not read {log_path}: {e})"


def _find_registered_temurin_product(major: int = 21) -> Optional[Dict[str, str]]:
    """
    Look for an existing Eclipse Temurin JDK registration in the Windows
    Installer uninstall registry, independent of whether
    find_temurin_jdk_install_dir() found working files on disk.

    This catches the classic "reconfigured the product ... error 1603"
    failure: a previous install attempt left a product registered (so a new
    /i install becomes an implicit repair) but the files/folder are
    missing/incomplete, and the repair then fails (WixRemoveFoldersEx /
    SECUREREPAIR errors). Returns
    {"ProductCode": ..., "InstallLocation": ..., "DisplayName": ...} or None.
    """
    if winreg is None:
        return None
    roots = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]
    for hive, path in roots:
        try:
            with winreg.OpenKey(hive, path) as base:
                i = 0
                while True:
                    try:
                        subkey_name = winreg.EnumKey(base, i)
                    except OSError:
                        break
                    i += 1
                    try:
                        with winreg.OpenKey(base, subkey_name) as sub:
                            try:
                                display_name = winreg.QueryValueEx(sub, "DisplayName")[0]
                            except FileNotFoundError:
                                continue
                            name_lower = display_name.lower()
                            if "temurin" in name_lower and "jdk" in name_lower and str(major) in display_name:
                                install_loc = ""
                                try:
                                    install_loc = winreg.QueryValueEx(sub, "InstallLocation")[0]
                                except FileNotFoundError:
                                    pass
                                return {
                                    "ProductCode": subkey_name,
                                    "InstallLocation": install_loc,
                                    "DisplayName": display_name,
                                }
                    except Exception:
                        continue
        except Exception:
            continue
    return None


def _install_kd_java_linux(version: str = TEMURIN_FEATURE_VERSION) -> Dict[str, Any]:
    """
    Download Eclipse Temurin JDK tar.gz for Linux x64 and extract under /opt/java.
    Sets JAVA_HOME for the current process and persists via /etc/profile.d when root.
    """
    import tarfile

    # Reuse an existing install if present
    existing = find_temurin_jdk_install_dir(int(version))
    if existing:
        home_path = ensure_java_home_and_path(existing)
        detail = f"Found existing JDK: {existing}; {home_path['Detail']}"
        log.info(f"  {detail}")
        return {"Success": True, "Detail": detail, "JavaHome": existing}

    install_root = Path("/opt/java")
    if os.geteuid() != 0:
        install_root = Path.home() / "java"
        log.info(f"  Not root — installing JDK under {install_root}")

    url = _temurin_download_url(version)
    tmp_tar = Path(tempfile.gettempdir()) / f"temurin-jdk{version}-linux-x64.tar.gz"

    try:
        ensure_kd_tls_opener()
        for attempt in (1, 2):
            log.info(
                f"Downloading Eclipse Temurin JDK {version} (Linux x64) from Adoptium "
                f"({url})... [attempt {attempt}/2]"
            )
            try:
                urllib.request.urlretrieve(url, tmp_tar)
            except Exception as e:
                log.warn(f"  Download attempt {attempt} failed: {e}")
                tmp_tar.unlink(missing_ok=True)
                continue
            size = tmp_tar.stat().st_size if tmp_tar.exists() else 0
            if size >= 50_000_000:  # Linux tarball is typically ~180 MB
                break
            log.warn(f"  Download looks truncated ({size / 1_000_000:.1f} MB); retrying...")
            tmp_tar.unlink(missing_ok=True)
        else:
            size = tmp_tar.stat().st_size if tmp_tar.exists() else 0
            return {
                "Success": False,
                "Detail": (
                    f"Download produced a suspiciously small file ({size} bytes) after 2 attempts. "
                    f"Check network access to api.adoptium.net, or install manually: "
                    f"sudo apt install openjdk-21-jdk  (or download from https://adoptium.net/)"
                ),
                "JavaHome": None,
            }

        size_mb = tmp_tar.stat().st_size // (1024 * 1024)
        log.info(f"  Downloaded {size_mb} MB. Extracting to {install_root}...")

        install_root.mkdir(parents=True, exist_ok=True)
        # Extract; Adoptium tarballs contain a single top-level jdk-21.x.x+y folder
        with tarfile.open(tmp_tar, "r:gz") as tf:
            # Python 3.12+ has filter=; older versions don't
            try:
                tf.extractall(path=install_root, filter="data")
            except TypeError:
                tf.extractall(path=install_root)

        java_home = find_temurin_jdk_install_dir(int(version))
        if not java_home:
            # Fallback: newest directory under install_root that has bin/java
            for c in sorted(install_root.iterdir(), reverse=True):
                if c.is_dir() and (c / "bin" / "java").is_file():
                    java_home = c
                    break
        if not java_home:
            return {
                "Success": False,
                "Detail": f"Extracted tarball under {install_root} but could not find bin/java",
                "JavaHome": None,
            }

        home_path = ensure_java_home_and_path(java_home)
        # Also prepend bin to PATH for this process
        bin_dir = str(java_home / "bin")
        if bin_dir not in os.environ.get("PATH", ""):
            os.environ["PATH"] = bin_dir + ":" + os.environ.get("PATH", "")

        detail = f"Installed Eclipse Temurin JDK {version} -> {java_home}; {home_path['Detail']}"
        log.info(f"  {detail}")
        return {"Success": True, "Detail": detail, "JavaHome": java_home}
    except Exception as e:
        return {
            "Success": False,
            "Detail": f"Exception during Java install: {e}",
            "JavaHome": None,
        }
    finally:
        try:
            tmp_tar.unlink(missing_ok=True)
        except Exception:
            pass


def install_kd_java(version: str = TEMURIN_FEATURE_VERSION, dry_run: bool = False) -> Dict[str, Any]:
    """
    Download and install Eclipse Temurin JDK 21 (required by NiFi 2.x), then set JAVA_HOME.

    Windows: official Adoptium MSI (silent msiexec).
    Linux:  official Adoptium tar.gz extracted under /opt/java (or ~/java if not root).
    """
    if dry_run:
        plat = "Linux x64 tar.gz" if not sys.platform.startswith("win") else "Windows MSI"
        return {
            "Success": True,
            "Detail": (
                f"[DryRun] Would download and install Eclipse Temurin JDK {version} "
                f"({plat}), then set JAVA_HOME"
            ),
            "JavaHome": None,
        }

    if not sys.platform.startswith("win"):
        return _install_kd_java_linux(version)

    # ---- Windows path (MSI) ----
    if not _is_elevated():
        return {
            "Success": False,
            "Detail": "Not running as Administrator. Silent MSI install (writes to Program Files and "
                      "HKLM) requires elevation. Re-run this installer from an elevated ('Run as "
                      "Administrator') PowerShell/CMD prompt.",
            "JavaHome": None,
        }

    if _pending_reboot():
        log.warn("  Windows has a reboot pending (PendingFileRenameOperations/CBS RebootPending). "
                 "This is a common cause of MSI error 1603. Reboot and re-run if the install below fails.")

    existing = _find_registered_temurin_product(int(version))
    if existing:
        install_loc = Path(existing["InstallLocation"]) if existing["InstallLocation"] else None
        if install_loc and (install_loc / "bin" / "java.exe").is_file():
            home_path = ensure_java_home_and_path(install_loc)
            detail = (
                f"Found existing working install: {existing['DisplayName']} -> {install_loc}; "
                f"{home_path['Detail']}"
            )
            log.info(f"  {detail}")
            return {"Success": True, "Detail": detail, "JavaHome": install_loc}

        log.warn(f"  Found a broken/incomplete existing registration for '{existing['DisplayName']}' "
                 f"(ProductCode {existing['ProductCode']}) - removing it before reinstalling "
                 f"to avoid an implicit repair failure...")
        subprocess.run(
            ["msiexec", "/x", existing["ProductCode"], "/qn", "/norestart"],
            capture_output=True,
            timeout=300,
        )
        time.sleep(2)

    tmp_msi = Path(tempfile.gettempdir()) / f"temurin-jdk{version}-installer.msi"
    msi_log = Path(tempfile.gettempdir()) / f"temurin-jdk{version}-install.log"
    try:
        ensure_kd_tls_opener()
        url = _temurin_download_url(version)

        for attempt in (1, 2):
            log.info(f"Downloading Eclipse Temurin JDK {version} from Adoptium "
                     f"({url})... [attempt {attempt}/2]")
            urllib.request.urlretrieve(url, tmp_msi)
            size = tmp_msi.stat().st_size
            if size >= 100_000_000:
                break
            log.warn(f"  Download looks truncated ({size / 1_000_000:.1f} MB); retrying...")
            tmp_msi.unlink(missing_ok=True)
        else:
            size = tmp_msi.stat().st_size if tmp_msi.exists() else 0

        if not tmp_msi.exists() or tmp_msi.stat().st_size < 1_000_000:
            return {
                "Success": False,
                "Detail": f"Download produced a suspiciously small file "
                          f"({tmp_msi.stat().st_size if tmp_msi.exists() else 0} bytes) after 2 attempts. "
                          f"Check network access to api.adoptium.net.",
                "JavaHome": None,
            }
        size_mb = tmp_msi.stat().st_size // (1024 * 1024)
        log.info(f"  Downloaded {size_mb} MB. Installing silently (this can take a minute)...")

        proc = subprocess.run(
            [
                "msiexec", "/i", str(tmp_msi), "/qn", "/norestart",
                "/l*v", str(msi_log),
                "ADDLOCAL=FeatureMain,FeatureEnvironment,FeatureJavaHome",
            ],
            capture_output=True,
            timeout=600,
        )
        if proc.returncode not in (0, 3010):
            log_tail = _tail_msi_log(msi_log) if msi_log.exists() else "(no log produced)"
            hint = ""
            if proc.returncode == 1603:
                hint = (
                    " Error 1603 is a generic MSI failure - most often: another install/Windows Update "
                    "is in progress, a reboot is pending, an existing Java Runtime install is in a broken "
                    "state, or antivirus is blocking the installer. "
                )
            return {
                "Success": False,
                "Detail": f"msiexec exited with code {proc.returncode}.{hint}"
                          f"Full log: {msi_log}\nLast lines:\n{log_tail}",
                "JavaHome": None,
            }

        java_home = find_temurin_jdk_install_dir(int(version))
        if not java_home:
            return {
                "Success": False,
                "Detail": "msiexec reported success but the JDK install directory was not found "
                          "under 'Program Files\\Eclipse Adoptium'",
                "JavaHome": None,
            }

        home_path = ensure_java_home_and_path(java_home)
        if not home_path["Success"]:
            log.warn(f"  {home_path['Detail']}; the MSI's own FeatureJavaHome/FeatureEnvironment may still have set them")

        detail = f"Installed Eclipse Temurin JDK {version} -> {java_home}; {home_path['Detail']}"
        if proc.returncode == 3010:
            detail += " (a reboot is recommended for the change to apply to already-open shells)"
        log.info(f"  {detail}")
        return {"Success": True, "Detail": detail, "JavaHome": java_home}
    except Exception as e:
        return {"Success": False, "Detail": f"Exception during Java install: {e}", "JavaHome": None}
    finally:
        try:
            tmp_msi.unlink(missing_ok=True)
        except Exception:
            pass


def confirm_kd_java_for_nifi(
    dry_run: bool = False,
    non_interactive: bool = False,
    auto_install: bool = True,
) -> Dict[str, Any]:
    """
    Run the Java 21 check when NiFi is requested. If Java 21+ isn't found,
    auto-install Eclipse Temurin JDK 21 and set JAVA_HOME:
      - Interactive: prompts first (default Yes).
      - Non-interactive or auto_install=True: installs without prompting.
      - Set NiFi.AutoInstallJava: false in config to only warn instead.
    """
    check = test_kd_java_version(21)
    if check["Pass"]:
        # Java is present — still ensure JAVA_HOME + %JAVA_HOME%\bin on PATH
        jh = check.get("JavaHome") or os.environ.get("JAVA_HOME") or get_permanent_env_var("JAVA_HOME")
        path_detail = ""
        if jh and not dry_run:
            home_path = ensure_java_home_and_path(jh)
            path_detail = f"; {home_path['Detail']}"
            log.info(f"  JAVA_HOME/PATH: {home_path['Detail']}")
        elif jh and dry_run:
            path_detail = f"; [DryRun] would ensure JAVA_HOME={jh} and %JAVA_HOME%\\bin on PATH"
        return {"Name": check["Name"], "Pass": True, "Detail": check["Detail"] + path_detail}

    log.warn(check["Detail"])

    if dry_run:
        return {
            "Name": check["Name"],
            "Pass": True,
            "Detail": f"[DryRun] {check['Detail']} - would auto-install Eclipse Temurin JDK 21, "
                      f"set JAVA_HOME, and add %JAVA_HOME%\\bin to PATH",
        }

    if not auto_install:
        return {
            "Name": check["Name"],
            "Pass": False,
            "Detail": f"{check['Detail']} (NiFi.AutoInstallJava is false; not installing automatically)",
        }

    do_install = True
    if not non_interactive:
        try:
            answer = input(
                f"{_ANSI_YELLOW}Java 21+ is required for Apache NiFi 2.x but was not detected. "
                f"Download and install Eclipse Temurin JDK 21 now? (Y/N) [Y]: {_ANSI_RESET}"
            ).strip()
            do_install = answer.upper() != "N"
        except EOFError:
            do_install = True  # non-interactive stdin: default to installing

    if not do_install:
        return {
            "Name": check["Name"],
            "Pass": False,
            "Detail": f"User declined automatic install. {check['Detail']}",
        }

    install_result = install_kd_java(dry_run=False)
    if not install_result["Success"]:
        return {
            "Name": check["Name"],
            "Pass": False,
            "Detail": f"Automatic Java install failed - {install_result['Detail']}. "
                      f"Install manually from https://adoptium.net/ and set JAVA_HOME.",
        }

    recheck = test_kd_java_version(21)
    return {
        "Name": check["Name"],
        "Pass": recheck["Pass"],
        "Detail": f"{install_result['Detail']} | {recheck['Detail']}",
    }


# ---------------------------------------------------------------------------
# Apache NiFi official binary download
# ---------------------------------------------------------------------------

NIFI_DEFAULT_VERSION = "2.10.0"
# Prefer CDN / downloads.apache.org; archive is last-resort (often much slower).
NIFI_DOWNLOAD_MIRRORS = [
    "https://dlcdn.apache.org/nifi/{version}/nifi-{version}-bin.zip",
    "https://downloads.apache.org/nifi/{version}/nifi-{version}-bin.zip",
    "https://archive.apache.org/dist/nifi/{version}/nifi-{version}-bin.zip",
]
NIFI_MIN_ZIP_BYTES = 10_000_000  # ~10 MB floor; real package is ~700–900 MB


def _nifi_probe_url(url: str, timeout: float = 8.0) -> Optional[float]:
    """
    Lightweight reachability + latency probe.
    Uses a 1-byte Range GET (works when HEAD is blocked). Returns seconds or None.
    """
    ensure_kd_tls_opener()
    t0 = time.perf_counter()
    try:
        req = urllib.request.Request(
            url,
            headers={"Range": "bytes=0-0", "User-Agent": "KD-Windows-Installer/1.0"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            code = getattr(resp, "status", None) or resp.getcode()
            if code not in (200, 206):
                return None
            resp.read(1)
        return time.perf_counter() - t0
    except Exception:
        return None


def resolve_nifi_download_url(version: str) -> str:
    """
    Pick the fastest reachable mirror for the given NiFi version.
    Probes CDN/downloads first; archive is only used if others fail.
    """
    ensure_kd_tls_opener()
    urls = [t.format(version=version) for t in NIFI_DOWNLOAD_MIRRORS]
    scored: List[tuple] = []
    log.info("Probing Apache NiFi download mirrors for best speed…")
    for url in urls:
        host = url.split("/")[2]
        latency = _nifi_probe_url(url)
        if latency is None:
            log.info(f"  mirror {host}: unreachable / timed out")
            continue
        log.info(f"  mirror {host}: ok ({latency * 1000:.0f} ms)")
        scored.append((latency, url))
    if scored:
        scored.sort(key=lambda x: x[0])
        best = scored[0][1]
        log.info(f"  selected: {best.split('/')[2]}")
        return best
    # Fall back to primary even if probes failed (some networks block Range)
    log.warn("All mirror probes failed — falling back to dlcdn.apache.org")
    return urls[0]


def _find_aria2c_exe() -> Optional[Path]:
    """Find aria2c, if installed. aria2 can use multiple connections for one file."""
    for name in ("aria2c.exe", "aria2c"):
        which = shutil.which(name)
        if which:
            return Path(which)
    common = [
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "aria2" / "aria2c.exe",
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "aria2" / "aria2c.exe",
    ]
    for cand in common:
        if cand.is_file():
            return cand
    return None


def _download_with_aria2c(url: str, dest: Path, resume_from: int = 0) -> bool:
    """High-speed segmented download when aria2c is available."""
    aria2 = _find_aria2c_exe()
    if not aria2:
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(aria2),
        "--allow-overwrite=true",
        "--auto-file-renaming=false",
        "--continue=true",
        "--file-allocation=none",
        "--max-connection-per-server=8",
        "--split=8",
        "--min-split-size=4M",
        "--max-tries=5",
        "--retry-wait=2",
        "--connect-timeout=15",
        "--timeout=60",
        "--summary-interval=2",
        "--user-agent=KD-Windows-Installer/1.0",
        "--dir", str(dest.parent),
        "--out", dest.name,
        url,
    ]
    if resume_from:
        log.info(f"  Resuming high-speed download from {resume_from // (1024 * 1024)} MB…")
    else:
        log.info("  Using aria2c with 8 parallel connections for faster transfer…")
    try:
        proc = subprocess.run(cmd, check=False)
        return proc.returncode == 0 and dest.is_file() and dest.stat().st_size >= NIFI_MIN_ZIP_BYTES
    except Exception as e:
        log.warn(f"  aria2c download failed: {e}")
        return False


def _find_curl_exe() -> Optional[Path]:
    """Prefer system curl.exe (Windows 10+ ships one; much faster than urllib)."""
    for cand in (
        Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "curl.exe",
        Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / "curl.exe",
    ):
        if cand.is_file():
            return cand
    which = shutil.which("curl") or shutil.which("curl.exe")
    return Path(which) if which else None


def _download_with_curl(url: str, dest: Path, resume_from: int = 0) -> bool:
    """
    High-speed download via curl.exe with progress meter and optional resume.
    Returns True on success.
    """
    curl = _find_curl_exe()
    if not curl:
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    # -L follow redirects, -f fail on HTTP errors, -C - resume, --retry for flaky links
    cmd = [
        str(curl),
        "-L",
        "-f",
        "--retry", "3",
        "--retry-delay", "2",
        "--connect-timeout", "20",
        "--max-time", "0",  # no overall cap (large package)
        "-A", "KD-Windows-Installer/1.0",
        "-o", str(dest),
        url,
    ]
    if resume_from > 0 and dest.is_file():
        cmd[1:1] = ["-C", "-"]  # continue at end of file
        log.info(f"  Resuming with curl from {resume_from // (1024 * 1024)} MB…")
    else:
        log.info("  Using curl.exe for faster transfer (progress below)…")
    try:
        # Let curl render its own progress bar on stderr/stdout
        proc = subprocess.run(cmd, check=False)
        return proc.returncode == 0 and dest.is_file() and dest.stat().st_size >= NIFI_MIN_ZIP_BYTES
    except Exception as e:
        log.warn(f"  curl download failed: {e}")
        return False


def _download_with_urllib(url: str, dest: Path, resume_from: int = 0) -> None:
    """
    Streaming urllib download with MB progress and optional Range resume.
    Raises on failure.
    """
    ensure_kd_tls_opener()
    headers = {"User-Agent": "KD-Windows-Installer/1.0"}
    mode = "wb"
    expected_total: Optional[int] = None
    if resume_from > 0 and dest.is_file():
        headers["Range"] = f"bytes={resume_from}-"
        mode = "ab"
        log.info(f"  Resuming urllib download from {resume_from // (1024 * 1024)} MB…")

    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as resp:
        code = getattr(resp, "status", None) or resp.getcode()
        if resume_from > 0 and code == 200:
            # Server ignored Range — restart full download
            mode = "wb"
            resume_from = 0
        length_hdr = resp.headers.get("Content-Length")
        if length_hdr and length_hdr.isdigit():
            expected_total = int(length_hdr) + (resume_from if code == 206 else 0)

        chunk = 1024 * 1024  # 1 MiB
        done = resume_from
        last_log = time.time()
        t0 = time.perf_counter()
        with open(dest, mode) as out:
            while True:
                buf = resp.read(chunk)
                if not buf:
                    break
                out.write(buf)
                done += len(buf)
                now = time.time()
                if now - last_log >= 2.0 or (expected_total and done >= expected_total):
                    elapsed = max(time.perf_counter() - t0, 0.001)
                    speed = (done - resume_from) / elapsed / (1024 * 1024)
                    if expected_total:
                        pct = min(100.0, 100.0 * done / expected_total)
                        log.info(
                            f"  … {done // (1024 * 1024)} / {expected_total // (1024 * 1024)} MB "
                            f"({pct:.0f}%) @ {speed:.1f} MB/s"
                        )
                    else:
                        log.info(f"  … {done // (1024 * 1024)} MB @ {speed:.1f} MB/s")
                    last_log = now


def download_nifi_zip(
    zip_path: str | Path,
    version: str = NIFI_DEFAULT_VERSION,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Download the official Apache NiFi binary ZIP into ZipPath.

    Optimizations vs the original urllib-only path:
      - Probe mirrors and pick the lowest-latency host (avoid slow archive by default)
      - Prefer Windows curl.exe when present (typically much faster)
      - Resume partial ``.zip.partial`` files after interruption
      - Progress logging every ~2s for urllib fallback
    """
    target_dir = Path(zip_path)
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except PermissionError as e:
        return {
            "Success": False,
            "Path": None,
            "Detail": (
                f"Permission denied creating ZipPath '{target_dir}'. "
                f"Run as root, or set ZipPath to a writable folder "
                f"(e.g. /opt/zip-folder). Detail: {e}"
            ),
            "Downloaded": False,
        }
    except Exception as e:
        return {
            "Success": False,
            "Path": None,
            "Detail": f"Cannot create ZipPath '{target_dir}': {e}",
            "Downloaded": False,
        }
    # Probe write access early (avoids partial download then Permission denied)
    probe = target_dir / ".kd-write-test"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
    except Exception as e:
        return {
            "Success": False,
            "Path": None,
            "Detail": (
                f"ZipPath '{target_dir}' is not writable by the current user. "
                f"Fix permissions (chown/chmod) or choose another ZipPath. Detail: {e}"
            ),
            "Downloaded": False,
        }
    filename = f"nifi-{version}-bin.zip"
    dest = target_dir / filename

    if dest.is_file() and dest.stat().st_size > NIFI_MIN_ZIP_BYTES:
        return {
            "Success": True,
            "Path": dest,
            "Detail": f"Already present: {dest} ({dest.stat().st_size // (1024 * 1024)} MB)",
            "Downloaded": False,
        }

    url = resolve_nifi_download_url(version)

    if dry_run:
        return {
            "Success": True,
            "Path": dest,
            "Detail": f"[DryRun] Would download {url} → {dest}",
            "Downloaded": False,
        }

    tmp = dest.with_suffix(".zip.partial")
    resume_from = tmp.stat().st_size if tmp.is_file() else 0

    log.info(f"Downloading official Apache NiFi {version}")
    log.info(f"  Source     : {url}")
    log.info(f"  Destination: {dest}  (~700–900 MB — please wait)")
    if resume_from:
        log.info(f"  Partial file found ({resume_from // (1024 * 1024)} MB) — will resume")

    try:
        # Prefer aria2c when available because it can download one large ZIP
        # using multiple HTTP connections. Fall back to curl, then urllib.
        ok = _download_with_aria2c(url, tmp, resume_from=resume_from)
        if not ok:
            ok = _download_with_curl(url, tmp, resume_from=resume_from)
        if not ok:
            # curl/aria2 missing or failed — streaming urllib with progress
            if not _find_curl_exe() and not _find_aria2c_exe():
                log.info("  aria2c/curl not found — using Python streaming download with progress")
            else:
                log.warn("  Fast transfer failed — falling back to Python streaming download")
            # If curl left a tiny/corrupt partial, restart
            if tmp.is_file() and tmp.stat().st_size < NIFI_MIN_ZIP_BYTES // 10:
                tmp.unlink(missing_ok=True)
                resume_from = 0
            else:
                resume_from = tmp.stat().st_size if tmp.is_file() else 0
            _download_with_urllib(url, tmp, resume_from=resume_from)

        if not tmp.is_file() or tmp.stat().st_size < NIFI_MIN_ZIP_BYTES:
            size = tmp.stat().st_size if tmp.is_file() else 0
            tmp.unlink(missing_ok=True)
            return {
                "Success": False,
                "Path": None,
                "Detail": (
                    f"Download produced a suspiciously small file ({size} bytes). "
                    "Check network / mirror, or place nifi-*-bin.zip in ZipPath manually."
                ),
                "Downloaded": False,
            }

        tmp.replace(dest)
        size_mb = dest.stat().st_size // (1024 * 1024)
        log.info(f"  Download complete: {dest} ({size_mb} MB)")
        return {
            "Success": True,
            "Path": dest,
            "Detail": f"Downloaded {filename} ({size_mb} MB) from {url.split('/')[2]}",
            "Downloaded": True,
        }
    except Exception as e:
        return {
            "Success": False,
            "Path": None,
            "Detail": f"Failed to download NiFi {version}: {e}",
            "Downloaded": False,
        }


def confirm_nifi_download(
    zip_path: str | Path,
    version: str = NIFI_DEFAULT_VERSION,
    dry_run: bool = False,
    non_interactive: bool = False,
    auto_download: bool = True,
) -> Dict[str, Any]:
    """
    Ensure a suitable NiFi ZIP exists in ZipPath.

    If the ZIP is missing, download the official Apache binary automatically
    by default.  Callers that explicitly pass ``auto_download=False`` may
    still use the interactive confirmation path.
    """
    # IMPORTANT: this dependency gate is specifically for the official Apache
    # NiFi binary package. Do not treat a product-specific package such as
    # NiFiIngest_26.3.0_WINDOWS_X86_64.zip as satisfying the Apache nifi-*-bin.zip
    # dependency. The user explicitly opted in at the prompt above, so the
    # download must start immediately when the official ZIP is absent.
    target_dir = Path(zip_path)
    target_dir.mkdir(parents=True, exist_ok=True)
    official_candidates = sorted(
        [p for p in target_dir.glob("nifi-*-bin.zip") if p.is_file()],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if official_candidates:
        z = official_candidates[0]
        return {
            "Success": True,
            "Path": z,
            "Detail": f"NiFi ZIP already present: {z.name}",
            "Downloaded": False,
            "SkippedPrompt": True,
        }

    log.warn(f"NiFi ZIP is missing from {target_dir} (expected nifi-*-bin.zip)")

    if dry_run:
        return {
            "Success": True,
            "Path": None,
            "Detail": f"[DryRun] Would offer to download official nifi-{version}-bin.zip from Apache",
            "Downloaded": False,
        }

    do_download = auto_download
    if not non_interactive and not auto_download:
        try:
            answer = input(
                f"{_ANSI_YELLOW}Download the official Apache NiFi {version} binary ZIP from nifi.apache.org now? (Y/N) [Y]: {_ANSI_RESET}"
            ).strip()
            do_download = answer.upper() != "N"
        except EOFError:
            do_download = False

    if not do_download:
        return {
            "Success": False,
            "Path": None,
            "Detail": "User declined (or non-interactive) – place nifi-*-bin.zip in ZipPath manually",
            "Downloaded": False,
        }

    return download_nifi_zip(zip_path, version=version, dry_run=False)


def detect_linux_distro_family() -> str:
    """
    Return one of: 'rhel', 'debian', 'sles', 'unknown'.
    Uses /etc/os-release ID / ID_LIKE.
    """
    if not sys.platform.startswith("linux"):
        return "unknown"
    os_release = Path("/etc/os-release")
    if not os_release.is_file():
        return "unknown"
    data: Dict[str, str] = {}
    try:
        for line in os_release.read_text(encoding="utf-8", errors="replace").splitlines():
            if "=" in line:
                k, _, v = line.partition("=")
                data[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        return "unknown"
    ident = (data.get("ID") or "").lower()
    like = (data.get("ID_LIKE") or "").lower()
    if ident in ("rhel", "centos", "rocky", "almalinux", "ol", "fedora") or "rhel" in like or "fedora" in like:
        return "rhel"
    if ident in ("debian", "ubuntu", "linuxmint", "pop") or "debian" in like or "ubuntu" in like:
        return "debian"
    if ident in ("sles", "suse", "opensuse", "opensuse-leap", "opensuse-tumbleweed") or "suse" in like:
        return "sles"
    return "unknown"


def _glibc_version() -> Optional[str]:
    """Best-effort GLIBC version string (e.g. '2.35') via ldd or libc."""
    try:
        r = subprocess.run(["ldd", "--version"], capture_output=True, text=True, timeout=10)
        # First line typically: "ldd (GNU libc) 2.35"
        for line in (r.stdout or r.stderr or "").splitlines():
            if "libc" in line.lower() or "ldd" in line.lower():
                parts = line.replace(",", " ").split()
                for p in reversed(parts):
                    if p[0].isdigit() and "." in p:
                        return p.strip()
    except Exception:
        pass
    try:
        # Fallback: strings on the actual libc.so.6
        for candidate in (
            "/lib/x86_64-linux-gnu/libc.so.6",
            "/lib64/libc.so.6",
            "/usr/lib/x86_64-linux-gnu/libc.so.6",
        ):
            p = Path(candidate)
            if p.is_file():
                r = subprocess.run(
                    ["strings", str(p)],
                    capture_output=True, text=True, timeout=15,
                )
                for line in (r.stdout or "").splitlines():
                    if line.startswith("GLIBC_") and line.count(".") >= 1:
                        # collect highest later; for now return first meaningful
                        ver = line.split("_", 1)[-1]
                        if ver[0].isdigit():
                            return ver  # rough; better parse max below
                # parse max GLIBC_ symbol
                max_ver = None
                for line in (r.stdout or "").splitlines():
                    if line.startswith("GLIBC_"):
                        ver = line[6:]
                        try:
                            from packaging.version import Version
                            if max_ver is None or Version(ver) > Version(max_ver):
                                max_ver = ver
                        except Exception:
                            if max_ver is None or ver > max_ver:
                                max_ver = ver
                if max_ver:
                    return max_ver
    except Exception:
        pass
    return None


def test_kd_glibc_requirement(min_version: str = "2.34") -> Dict[str, Any]:
    """
    Check that the host provides at least GLIBC_2.34 (official KD minimum).
    On non-Linux this always passes (skipped).
    """
    if not sys.platform.startswith("linux"):
        return {
            "Name": "GLIBC >= 2.34",
            "Pass": True,
            "Version": None,
            "Detail": "Not required on non-Linux platforms",
            "Skipped": True,
        }
    ver = _glibc_version()
    if not ver:
        return {
            "Name": "GLIBC >= 2.34",
            "Pass": False,
            "Version": None,
            "Detail": "Could not determine GLIBC version (ldd / libc.so.6 unavailable)",
        }
    try:
        from packaging.version import Version
        meets = Version(ver) >= Version(min_version)
    except Exception:
        # simple tuple compare
        def _parts(s: str):
            return tuple(int(x) for x in s.split(".") if x.isdigit())
        meets = _parts(ver) >= _parts(min_version)
    return {
        "Name": f"GLIBC >= {min_version}",
        "Pass": meets,
        "Version": ver,
        "Detail": f"Detected GLIBC {ver}" + ("" if meets else f" (required >= {min_version})"),
    }


# Official WKOOP package lists (from OpenText Knowledge Discovery docs)
WKOOP_PACKAGES: Dict[str, List[str]] = {
    "rhel": [
        "libatomic",
        "libX11",
        "libX11-xcb",
        "libXtst",
        "libXScrnSaver",
        "libXcomposite",
        "atk",
        "at-spi2-core",
        "at-spi2-atk",
        "cups",
        "cairo",
        "pango",
        "alsa-lib",          # runtime counterpart of alsa-lib-devel listed in docs
        "alsa-lib-devel",
    ],
    "debian": [
        # Runtime packages required by Chromium/WKOOP on Debian & Ubuntu.
        # On Ubuntu 24.04+ some libraries use the t64 suffix; detection
        # accepts either name (see _is_package_installed).
        "libatomic1",
        "libx11-6",
        "libx11-xcb1",
        "libxcursor1",          # runtime (docs listed -dev; runtime is sufficient)
        "libxdamage1",
        "libxrandr2",
        "libxtst6",
        "libxss1",
        "libxcomposite1",
        "libatk1.0-0",          # or libatk1.0-0t64
        "at-spi2-core",
        "libatk-bridge2.0-0",   # or libatk-bridge2.0-0t64 / libatk-adaptor
        "libcups2",             # cups runtime
        "libcairo2",
        "libpango-1.0-0",
        "libpangocairo-1.0-0",
        "libpciaccess0",        # modern replacement for the older libpci3 name
        "libasound2t64",        # ALSA (optional but listed in many Chromium deps)
        "libgbm1",
        "libnspr4",
        "libnss3",
        "libdrm2",
    ],
    "sles": [
        "libatomic1",
        "libX11-6",
        "libXtst6",
        "libXss1",
        "libXcomposite1",
        "at-spi2-core",
        "cups",
        "libcairo2",
        "libpci3",
    ],
}


def _package_manager_for_family(family: str) -> Optional[List[str]]:
    """Return the install command prefix for the distro family, e.g. ['apt-get', 'install', '-y']."""
    if family == "debian":
        return ["apt-get", "install", "-y"]
    if family == "rhel":
        # Prefer dnf, fall back to yum
        if shutil.which("dnf"):
            return ["dnf", "install", "-y"]
        if shutil.which("yum"):
            return ["yum", "install", "-y"]
        return None
    if family == "sles":
        if shutil.which("zypper"):
            return ["zypper", "--non-interactive", "install"]
        return None
    return None


def _dpkg_is_installed(pkg: str) -> bool:
    """Return True if the named package is in 'install ok installed' state."""
    try:
        r = subprocess.run(
            ["dpkg-query", "-W", "-f=${Status}", pkg],
            capture_output=True, text=True, timeout=15,
        )
        return r.returncode == 0 and "install ok installed" in (r.stdout or "")
    except Exception:
        return False


def _is_package_installed(pkg: str, family: str) -> bool:
    """
    Best-effort check whether a package (or an equivalent that Provides it)
    is already installed.

    On Debian/Ubuntu 24.04+ many libraries were renamed with a ``t64`` suffix
    as part of the time_t 64-bit transition (e.g. libatk1.0-0 → libatk1.0-0t64).
    The t64 package still *Provides* the old name, so we treat either as present.
    """
    try:
        if family == "debian":
            if _dpkg_is_installed(pkg):
                return True
            # time64 transition (Ubuntu 24.04 / Debian 13+)
            if not pkg.endswith("t64") and _dpkg_is_installed(pkg + "t64"):
                return True
            return False
        if family in ("rhel", "sles"):
            r = subprocess.run(["rpm", "-q", pkg], capture_output=True, text=True, timeout=15)
            return r.returncode == 0
    except Exception:
        pass
    return False


def test_kd_wkoop_packages() -> Dict[str, Any]:
    """
    Report which WKOOP packages are missing for the current distro.
    Always Pass=True on non-Linux; on Linux returns Pass=False only when
    packages are missing (so callers can decide whether to auto-install).
    """
    if not sys.platform.startswith("linux"):
        return {
            "Name": "WKOOP OS packages",
            "Pass": True,
            "Missing": [],
            "Family": None,
            "Detail": "Not required on non-Linux platforms",
            "Skipped": True,
        }
    family = detect_linux_distro_family()
    pkgs = WKOOP_PACKAGES.get(family, [])
    if not pkgs:
        return {
            "Name": "WKOOP OS packages",
            "Pass": True,
            "Missing": [],
            "Family": family,
            "Detail": f"No package list defined for distro family '{family}' (install manually if using WKOOP components)",
            "Skipped": True,
        }
    missing = [p for p in pkgs if not _is_package_installed(p, family)]
    return {
        "Name": "WKOOP OS packages",
        "Pass": len(missing) == 0,
        "Missing": missing,
        "Family": family,
        "Detail": (
            "All required packages present"
            if not missing
            else f"Missing {len(missing)} package(s): {', '.join(missing)}"
        ),
    }


def install_kd_wkoop_packages(dry_run: bool = False) -> Dict[str, Any]:
    """
    Install the WKOOP packages required by the official Knowledge Discovery docs.
    Uses apt-get / dnf / yum / zypper according to the detected distro family.
    Requires root (or passwordless sudo).
    """
    if not sys.platform.startswith("linux"):
        return {"Success": True, "Detail": "Skipped (not Linux)", "Installed": []}

    family = detect_linux_distro_family()
    pkgs = WKOOP_PACKAGES.get(family, [])
    if not pkgs:
        return {
            "Success": True,
            "Detail": f"No WKOOP package list for family '{family}'",
            "Installed": [],
            "Family": family,
        }

    missing = [p for p in pkgs if not _is_package_installed(p, family)]
    if not missing:
        return {
            "Success": True,
            "Detail": "All WKOOP packages already installed",
            "Installed": [],
            "Family": family,
        }

    if dry_run:
        return {
            "Success": True,
            "Detail": f"[DryRun] Would install: {', '.join(missing)}",
            "Installed": missing,
            "Family": family,
        }

    pm = _package_manager_for_family(family)
    if not pm:
        return {
            "Success": False,
            "Detail": f"No supported package manager found for family '{family}'",
            "Installed": [],
            "Family": family,
        }

    prefix = [] if os.geteuid() == 0 else ["sudo"]
    # Debian needs an update first for fresh images
    if family == "debian":
        log.info("  Running apt-get update ...")
        subprocess.run(prefix + ["apt-get", "update", "-qq"], capture_output=True, text=True, timeout=180)

    # Resolve installable names (prefer exact, fall back to t64 variant when
    # the classic name is unknown to apt – common on Ubuntu 24.04+).
    to_install: List[str] = []
    unresolved: List[str] = []
    for pkg in missing:
        candidates = [pkg]
        if family == "debian" and not pkg.endswith("t64"):
            candidates.append(pkg + "t64")
        chosen = None
        for cand in candidates:
            # Does the package exist in the apt/yum index at all?
            if family == "debian":
                chk = subprocess.run(
                    prefix + ["apt-cache", "show", cand],
                    capture_output=True, text=True, timeout=20,
                )
                if chk.returncode == 0 and (chk.stdout or "").strip():
                    chosen = cand
                    break
            else:
                # rpm-based: just try the name; package manager will complain if unknown
                chosen = cand
                break
        if chosen:
            to_install.append(chosen)
        else:
            unresolved.append(pkg)

    if not to_install and unresolved:
        return {
            "Success": False,
            "Detail": (
                f"None of the missing packages could be resolved in the package index: "
                f"{', '.join(unresolved)}. They may have been renamed or are optional on this release."
            ),
            "Installed": [],
            "Family": family,
            "Unresolved": unresolved,
        }

    if not to_install:
        return {
            "Success": True,
            "Detail": "Nothing left to install",
            "Installed": [],
            "Family": family,
        }

    cmd = prefix + pm + to_install
    log.info(f"  Installing WKOOP packages ({family}): {' '.join(to_install)}")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()[:500]
            # Partial success is still useful – re-check what is now present
            still_missing = [p for p in missing if not _is_package_installed(p, family)]
            installed_now = [p for p in missing if p not in still_missing]
            detail = f"Package install returned rc={proc.returncode}: {err}"
            if installed_now:
                detail += f" | Now present: {', '.join(installed_now)}"
            if still_missing:
                detail += f" | Still missing: {', '.join(still_missing)}"
            if unresolved:
                detail += f" | Unresolvable names: {', '.join(unresolved)}"
            return {
                "Success": len(still_missing) == 0,
                "Detail": detail,
                "Installed": installed_now,
                "Family": family,
                "Command": " ".join(cmd),
            }
        # Re-check original logical names (t64 counts as installed)
        still_missing = [p for p in missing if not _is_package_installed(p, family)]
        installed_now = [p for p in missing if p not in still_missing]
        if still_missing or unresolved:
            detail = f"Installed {len(installed_now)} package(s)"
            if still_missing:
                detail += f"; still missing: {', '.join(still_missing)}"
            if unresolved:
                detail += f"; unresolvable (optional/renamed): {', '.join(unresolved)}"
            # Unresolvable names (e.g. libpci3 on some Ubuntu releases) are
            # treated as non-fatal – the core WKOOP libs are the important ones.
            success = len(still_missing) == 0
            return {
                "Success": success,
                "Detail": detail,
                "Installed": installed_now,
                "Family": family,
                "Unresolved": unresolved,
            }
        return {
            "Success": True,
            "Detail": f"Installed {len(installed_now)} package(s): {', '.join(to_install)}",
            "Installed": installed_now,
            "Family": family,
        }
    except subprocess.TimeoutExpired:
        return {"Success": False, "Detail": "Package install timed out", "Installed": [], "Family": family}
    except Exception as e:
        return {"Success": False, "Detail": f"Exception during package install: {e}", "Installed": [], "Family": family}


def ensure_linux_dependencies(
    dry_run: bool = False,
    install_packages: bool = True,
) -> Dict[str, Any]:
    """
    High-level entry point used by the installer / environment prep.

    1. Checks GLIBC >= 2.34 (reports only; cannot be auto-fixed).
    2. Optionally installs the WKOOP packages required by components that
       process HTML / web pages (Web Connector, NiFi Ingest, CFS, ...).

    Notes from official documentation that are surfaced in the Detail string:
    - The KD installer ships matching libgcc_s / libstdc++ under
      InstallDir/common and InstallDir/common/runtimes. When starting
      components from the command line (instead of the systemd unit), set
      LD_LIBRARY_PATH to include those directories, or copy the shared
      libraries into the component working directory.
    - Java requirements (already enforced elsewhere):
        Find / Data Admin / Site Admin          → JRE 17 or 21
        NiFi Ingest                             → JRE 21
        Documentum / FileNet / Hadoop Connectors → JRE 11+
        Named Entity Recognition Java SDK       → JDK 8 or 11
        View (non-Windows)                      → JRE 8–17
        MMAP                                    → JRE 8 or 11
    """
    results: Dict[str, Any] = {
        "Success": True,
        "Glibc": None,
        "Wkoop": None,
        "Detail": "",
    }
    if not sys.platform.startswith("linux"):
        results["Detail"] = "Skipped (not Linux)"
        return results

    glibc = test_kd_glibc_requirement()
    results["Glibc"] = glibc
    if not glibc.get("Pass"):
        results["Success"] = False
        log.warn(f"  GLIBC check: {glibc['Detail']}")
    else:
        log.info(f"  GLIBC check: {glibc['Detail']}")

    wkoop = test_kd_wkoop_packages()
    results["Wkoop"] = wkoop
    if wkoop.get("Pass"):
        log.info(f"  WKOOP packages: {wkoop['Detail']}")
    else:
        log.warn(f"  WKOOP packages: {wkoop['Detail']}")
        if install_packages:
            inst = install_kd_wkoop_packages(dry_run=dry_run)
            results["WkoopInstall"] = inst
            if not inst.get("Success"):
                results["Success"] = False
                log.error(f"  WKOOP package install failed: {inst.get('Detail')}")
            else:
                log.info(f"  WKOOP package install: {inst.get('Detail')}")
                # refresh status
                results["Wkoop"] = test_kd_wkoop_packages()
        else:
            results["Success"] = False

    # Build a human-readable summary
    parts = [f"GLIBC: {glibc.get('Detail')}"]
    if results.get("WkoopInstall"):
        parts.append(f"WKOOP install: {results['WkoopInstall'].get('Detail')}")
    else:
        parts.append(f"WKOOP: {wkoop.get('Detail')}")
    results["Detail"] = " | ".join(parts)
    return results


# Software dependency summary (documentation only – Java checks live above)
KD_COMPONENT_JAVA_REQUIREMENTS: Dict[str, str] = {
    "Find": "JRE 17 or 21",
    "Data Admin": "JRE 17 or 21",
    "Documentum Connector": "JRE 11 or later",
    "FileNet P8 Connector": "JRE 11 or later",
    "Hadoop Connector": "JRE 11 or later",
    "Named Entity Recognition Java SDK": "JDK 8 or 11",
    "NiFi Ingest": "JRE 21",
    "Site Admin": "JRE 17 or 21",
    "View": "JRE 8 to 17 (non-Windows; dependency of File Content Extraction HTML Export)",
    "MMAP": "JRE 8 or 11",
}
