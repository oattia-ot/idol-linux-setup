#!/usr/bin/env python3
"""
Knowledge Discovery 26.2 Windows Installer - Python backend
Entry point. Can be called directly or from the PowerShell UI wrapper.

Usage examples:
  python install_kd.py --mode Install --dry-run
  python install_kd.py --mode Install --non-interactive --config config/my-config.json
  python install_kd.py --mode Uninstall --non-interactive
  python install_kd.py --mode Configure
"""

from __future__ import annotations

_ANSI_YELLOW = "\033[33m"
_ANSI_RESET = "\033[0m"

import argparse
import os
import platform
import socket
import subprocess
import sys
from pathlib import Path

# Ensure package is importable when run as script
sys.path.insert(0, str(Path(__file__).resolve().parent))

from kd.config import get_kd_config, test_kd_config_schema
from kd.logging import log
from kd import validation, prerequisites, installer, service_manager


def _detect_hostname() -> str:
    try:
        return socket.gethostname()
    except Exception:
        return platform.node() or "localhost"


def _detect_mac() -> str:
    """Best-effort MAC of first physical adapter (Windows)."""
    try:
        import uuid
        mac = uuid.getnode()
        if (mac >> 40) % 2:
            return "00:00:00:00:00:00"  # locally administered / random
        return ":".join(f"{(mac >> ele) & 0xff:02x}" for ele in range(40, -1, -8))
    except Exception:
        return "00:00:00:00:00:00"


def _url_path_suffix(url: str) -> str:
    """
    Return the path+query part of a URL (everything after scheme://host:port).
    Examples:
      http://localhost:16000/action=admin  -> /action=admin
      https://host:8443/nifi               -> /nifi
      http://localhost:8080/               -> /
    """
    if not url or not isinstance(url, str):
        return ""
    s = url.strip()
    # strip scheme
    for scheme in ("https://", "http://"):
        if s.lower().startswith(scheme):
            s = s[len(scheme):]
            break
    # strip host[:port]
    slash = s.find("/")
    if slash < 0:
        return "/"
    return s[slash:] or "/"


def _rebind_url(existing: str, new_base: str, default_path: str) -> str:
    """
    Keep a custom path endpoint from an existing BrowserUrls entry, but always
    apply the current host:port base. Falls back to default_path when no
    existing URL is present.
    """
    if existing and isinstance(existing, str) and existing.strip():
        path = _url_path_suffix(existing)
        if path:
            return f"{new_base.rstrip('/')}{path}"
    return f"{new_base.rstrip('/')}{default_path}"


def build_browser_urls(config: dict) -> dict:
    """
    Build a structured BrowserUrls dict from Ports / NiFi / Find settings.

    - Host + port always come from current config (Ports / NiFi / Find / ThisHost).
    - Path endpoints (e.g. /action=admin, /action=pid) are taken from an existing
      BrowserUrls entry when present, so hand-edited paths are preserved across
      option-10 runs. Defaults are used only when no prior entry exists.
    """
    host = (config.get("ThisHost") or "localhost").strip() or "localhost"
    ports = config.get("Ports") or {}
    components = {str(c).lower() for c in (config.get("Components") or [])}
    existing = config.get("BrowserUrls") or {}
    if not isinstance(existing, dict):
        existing = {}

    def _port(*keys: str, default: str = "") -> str:
        for k in keys:
            v = ports.get(k)
            if v is not None and str(v).strip():
                return str(v).strip()
        return default

    def _existing(component: str, label: str) -> str:
        block = existing.get(component) or {}
        if isinstance(block, dict):
            return str(block.get(label) or "")
        return ""

    urls: dict = {}

    # LicenseServer – special Status action
    if "licenseserver" in components:
        p = _port("LicenseServer", "licenseserver", default="20000")
        base = f"http://{host}:{p}"
        urls["LicenseServer"] = {
            "Status": _rebind_url(_existing("LicenseServer", "Status"), base, "/a=getlicenseinfo"),
            "Admin UI": _rebind_url(_existing("LicenseServer", "Admin UI"), base, "/action=admin"),
            "Service Log": _rebind_url(_existing("LicenseServer", "Service Log"), base, "/action=grl"),
        }

    # Content – Status uses action=admin by default
    if "content" in components:
        p = _port("Content", "content", default="9100")
        base = f"http://{host}:{p}"
        urls["Content"] = {
            "Status": _rebind_url(_existing("Content", "Status"), base, "/action=admin"),
            "Service Log": _rebind_url(_existing("Content", "Service Log"), base, "/action=grl"),
        }

    # Standard Admin UI + Service Log components
    for label, keys, default_port in (
        ("Community", ("Community", "community"), "9030"),
        ("Category", ("Category", "category"), "9020"),
        ("Agentstore", ("Agentstore", "agentstore"), "9050"),
        ("QMSAgentStore", ("QMSAgentStore", "qmsagentstore"), "9150"),
        ("AnswerBankAgentStore", ("AnswerBankAgentStore", "answerbankagentstore"), "9450"),
        ("ConversationAgentStore", ("ConversationAgentStore", "conversationagentstore"), "9550"),
        ("QMS", ("QMS", "qms"), "16000"),
        ("AnswerServer", ("AnswerServer", "answerserver"), "12000"),
    ):
        if label.lower() in components:
            p = _port(*keys, default=default_port)
            base = f"http://{host}:{p}"
            urls[label] = {
                "Admin UI": _rebind_url(_existing(label, "Admin UI"), base, "/action=admin"),
                "Service Log": _rebind_url(_existing(label, "Service Log"), base, "/action=grl"),
            }

    # View – Status uses getstatus by default
    if "view" in components:
        p = _port("View", "view", default="9080")
        base = f"http://{host}:{p}"
        urls["View"] = {
            "Status": _rebind_url(_existing("View", "Status"), base, "/action=getstatus"),
            "Service Log": _rebind_url(_existing("View", "Service Log"), base, "/action=grl"),
        }

    # StatsServer – Status uses getstatus by default
    if "statsserver" in components:
        p = _port("StatsServer", "statsserver", default="19870")
        base = f"http://{host}:{p}"
        urls["StatsServer"] = {
            "Status": _rebind_url(_existing("StatsServer", "Status"), base, "/action=getstatus"),
            "Service Log": _rebind_url(_existing("StatsServer", "Service Log"), base, "/action=grl"),
        }

    # NiFi (HTTPS)
    if "nifi" in components:
        nifi_cfg = config.get("NiFi") or {}
        nifi_port = (
            nifi_cfg.get("WebHttpsPort")
            or _port("NiFi", "nifi", default="8443")
        )
        base = f"https://{host}:{nifi_port}"
        urls["NiFi"] = {
            "Login": _rebind_url(_existing("NiFi", "Login"), base, "/nifi"),
            "Diagnostics": _rebind_url(
                _existing("NiFi", "Diagnostics"), base, "/nifi-api/system-diagnostics"
            ),
        }

    # Find
    if "find" in components:
        find_cfg = config.get("Find") or {}
        find_port = (
            find_cfg.get("ServerPort")
            or _port("Find", "find", default="8080")
        )
        base = f"http://{host}:{find_port}"
        urls["Find"] = {
            "UI": _rebind_url(_existing("Find", "UI"), base, "/"),
        }

    return urls


def print_browser_urls(config: dict) -> None:
    """Print the reformatted Browser URLs block matching the desired layout."""
    urls = build_browser_urls(config)
    # Preferred display order (matches historical installer summary)
    preferred = [
        "LicenseServer",
        "Content",
        "Community",
        "Category",
        "Agentstore",
        "View",
        "QMSAgentStore",
        "AnswerBankAgentStore",
        "ConversationAgentStore",
        "QMS",
        "AnswerServer",
        "StatsServer",
        "NiFi",
        "Find",
    ]
    log.info("=== BROWSER URLS (open in a web browser) ===")
    seen = set()
    for component in preferred:
        if component not in urls:
            continue
        seen.add(component)
        entries = urls[component]
        if component == "Find":
            ui = entries.get("UI") or next(iter(entries.values()), "")
            log.info(f"Find {ui}")
            continue
        log.info(f"{component}:")
        for label, url in entries.items():
            log.info(f"{label} {url}")
    # Any remaining components not in the preferred list
    for component, entries in urls.items():
        if component in seen:
            continue
        log.info(f"{component}:")
        for label, url in entries.items():
            log.info(f"{label} {url}")


def _prompt(label: str, default: str) -> str:
    try:
        val = input(f"{_ANSI_YELLOW}{label} [{default}]: {_ANSI_RESET}").strip()
        return val if val else default
    except EOFError:
        return default


def _ssl_certs_complete(ssl_root: Path) -> bool:
    """
    Return True if the SSL output folder looks complete for KD use.
    Expects the layout produced by tools/generate_ssl.py:
      ssl/intermediate/certs/ca.cert.pem
      ssl/intermediate/certs/intermediate.cert.pem
      ssl/intermediate/certs/ca-chain.cert.pem
      ssl/intermediate/nifi/keystore.p12   (optional but preferred)
    """
    if not ssl_root.is_dir():
        return False
    certs = ssl_root / "intermediate" / "certs"
    required = [
        certs / "ca.cert.pem",
        certs / "intermediate.cert.pem",
        certs / "ca-chain.cert.pem",
    ]
    if not all(p.is_file() and p.stat().st_size > 0 for p in required):
        return False
    # Prefer NiFi keystores when present; still accept CA-only if nifi dir missing
    nifi_ks = ssl_root / "intermediate" / "nifi" / "keystore.p12"
    if nifi_ks.parent.is_dir() and not (nifi_ks.is_file() and nifi_ks.stat().st_size > 0):
        return False
    return True



def _detect_default_extra_ip() -> str:
    """
    Best-effort default for the Extra IP SAN prompt.
    Order: EXTRA_IP_SANS_ENV → IDOL_NET_HOST_IP → primary outbound IPv4.
    """
    for key in ("EXTRA_IP_SANS_ENV", "IDOL_NET_HOST_IP"):
        val = (os.environ.get(key) or "").strip().split(",")[0].strip()
        if val:
            return val
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
        finally:
            s.close()
        if ip and not ip.startswith("127."):
            return ip
    except Exception:
        pass
    return ""


def _ensure_ssl_certificates(
    *,
    non_interactive: bool,
    force: bool,
    extra_ip: str | None = None,
    web_https_host: str | None = None,
) -> bool:
    """
    Ensure SSL certificates exist for Install / Upgrade / Repair.

    Interactive behaviour:
      - If a complete set is found: ask whether to *regenerate* (default N).
      - If missing/incomplete: ask whether to *generate* (default Y).
      - When generating/regenerating: ask whether to include an *external IP*
        in the certificate SANs (default N). If Y, prompt for the IP
        (default from config ExternalIIPSAN / EXTRA_IP_SANS_ENV / IDOL_NET_HOST_IP /
        outbound IPv4) and pass ``--extra-ip <ip>`` to generate_ssl.py.
    Non-interactive / --force:
      - Generate only when missing or incomplete (never overwrite without a prompt
        unless the user already chose regenerate in interactive mode).
      - Prefer ``extra_ip`` argument (from NiFi.ExternalIIPSAN in my-config.json);
        if empty, default EXTRA_IP_SANS to 127.0.0.1.
        Also honour EXTRA_IP_SANS_ENV when set.
      - ``web_https_host`` (NiFi.WebHttpsHost) is passed as ``--external-hostname``
        when it is a non-wildcard bind address.

    Preferred script locations (first that exists wins):
      1. /opt/kd-setup/idol-linux-setup/tools/generate_ssl.py
      2. <toolkit>/tools/generate_ssl.py  (next to this install_kd.py)
    Returns True if certs are present (or were generated successfully).
    """
    toolkit_root = Path(__file__).resolve().parent
    candidates = [
        Path("/opt/kd-setup/idol-linux-setup/tools/generate_ssl.py"),
        toolkit_root / "tools" / "generate_ssl.py",
    ]
    # Also accept ssl next to BasePath-style installs under C:\KD-Setup
    ssl_roots = [
        toolkit_root / "ssl",
        Path("/opt/kd-setup/idol-linux-setup/ssl"),
        Path("/opt/kd-setup/ssl"),
    ]

    complete_root: Path | None = None
    for root in ssl_roots:
        if _ssl_certs_complete(root):
            complete_root = root
            break

    want_generate = False
    if complete_root is not None:
        log.step_result(
            "SSL certificates",
            True,
            f"Found complete set under {complete_root}",
        )
        # Interactive: offer regenerate even when certs already exist
        if not non_interactive and not force:
            try:
                ans = input(
                    f"{_ANSI_YELLOW}SSL certificates already exist. Regenerate them? (Y/N) [N]: {_ANSI_RESET}"
                ).strip()
            except EOFError:
                ans = "N"
            if ans.upper() == "Y":
                want_generate = True
                log.info("User chose to regenerate SSL certificates.")
            else:
                return True
        else:
            # Non-interactive or --force with complete certs: keep existing
            return True
    else:
        log.warn("SSL certificate folder missing or incomplete — will try to generate.")
        if not non_interactive and not force:
            try:
                ans = input(
                    f"{_ANSI_YELLOW}Generate SSL certificates now with generate_ssl.py --auto --kd-services? (Y/N) [Y]: {_ANSI_RESET}"
                ).strip()
            except EOFError:
                ans = "Y"
            if ans.upper() == "N":
                log.step_result("SSL certificates", False, "Generation declined by user")
                return False
        want_generate = True

    if not want_generate:
        return complete_root is not None

    script = next((p for p in candidates if p.is_file()), None)
    if script is None:
        log.step_result(
            "SSL certificates",
            False,
            "generate_ssl.py not found (checked C:\\KD-Setup\\... and toolkit tools\\)",
        )
        return False

    # Resolve EXTRA_IP_SANS:
    #   1. explicit argument (from NiFi.ExternalIIPSAN / config)
    #   2. interactive prompt (default from env / outbound IP)
    #   3. EXTRA_IP_SANS_ENV
    #   4. if still empty → 127.0.0.1  (matches nifi.properties rule)
    resolved_extra = (extra_ip or "").strip() if extra_ip is not None else ""
    if not non_interactive and not force and not resolved_extra:
        try:
            ans = input(
                f"{_ANSI_YELLOW}Include an external IP address in the certificate SANs?\n"
                f"  (required for browser access via public IP, e.g. https://<IP>:8443/nifi) (Y/N) [N]: {_ANSI_RESET}"
            ).strip()
        except EOFError:
            ans = "N"
        if ans.upper() == "Y":
            ip_default = _detect_default_extra_ip() or "127.0.0.1"
            resolved_extra = _prompt(
                "  External IP address (comma-separated if several)",
                ip_default,
            ).strip()
            if not resolved_extra:
                log.info("  No IP entered — using EXTRA_IP_SANS=127.0.0.1")
                resolved_extra = "127.0.0.1"
            else:
                log.info(f"  Extra IP SAN(s): {resolved_extra}")
        else:
            log.info("  Generating SSL certificates with EXTRA_IP_SANS=127.0.0.1")
            resolved_extra = "127.0.0.1"
    else:
        if not resolved_extra:
            resolved_extra = (os.environ.get("EXTRA_IP_SANS_ENV") or "").strip()
        if not resolved_extra:
            resolved_extra = "127.0.0.1"
            log.info("  EXTRA_IP_SANS empty — defaulting to 127.0.0.1")
        else:
            log.info(f"  Using EXTRA_IP_SANS={resolved_extra}")
    extra_ip = resolved_extra

    print()
    log.info(f"=== Generating SSL certificates via {script} ===")
    cmd = [
        sys.executable,
        str(script),
        "--auto",
        "--kd-services",
        "--force",
        "--no-trust-store",  # trust store needs elevation; optional
    ]
    if extra_ip:
        cmd.extend(["--extra-ip", extra_ip])
        log.info(f"  Using --extra-ip={extra_ip}")
    # Pass WebHttpsHost as external-hostname when it is a concrete host/IP
    # (skip 0.0.0.0 / empty / wildcard bind addresses).
    host = (web_https_host or "").strip()
    if host and host not in ("0.0.0.0", "*", "::", "[::]"):
        cmd.extend(["--external-hostname", host])
        log.info(f"  Using --external-hostname={host}")
    # Prefer writing next to the toolkit so relative paths stay consistent
    out_dir = toolkit_root / "ssl"
    cmd.extend(["--output-dir", str(out_dir)])

    def _run_generate_ssl() -> subprocess.CompletedProcess:
        # Stream stdout/stderr straight through to the console (not
        # captured) so generate_ssl.py's own colored output and console
        # encoding are preserved exactly as if it were run directly —
        # capturing would make it think it's not writing to a real
        # terminal (disabling its colors) and risks mis-decoding non-ASCII
        # output under Python's default locale encoding on Windows.
        return subprocess.run(
            cmd,
            cwd=str(toolkit_root),
            timeout=180,
            capture_output=False,
        )

    def _cryptography_missing() -> bool:
        # Cheap, silent probe run against this same interpreter, instead of
        # parsing generate_ssl.py's captured output for the error text.
        try:
            probe = subprocess.run(
                [sys.executable, "-c", "import cryptography"],
                cwd=str(toolkit_root),
                capture_output=True,
                timeout=30,
            )
            return probe.returncode != 0
        except Exception:
            return False

    try:
        log.info(f"Running: {' '.join(cmd)}")
        proc = _run_generate_ssl()

        if proc.returncode != 0 and _cryptography_missing():
            # requirements.txt lists cryptography, but it can end up missing
            # from *this* interpreter (e.g. requirements.txt was installed
            # under a different Python, or never installed at all for this
            # one). Retry once by installing it directly via the SetupPath
            # toolkit's requirements.txt.
            req_file = toolkit_root / "requirements.txt"
            log.warn(
                "  'cryptography' package missing - installing it now via "
                f"{sys.executable} -m pip install -r {req_file} ..."
            )
            pip_cmd = [sys.executable, "-m", "pip", "install", "-r", str(req_file)]
            try:
                pip_proc = subprocess.run(
                    pip_cmd,
                    cwd=str(toolkit_root),
                    timeout=300,
                    capture_output=False,
                )
                if pip_proc.returncode != 0:
                    log.step_result(
                        "SSL certificates",
                        False,
                        f"cryptography install failed (pip exit code {pip_proc.returncode}); "
                        f"install manually: {sys.executable} -m pip install -r {req_file}",
                    )
                    return False
            except Exception as e:
                log.step_result(
                    "SSL certificates",
                    False,
                    f"Failed to run pip install -r {req_file}: {e}",
                )
                return False

            log.info("  cryptography installed - retrying SSL certificate generation...")
            proc = _run_generate_ssl()

        if proc.returncode != 0:
            log.step_result(
                "SSL certificates",
                False,
                f"generate_ssl.py exited with code {proc.returncode}",
            )
            return False
    except Exception as e:
        log.step_result("SSL certificates", False, f"Failed to run generate_ssl.py: {e}")
        return False

    if _ssl_certs_complete(out_dir):
        log.step_result("SSL certificates", True, f"Generated successfully under {out_dir}")
        return True

    log.step_result(
        "SSL certificates",
        False,
        f"generate_ssl.py finished but expected files still missing under {out_dir}",
    )
    return False


def build_parser() -> argparse.ArgumentParser:
    epilog = r"""
examples:
  python install_kd.py --help
  python install_kd.py --mode Install --dry-run
  python install_kd.py --mode Install --force --non-interactive
  python install_kd.py --mode Install --config config/my-config.json --non-interactive
  python install_kd.py --mode Uninstall --non-interactive
  python install_kd.py --mode Configure --non-interactive
  python install_kd.py --mode Repair --force

notes:
  Packages are extracted automatically via unzip-one.sh (unzip/tar).
  unzip-one.sh / unzip-all.sh are still available for manual extraction.
  Prefer the PowerShell wrapper for interactive use:
    ./install-kd.sh --help
    ./install-kd.sh --mode Install --force

environment overrides:
  KD_BASEPATH  KD_ZIPPATH  KD_LICENSEHOST  KD_LICENSEKEYPATH  KD_THISHOST
"""
    p = argparse.ArgumentParser(
        prog="install_kd.py",
        description="Knowledge Discovery 26.2 Windows Installer (Python backend)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=epilog,
    )
    p.add_argument(
        "--mode",
        choices=[
            "Install",
            "Uninstall",
            "Repair",
            "Upgrade",
            "Configure",
            "InstallJavaServices",
            "InstallNiFiService",
            "SetNiFiCredentials",
            "ShowBrowserUrls",
        ],
        default="Install",
        help=(
            "Operation mode (default: Install). "
            "InstallJavaServices stops/removes any existing KD-NiFi and KD-Find services, "
            "force re-extracts NiFi and Find under BasePath (overwriting the target folders), "
            "ensures Java 21, restores staged NiFi connector .nar files into "
            "BasePath\\NiFi\\extensions, then registers and starts KD-NiFi and KD-Find. "
            "InstallNiFiService is the NiFi-only equivalent (kept for backward compatibility). "
            "SetNiFiCredentials applies nifi.cmd set-single-user-credentials "
            "(NiFi must already be extracted). "
            "ShowBrowserUrls prints component Admin/Status/Log URLs from the config ports."
        ),
    )
    p.add_argument(
        "--config",
        dest="config_path",
        metavar="PATH",
        help="Path to JSON config file (default: config/my-config.json if present, else config/default-config.json)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would happen without making changes",
    )
    p.add_argument(
        "--non-interactive",
        action="store_true",
        help="Skip all prompts; use config / env values only",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Continue past non-fatal environment validation failures",
    )
    p.add_argument(
        "--resume",
        action="store_true",
        help="Skip stages already completed in kd-install-state.json",
    )
    p.add_argument(
        "--extract-only",
        action="store_true",
        help="Only verify extracted folders; do not configure or install services",
    )
    p.add_argument(
        "--nifi-username",
        default=None,
        help="Override NiFi.Username for SetNiFiCredentials mode",
    )
    p.add_argument(
        "--nifi-password",
        default=None,
        help="Override NiFi.Password for SetNiFiCredentials mode",
    )
    p.add_argument("--basepath", metavar="PATH", help="Override BasePath")
    p.add_argument("--zippath", metavar="PATH", help="Override ZipPath")
    p.add_argument("--licensehost", metavar="HOST", help="Override LicenseHost")
    p.add_argument("--licensekeypath", metavar="PATH", help="Override LicenseKeyPath")
    p.add_argument("--thishost", metavar="HOST", help="Override ThisHost")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # Banner
    print("=" * 60)
    print("  Knowledge Discovery 26.2 Windows Installer (Python)")
    print(f"  Mode: {args.mode}  |  DryRun: {args.dry_run}  |  NonInteractive: {args.non_interactive}")
    print("=" * 60)
    print()

    # Load config
    overrides = {}
    if args.basepath:
        overrides["BasePath"] = args.basepath
    if args.zippath:
        overrides["ZipPath"] = args.zippath
    if args.licensehost:
        overrides["LicenseHost"] = args.licensehost
    if args.licensekeypath:
        overrides["LicenseKeyPath"] = args.licensekeypath
    if args.thishost:
        overrides["ThisHost"] = args.thishost

    try:
        config = get_kd_config(args.config_path, overrides or None)
    except Exception as e:
        print(f"FATAL: {e}", file=sys.stderr)
        return 1

    # ShowBrowserUrls: lightweight mode – just print URLs from current config and exit
    if args.mode == "ShowBrowserUrls":
        # Minimal log init so rich / file logging works
        log_dir = Path(config.get("BasePath") or ".") / "logs"
        try:
            log.initialize(log_dir)
        except Exception:
            log.initialize(Path(os.environ.get("TEMP", "/tmp")))
        # Ensure ThisHost is set for URL construction
        if not config.get("ThisHost"):
            config["ThisHost"] = _detect_hostname() or "localhost"

        from kd.config import resolve_default_config_path
        cfg_path = Path(args.config_path) if args.config_path else resolve_default_config_path()
        ports = config.get("Ports") or {}
        log.info(f"Config file : {cfg_path}")
        log.info(f"ThisHost    : {config.get('ThisHost')}")
        log.info("Ports used for host:port (edit Ports.* to change the port in URLs):")
        for name in sorted(ports.keys(), key=str.lower):
            log.info(f"  {name}: {ports[name]}")
        log.info(
            "Path endpoints (/action=..., /a=..., etc.) are kept from existing "
            "BrowserUrls when present; only host:port is refreshed from Ports."
        )

        # host:port  <- Ports / NiFi / Find / ThisHost
        # path       <- existing BrowserUrls entry when present, else built-in default
        browser_urls = build_browser_urls(config)

        # Surgical update: only write BrowserUrls key, leave the rest of the JSON intact.
        try:
            from kd.config import update_config_key
            if update_config_key(cfg_path, "BrowserUrls", browser_urls):
                log.info(f"Updated BrowserUrls only in {cfg_path}")
            else:
                log.warn(f"Could not update BrowserUrls in {cfg_path}")
        except Exception as e:
            log.warn(f"Could not update BrowserUrls: {e}")
        print()
        print_browser_urls(config)
        log.info("KD Installer finished.")
        return 0

    # Network identity
    # Hostname / MAC prompts are Install-only (menu option 1). All other modes
    # (Configure / Upgrade / Repair / Uninstall / InstallNiFiService /
    # SetNiFiCredentials / ShowBrowserUrls) use config or auto-detected values silently.
    detected_host = _detect_hostname()
    detected_mac = _detect_mac()
    # Menu option 1 only — Extract-only (option 6) is Mode=Install but must not prompt either
    install_like = args.mode == "Install" and not getattr(args, "extract_only", False)

    if install_like and not args.non_interactive:
        print("=== Step 1: Network Identity ===")
        print(f"Detected hostname:    {detected_host}")
        print(f"Detected MAC address: {detected_mac}")
        config["ThisHost"] = _prompt("Hostname to use for this install", config.get("ThisHost") or detected_host)
        config["MacAddress"] = _prompt("MAC address to use for this install", detected_mac)
    else:
        config.setdefault("ThisHost", config.get("ThisHost") or detected_host)
        config.setdefault("MacAddress", config.get("MacAddress") or detected_mac)
        if install_like and args.non_interactive:
            print("=== Step 1: Network Identity ===")
            print(f"Detected hostname:    {detected_host}")
            print(f"Detected MAC address: {detected_mac}")
            print("[NonInteractive] Using detected / configured hostname + MAC")

    # Schema check
    schema = test_kd_config_schema(config)
    if not schema["IsValid"]:
        print(f"FATAL: Configuration is missing required keys: {', '.join(schema['MissingKeys'])}", file=sys.stderr)
        return 1

    # Detect KD package version from ZIP names (informational). Normalize BasePath so
    # it never contains a version leaf (…/26.3); IndexPath stays <BasePath>/Indexes.
    from kd import discovery as kd_discovery

    zip_version_analysis = kd_discovery.analyze_kd_zip_versions(config.get("ZipPath") or "")
    base_path_update = kd_discovery.apply_versioned_base_path(
        config, zip_version_analysis, only_if_default=True
    )
    # Persist updated BasePath/IndexPath when config file is writable
    if base_path_update.get("Changed") and args.config_path:
        try:
            from kd.config import update_config_key

            update_config_key(args.config_path, "BasePath", config["BasePath"])
            if config.get("IndexPath"):
                update_config_key(args.config_path, "IndexPath", config["IndexPath"])
        except Exception:
            pass

    # Logging
    log_dir = Path(config["BasePath"]) / "logs"
    try:
        log_file = log.initialize(log_dir)
    except Exception:
        log_file = log.initialize(Path(os.environ.get("TEMP", "/tmp")))

    log.info(f"Starting KD Installer. Mode={args.mode} DryRun={args.dry_run} Resume={args.resume}")
    if base_path_update.get("Changed"):
        log.info(
            f"BasePath normalized (no version leaf): {base_path_update.get('BasePath')} "
            f"(package version detected: {base_path_update.get('MajorMinor') or 'n/a'})"
        )
    for w in zip_version_analysis.get("Warnings") or []:
        log.warn(w)

    # Summary — all lines via log so text aligns under the INFO column.
    # License Key and Components are listed last; entire line in orange.
    print()
    log.info("=== CONFIGURATION SUMMARY ===")
    log.info(f"  Install Path : {config['BasePath']}")
    if zip_version_analysis.get("MajorMinor"):
        log.info(f"  KD version   : {zip_version_analysis['MajorMinor']} (from ZIP names)")
    log.info(f"  ZIP Folder   : {config['ZipPath']}")
    log.info(f"  License      : {config.get('LicenseHost')}:{config.get('LicensePort')}")
    log.info(f"  This Host    : {config['ThisHost']}")
    log.info(f"  Services     : {config.get('InstallService')} ({config.get('StartMode')})")
    log.info(f"  Log file     : {log_file}")
    license_key = config.get("LicenseKeyPath") or "(not set)"
    components = ", ".join(config.get("Components", [])) or "(none)"
    # Entire line orange; listed last for visibility
    log.info_orange(f"  License Key  : {license_key}")
    log.info_orange(f"  Components   : {components}")
    print()

    if not args.non_interactive:
        proceed = _prompt(f"Proceed with '{args.mode}'?", "N")
        if proceed.upper() != "Y":
            log.warn("Aborted by user.")
            return 0

    # Pre-flight validation
    # Port availability and VC++ are only meaningful for install-like modes.
    # For Uninstall / Configure we still check admin/OS/disk basics, but never
    # fail because a service port is already in use (the services being removed
    # are the ones holding the ports).
    # SetNiFiCredentials is a light path: admin + OS only.
    # InstallNiFiService still uses minimal env checks, then runs the full NiFi
    # dependency path (Java / ZIP download + extract) below.
    print()
    log.info("=== ENVIRONMENT VALIDATION ===")
    install_like = args.mode in ("Install", "Upgrade", "Repair")
    nifi_service_only = args.mode in ("InstallJavaServices", "InstallNiFiService", "SetNiFiCredentials")
    required_ports = []
    if install_like and config.get("InstallService"):
        ports = config.get("Ports") or {}
        required_ports = [int(v) for v in ports.values() if str(v).isdigit()]

    if nifi_service_only:
        # Minimal checks only — NiFi binaries are expected to already exist
        for check in (
            validation.test_kd_admin_rights(),
            validation.test_kd_operating_system(),
            validation.test_kd_powershell_version(),
        ):
            log.step_result(check["Name"], check["Pass"], check["Detail"])
            if not check["Pass"] and not args.force:
                log.error("Environment validation failed. Re-run as Administrator or use --force.")
                return 1
        validation_result = {"AllPassed": True, "Results": [], "Failed": []}
    else:
        validation_result = validation.test_kd_environment(config, required_ports)
        for r in validation_result["Results"]:
            log.step_result(
                r["Name"],
                r["Pass"],
                r["Detail"],
                warning=bool(r.get("Warning")),
            )

    # VC++ check is Windows-only (STATUS_DLL_NOT_FOUND does not apply on Linux)
    vcredist = None
    if (
        install_like
        and config.get("InstallService")
        and sys.platform.startswith("win")
    ):
        vcredist = prerequisites.confirm_kd_visual_cpp_redist(
            architecture="X64",
            dry_run=args.dry_run,
            non_interactive=args.non_interactive,
        )
        log.step_result(vcredist["Name"], vcredist["Pass"], vcredist["Detail"])

    # Linux: official KD requirements – GLIBC_2.34+ and WKOOP packages for
    # components that process HTML / web pages (Web Connector, NiFi Ingest,
    # Connector Framework Server, File Content Extraction, PDF Export, View).
    # The product also ships matching libgcc_s / libstdc++ under InstallDir/common;
    # systemd units already run with the component working directory, but
    # command-line launches may need LD_LIBRARY_PATH set to common/ and
    # common/runtimes/.
    if install_like and sys.platform.startswith("linux"):
        try:
            # Only auto-install packages in non-interactive mode. Interactive
            # runs just report; the user is expected to have run setup.sh
            # (which prompts) or to install packages manually.
            linux_deps = prerequisites.ensure_linux_dependencies(
                dry_run=args.dry_run,
                install_packages=(not args.dry_run) and bool(args.non_interactive),
            )
            log.step_result(
                "Linux OS dependencies (GLIBC + WKOOP)",
                bool(linux_deps.get("Success")),
                linux_deps.get("Detail") or "",
                warning=not linux_deps.get("Success"),
            )
        except Exception as e:
            log.warn(f"Linux dependency check failed: {e}")

    # Optional Apache NiFi download prompt + Java 21 check
    # - Full Install/Upgrade/Repair when NiFi is in Components
    # - InstallNiFiService (menu 07) always — verifies ZIP exists, downloads if missing,
    #   ensures Java 21 the same way option 01 does
    nifi_dl = None
    java_check = None
    components_lower = [c.lower() for c in config.get("Components", [])]
    need_nifi_deps = (
        args.mode in ("InstallJavaServices", "InstallNiFiService")
        or ("nifi" in components_lower and args.mode in ("Install", "Upgrade", "Repair"))
    )
    if need_nifi_deps:
        nifi_cfg = config.get("NiFi") or {}
        nifi_manual_wait = False

        # Single upfront gate: NiFi may need several dependencies auto-installed
        # (Java 21 runtime, systemd unit, the NiFi binary package itself).
        # Ask once here instead of several separate prompts later, and let the
        # user bail out of setup cleanly if they'd rather not.
        deps_confirmed = args.non_interactive or args.dry_run
        if not deps_confirmed:
            print()
            print("Apache NiFi may need the following installed automatically if missing:")
            print("  - Java 21 runtime (Eclipse Temurin JDK)")
            print("  - systemd unit (kd-nifi.service)")
            print("  - The NiFi binary package itself (if not already in ZipPath)")
            try:
                answer = input(
                    f"{_ANSI_YELLOW}Install any missing NiFi dependencies now? (Y/N) [Y]: {_ANSI_RESET}"
                ).strip()
            except EOFError:
                answer = "Y"
            if answer.upper() == "N":
                log.error("NiFi dependency installation declined - exiting setup.")
                print()
                print("To proceed, either:")
                print('  - Re-run and answer "Y" to this prompt')
                print("  - Install Java 21 manually and place the NiFi ZIP in ZipPath, "
                      "then re-run with --non-interactive")
                if args.mode != "InstallNiFiService":
                    print('  - Remove "NiFi" from Components in your config to install without it')
                    print("  - Or run menu 07) Install Java Services (NiFi|Find) later (it will offer the same download)")
                sys.exit(1)
            deps_confirmed = True  # user said Y; don't ask again below
            # The Y response is the explicit opt-in for dependency installation.
            # If the Apache binary is missing, offer a manual-copy path before
            # starting the large download. This avoids downloading hundreds of
            # MB when the operator already has the ZIP available elsewhere.
            version_hint = str(nifi_cfg.get("VersionHint") or prerequisites.NIFI_DEFAULT_VERSION)
            zip_dir = Path(config.get("ZipPath", ""))
            official_nifi_zip = next(
                (p for p in sorted(zip_dir.glob("nifi-*-bin.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
                 if p.is_file()),
                None,
            ) if zip_dir.exists() else None
            if official_nifi_zip is None and not args.non_interactive and not args.dry_run:
                print()
                print(f"Apache NiFi {version_hint} binary ZIP was not found in:")
                print(f"  {zip_dir}")
                print()
                print(f"You can manually copy nifi-{version_hint}-bin.zip (or another nifi-*-bin.zip) into that folder")
                print("to skip the download, or continue and download it automatically now.")
                try:
                    dl_choice = input(f"{_ANSI_YELLOW}Skip download and copy the ZIP manually? (Y/N) [N]: {_ANSI_RESET}").strip()
                except EOFError:
                    dl_choice = "N"
                if dl_choice.upper() == "Y":
                    log.info("NiFi download skipped by user; place nifi-*-bin.zip in ZipPath and re-run the installation.")
                    nifi_manual_wait = True
                else:
                    nifi_manual_wait = False
            else:
                nifi_manual_wait = False
            log.info("NiFi dependency prompt accepted (Y); checking for nifi-*-bin.zip now.")

        # Java 21 is mandatory for NiFi 2.x (official Apache + OpenText guidance).
        # Auto-installs Eclipse Temurin JDK 21 and sets JAVA_HOME if missing,
        # unless NiFi.AutoInstallJava is explicitly set to false. Already
        # confirmed above, so don't prompt a second time.
        java_check = prerequisites.confirm_kd_java_for_nifi(
            dry_run=args.dry_run,
            non_interactive=args.non_interactive or deps_confirmed,
            auto_install=bool(nifi_cfg.get("AutoInstallJava", True)),
        )
        log.step_result(java_check["Name"], java_check["Pass"], java_check["Detail"])

        version = str(nifi_cfg.get("VersionHint") or prerequisites.NIFI_DEFAULT_VERSION)
        # NiFi binary packages are now always downloaded automatically when
        # missing.  This keeps the installer deterministic and avoids leaving
        # the setup waiting for a second download prompt.  The upfront NiFi
        # dependency confirmation above remains the single opt-in gate for
        # automatic dependency installation.
        # If the operator chose manual copy above, do not start a download.
        # Otherwise the earlier Y confirmation is the opt-in and download is automatic.
        auto = not nifi_manual_wait
        nifi_dl = prerequisites.confirm_nifi_download(
            zip_path=config.get("ZipPath", ""),
            version=version,
            dry_run=args.dry_run,
            non_interactive=True,
            auto_download=auto,
        )
        log.step_result(
            "Apache NiFi binary package",
            nifi_dl["Success"],
            nifi_dl["Detail"],
        )
        # If the ZIP is still missing after the prompt, guide the operator
        if not nifi_dl.get("Success"):
            print()
            log.warn("NiFi binary ZIP is not available in ZipPath.")
            if args.mode in ("InstallJavaServices", "InstallNiFiService"):
                print("Cannot register KD-NiFi without the binary package.")
                print("The installer will automatically download the NiFi ZIP when it is missing; check the log above for the download error.")
                if not args.force:
                    return 1
            else:
                print("Next step if you want NiFi later:")
                print("  Run menu  07) Install Java Services (NiFi|Find)")
                print("  (it will verify the ZIP, automatically download it if missing, extract, and register KD-NiFi)")
                print('  Or place nifi-*-bin.zip in ZipPath and re-run Install with NiFi in Components.')
        # Soft failure for full Install unless --force (extraction will fail clearly later)

    # ------------------------------------------------------------------
    # SSL certificate pre-check (Install / Upgrade / Repair only)
    # If the expected SSL folder is missing or incomplete, run the
    # toolkit's generate_ssl.py (preferred path: /opt/kd-setup/idol-linux-setup/...
    # or the copy next to this script).
    # ------------------------------------------------------------------
    if install_like and not args.dry_run:
        nifi_cfg = config.get("NiFi") or {}
        # Prefer ExternalIIPSAN; one-time read of legacy ExternalIpAddress for old configs
        cfg_extra_ip = str(
            nifi_cfg.get("ExternalIIPSAN")
            or nifi_cfg.get("ExternalIpAddress")  # legacy only
            or ""
        ).strip()
        if cfg_extra_ip and not str(nifi_cfg.get("ExternalIIPSAN") or "").strip():
            nifi_cfg["ExternalIIPSAN"] = cfg_extra_ip
            nifi_cfg.pop("ExternalIpAddress", None)
        cfg_host = str(nifi_cfg.get("WebHttpsHost") or "").strip()
        ssl_ok = _ensure_ssl_certificates(
            non_interactive=args.non_interactive,
            force=args.force,
            extra_ip=cfg_extra_ip or None,
            web_https_host=cfg_host or None,
        )
        if not ssl_ok and not args.force:
            print()
            print(
                "FATAL: SSL certificates are missing or incomplete, and generation failed. "
                "Re-run with --force to continue without SSL, or generate manually:",
                file=sys.stderr,
            )
            print('  python tools/generate_ssl.py --auto --kd-services', file=sys.stderr)
            print('  or:  /opt/kd-setup/idol-linux-setup/tools/generate_ssl.py --auto --kd-services', file=sys.stderr)
            return 1

    all_passed = (
        validation_result["AllPassed"]
        and (vcredist is None or vcredist["Pass"])
        and (java_check is None or java_check["Pass"])
    )
    if not all_passed and not args.force:
        print()
        print("FATAL: Environment validation failed. Fix the issues above, or re-run with --force.", file=sys.stderr)
        print("  Shell:   ./install-kd.sh --mode Install --force", file=sys.stderr)
        print("  Python:  python install_kd.py --mode Install --force --non-interactive", file=sys.stderr)
        return 1

    # ------------------------------------------------------------------
    # Pre-extraction ZIP SHA-256 integrity gate
    # Runs for Install / Upgrade / Repair / Extract-Only before any unzip.
    # Interactive: requires yes/y; mismatches listed first.
    # Non-interactive: aborts on hard hash mismatches; continues if all
    # match or if sha256.txt is absent (warning only).
    # Dry-run still computes & logs hashes but does not prompt.
    # ------------------------------------------------------------------
    extract_modes = ("Install", "Upgrade", "Repair")
    if args.mode in extract_modes or getattr(args, "extract_only", False):
        zip_path = config.get("ZipPath") or ""
        sha_result = validation.confirm_zip_hashes(
            zip_path,
            non_interactive=args.non_interactive,
            dry_run=args.dry_run,
        )
        if not sha_result.get("Success"):
            print()
            print(
                "FATAL: ZIP SHA-256 confirmation declined or failed. "
                "Setup aborted before extraction.",
                file=sys.stderr,
            )
            print(
                "  Re-run after verifying the packages, or place a matching "
                "sha256.txt next to the ZIPs. Non-interactive mode still "
                "logs hashes and only hard-fails on mismatches.",
                file=sys.stderr,
            )
            return 1

    # Dispatch
    print()
    log.info(f"=== {args.mode.upper()} ===")
    result = None

    if args.mode in ("Install", "Upgrade"):
        if args.mode == "Upgrade":
            log.info("Upgrade mode: re-running install; existing services/components are left in place unless removed first.")
        result = installer.invoke_kd_install(
            config,
            dry_run=args.dry_run,
            force=args.force,
            resume=args.resume,
            extract_only=args.extract_only,
            non_interactive=args.non_interactive,
        )
    elif args.mode == "Repair":
        result = installer.invoke_kd_repair(
            config,
            dry_run=args.dry_run,
            force=args.force,
            non_interactive=args.non_interactive,
        )
    elif args.mode == "Uninstall":
        result = installer.invoke_kd_uninstall(
            config,
            dry_run=args.dry_run,
            non_interactive=args.non_interactive,
        )
    elif args.mode == "Configure":
        log.info("Configure mode: applying configuration changes to existing installation.")
        result = installer.invoke_kd_configure(config, dry_run=args.dry_run)
    elif args.mode in ("InstallJavaServices", "InstallNiFiService"):
        is_combined = args.mode == "InstallJavaServices"
        components_to_install = ["NiFi", "Find"] if is_combined else ["NiFi"]
        log.info(
            ("InstallJavaServices mode: " if is_combined else "InstallNiFiService mode: ")
            + "stop/remove any existing KD-NiFi"
            + ("/KD-Find" if is_combined else "")
            + " service(s), force re-extract (overwrite) "
            + ("NiFi + Find" if is_combined else "NiFi")
            + " under BasePath, ensure Java 21, then register the systemd service(s)."
        )
        base_path = Path(config["BasePath"])

        # Stop + delete any existing services FIRST, before extraction - a
        # running NiFi/Find JVM locks files under its own folder, which would
        # otherwise make the force-extract overwrite below fail partway through.
        if not args.dry_run:
            for comp in components_to_install:
                svc_name = service_manager.get_kd_service_name(comp)
                cleanup = service_manager.remove_kd_service_if_present(svc_name)
                log.step_result(f"Stop/remove existing {svc_name}", cleanup["Success"], cleanup["Detail"])
        else:
            for comp in components_to_install:
                log.info(f"  [DryRun] Would stop-if-running and remove existing {service_manager.get_kd_service_name(comp)}")

        extraction_failed = []
        for comp in components_to_install:
            comp_path = base_path / comp
            if not comp_path.is_dir() and base_path.is_dir():
                for child in base_path.iterdir():
                    if child.is_dir() and child.name.lower() == comp.lower():
                        comp_path = child
                        break
            if not args.dry_run:
                extracted = installer._ensure_component_extracted(
                    component=comp,
                    component_path=comp_path if comp_path.is_dir() else (base_path / comp),
                    base_path=base_path,
                    zip_path=str(config.get("ZipPath") or ""),
                    dry_run=False,
                    non_interactive=args.non_interactive,
                    force=True,  # menu 07 is an explicit (re)install - always overwrite the target folder
                    nifi_version=str((config.get("NiFi") or {}).get("VersionHint") or prerequisites.NIFI_DEFAULT_VERSION),
                )
                if not extracted:
                    extraction_failed.append(comp)

        if extraction_failed:
            detail = (
                f"{'/'.join(extraction_failed)} folder(s) not found under {base_path} and extraction failed. "
                "Place the matching ZIP(s) in ZipPath (or answer Y to download NiFi), then re-run "
                f"menu 07) Install Java Services{'' if is_combined else ' (NiFi)'}."
            )
            log.error(detail)
            print()
            print("Next step:")
            print(f"  1. Ensure ZipPath contains the ZIP(s) for: {', '.join(extraction_failed)}")
            print("  2. Re-run:  ./install-kd.sh  →  07) Install Java Services (NiFi|Find)")
            print(f"     or:     python install_kd.py --mode {args.mode}")
            return 1

        nifi_result = installer.invoke_kd_install_nifi_service(
            config,
            dry_run=args.dry_run,
            start_after=not args.dry_run,
        )
        if is_combined:
            find_result = installer.invoke_kd_install_find_service(
                config,
                dry_run=args.dry_run,
                start_after=not args.dry_run,
            )
            result = {
                "Completed": nifi_result.get("Completed", []) + find_result.get("Completed", []),
                "Failed": nifi_result.get("Failed", []) + find_result.get("Failed", []),
                "Detail": f"NiFi: {nifi_result.get('Detail', '')} | Find: {find_result.get('Detail', '')}",
            }
        else:
            result = nifi_result
    elif args.mode == "SetNiFiCredentials":
        log.info(
            "SetNiFiCredentials mode: apply nifi.cmd set-single-user-credentials "
            "(NiFi must already be extracted under BasePath\\NiFi)."
        )
        result = installer.invoke_kd_set_nifi_credentials(
            config,
            username=args.nifi_username,
            password=args.nifi_password,
            dry_run=args.dry_run,
            config_path=args.config_path,
        )

    # Health check
    if args.mode in ("Install", "Upgrade", "Repair") and not args.dry_run and not args.extract_only:
        print()
        log.info("=== HEALTH CHECK ===")
        health = installer.invoke_kd_health_check(config)
        for h in health:
            log.step_result(f"Service health: {h['Component']}", h["Success"], h["Detail"])
    elif args.mode in ("InstallJavaServices", "InstallNiFiService") and not args.dry_run:
        print()
        log.info("=== HEALTH CHECK ===")
        health_components = ["NiFi", "Find"] if args.mode == "InstallJavaServices" else ["NiFi"]
        for comp in health_components:
            h = service_manager.test_kd_service_healthy(comp)
            log.step_result(f"Service health: {comp}", h["Success"], h["Detail"])

    # Final report
    print()
    log.info("=== SUMMARY ===")
    print(f"Mode        : {args.mode}")
    print(f"Log file    : {log_file}")
    if result:
        print(f"Completed   : {', '.join(result.get('Completed', [])) or '(none)'}")
        if result.get("Failed"):
            print(f"Failed      : {', '.join(result['Failed'])}")
        if "StateFile" in result:
            print(f"State file  : {result['StateFile']}")

    if args.mode in ("Install", "Upgrade") and not args.extract_only:
        print()
        print("Next steps:")
        print("  1. Start services:  Get-Service KD-* | Start-Service   (or use Services MMC)")
        print("  2. Check status:    Get-Service KD-*")
        # Enrich config with BrowserUrls derived from selected ports.
        # Surgical update only — do not rewrite the whole JSON file.
        browser_urls = build_browser_urls(config)
        config["BrowserUrls"] = browser_urls
        if args.config_path:
            try:
                from kd.config import update_config_key
                update_config_key(args.config_path, "BrowserUrls", browser_urls)
            except Exception:
                pass
        print()
        print_browser_urls(config)

    elif args.mode in ("InstallJavaServices", "InstallNiFiService") and not args.dry_run:
        print()
        print("Next steps:")
        if args.mode == "InstallJavaServices":
            print("  1. Check status:  Get-Service KD-NiFi, KD-Find")
            print("  2. Or:            ./manage-kdservices.sh status --components NiFi,Find")
        else:
            print("  1. Check status:  Get-Service KD-NiFi")
            print("  2. Or:            ./manage-kdservices.sh status --components NiFi")
        print()
        print_browser_urls(config)

    elif args.mode == "SetNiFiCredentials" and not args.dry_run and not (result and result.get("Failed")):
        print()
        print("Next steps:")
        print("  1. Restart NiFi if it is already running so the new login applies:")
        print(r"       ./manage-kdservices.sh restart --components NiFi")
        print("  2. Open the UI and sign in with the credentials you set.")
        print()
        print_browser_urls(config)

    log.info("KD Installer finished.")
    return 0 if not (result and result.get("Failed")) else 1


if __name__ == "__main__":
    sys.exit(main())
