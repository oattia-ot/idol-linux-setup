#!/usr/bin/env python3
"""
KD Configuration Dashboard — local web server (Linux / cross-platform).

Serves the configuration UI, exposes a small JSON API for load/save,
ZIP / NAR management, path & port checks, host reachability (HTTP + WebSocket),
directory browsing, and automatic NiFi connector synchronisation
(extract + copy NARs from NiFiIngest ZIPs).

Default URL:  http://127.0.0.1:5000/kd-config-dashboard.html

Exported configuration is written to:
  <setup-root>\\config\\my-config.json

Usage:
  python kd-config-server.py
  python kd-config-server.py --port 5000
  python kd-config-server.py --setup-root "/opt/kd-setup/idol-linux-setup"
  python kd-config-server.py --config-path "/opt/kd-setup/idol-linux-setup\\config\\my-config.json"
"""

from __future__ import annotations

import argparse
import base64
import datetime
import hashlib
import json
import re
import os
import shutil
import socket
import struct
import sys
import threading
import time
import webbrowser
import zipfile
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from fnmatch import fnmatch
from pathlib import Path
from urllib.parse import urlparse, parse_qs

# WebSocket (RFC 6455) — minimal text-frame support for host-status streaming
_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _current_uid_gid():
    """Return (uid, gid) for the effective end-user (prefer SUDO_USER).
    No-op friendly on Windows (os.getuid is not available)."""
    if sys.platform.startswith("win"):
        return -1, -1
    sudo_user = (os.environ.get("SUDO_USER") or "").strip()
    if sudo_user:
        try:
            import pwd
            pw = pwd.getpwnam(sudo_user)
            return pw.pw_uid, pw.pw_gid
        except Exception:
            pass
    return os.getuid(), os.getgid()


def _chown_path(path, uid=None, gid=None):
    """
    Set ownership of path (file or directory) to the current user:group.
    Best-effort: silently ignores permission errors when not root.
    No-op on Windows.
    """
    if sys.platform.startswith("win"):
        return
    try:
        target = Path(path)
        if not target.exists():
            return
        if uid is None or gid is None:
            uid, gid = _current_uid_gid()
        os.chown(target, uid, gid)
    except Exception:
        pass


def _chown_tree(path):
    """Recursively chown path to the current user:group. No-op on Windows."""
    if sys.platform.startswith("win"):
        return
    try:
        uid, gid = _current_uid_gid()
        root = Path(path)
        if not root.exists():
            return
        for item in [root, *root.rglob("*")]:
            try:
                os.chown(item, uid, gid)
            except Exception:
                pass
    except Exception:
        pass

DEFAULT_PORT = 5000
HOST = "127.0.0.1"
DASHBOARD = "kd-config-dashboard.html"

# Resolved at startup; used by the request handler
CONFIG_PATH: Path | None = None
SETUP_ROOT: Path | None = None
SERVE_DIR: Path | None = None
HTTP_SERVER = None  # set in main()
# LOG_PATH set below in main()
DIALOG_LOCK = threading.Lock()


def _ws_accept_key(sec_key: str) -> str:
    digest = hashlib.sha1((sec_key.strip() + _WS_GUID).encode("utf-8")).digest()
    return base64.b64encode(digest).decode("ascii")


def _ws_send_text(sock: socket.socket, text: str) -> None:
    """Send a single unmasked text WebSocket frame."""
    data = text.encode("utf-8")
    n = len(data)
    if n < 126:
        header = struct.pack("!BB", 0x81, n)
    elif n < 65536:
        header = struct.pack("!BBH", 0x81, 126, n)
    else:
        header = struct.pack("!BBQ", 0x81, 127, n)
    sock.sendall(header + data)


def _ws_recv_frame(sock: socket.socket, max_payload: int = 65536):
    """
    Read one WebSocket frame. Returns (opcode, payload_bytes) or None on close/error.
    Handles masked client frames (required by the browser).
    """
    try:
        hdr = sock.recv(2)
        if len(hdr) < 2:
            return None
        b1, b2 = hdr[0], hdr[1]
        opcode = b1 & 0x0F
        masked = bool(b2 & 0x80)
        length = b2 & 0x7F
        if length == 126:
            ext = sock.recv(2)
            if len(ext) < 2:
                return None
            length = struct.unpack("!H", ext)[0]
        elif length == 127:
            ext = sock.recv(8)
            if len(ext) < 8:
                return None
            length = struct.unpack("!Q", ext)[0]
        if length > max_payload:
            return None
        mask = b""
        if masked:
            mask = sock.recv(4)
            if len(mask) < 4:
                return None
        payload = b""
        while len(payload) < length:
            chunk = sock.recv(length - len(payload))
            if not chunk:
                return None
            payload += chunk
        if masked and mask:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return opcode, payload
    except (OSError, struct.error, ValueError):
        return None


def _is_safe_host(host: str) -> bool:
    """Reject empty / oversized / shell-metachar hosts before DNS or ping."""
    if not host or len(host) > 253:
        return False
    if " " in host or ".." in host:
        return False
    return bool(re.match(r"^[a-zA-Z0-9][a-zA-Z0-9.\-:]{0,252}$", host))


def _resolve_host(host: str) -> tuple[bool, list[str], str | None]:
    """
    DNS resolution via getaddrinfo. Returns (ok, addresses, error).
    For literal IPs this still validates the address family is usable.
    """
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        addrs: list[str] = []
        seen: set[str] = set()
        for info in infos:
            ip = info[4][0]
            if ip not in seen:
                seen.add(ip)
                addrs.append(ip)
        if not addrs:
            return False, [], "No addresses returned"
        return True, addrs, None
    except socket.gaierror as e:
        return False, [], f"DNS failed: {e}"
    except OSError as e:
        return False, [], f"Resolve error: {e}"
    except Exception as e:
        return False, [], str(e)


def _summarize_ping_failure(stdout: str, stderr: str) -> str:
    """Turn raw ping output into a short UI-friendly message."""
    text = f"{stderr or ''}\n{stdout or ''}".lower()
    if "100% packet loss" in text or "0 received" in text:
        return "No reply (100% packet loss)"
    if "name or service not known" in text or "temporary failure" in text:
        return "Unknown host"
    if "network is unreachable" in text:
        return "Network unreachable"
    if "permission denied" in text:
        return "Ping permission denied"
    if "operation not permitted" in text:
        return "Ping not permitted"
    # Prefer last non-empty line if short; otherwise generic
    for line in reversed((stdout or "").splitlines() + (stderr or "").splitlines()):
        line = line.strip()
        if line and not line.lower().startswith("ping ") and "bytes of data" not in line.lower():
            if len(line) <= 80 and "---" not in line:
                return line
    return "Host not reachable"


def _ping_host(host: str, timeout_s: float = 2.0) -> tuple[bool, str | None]:
    """ICMP ping once. Returns (reachable, short error_message). Cross-platform."""
    import subprocess
    try:
        if sys.platform.startswith("win"):
            # Windows: -n count, -w timeout in milliseconds
            cmd = ["ping", "-n", "1", "-w", str(max(1, int(timeout_s * 1000))), host]
        else:
            # Linux/macOS: -c count, -W timeout in seconds
            cmd = ["ping", "-c", "1", "-W", str(max(1, int(timeout_s))), host]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s + 3)
        if result.returncode == 0:
            return True, None
        return False, _summarize_ping_failure(result.stdout or "", result.stderr or "")
    except subprocess.TimeoutExpired:
        return False, "Timeout"
    except FileNotFoundError:
        return False, "ping command not found"
    except Exception as e:
        return False, str(e)


# Ports commonly used by browser UIs / this KD stack — used when ICMP is blocked.
_TCP_PROBE_PORTS = (80, 443, 8080, 8443, 8000, 9000, 5000)


def _tcp_probe_host(
    host: str,
    ports: tuple[int, ...] | list[int] | None = None,
    timeout_s: float = 1.5,
) -> tuple[bool, int | None, str | None]:
    """
    Try a short TCP connect to one or more ports.
    Returns (ok, open_port_or_None, error_or_None).
    Many public/cloud hosts block ICMP but still accept TCP on 80/443/etc.
    """
    ports = tuple(ports) if ports else _TCP_PROBE_PORTS
    last_err: str | None = None
    for port in ports:
        try:
            # Prefer IPv4 then IPv6 via getaddrinfo
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError as e:
            last_err = str(e)
            continue
        for family, socktype, proto, _canon, sockaddr in infos:
            sock = None
            try:
                sock = socket.socket(family, socktype, proto)
                sock.settimeout(timeout_s)
                sock.connect(sockaddr)
                return True, port, None
            except OSError as e:
                last_err = str(e)
            finally:
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass
    return False, None, last_err or "No TCP ports responded"


def _check_host_reachability(host: str) -> dict:
    """
    Full reachability check used by HTTP + WebSocket handlers.
    Order: DNS → ICMP ping → TCP probe (fallback when ICMP is blocked).
    """
    out: dict = {
        "host": host,
        "resolved": False,
        "addresses": [],
        "reachable": False,
        "method": None,  # "ping" | "tcp" | None
        "tcp_port": None,
        "error": None,
        "ping_error": None,
    }
    resolved, addresses, dns_err = _resolve_host(host)
    if not resolved:
        out["error"] = dns_err or "DNS resolution failed"
        return out
    out["resolved"] = True
    out["addresses"] = addresses

    ping_ok, ping_err = _ping_host(host)
    if ping_ok:
        out["reachable"] = True
        out["method"] = "ping"
        return out
    out["ping_error"] = ping_err or "No reply (100% packet loss)"

    tcp_ok, tcp_port, tcp_err = _tcp_probe_host(host)
    if tcp_ok:
        out["reachable"] = True
        out["method"] = "tcp"
        out["tcp_port"] = tcp_port
        return out

    # Both failed
    parts = [out["ping_error"]]
    if tcp_err:
        parts.append(f"TCP: {tcp_err}")
    out["error"] = " · ".join(parts) if parts else "Host not reachable"
    return out


def _as_path(value) -> Path:
    """
    Convert a UI-supplied path string to a pathlib.Path.

    The config dashboard historically joined paths with backslashes (Windows).
    On Linux those backslashes are literal characters, which produces folders
    named like KnowledgeDiscovery\\NiFi\\extensions. Always normalise
    separators before constructing Path objects.
    """
    s = str(value or "").strip()
    if not s:
        return Path("")
    # Turn Windows separators into POSIX so Path does not treat them as
    # literal characters in the directory name on Linux.
    s = s.replace(chr(92), "/")
    while "//" in s and not s.startswith("//"):
        s = s.replace("//", "/")
    return Path(s).expanduser()


LOG_PATH: Path | None = None  # set in main() — dashboard error log file
_LOG_LOCK = threading.Lock()


def _resolve_log_path() -> Path:
    """Prefer <setup-root>/logs/dashboard.log, else next to this script."""
    if SETUP_ROOT is not None:
        return (SETUP_ROOT / "logs" / "dashboard.log").resolve()
    here = Path(__file__).resolve().parent
    # ui-config -> config -> setup root
    setup = here.parent.parent if here.name == "ui-config" else here
    return (setup / "logs" / "dashboard.log").resolve()


def append_dashboard_log(level: str, message: str, **extra) -> None:
    """
    Append one line to the dashboard log file and echo to stderr.
    Safe to call from any request thread.
    """
    global LOG_PATH
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    level = (level or "INFO").upper()
    parts = [f"[{ts}]", f"[{level}]", message]
    if extra:
        try:
            parts.append(json.dumps(extra, ensure_ascii=False, default=str))
        except Exception:
            parts.append(str(extra))
    line = " ".join(parts)
    try:
        print(line, file=sys.stderr, flush=True)
    except Exception:
        pass
    path = LOG_PATH or _resolve_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # When the menu re-execs under sudo, mkdir would leave logs/ root-owned.
        # Prefer SUDO_USER (via _chown_path) so the invoking user keeps ownership.
        _chown_path(path.parent)
        with _LOG_LOCK:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        _chown_path(path)
        LOG_PATH = path
    except Exception as e:
        try:
            print(f"[dashboard-log] failed to write {path}: {e}", file=sys.stderr, flush=True)
        except Exception:
            pass


def _pick_native_path(
    *,
    mode: str = "folder",
    initial_dir: str | None = None,
    title: str = "Select",
    filetypes: list[tuple[str, str]] | None = None,
) -> tuple[str | None, bool, list[str]]:
    """
    Open a native path dialog and return (chosen_path, dialog_completed, errors).

    mode: "folder" | "file"
    Preference order:
      - Linux: zenity → kdialog → tkinter
      - Windows: PowerShell OpenFileDialog → tkinter
      - Other: tkinter
    """
    import shutil
    import subprocess

    initial = initial_dir or str(Path.home())
    try:
        if not Path(initial).is_dir():
            initial = str(Path.home())
    except Exception:
        initial = str(Path.home())

    chosen: str | None = None
    dialog_completed = False
    errors: list[str] = []

    # ----- Linux: zenity -----
    if not sys.platform.startswith("win") and shutil.which("zenity"):
        try:
            cmd = ["zenity", "--file-selection", f"--title={title}"]
            if mode == "folder":
                cmd.append("--directory")
            else:
                # file mode
                if filetypes:
                    # zenity --file-filter format: "Label | *.ext *.ext2"
                    for label, pattern in filetypes:
                        cmd.append(f"--file-filter={label} | {pattern}")
                    cmd.append("--file-filter=All files | *")
            cmd.append(f"--filename={initial.rstrip('/')}/")
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            out = (result.stdout or "").strip()
            if result.returncode == 0 and out:
                chosen = out
                dialog_completed = True
            elif result.returncode in (1, 255):  # cancel
                chosen = None
                dialog_completed = True
            else:
                errors.append(f"zenity exit {result.returncode}: {(result.stderr or '').strip()}")
        except subprocess.TimeoutExpired:
            errors.append("zenity dialog timed out")
        except Exception as e:
            errors.append(f"zenity: {e}")

    # ----- Linux: kdialog -----
    if not chosen and not dialog_completed and not sys.platform.startswith("win") and shutil.which("kdialog"):
        try:
            if mode == "folder":
                cmd = ["kdialog", "--getexistingdirectory", initial, "--title", title]
            else:
                # patterns like "*.nar *.zip"
                filt = ""
                if filetypes:
                    patterns = " ".join(ft[1] for ft in filetypes)
                    filt = f"{filetypes[0][0]} ({patterns})"
                cmd = ["kdialog", "--getopenfilename", initial, filt or "*", "--title", title]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            out = (result.stdout or "").strip()
            if result.returncode == 0 and out:
                chosen = out
                dialog_completed = True
            elif result.returncode == 1:
                chosen = None
                dialog_completed = True
            else:
                errors.append(f"kdialog exit {result.returncode}: {(result.stderr or '').strip()}")
        except subprocess.TimeoutExpired:
            errors.append("kdialog dialog timed out")
        except Exception as e:
            errors.append(f"kdialog: {e}")

    # ----- Windows: PowerShell -----
    if not chosen and not dialog_completed and sys.platform.startswith("win"):
        try:
            ps_dir = initial.replace("'", "''")
            if mode == "folder":
                ps = (
                    "Add-Type -AssemblyName System.Windows.Forms; "
                    "[System.Windows.Forms.Application]::EnableVisualStyles(); "
                    "$d = New-Object System.Windows.Forms.OpenFileDialog; "
                    f"$d.Title = '{title.replace(chr(39), chr(39)+chr(39))}'; "
                    f"$d.InitialDirectory = '{ps_dir}'; "
                    "$d.ValidateNames = $false; "
                    "$d.CheckFileExists = $false; "
                    "$d.CheckPathExists = $true; "
                    "$d.Multiselect = $false; "
                    "$d.FileName = 'Select this folder'; "
                    "if ($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { "
                    "    [Console]::Out.WriteLine([System.IO.Path]::GetDirectoryName($d.FileName)); "
                    "} else { "
                    "    [Console]::Error.WriteLine('__USER_CANCELLED__'); "
                    "}"
                )
            else:
                # file
                filter_str = "All files (*.*)|*.*"
                if filetypes:
                    parts = []
                    for label, pattern in filetypes:
                        # pattern like "*.nar" or "*.nar;*.zip"
                        p = pattern.replace(" ", ";")
                        parts.append(f"{label} ({p})|{p}")
                    parts.append("All files (*.*)|*.*")
                    filter_str = "|".join(parts)
                filter_ps = filter_str.replace("'", "''")
                ps = (
                    "Add-Type -AssemblyName System.Windows.Forms; "
                    "[System.Windows.Forms.Application]::EnableVisualStyles(); "
                    "$d = New-Object System.Windows.Forms.OpenFileDialog; "
                    f"$d.Title = '{title.replace(chr(39), chr(39)+chr(39))}'; "
                    f"$d.InitialDirectory = '{ps_dir}'; "
                    f"$d.Filter = '{filter_ps}'; "
                    "$d.Multiselect = $false; "
                    "if ($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { "
                    "    [Console]::Out.WriteLine($d.FileName); "
                    "} else { "
                    "    [Console]::Error.WriteLine('__USER_CANCELLED__'); "
                    "}"
                )
            result = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-STA", "-ExecutionPolicy", "Bypass", "-Command", ps],
                capture_output=True, text=True, timeout=300,
            )
            stdout = (result.stdout or "").strip()
            stderr = (result.stderr or "").strip()
            lines = [ln.strip() for ln in stdout.splitlines() if ln.strip()]
            if lines:
                chosen = lines[-1]
                dialog_completed = True
            elif result.returncode == 0 or "__USER_CANCELLED__" in stderr:
                chosen = None
                dialog_completed = True
            else:
                if result.returncode not in (0, None):
                    errors.append(f"PowerShell dialog failed (exit {result.returncode})")
                if stderr:
                    errors.append(f"PowerShell: {stderr}")
        except subprocess.TimeoutExpired:
            errors.append("PowerShell dialog timed out")
        except Exception as e:
            errors.append(f"PowerShell: {e}")

    # ----- tkinter fallback (all platforms) -----
    if not chosen and not dialog_completed:
        try:
            import tkinter as tk
            from tkinter import filedialog

            result_box = {"path": None}

            def _ask():
                root = tk.Tk()
                root.withdraw()
                try:
                    root.attributes("-topmost", True)
                except Exception:
                    pass
                root.update()
                if mode == "folder":
                    result_box["path"] = filedialog.askdirectory(
                        parent=root,
                        title=title,
                        initialdir=initial,
                        mustexist=False,
                    )
                else:
                    tk_types = filetypes or [("All files", "*.*")]
                    result_box["path"] = filedialog.askopenfilename(
                        parent=root,
                        title=title,
                        initialdir=initial,
                        filetypes=tk_types,
                    )
                try:
                    root.destroy()
                except Exception:
                    pass

            thread = threading.Thread(target=_ask, daemon=True)
            thread.start()
            thread.join(timeout=300)
            if thread.is_alive():
                errors.append("tkinter dialog timed out")
            else:
                chosen = result_box.get("path") or None
                if chosen == "":
                    chosen = None
                dialog_completed = True
        except Exception as e:
            errors.append(f"tkinter: {e}")

    if chosen:
        try:
            chosen = str(Path(chosen).expanduser())
        except Exception:
            pass

    return chosen, dialog_completed, errors




def _dialog_lock(handler):
    """
    Prevents more than one native folder/file dialog from being opened
    at the same time. If a second pick request arrives while one is
    already open, it is rejected with HTTP 423.
    """
    def wrapper(self, *args, **kwargs):
        if not DIALOG_LOCK.acquire(blocking=False):
            self._send_json(
                423,
                {
                    "ok": False,
                    "cancelled": False,
                    "error": "Another native dialog is already open",
                },
            )
            return

        try:
            return handler(self, *args, **kwargs)
        finally:
            DIALOG_LOCK.release()

    return wrapper



def resolve_config_path(setup_root: Path | None, explicit: Path | None) -> Path:
    """
    Prefer --config-path, else <setup>/config/my-config.json, else
    walk up from this script looking for config/my-config.json.
    """
    if explicit is not None:
        return explicit.resolve()

    if setup_root is not None:
        return (setup_root / "config" / "my-config.json").resolve()

    here = Path(__file__).resolve().parent
    candidates = [
        # This script normally lives in <setup>\config\ui-config, so
        # my-config.json is directly in the parent folder (<setup>\config).
        here.parent / "my-config.json",
        here / "config" / "my-config.json",
        here.parent / "config" / "my-config.json",
        # Linux defaults
        Path("/opt/kd-setup/idol-linux-setup/config/my-config.json"),
        Path("/opt/kd-setup/idol-linux-setup-main/config/my-config.json"),
        # Generic fallbacks
        Path.home() / "kd-setup" / "config" / "my-config.json",
    ]
    for c in candidates:
        if c.is_file() or c.parent.is_dir():
            return c.resolve()
    # Last resort: next to the dashboard
    return (here / "my-config.json").resolve()


class KDHandler(SimpleHTTPRequestHandler):
    """Static files + small JSON API for saving config."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(SERVE_DIR), **kwargs)

    def log_message(self, fmt: str, *args) -> None:
        try:
            code = int(args[1]) if len(args) > 1 else 0
        except (TypeError, ValueError):
            code = 0
        if code >= 400:
            super().log_message(fmt, *args)

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        # Reflect request Origin when present so external clients (not just
        # 127.0.0.1) can use the API; fall back to localhost for same-origin.
        origin = self.headers.get("Origin") or f"http://{HOST}:{self.server.server_address[1]}"
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        super().end_headers()

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.end_headers()

    def do_GET(self) -> None:
        """Serve static assets, JSON GET endpoints, and WebSocket upgrades."""
        parsed = urlparse(self.path)

        # WebSocket: real-time host DNS + ping status stream
        if parsed.path == "/ws/host-status":
            if (self.headers.get("Upgrade") or "").lower() == "websocket":
                self._handle_ws_host_status()
                return
            self.send_error(426, "Upgrade Required")
            return

        if parsed.path == "/api/config-path":
            self._send_json(
                200,
                {
                    "configPath": str(CONFIG_PATH) if CONFIG_PATH else None,
                    "setupRoot": str(SETUP_ROOT) if SETUP_ROOT else None,
                    "exists": bool(CONFIG_PATH and CONFIG_PATH.is_file()),
                },
            )
            return

        if parsed.path == "/api/app-version":
            self._handle_app_version()
            return

        if parsed.path == "/api/load-config":
            self._handle_load_config()
            return

        if parsed.path == "/api/default-config":
            self._handle_default_config()
            return

        if parsed.path == "/api/replacements":
            self._handle_get_replacements()
            return

        if parsed.path in ("/", "/index.html"):
            self.path = "/" + DASHBOARD

        return super().do_GET()

    def do_POST(self) -> None:
        """Dispatch POST /api/* requests to the matching handler."""
        parsed = urlparse(self.path)
        routes = {
            "/api/save-config": self._handle_save_config,
            "/api/check-zips": self._handle_check_zips,
            "/api/check-file": self._handle_check_file,
            "/api/check-port": self._handle_check_port,
            "/api/check-path": self._handle_check_path,
            "/api/ping-host": self._handle_ping_host,
            "/api/basepath-version": self._handle_basepath_version,
            "/api/upload-zip": self._handle_upload_zip,
            "/api/pick-zip": self._handle_pick_zip,
            "/api/pick-folder": self._handle_pick_folder,
            "/api/list-dir": self._handle_list_dir,
            "/api/copy-server-file": self._handle_copy_server_file,
            "/api/client-log": self._handle_client_log,
            "/api/list-nars": self._handle_list_nars,
            "/api/pick-nar": self._handle_pick_nar,
            "/api/delete-nar": self._handle_delete_nar,
            "/api/extract-nars-from-zip": self._handle_extract_nars_from_zip,
            "/api/copy-nars": self._handle_copy_nars,
            "/api/auto-sync-nifi": self._handle_auto_sync_nifi,
            "/api/open-urls": self._handle_open_urls,
            "/api/shutdown": self._handle_shutdown,
            "/api/replacements": self._handle_save_replacements,
            "/api/validate-replacements": self._handle_validate_replacements,
            "/api/apply-replacements": self._handle_apply_replacements,
        }
        handler = routes.get(parsed.path)
        if handler is not None:
            handler()
            return
        self.send_error(404, "Not found")

    def _handle_check_zips(self) -> None:
        """Check whether expected component ZIPs exist under ZipPath."""
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length <= 0 or length > 1_000_000:
            self._send_json(400, {"ok": False, "error": "Invalid body size"})
            return
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception as e:
            self._send_json(400, {"ok": False, "error": f"Invalid JSON: {e}"})
            return

        zip_path = _as_path(data.get("zipPath"))
        items = data.get("items") or []
        if not isinstance(items, list):
            self._send_json(400, {"ok": False, "error": "items must be a list"})
            return

        folder_ok = zip_path.is_dir()
        results = []
        for item in items:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "")
            pattern = str(item.get("pattern") or f"*{name}*.zip")
            sample = str(item.get("sample") or "")
            found = []
            status = "missing"
            matched = None
            if not folder_ok:
                status = "folder_missing"
            else:
                try:
                    # Case-insensitive glob for Windows-style names
                    for p in zip_path.iterdir():
                        if not p.is_file():
                            continue
                        if not p.suffix.lower() == ".zip":
                            continue
                        if fnmatch(p.name.lower(), pattern.lower()):
                            found.append(p.name)
                    if found:
                        # Prefer sample exact match, else first match
                        if sample and sample in found:
                            matched = sample
                        else:
                            matched = sorted(found)[0]
                        status = "found"
                    else:
                        status = "missing"
                except Exception as e:
                    status = "error"
                    matched = str(e)
            results.append({
                "name": name,
                "pattern": pattern,
                "sample": sample,
                "status": status,
                "matched": matched,
                "matches": found[:10],
            })

        # KD major.minor from package names (Content_26.3.1_LINUX… → 26.3)
        version_info = {
            "success": False,
            "majorMinor": None,
            "suggestedBasePath": None,
            "warnings": [],
            "mismatches": [],
            "detail": "",
        }
        if folder_ok:
            try:
                # Prefer toolkit discovery helpers when available
                toolkit_root = Path(__file__).resolve().parents[2]
                if str(toolkit_root) not in sys.path:
                    sys.path.insert(0, str(toolkit_root))
                from kd import discovery as kd_discovery  # type: ignore

                analysis = kd_discovery.analyze_kd_zip_versions(zip_path)
                version_info = {
                    "success": bool(analysis.get("Success")),
                    "majorMinor": analysis.get("MajorMinor"),
                    "suggestedBasePath": analysis.get("SuggestedBasePath"),
                    "warnings": list(analysis.get("Warnings") or []),
                    "mismatches": list(analysis.get("Mismatches") or []),
                    "unversioned": list(analysis.get("Unversioned") or []),
                    "detail": analysis.get("Detail") or "",
                    "counts": analysis.get("Counts") or {},
                }
            except Exception as e:
                version_info["detail"] = f"Version scan unavailable: {e}"

        self._send_json(200, {
            "ok": True,
            "zipPath": str(zip_path),
            "folderExists": folder_ok,
            "results": results,
            "version": version_info,
        })

    def _handle_check_file(self) -> None:
        """Check whether a single file (e.g. LicenseKeyPath) exists on disk."""
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length <= 0 or length > 100_000:
            self._send_json(400, {"ok": False, "error": "Invalid body size"})
            return
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception as e:
            self._send_json(400, {"ok": False, "error": f"Invalid JSON: {e}"})
            return

        raw_path = str(data.get("path") or "").strip()
        if not raw_path:
            self._send_json(200, {
                "ok": True,
                "path": raw_path,
                "exists": False,
                "isFile": False,
            })
            return

        try:
            p = Path(raw_path)
            exists = p.is_file()
            self._send_json(200, {
                "ok": True,
                "path": str(p),
                "exists": exists,
                "isFile": exists,
            })
        except Exception as e:
            self._send_json(200, {
                "ok": True,
                "path": raw_path,
                "exists": False,
                "isFile": False,
                "error": str(e),
            })

    @staticmethod
    def _port_is_free(port: int) -> bool:
        """
        Best-effort "is this port free right now" check on the local
        machine: try to bind a TCP socket to it. If the bind succeeds the
        port is free (nothing is listening on it); if it raises OSError
        (e.g. WinError 10048 / errno EADDRINUSE) something already has it.
        Checked against both 127.0.0.1 and 0.0.0.0 since a service bound to
        one may not show up as busy on the other.
        """
        for bind_host in (HOST, "0.0.0.0"):
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((bind_host, port))
            except OSError:
                s.close()
                return False
            else:
                s.close()
        return True

    def _handle_check_port(self) -> None:
        """Check whether a single port is currently free on this machine."""
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length <= 0 or length > 10_000:
            self._send_json(400, {"ok": False, "error": "Invalid body size"})
            return
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception as e:
            self._send_json(400, {"ok": False, "error": f"Invalid JSON: {e}"})
            return

        raw_port = str(data.get("port") or "").strip()
        try:
            port_num = int(raw_port)
        except (TypeError, ValueError):
            self._send_json(200, {
                "ok": True, "port": raw_port, "valid": False, "free": None,
                "error": "Not a number",
            })
            return

        if port_num < 1 or port_num > 65535:
            self._send_json(200, {
                "ok": True, "port": port_num, "valid": False, "free": None,
                "error": "Out of range (1-65535)",
            })
            return

        try:
            free = self._port_is_free(port_num)
            self._send_json(200, {"ok": True, "port": port_num, "valid": True, "free": free})
        except Exception as e:
            self._send_json(200, {
                "ok": True, "port": port_num, "valid": True, "free": None,
                "error": str(e),
            })

    def _handle_check_path(self) -> None:
        """
        Check whether a filesystem path (folder or file) currently exists —
        used for the "Paths & identity" fields (BasePath, SetupPath,
        ZipPath, IndexPath), the same way _handle_check_port checks ports:
        the frontend shows a green check when it exists, a red X when it
        doesn't.
        """
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length <= 0 or length > 10_000:
            self._send_json(400, {"ok": False, "error": "Invalid body size"})
            return
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception as e:
            self._send_json(400, {"ok": False, "error": f"Invalid JSON: {e}"})
            return

        raw_path = str(data.get("path") or "").strip()
        if not raw_path:
            self._send_json(200, {
                "ok": True, "path": raw_path, "exists": False,
                "isDir": False, "isFile": False, "error": "Empty path",
            })
            return

        try:
            p = Path(raw_path)
            exists = p.exists()
            self._send_json(200, {
                "ok": True,
                "path": str(p),
                "exists": exists,
                "isDir": exists and p.is_dir(),
                "isFile": exists and p.is_file(),
            })
        except Exception as e:
            self._send_json(200, {
                "ok": True, "path": raw_path, "exists": False,
                "isDir": False, "isFile": False, "error": str(e),
            })

    def _handle_ping_host(self) -> None:
        """
        HTTP fallback for host override checks.
        Flow: format → DNS → ICMP ping → TCP probe (if ping fails).
        Many public IPs block ICMP; TCP on 80/443/etc. is accepted as reachable.
        """
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length <= 0 or length > 10_000:
            self._send_json(400, {"ok": False, "error": "Invalid body size"})
            return
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception as e:
            self._send_json(400, {"ok": False, "error": f"Invalid JSON: {e}"})
            return

        host = str(data.get("host") or "").strip()
        if not host:
            self._send_json(200, {
                "ok": True, "host": host, "resolved": False, "addresses": [],
                "reachable": False, "method": None, "tcp_port": None,
                "error": "Empty host",
            })
            return
        if not _is_safe_host(host):
            self._send_json(200, {
                "ok": True, "host": host, "resolved": False, "addresses": [],
                "reachable": False, "method": None, "tcp_port": None,
                "error": "Invalid host format",
            })
            return

        result = _check_host_reachability(host)
        self._send_json(200, {"ok": True, **result})

    def _handle_ws_host_status(self) -> None:
        """
        WebSocket endpoint /ws/host-status — streams real-time reachability stages.

        Client → server (text JSON):
          {"action":"check","host":"example.com"}
          {"action":"ping"}   (keepalive; optional)

        Server → client stages:
          validate → dns → ping → [tcp if ping fails] → done
        done includes: resolved, addresses, reachable, method ("ping"|"tcp"), tcp_port
        """
        key = self.headers.get("Sec-WebSocket-Key")
        if not key:
            self.send_error(400, "Missing Sec-WebSocket-Key")
            return

        try:
            self.send_response(101, "Switching Protocols")
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.send_header("Sec-WebSocket-Accept", _ws_accept_key(key))
            self.end_headers()
        except Exception:
            return

        sock = self.connection
        try:
            sock.settimeout(60)
        except Exception:
            pass

        def emit(obj: dict) -> bool:
            try:
                _ws_send_text(sock, json.dumps(obj, ensure_ascii=False))
                return True
            except OSError:
                return False

        try:
            while True:
                frame = _ws_recv_frame(sock)
                if frame is None:
                    break
                opcode, payload = frame
                if opcode == 0x8:  # close
                    break
                if opcode == 0x9:  # ping → pong
                    try:
                        data = payload
                        n = len(data)
                        if n < 126:
                            hdr = struct.pack("!BB", 0x8A, n)
                        else:
                            hdr = struct.pack("!BBH", 0x8A, 126, n)
                        sock.sendall(hdr + data)
                    except OSError:
                        break
                    continue
                if opcode != 0x1:  # only text
                    continue
                try:
                    msg = json.loads(payload.decode("utf-8"))
                except Exception:
                    if not emit({"stage": "error", "ok": False, "error": "Invalid JSON"}):
                        break
                    continue

                action = str(msg.get("action") or "").strip().lower()
                if action == "ping":
                    emit({"stage": "pong", "ok": True})
                    continue
                if action != "check":
                    emit({"stage": "error", "ok": False, "error": "Unknown action"})
                    continue

                host = str(msg.get("host") or "").strip()
                if not host:
                    emit({
                        "stage": "done", "ok": False, "host": "",
                        "resolved": False, "addresses": [], "reachable": False,
                        "method": None, "tcp_port": None, "error": "Empty host",
                    })
                    continue

                if not _is_safe_host(host):
                    emit({"stage": "validate", "ok": False, "error": "Invalid host format"})
                    emit({
                        "stage": "done", "ok": False, "host": host,
                        "resolved": False, "addresses": [], "reachable": False,
                        "method": None, "tcp_port": None,
                        "error": "Invalid host format",
                    })
                    continue

                if not emit({"stage": "validate", "ok": True, "host": host}):
                    break

                # --- DNS ---
                if not emit({"stage": "dns", "ok": None, "host": host, "status": "resolving"}):
                    break
                resolved, addresses, dns_err = _resolve_host(host)
                if not resolved:
                    emit({
                        "stage": "dns", "ok": False, "host": host,
                        "addresses": [], "error": dns_err or "DNS resolution failed",
                    })
                    emit({
                        "stage": "done", "ok": False, "host": host,
                        "resolved": False, "addresses": [], "reachable": False,
                        "method": None, "tcp_port": None,
                        "error": dns_err or "DNS resolution failed",
                    })
                    continue
                if not emit({
                    "stage": "dns", "ok": True, "host": host, "addresses": addresses,
                }):
                    break

                # --- ICMP ping ---
                if not emit({"stage": "ping", "ok": None, "host": host, "status": "pinging"}):
                    break
                ping_ok, ping_err = _ping_host(host)
                if not emit({
                    "stage": "ping",
                    "ok": ping_ok,
                    "host": host,
                    "error": None if ping_ok else (ping_err or "ping failed"),
                }):
                    break

                method = None
                tcp_port = None
                reachable = False
                final_err = None

                if ping_ok:
                    reachable = True
                    method = "ping"
                else:
                    # --- TCP fallback (ICMP often blocked on public/cloud hosts) ---
                    if not emit({
                        "stage": "tcp", "ok": None, "host": host,
                        "status": "probing",
                        "message": "ICMP blocked — trying TCP…",
                    }):
                        break
                    tcp_ok, tcp_port, tcp_err = _tcp_probe_host(host)
                    if not emit({
                        "stage": "tcp",
                        "ok": tcp_ok,
                        "host": host,
                        "tcp_port": tcp_port,
                        "error": None if tcp_ok else (tcp_err or "No TCP ports responded"),
                    }):
                        break
                    if tcp_ok:
                        reachable = True
                        method = "tcp"
                    else:
                        final_err = (ping_err or "No reply (100% packet loss)")
                        if tcp_err:
                            final_err = f"{final_err} · TCP: {tcp_err}"

                emit({
                    "stage": "done",
                    "ok": bool(reachable),
                    "host": host,
                    "resolved": True,
                    "addresses": addresses,
                    "reachable": bool(reachable),
                    "method": method,
                    "tcp_port": tcp_port,
                    "error": final_err,
                    "ping_error": None if ping_ok else (ping_err or None),
                })
        except Exception:
            pass
        finally:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                sock.close()
            except Exception:
                pass

    def _handle_basepath_version(self) -> None:
        """
        Read the first line of <BasePath>\\VERSION.txt — the version marker
        for whatever is actually installed at BasePath — so the dashboard
        header can show it. This is independent of this setup toolkit's own
        top-level VERSION.txt.
        """
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length <= 0 or length > 10_000:
            self._send_json(400, {"ok": False, "error": "Invalid body size"})
            return
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception as e:
            self._send_json(400, {"ok": False, "error": f"Invalid JSON: {e}"})
            return

        raw_path = str(data.get("basePath") or "").strip()
        if not raw_path:
            self._send_json(200, {"ok": True, "exists": False, "version": None, "error": "Empty path"})
            return

        try:
            version_file = Path(raw_path) / "VERSION.txt"
            if not version_file.is_file():
                self._send_json(200, {"ok": True, "exists": False, "version": None})
                return
            with open(version_file, "r", encoding="utf-8", errors="replace") as f:
                first_line = f.readline().strip()
            self._send_json(200, {"ok": True, "exists": True, "version": first_line})
        except Exception as e:
            self._send_json(200, {"ok": False, "exists": False, "version": None, "error": str(e)})

    def _handle_app_version(self) -> None:
      """
      Read the first non-empty line from the setup toolkit VERSION.txt.

      VERSION.txt is expected to be located at the setup root:

          <SetupRoot>\\VERSION.txt

      The first line is displayed by the dashboard as the application
      version.

      Example VERSION.txt:

          ver-0.6r66m3
          2026-08-11T14:12:24+00:00
      """
      try:
          setup_root = SETUP_ROOT

          # If SETUP_ROOT was not explicitly supplied, infer it from
          # CONFIG_PATH.
          if setup_root is None and CONFIG_PATH is not None:
              try:
                  if CONFIG_PATH.parent.name.lower() == "config":
                      setup_root = CONFIG_PATH.parent.parent
              except Exception:
                  setup_root = None

          if setup_root is None:
              setup_root = Path(__file__).resolve().parents[2]

          setup_root = Path(setup_root)

          # Prefer VERSION.txt, but also support version.txt on systems
          # where case sensitivity matters.
          candidates = [
              setup_root / "VERSION.txt",
              setup_root / "version.txt",
          ]

          version_file = next(
              (p for p in candidates if p.is_file()),
              None,
          )

          if version_file is None:
              self._send_json(
                  200,
                  {
                      "ok": True,
                      "exists": False,
                      "version": None,
                      "path": str(candidates[0]),
                  },
              )
              return

          with open(
              version_file,
              "r",
              encoding="utf-8",
              errors="replace",
          ) as f:
              first_line = f.readline().strip()

          if not first_line:
              self._send_json(
                  200,
                  {
                      "ok": True,
                      "exists": True,
                      "version": None,
                      "path": str(version_file),
                  },
              )
              return

          self._send_json(
              200,
              {
                  "ok": True,
                  "exists": True,
                  "version": first_line,
                  "path": str(version_file),
              },
          )

      except Exception as e:
          self._send_json(
              500,
              {
                  "ok": False,
                  "exists": False,
                  "version": None,
                  "error": str(e),
              },
          )

    def _handle_load_config(self) -> None:
        """
        Read my-config.json (the same file _handle_save_config writes) so
        the dashboard can populate its fields — including "Paths &
        identity" — from whatever was last saved, instead of only the
        hardcoded INITIAL defaults baked into the page.
        """
        if CONFIG_PATH is None:
            self._send_json(200, {"ok": True, "exists": False, "path": None})
            return
        if not CONFIG_PATH.is_file():
            self._send_json(200, {"ok": True, "exists": False, "path": str(CONFIG_PATH)})
            return
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            if not isinstance(cfg, dict):
                raise ValueError("my-config.json does not contain a JSON object")
            self._send_json(200, {"ok": True, "exists": True, "path": str(CONFIG_PATH), "config": cfg})
        except Exception as e:
            self._send_json(200, {
                "ok": False, "exists": True, "path": str(CONFIG_PATH),
                "error": f"Could not read config: {e}",
            })

    def _resolve_default_config_path(self) -> Path | None:
        """Locate config/default-config.json next to my-config.json (or under setup root)."""
        candidates = []
        if CONFIG_PATH is not None:
            candidates.append(CONFIG_PATH.parent / "default-config.json")
        if SETUP_ROOT is not None:
            candidates.append(SETUP_ROOT / "config" / "default-config.json")
        # Script lives in <setup>/config/ui-config → parent is config/
        here = Path(__file__).resolve().parent
        candidates.extend([
            here.parent / "default-config.json",
            here / "default-config.json",
            here.parent / "config" / "default-config.json",
        ])
        for c in candidates:
            try:
                if c.is_file():
                    return c.resolve()
            except Exception:
                continue
        return candidates[0].resolve() if candidates else None

    def _handle_default_config(self) -> None:
        """
        Serve config/default-config.json so the dashboard can use it as the
        true INITIAL defaults instead of a large object baked into the HTML.
        """
        path = self._resolve_default_config_path()
        if path is None:
            self._send_json(200, {"ok": False, "exists": False, "path": None, "error": "No default-config path resolved"})
            return
        if not path.is_file():
            self._send_json(200, {"ok": False, "exists": False, "path": str(path), "error": "default-config.json not found"})
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            if not isinstance(cfg, dict):
                raise ValueError("default-config.json does not contain a JSON object")
            self._send_json(200, {"ok": True, "exists": True, "path": str(path), "config": cfg})
        except Exception as e:
            self._send_json(200, {
                "ok": False, "exists": True, "path": str(path),
                "error": f"Could not read default-config: {e}",
            })

    def _handle_list_nars(self) -> None:
        length = int(self.headers.get("Content-Length", "0") or 0)
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8")) if length > 0 else {}
        except Exception as e:
            self._send_json(400, {"ok": False, "error": str(e)})
            return
        ext = _as_path(data.get("extensionsPath"))
        files = []
        if ext.is_dir():
            for p in sorted(ext.glob("*.nar")):
                try:
                    st = p.stat()
                    import datetime
                    files.append({
                        "name": p.name,
                        "path": str(p),
                        "size": st.st_size,
                        "mtime": datetime.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
                    })
                except Exception:
                    files.append({"name": p.name, "path": str(p)})
        self._send_json(200, {"ok": True, "extensionsPath": str(ext), "files": files, "folderExists": ext.is_dir()})

    @_dialog_lock
    def _handle_pick_nar(self) -> None:
        """
        Open a native file dialog filtered to *.nar and copy the selected
        connector into BasePath\\NiFi\\extensions.

        Important behavior:
          - On Windows, PowerShell OpenFileDialog is preferred.
          - If the user cancels the PowerShell dialog, the function returns
            immediately.
          - tkinter is used only if the PowerShell dialog itself fails.
          - A normal Cancel never opens a second dialog.
        """

        # ------------------------------------------------------------------
        # Read request
        #
        # Two distinct paths are involved:
        #   - extensionsPath (body) - the COPY DESTINATION, always
        #     BasePath\\NiFi\\extensions. The connector is copied here
        #     regardless of where it was browsed from.
        #   - browsePath (body, falling back to the X-Nar-Path header) -
        #     where the file-picker dialog should START, normally the
        #     NiFi Connectors Source folder (SetupPath\\nifi\\nifi-connectors).
        # ------------------------------------------------------------------
        length = int(
            self.headers.get("Content-Length", "0") or 0
        )

        hdr_browse = (
            self.headers.get("X-Nar-Path") or ""
        ).strip()

        body = {}

        if length > 0:
            try:
                body = json.loads(
                    self.rfile.read(length).decode("utf-8")
                )
            except Exception:
                body = {}

        ext_hdr = str(
            body.get("extensionsPath") or ""
        ).strip()

        if not ext_hdr:
            self._send_json(
                400,
                {
                    "ok": False,
                    "cancelled": False,
                    "error": "Missing extensions path",
                },
            )
            return

        # Where the dialog should open: prefer an explicit browsePath from
        # the request body, then the X-Nar-Path header, then fall back to
        # the destination (target) folder itself.
        browse_hdr = str(
            body.get("browsePath") or hdr_browse or ext_hdr
        ).strip()

        # ------------------------------------------------------------------
        # Prepare destination directory
        # ------------------------------------------------------------------
        dest_dir = _as_path(ext_hdr)

        try:
            dest_dir.mkdir(
                parents=True,
                exist_ok=True,
            )
        except Exception as e:
            self._send_json(
                500,
                {
                    "ok": False,
                    "cancelled": False,
                    "error": (
                        f"Cannot create extensions folder: {e}"
                    ),
                },
            )
            return

        # Start the dialog in the Source (nifi-connectors) folder when it
        # exists; otherwise fall back to the destination (target) folder
        # so the dialog always has a valid starting point.
        browse_dir = Path(browse_hdr) if browse_hdr else dest_dir
        if not browse_dir.is_dir():
            browse_dir = dest_dir

        initial_dir = str(
            browse_dir.resolve()
        )

        chosen, dialog_completed, errors = _pick_native_path(
            mode="file",
            initial_dir=initial_dir,
            title="Select NiFi Connector (.nar)",
            filetypes=[("NiFi Connectors", "*.nar"), ("All files", "*.*")],
        )

        # ------------------------------------------------------------------
        # User cancelled
        # ------------------------------------------------------------------
        if not chosen:
            self._send_json(
                200,
                {
                    "ok": False,
                    "cancelled": True,
                    "error": (
                        "; ".join(errors)
                        if errors
                        else "No file selected"
                    ),
                },
            )
            return

        # ------------------------------------------------------------------
        # Validate selected file
        # ------------------------------------------------------------------
        src = Path(chosen)

        if not src.is_file():
            self._send_json(
                400,
                {
                    "ok": False,
                    "cancelled": False,
                    "error": (
                        f"File not found: {chosen}"
                    ),
                },
            )
            return

        if src.suffix.lower() != ".nar":
            self._send_json(
                400,
                {
                    "ok": False,
                    "cancelled": False,
                    "error": (
                        "Only .nar files are allowed"
                    ),
                },
            )
            return

        # ------------------------------------------------------------------
        # Copy selected NAR into NiFi extensions
        # ------------------------------------------------------------------
        dest_file = (
            dest_dir / src.name
        )

        try:
            already_there = False
            try:
                if src.resolve() == dest_file.resolve():
                    already_there = True
                elif dest_file.exists() and src.samefile(dest_file):
                    already_there = True
            except (OSError, ValueError):
                pass
            if not already_there:
                shutil.copy2(
                    src,
                    dest_file,
                )
                _chown_path(dest_file)
        except shutil.SameFileError:
            pass
        except Exception as e:
            self._send_json(
                500,
                {
                    "ok": False,
                    "cancelled": False,
                    "error": (
                        f"Copy failed: {e}"
                    ),
                },
            )
            return

        # ------------------------------------------------------------------
        # Log and return success
        # ------------------------------------------------------------------
        print(
            f"  Picked NAR → {src}"
        )

        print(
            f"  Copied to  → {dest_file}"
        )

        self._send_json(
            200,
            {
                "ok": True,
                "cancelled": False,
                "source": str(src),
                "path": str(dest_file),
                "filename": dest_file.name,
            },
        )


    def _read_config_dict(self) -> dict:
        """
        Read the saved my-config.json file, if present, so auto-sync
        can discover ZipPath / SetupPath / BasePath even when the UI
        does not explicitly send them.
        """
        if CONFIG_PATH and CONFIG_PATH.is_file():
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
        return {}

    def _config_find_value(self, obj, names, depth=0):
        """
        Recursively search a saved JSON config object for one of the
        supplied key names, case-insensitively.
        """
        if depth > 6 or obj is None:
            return None

        wanted = {str(name).lower() for name in names}

        if isinstance(obj, dict):
            for key, value in obj.items():
                if str(key).lower() in wanted:
                    text = str(value or "").strip()
                    if text:
                        return text

            for value in obj.values():
                found = self._config_find_value(value, names, depth + 1)
                if found:
                    return found

        elif isinstance(obj, list):
            for value in obj:
                found = self._config_find_value(value, names, depth + 1)
                if found:
                    return found

        return None

    def _sync_nifi_from_zip(
        self,
        zip_folder_value,
        setup_path_value,
        target_path_value,
        pattern="*NiFiIngest*.zip",
        explicit_zip="",
    ):
        """
        Core NiFi NAR synchronization helper:

            NiFiIngest ZIP
                -> extract *.nar to SetupPath\\nifi\\nifi-connectors
                -> copy them to BasePath\\NiFi\\extensions

        Returns a JSON-serializable dict.
        """
        zip_folder = _as_path(zip_folder_value)
        setup_path = _as_path(setup_path_value)
        target_path = _as_path(target_path_value)
        source_dir = setup_path / "nifi" / "nifi-connectors"

        base = {
            "ok": False,
            "source": str(source_dir),
            "target": str(target_path),
            "extracted": [],
            "copied": [],
            "extractionErrors": [],
            "copyErrors": [],
            "sourceFiles": [],
            "targetFiles": [],
            "statusCode": 500,
        }

        if not str(setup_path):
            base.update(
                {
                    "error": "Missing SetupPath. Set or save SetupPath first.",
                    "statusCode": 400,
                }
            )
            return base

        if not str(target_path):
            base.update(
                {
                    "error": "Missing targetPath / BasePath. Set or save BasePath first.",
                    "statusCode": 400,
                }
            )
            return base

        if not zip_folder.is_dir():
            base.update(
                {
                    "error": f"ZipPath not found: {zip_folder}",
                    "statusCode": 400,
                }
            )
            return base

        try:
            source_dir.mkdir(parents=True, exist_ok=True)
            target_path.mkdir(parents=True, exist_ok=True)
            _chown_path(source_dir)
            _chown_path(target_path)
        except Exception as exc:
            base.update(
                {
                    "error": f"Cannot create NiFi folders: {exc}",
                    "statusCode": 500,
                }
            )
            return base

        zip_file = None

        if explicit_zip:
            candidate = Path(str(explicit_zip)).expanduser()
            if not candidate.is_file():
                candidate = zip_folder / Path(explicit_zip).name
            if candidate.is_file():
                zip_file = candidate

        if zip_file is None:
            try:
                matches = sorted(
                    p
                    for p in zip_folder.iterdir()
                    if p.is_file()
                    and p.suffix.lower() == ".zip"
                    and fnmatch(p.name.lower(), pattern.lower())
                )

                if not matches:
                    matches = sorted(
                        p
                        for p in zip_folder.iterdir()
                        if p.is_file()
                        and p.suffix.lower() == ".zip"
                        and "nifi" in p.name.lower()
                        and "ingest" in p.name.lower()
                    )
            except Exception as exc:
                base.update(
                    {
                        "error": f"Cannot scan ZipPath: {exc}",
                        "statusCode": 500,
                    }
                )
                return base

            if not matches:
                base.update(
                    {
                        "error": f"No ZIP matching {pattern} under {zip_folder}",
                        "statusCode": 404,
                    }
                )
                return base

            zip_file = matches[0]

        if not zip_file.is_file():
            base.update(
                {
                    "error": f"ZIP file does not exist: {zip_file}",
                    "statusCode": 404,
                }
            )
            return base

        extracted = []
        extraction_errors = []

        try:
            with zipfile.ZipFile(zip_file, "r") as zf:
                for info in zf.infolist():
                    if info.is_dir():
                        continue

                    fname = info.filename.replace("\\", "/")
                    if not fname.lower().endswith(".nar"):
                        continue

                    name = Path(fname).name
                    if not name:
                        continue

                    destination = source_dir / name

                    try:
                        with zf.open(info, "r") as src:
                            with open(destination, "wb") as dst:
                                shutil.copyfileobj(src, dst)
                        _chown_path(destination)
                        extracted.append(name)
                    except Exception as exc:
                        extraction_errors.append(f"{name}: {exc}")

        except zipfile.BadZipFile as exc:
            base.update(
                {
                    "error": f"Bad ZIP: {exc}",
                    "zip": str(zip_file),
                    "zipName": zip_file.name,
                    "statusCode": 400,
                }
            )
            return base
        except Exception as exc:
            base.update(
                {
                    "error": f"Failed to extract NAR files: {exc}",
                    "zip": str(zip_file),
                    "zipName": zip_file.name,
                    "statusCode": 500,
                }
            )
            return base

        copied = []
        copy_errors = []

        for name in extracted:
            source_file = source_dir / name
            target_file = target_path / name

            try:
                if not source_file.is_file():
                    copy_errors.append(f"{name}: source file not found")
                    continue

                already_there = False
                try:
                    if source_file.resolve() == target_file.resolve():
                        already_there = True
                    elif target_file.exists() and source_file.samefile(target_file):
                        already_there = True
                except (OSError, ValueError):
                    pass
                if not already_there:
                    shutil.copy2(source_file, target_file)
                    _chown_path(source_file)
                    _chown_path(target_file)
                copied.append(name)
            except shutil.SameFileError:
                copied.append(name)
            except PermissionError as exc:
                copy_errors.append(f"{name}: permission denied: {exc}")
            except OSError as exc:
                copy_errors.append(f"{name}: OS error: {exc}")
            except Exception as exc:
                copy_errors.append(f"{name}: {exc}")

        def _list_nar_dir(path_value: Path):
            items = []
            try:
                if path_value.is_dir():
                    for p in sorted(path_value.glob("*.nar")):
                        try:
                            st = p.stat()
                            items.append(
                                {
                                    "name": p.name,
                                    "path": str(p),
                                    "size": st.st_size,
                                    "mtime": datetime.datetime.fromtimestamp(
                                        st.st_mtime
                                    ).strftime("%Y-%m-%d %H:%M"),
                                }
                            )
                        except Exception:
                            items.append(
                                {
                                    "name": p.name,
                                    "path": str(p),
                                }
                            )
            except Exception:
                pass
            return items

        source_files = _list_nar_dir(source_dir)
        target_files = _list_nar_dir(target_path)

        if not extracted:
            base.update(
                {
                    "error": "No .nar files were found in the ZIP",
                    "zip": str(zip_file),
                    "zipName": zip_file.name,
                    "statusCode": 404,
                    "sourceFiles": source_files,
                    "targetFiles": target_files,
                }
            )
            return base

        ok = (
            len(extraction_errors) == 0
            and len(copy_errors) == 0
            and len(copied) == len(extracted)
        )

        return {
            "ok": ok,
            "statusCode": 200 if ok else 207,
            "zip": str(zip_file),
            "zipName": zip_file.name,
            "source": str(source_dir),
            "dest": str(source_dir),
            "target": str(target_path),
            "extracted": extracted,
            "copied": copied,
            "extractionErrors": extraction_errors,
            "copyErrors": copy_errors,
            "extractCount": len(extracted),
            "copyCount": len(copied),
            "count": len(extracted),
            "sourceFiles": source_files,
            "targetFiles": target_files,
        }

    def _handle_auto_sync_nifi(self) -> None:
        """
        One-call NiFi connector synchronization endpoint.

        POST /api/auto-sync-nifi

        Body may contain overrides:
            zipPath, setupPath, basePath, targetPath, pattern, zipFile

        If omitted, values are read from saved my-config.json where possible.
        """
        body = self._read_json_body()
        cfg = self._read_config_dict()

        zip_path = str(
            body.get("zipPath")
            or self._config_find_value(
                cfg,
                ["zipPath", "zip_path", "zipFolder", "zip"],
            )
            or ""
        ).strip()

        setup_path = str(
            body.get("setupPath")
            or self._config_find_value(
                cfg,
                ["setupPath", "setup_path", "setupRoot", "setup"],
            )
            or (str(SETUP_ROOT) if SETUP_ROOT else "")
        ).strip()

        base_path = str(
            body.get("basePath")
            or self._config_find_value(
                cfg,
                ["basePath", "base_path", "baseFolder", "installPath", "base"],
            )
            or ""
        ).strip()

        target_path = str(body.get("targetPath") or "").strip()

        if not target_path and base_path:
            target_path = str(Path(base_path) / "NiFi" / "extensions")

        pattern = (
            str(body.get("pattern") or "*NiFiIngest*.zip").strip()
            or "*NiFiIngest*.zip"
        )

        explicit_zip = str(body.get("zipFile") or "").strip()

        if not zip_path:
            self._send_json(
                400,
                {
                    "ok": False,
                    "error": "Missing ZipPath. Set or save ZipPath first.",
                },
            )
            return

        if not setup_path:
            self._send_json(
                400,
                {
                    "ok": False,
                    "error": "Missing SetupPath. Set or save SetupPath first.",
                },
            )
            return

        if not target_path:
            self._send_json(
                400,
                {
                    "ok": False,
                    "error": "Missing BasePath/targetPath. Set or save BasePath first.",
                },
            )
            return

        result = self._sync_nifi_from_zip(
            zip_path,
            setup_path,
            target_path,
            pattern,
            explicit_zip,
        )

        status_code = int(result.pop("statusCode", 500))
        self._send_json(status_code, result)

    def _handle_extract_nars_from_zip(self) -> None:
        """
        Find a NiFiIngest ZIP under ZipPath, extract every *.nar member into:

            <SetupPath>\\nifi\\nifi-connectors

        Then copy all successfully extracted NAR files into:

            <targetPath>

        Normally targetPath is:

            <BasePath>\\NiFi\\extensions

        The operation is:

            ZIP
              |
              v
            SetupPath\\nifi\\nifi-connectors
              |
              v
            BasePath\\NiFi\\extensions
        """

        # ------------------------------------------------------------------
        # Read request
        # ------------------------------------------------------------------
        length = int(
            self.headers.get("Content-Length", "0") or 0
        )

        try:
            data = (
                json.loads(
                    self.rfile.read(length).decode("utf-8")
                )
                if length > 0
                else {}
            )

        except Exception as e:
            self._send_json(
                400,
                {
                    "ok": False,
                    "error": f"Invalid JSON: {e}",
                },
            )
            return

        # ------------------------------------------------------------------
        # Read parameters
        # ------------------------------------------------------------------
        zip_folder = Path(
            str(
                data.get("zipPath") or ""
            ).strip()
        )

        setup_path_value = str(
            data.get("setupPath") or ""
        ).strip()

        target_path_value = str(
            data.get("targetPath") or ""
        ).strip()

        explicit_zip = str(
            data.get("zipFile") or ""
        ).strip()

        pattern = (
            str(
                data.get("pattern")
                or "*NiFiIngest*.zip"
            ).strip()
            or "*NiFiIngest*.zip"
        )

        # ------------------------------------------------------------------
        # SetupPath
        # ------------------------------------------------------------------
        if not setup_path_value:
            if SETUP_ROOT:
                setup_path = Path(
                    SETUP_ROOT
                )
            else:
                self._send_json(
                    400,
                    {
                        "ok": False,
                        "error": (
                            "Missing SetupPath "
                            "(and setupRoot not set)"
                        ),
                    },
                )
                return
        else:
            setup_path = Path(
                setup_path_value
            )

        # ------------------------------------------------------------------
        # Source directory
        # ------------------------------------------------------------------
        source_dir = (
            setup_path
            / "nifi"
            / "nifi-connectors"
        )

        try:
            source_dir.mkdir(
                parents=True,
                exist_ok=True,
            )

        except Exception as e:
            self._send_json(
                500,
                {
                    "ok": False,
                    "error": (
                        f"Cannot create {source_dir}: {e}"
                    ),
                },
            )
            return

        # ------------------------------------------------------------------
        # Target directory
        #
        # The frontend sends:
        #
        #   narExtensionsDir()
        #
        # which resolves to:
        #
        #   BasePath\\NiFi\\extensions
        # ------------------------------------------------------------------
        if not target_path_value:
            self._send_json(
                400,
                {
                    "ok": False,
                    "error": (
                        "Missing targetPath. "
                        "Set BasePath first."
                    ),
                    "source": str(source_dir),
                },
            )
            return

        target_dir = Path(
            target_path_value
        )

        try:
            target_dir.mkdir(
                parents=True,
                exist_ok=True,
            )

        except Exception as e:
            self._send_json(
                500,
                {
                    "ok": False,
                    "error": (
                        f"Cannot create target directory "
                        f"{target_dir}: {e}"
                    ),
                    "source": str(source_dir),
                    "target": str(target_dir),
                },
            )
            return

        # ------------------------------------------------------------------
        # Resolve ZIP
        # ------------------------------------------------------------------
        zip_file: Path | None = None

        # Explicit ZIP supplied by caller.
        if explicit_zip:
            candidate = Path(
                explicit_zip
            )

            if (
                not candidate.is_file()
                and zip_folder.is_dir()
            ):
                candidate = (
                    zip_folder
                    / Path(explicit_zip).name
                )

            if candidate.is_file():
                zip_file = candidate

        # ------------------------------------------------------------------
        # Auto-discover ZIP
        # ------------------------------------------------------------------
        if zip_file is None:

            if not zip_folder.is_dir():
                self._send_json(
                    400,
                    {
                        "ok": False,
                        "error": (
                            f"ZipPath not found: "
                            f"{zip_folder}"
                        ),
                        "source": str(source_dir),
                        "target": str(target_dir),
                    },
                )
                return

            try:
                matches = sorted(
                    p
                    for p in zip_folder.iterdir()
                    if (
                        p.is_file()
                        and p.suffix.lower() == ".zip"
                        and fnmatch(
                            p.name.lower(),
                            pattern.lower(),
                        )
                    )
                )

            except Exception as e:
                self._send_json(
                    500,
                    {
                        "ok": False,
                        "error": (
                            f"Cannot scan ZipPath: {e}"
                        ),
                        "source": str(source_dir),
                        "target": str(target_dir),
                    },
                )
                return

            # --------------------------------------------------------------
            # Fallback discovery for NiFiIngest ZIPs
            # --------------------------------------------------------------
            if not matches:
                matches = sorted(
                    p
                    for p in zip_folder.iterdir()
                    if (
                        p.is_file()
                        and p.suffix.lower() == ".zip"
                        and "nifi" in p.name.lower()
                        and "ingest" in p.name.lower()
                    )
                )

            if not matches:
                self._send_json(
                    404,
                    {
                        "ok": False,
                        "error": (
                            f"No ZIP matching {pattern} "
                            f"under {zip_folder}"
                        ),
                        "source": str(source_dir),
                        "target": str(target_dir),
                    },
                )
                return

            zip_file = matches[0]

        # ------------------------------------------------------------------
        # Verify ZIP
        # ------------------------------------------------------------------
        if not zip_file.is_file():
            self._send_json(
                404,
                {
                    "ok": False,
                    "error": (
                        f"ZIP file does not exist: "
                        f"{zip_file}"
                    ),
                    "source": str(source_dir),
                    "target": str(target_dir),
                },
            )
            return

        # ------------------------------------------------------------------
        # Extract NAR files
        # ------------------------------------------------------------------
        extracted = []
        extraction_errors = []

        try:
            with zipfile.ZipFile(
                zip_file,
                "r",
            ) as zf:

                for info in zf.infolist():

                    if info.is_dir():
                        continue

                    # Only extract NAR files.
                    if not info.filename.lower().endswith(
                        ".nar"
                    ):
                        continue

                    # Flatten ZIP directories.
                    #
                    # Example:
                    #
                    #   foo/bar/NiFiIngest-xxx.nar
                    #
                    # becomes:
                    #
                    #   nifi-connectors/NiFiIngest-xxx.nar
                    #
                    name = Path(
                        info.filename
                    ).name

                    if not name:
                        continue

                    destination = (
                        source_dir / name
                    )

                    try:
                        with zf.open(
                            info,
                            "r",
                        ) as src:

                            with open(
                                destination,
                                "wb",
                            ) as dst:

                                shutil.copyfileobj(
                                    src,
                                    dst,
                                )
                        _chown_path(destination)

                        extracted.append(
                            name
                        )

                    except Exception as e:
                        extraction_errors.append(
                            f"{name}: {e}"
                        )

        except zipfile.BadZipFile as e:
            self._send_json(
                400,
                {
                    "ok": False,
                    "error": f"Bad ZIP: {e}",
                    "zip": str(zip_file),
                    "source": str(source_dir),
                    "target": str(target_dir),
                },
            )
            return

        except Exception as e:
            self._send_json(
                500,
                {
                    "ok": False,
                    "error": (
                        f"Failed to extract NAR files: {e}"
                    ),
                    "zip": str(zip_file),
                    "source": str(source_dir),
                    "target": str(target_dir),
                },
            )
            return

        # ------------------------------------------------------------------
        # Copy extracted NAR files to NiFi extensions
        # ------------------------------------------------------------------
        copied = []
        copy_errors = []

        for name in extracted:

            source_file = (
                source_dir / name
            )

            target_file = (
                target_dir / name
            )

            try:

                if not source_file.is_file():
                    copy_errors.append(
                        f"{name}: source file not found"
                    )
                    continue

                already_there = False
                try:
                    if source_file.resolve() == target_file.resolve():
                        already_there = True
                    elif target_file.exists() and source_file.samefile(target_file):
                        already_there = True
                except (OSError, ValueError):
                    pass
                if not already_there:
                    shutil.copy2(source_file, target_file)
                    _chown_path(source_file)
                    _chown_path(target_file)

                copied.append(
                    name
                )

            except shutil.SameFileError:
                copied.append(name)

            except PermissionError as e:
                copy_errors.append(
                    f"{name}: permission denied: {e}"
                )

            except OSError as e:
                copy_errors.append(
                    f"{name}: OS error: {e}"
                )

            except Exception as e:
                copy_errors.append(
                    f"{name}: {e}"
                )

        # ------------------------------------------------------------------
        # Log results
        # ------------------------------------------------------------------
        print(
            f"  NiFiIngest ZIP : {zip_file}"
        )

        print(
            f"  NAR source     : {source_dir}"
        )

        print(
            f"  NAR target     : {target_dir}"
        )

        print(
            f"  Extracted      : {len(extracted)}"
        )

        print(
            f"  Copied         : {len(copied)}"
        )

        if extraction_errors:
            print(
                f"  Extraction errors: "
                f"{len(extraction_errors)}"
            )

        if copy_errors:
            print(
                f"  Copy errors: "
                f"{len(copy_errors)}"
            )

        # ------------------------------------------------------------------
        # Determine result
        # ------------------------------------------------------------------
        if not extracted:
            self._send_json(
                404,
                {
                    "ok": False,
                    "error": (
                        "No .nar files were found "
                        "in the ZIP"
                    ),
                    "zip": str(zip_file),
                    "zipName": zip_file.name,
                    "source": str(source_dir),
                    "target": str(target_dir),
                    "extracted": [],
                    "copied": [],
                    "extractionErrors": extraction_errors,
                    "copyErrors": copy_errors,
                    "extractCount": 0,
                    "copyCount": 0,
                    "count": 0,
                },
            )
            return

        ok = (
            len(extraction_errors) == 0
            and len(copy_errors) == 0
            and len(copied) == len(extracted)
        )

        # ------------------------------------------------------------------
        # Return result
        # ------------------------------------------------------------------
        self._send_json(
            200 if ok else 500,
            {
                "ok": ok,

                "zip": str(zip_file),
                "zipName": zip_file.name,

                "setupPath": str(setup_path),

                "source": str(source_dir),
                "dest": str(source_dir),

                "target": str(target_dir),

                "extracted": extracted,
                "copied": copied,

                "extractionErrors": extraction_errors,
                "copyErrors": copy_errors,

                "extractCount": len(extracted),
                "copyCount": len(copied),

                # Backward compatibility with existing UI.
                "count": len(extracted),
            },
        )

    # ----------------------------------------------------------------
    # Core JSON response helper
    #
    # NOTE: this method (and everything else below it, down to
    # _handle_get_replacements) was previously missing entirely from
    # this file — every request that reached one of these handlers,
    # or that called self._send_json(), crashed with an unhandled
    # AttributeError, which the browser sees as ERR_EMPTY_RESPONSE.
    # ----------------------------------------------------------------
    def _send_json(self, code: int, obj: dict) -> None:
        # Persist API errors to the server log file (and stderr)
        try:
            is_error = code >= 400 or (isinstance(obj, dict) and obj.get("ok") is False)
            if is_error:
                err = ""
                if isinstance(obj, dict):
                    err = str(obj.get("error") or obj.get("detail") or "")
                path = getattr(self, "path", "") or ""
                append_dashboard_log(
                    "ERROR" if code >= 500 else "WARN",
                    f"HTTP {code} {path}" + (f" — {err}" if err else ""),
                    client=getattr(self, "client_address", ("?",))[0],
                    body=obj if isinstance(obj, dict) else {"raw": str(obj)},
                )
        except Exception:
            pass
        body = json.dumps(obj).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass

    def _read_json_body(self, max_size: int = 5_000_000) -> dict:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length <= 0 or length > max_size:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return {}

    def _handle_save_config(self) -> None:
        """
        Body is the raw config JSON object itself (not wrapped).

        All dashboard fields are expected in the payload. Nested maps such as
        Ports / NiFi / Find / BrowserUrls are replaced from the payload so the
        UI is the source of truth. Unknown top-level keys that the UI does not
        edit are preserved from the existing file when present.
        """
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length <= 0 or length > 5_000_000:
            self._send_json(400, {"ok": False, "error": "Invalid body size"})
            return
        try:
            raw = self.rfile.read(length).decode("utf-8")
            data = json.loads(raw)
        except Exception as e:
            self._send_json(400, {"ok": False, "error": f"Invalid JSON: {e}"})
            return

        if not isinstance(data, dict):
            self._send_json(400, {"ok": False, "error": "Config body must be a JSON object"})
            return

        target = CONFIG_PATH
        if target is None:
            self._send_json(500, {"ok": False, "error": "Config path could not be resolved"})
            return

        # Preserve non-UI top-level keys from the existing file, then overlay
        # every key the dashboard sent (UI wins for edited fields).
        existing = {}
        try:
            if target.is_file():
                with open(target, encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    existing = loaded
        except Exception:
            existing = {}

        # Keys the dashboard owns completely (replace, do not deep-merge lists/maps
        # so deletions in the UI stick).
        ui_owned = {
            "BasePath", "IndexPath", "SetupPath", "ZipPath",
            "ThisHost", "LicenseHost", "LicensePort", "LicenseKeyPath",
            "EnvironmentType", "InstallService", "StartMode",
            "Components", "Ports", "BrowserUrls", "NiFi", "Find",
            "ServiceInstallArgsTemplate", "ServiceUninstallArgsTemplate",
            "CleanupLeftoverServices",
        }
        merged = dict(existing)
        for key, value in data.items():
            merged[key] = value
        # Ensure every ui_owned key present in the payload is written even if
        # value is empty string / empty list (explicit clear from UI).
        for key in ui_owned:
            if key in data:
                merged[key] = data[key]

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "w", encoding="utf-8") as f:
                json.dump(merged, f, indent=2, ensure_ascii=False)
                f.write("\n")
            # Dashboard often runs under sudo from the main menu; keep
            # my-config.json (and its parent config/ dir) owned by the
            # invoking user rather than root.
            _chown_path(target.parent)
            _chown_path(target)
        except Exception as e:
            self._send_json(500, {"ok": False, "error": f"Write failed: {e}"})
            return

        self._send_json(
            200,
            {
                "ok": True,
                "configPath": str(target),
                "keysWritten": sorted(merged.keys()),
            },
        )

    def _handle_upload_zip(self) -> None:
        """Raw binary upload: headers carry filename / target folder."""
        length = int(self.headers.get("Content-Length", "0") or 0)
        filename = (self.headers.get("X-Filename") or "").strip()
        zip_path = (self.headers.get("X-Zip-Path") or "").strip()

        if not zip_path:
            self._send_json(400, {"ok": False, "error": "Missing X-Zip-Path"})
            return
        if not filename:
            self._send_json(400, {"ok": False, "error": "Missing X-Filename"})
            return
        if not filename.lower().endswith(".zip"):
            self._send_json(400, {"ok": False, "error": "Only .zip files are allowed"})
            return
        if length <= 0:
            self._send_json(400, {"ok": False, "error": "Empty upload"})
            return

        dest_dir = Path(zip_path)
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            self._send_json(500, {"ok": False, "error": f"Cannot create ZipPath folder: {e}"})
            return

        dest_file = dest_dir / Path(filename).name
        try:
            remaining = length
            chunk_size = 1024 * 1024
            with open(dest_file, "wb") as out:
                while remaining > 0:
                    chunk = self.rfile.read(min(chunk_size, remaining))
                    if not chunk:
                        break
                    out.write(chunk)
                    remaining -= len(chunk)
        except Exception as e:
            self._send_json(500, {"ok": False, "error": f"Upload write failed: {e}"})
            return

        self._send_json(200, {"ok": True, "filename": dest_file.name, "path": str(dest_file)})


    def _handle_list_dir(self) -> None:
        """
        Web path browser API — lists directories (and optionally files) on the
        *server* filesystem. Works from any client without a native GUI.

        Request JSON:
          {
            "path": "/optional/start",
            "mode": "folder" | "file",          # default folder
            "extensions": [".zip", ".dat"]      # only when mode=file; empty = all files
          }
        """
        body = self._read_json_body()
        raw = str(body.get("path") or "").strip()
        mode = str(body.get("mode") or "folder").strip().lower()
        if mode not in ("folder", "file"):
            mode = "folder"
        exts_raw = body.get("extensions") or []
        if isinstance(exts_raw, str):
            exts_raw = [exts_raw]
        extensions = []
        for e in exts_raw:
            e = str(e).strip().lower()
            if not e:
                continue
            if not e.startswith("."):
                e = "." + e
            extensions.append(e)

        if raw:
            try:
                cur = Path(raw).expanduser()
            except Exception:
                cur = Path.home()
        else:
            cur = Path.home()

        # If a file path was passed (e.g. LicenseKeyPath), start in its parent
        try:
            if cur.is_file():
                cur = cur.parent
        except Exception:
            pass

        try:
            probe = cur
            while probe is not None and not probe.is_dir():
                parent = probe.parent
                probe = parent if parent != probe else None
            cur = probe if probe is not None else Path.home()
            cur = cur.resolve()
        except Exception:
            cur = Path.home()

        parent = None
        try:
            if cur.parent != cur:
                parent = cur.parent.as_posix() if not sys.platform.startswith("win") else str(cur.parent)
        except Exception:
            parent = None

        roots = []
        if sys.platform.startswith("win"):
            import string
            for letter in string.ascii_uppercase:
                drive = Path(f"{letter}:/")
                if drive.exists():
                    roots.append({"name": f"{letter}:", "path": str(drive)})
        else:
            for r in ["/", str(Path.home()), "/opt", "/home", "/var", "/mnt", "/media"]:
                p = Path(r)
                if p.is_dir():
                    roots.append({"name": r, "path": p.as_posix()})

        entries = []
        try:
            children = sorted(cur.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
            for child in children:
                try:
                    is_dir = child.is_dir()
                    if is_dir:
                        path_str = child.as_posix() if not sys.platform.startswith("win") else str(child)
                        entries.append({"name": child.name, "path": path_str, "is_dir": True})
                    elif mode == "file":
                        if extensions and child.suffix.lower() not in extensions:
                            continue
                        path_str = child.as_posix() if not sys.platform.startswith("win") else str(child)
                        try:
                            size = child.stat().st_size
                        except OSError:
                            size = 0
                        entries.append({
                            "name": child.name,
                            "path": path_str,
                            "is_dir": False,
                            "size": size,
                        })
                except (PermissionError, OSError):
                    continue
        except PermissionError as e:
            path_out = cur.as_posix() if not sys.platform.startswith("win") else str(cur)
            self._send_json(200, {
                "ok": False, "error": f"Permission denied: {e}",
                "path": path_out, "parent": parent, "roots": roots,
                "entries": [], "mode": mode,
            })
            return
        except Exception as e:
            self._send_json(200, {
                "ok": False, "error": str(e),
                "path": str(cur), "parent": parent, "roots": roots,
                "entries": [], "mode": mode,
            })
            return

        path_out = cur.as_posix() if not sys.platform.startswith("win") else str(cur)
        self._send_json(200, {
            "ok": True, "path": path_out, "parent": parent,
            "roots": roots, "entries": entries, "mode": mode,
        })


    def _handle_client_log(self) -> None:
        """
        Accept browser-side error reports and write them to the server log file.
        Body JSON: { "level": "error"|"warn"|"info", "message": "...", "context": {...} }
        """
        body = self._read_json_body()
        level = str(body.get("level") or "error").upper()
        if level not in ("ERROR", "WARN", "WARNING", "INFO", "DEBUG"):
            level = "ERROR"
        if level == "WARNING":
            level = "WARN"
        message = str(body.get("message") or body.get("error") or "").strip() or "(no message)"
        context = body.get("context") if isinstance(body.get("context"), dict) else {}
        try:
            context = dict(context)
            context["source"] = "browser"
            context["client"] = self.client_address[0] if self.client_address else "?"
            context["userAgent"] = (self.headers.get("User-Agent") or "")[:180]
        except Exception:
            context = {"source": "browser"}
        append_dashboard_log(level, message, **context)
        self._send_json(200, {
            "ok": True,
            "logged": True,
            "logPath": str(LOG_PATH or _resolve_log_path()),
        })

    def _handle_copy_server_file(self) -> None:
        """
        Copy a file that already exists on the server into ZipPath (or any dest dir).
        Used by the web file browser when picking a ZIP package.

        Request: { "source": "/path/to/file.zip", "destDir": "/opt/zip-folder" }

        Common 500 causes:
          - ZipPath under /opt/... and the dashboard is not running as root
            (Permission denied on mkdir or copy)
          - Destination filesystem is read-only / out of disk space
        """
        body = self._read_json_body()
        source = str(body.get("source") or "").strip()
        dest_dir = str(body.get("destDir") or body.get("zipPath") or "").strip()
        if not source:
            self._send_json(400, {"ok": False, "error": "Missing source path"})
            return
        if not dest_dir:
            self._send_json(400, {"ok": False, "error": "Missing ZipPath / destDir — set ZipPath first"})
            return
        src = Path(source).expanduser()
        if not src.is_file():
            self._send_json(400, {"ok": False, "error": f"Source file not found: {source}"})
            return
        dest = Path(dest_dir).expanduser()
        try:
            dest.mkdir(parents=True, exist_ok=True)
        except PermissionError as e:
            self._send_json(500, {
                "ok": False,
                "error": (
                    f"Permission denied creating ZipPath '{dest}'. "
                    f"Run the dashboard with write access to that folder "
                    f"(e.g. sudo, or choose a path under your home directory). "
                    f"Detail: {e}"
                ),
            })
            return
        except Exception as e:
            self._send_json(500, {"ok": False, "error": f"Cannot create destination '{dest}': {e}"})
            return
        dest_file = dest / src.name
        try:
            # When the web file browser starts at ZipPath the user often
            # selects a ZIP that is *already* in destDir.  shutil.copy2
            # raises SameFileError in that case — treat it as success.
            already_there = False
            try:
                if src.resolve() == dest_file.resolve():
                    already_there = True
                elif dest_file.exists() and src.samefile(dest_file):
                    already_there = True
            except (OSError, ValueError):
                pass
            if not already_there:
                shutil.copy2(src, dest_file)
                _chown_path(dest_file)
        except shutil.SameFileError:
            # Race / symlink edge case: still count as success
            pass
        except PermissionError as e:
            self._send_json(500, {
                "ok": False,
                "error": (
                    f"Permission denied writing '{dest_file}'. "
                    f"The dashboard process cannot write into ZipPath. "
                    f"Detail: {e}"
                ),
            })
            return
        except Exception as e:
            self._send_json(500, {"ok": False, "error": f"Copy failed: {e}"})
            return
        path_out = dest_file.as_posix() if not sys.platform.startswith("win") else str(dest_file)
        self._send_json(200, {
            "ok": True,
            "filename": dest_file.name,
            "path": path_out,
        })

    @_dialog_lock
    def _handle_pick_folder(self) -> None:
        """
        Open a native folder-browser dialog and return the chosen folder
        path, for filling in one of the "Paths & identity" folder fields
        (BasePath, IndexPath, SetupPath, ZipPath).

        On Linux uses zenity / kdialog / tkinter; on Windows PowerShell /
        tkinter. Request body: { "currentPath": "<optional starting folder>" }
        """
        body = self._read_json_body()
        current = str(body.get("currentPath") or "").strip()

        start_dir = Path(current) if current else None
        if start_dir is not None:
            probe = start_dir
            while probe is not None and not probe.is_dir():
                parent = probe.parent
                probe = parent if parent != probe else None
            start_dir = probe

        initial_dir = str(start_dir.resolve()) if start_dir else str(Path.home())

        chosen, dialog_completed, errors = _pick_native_path(
            mode="folder",
            initial_dir=initial_dir,
            title="Select folder",
        )

        if not chosen:
            self._send_json(200, {
                "ok": False, "cancelled": True,
                "error": "; ".join(errors) if errors else "No folder selected",
            })
            return

        # Always return POSIX-style path on non-Windows for consistency
        path_out = str(Path(chosen))
        if not sys.platform.startswith("win"):
            path_out = Path(chosen).as_posix()

        self._send_json(200, {"ok": True, "cancelled": False, "path": path_out})

    @_dialog_lock
    def _handle_pick_zip(self) -> None:
        """
        Open a native file dialog filtered to *.zip and copy the selected
        package into ZipPath. Mirrors _handle_pick_nar's dialog behavior.
        """
        zip_hdr = (self.headers.get("X-Zip-Path") or "").strip()
        body = self._read_json_body()
        if not zip_hdr:
            zip_hdr = str(body.get("zipPath") or "").strip()
        if not zip_hdr:
            self._send_json(400, {"ok": False, "cancelled": False, "error": "Missing ZipPath"})
            return

        dest_dir = Path(zip_hdr)
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            self._send_json(500, {"ok": False, "cancelled": False, "error": f"Cannot create ZipPath folder: {e}"})
            return

        initial_dir = str(dest_dir.resolve()) if dest_dir.is_dir() else str(Path.home())
        chosen, dialog_completed, errors = _pick_native_path(
            mode="file",
            initial_dir=initial_dir,
            title="Select ZIP package",
            filetypes=[("ZIP packages", "*.zip"), ("All files", "*.*")],
        )

        if not chosen:
            self._send_json(200, {
                "ok": False, "cancelled": True,
                "error": "; ".join(errors) if errors else "No file selected",
            })
            return

        src = Path(chosen)
        if not src.is_file():
            self._send_json(400, {"ok": False, "cancelled": False, "error": f"File not found: {chosen}"})
            return
        if src.suffix.lower() != ".zip":
            self._send_json(400, {"ok": False, "cancelled": False, "error": "Only .zip files are allowed"})
            return

        dest_file = dest_dir / src.name
        try:
            already_there = False
            try:
                if src.resolve() == dest_file.resolve():
                    already_there = True
                elif dest_file.exists() and src.samefile(dest_file):
                    already_there = True
            except (OSError, ValueError):
                pass
            if not already_there:
                shutil.copy2(src, dest_file)
                _chown_path(dest_file)
        except shutil.SameFileError:
            pass
        except Exception as e:
            self._send_json(500, {"ok": False, "cancelled": False, "error": f"Copy failed: {e}"})
            return

        self._send_json(200, {
            "ok": True, "cancelled": False,
            "source": str(src), "path": str(dest_file), "filename": dest_file.name,
        })

    def _handle_delete_nar(self) -> None:
        body = self._read_json_body()
        ext = str(body.get("extensionsPath") or "").strip()
        filename = str(body.get("filename") or "").strip()
        if not ext or not filename:
            self._send_json(400, {"ok": False, "error": "Missing extensionsPath or filename"})
            return
        target = Path(ext) / Path(filename).name
        try:
            if target.is_file():
                target.unlink()
            self._send_json(200, {"ok": True})
        except Exception as e:
            self._send_json(500, {"ok": False, "error": f"Delete failed: {e}"})

    def _handle_copy_nars(self) -> None:
        body = self._read_json_body()
        src = _as_path(body.get("sourcePath"))
        tgt = _as_path(body.get("targetPath"))
        if not str(src) or not str(tgt):
            self._send_json(400, {"ok": False, "error": "Missing sourcePath or targetPath"})
            return
        if not src.is_dir():
            self._send_json(400, {"ok": False, "error": f"Source folder not found: {src}"})
            return
        try:
            tgt.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            self._send_json(500, {"ok": False, "error": f"Cannot create target folder: {e}"})
            return

        copied = []
        errors = []
        for p in sorted(src.glob("*.nar")):
            try:
                dest = tgt / p.name
                already_there = False
                try:
                    if p.resolve() == dest.resolve():
                        already_there = True
                    elif dest.exists() and p.samefile(dest):
                        already_there = True
                except (OSError, ValueError):
                    pass
                if not already_there:
                    shutil.copy2(p, dest)
                    _chown_path(dest)
                copied.append(p.name)
            except shutil.SameFileError:
                copied.append(p.name)
            except Exception as e:
                errors.append(f"{p.name}: {e}")

        self._send_json(200, {"ok": True, "count": len(copied), "copied": copied, "errors": errors})

    def _handle_open_urls(self) -> None:
        body = self._read_json_body()
        urls = body.get("urls") or []
        if not isinstance(urls, list) or not urls:
            self._send_json(400, {"ok": False, "error": "No URLs provided"})
            return
        opened = 0
        for u in urls:
            try:
                webbrowser.open_new_tab(str(u))
                opened += 1
            except Exception:
                pass
        self._send_json(200, {"ok": True, "count": opened})

    def _handle_shutdown(self) -> None:
        self._send_json(200, {"ok": True})

        def _stop():
            time.sleep(0.3)
            try:
                if HTTP_SERVER:
                    HTTP_SERVER.shutdown()
            except Exception:
                pass

        threading.Thread(target=_stop, daemon=True).start()

    # ----------------------------------------------------------------
    # Custom replacements (tools/replacements.json)
    # ----------------------------------------------------------------
    def _replacements_path(self) -> Path:
        root = SETUP_ROOT
        if root is None and CONFIG_PATH is not None:
            root = CONFIG_PATH.parent.parent
        if root is None:
            root = Path(__file__).resolve().parents[2]
        return root / "tools" / "replacements.json"

    def _load_replacements_entries(self) -> list:
        p = self._replacements_path()
        try:
            if p.is_file():
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        return data
        except Exception:
            pass
        return []

    @staticmethod
    def _section_body_span(text: str, section: str):
        """Return (start, end) character offsets of the body of [section], or None.

        Body starts after the section header line and ends at the next [Header]
        or EOF. Mirrors Update-ConfigFiles / update-configfiles.sh section scoping.
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

    def _validate_entries(self, entries: list) -> tuple:
        root = SETUP_ROOT
        if root is None and CONFIG_PATH is not None:
            root = CONFIG_PATH.parent.parent
        validation = []
        warning_count = 0
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            rel_file = str(entry.get("file") or "")
            reps = entry.get("replacements") or []
            full_path = (root / rel_file) if root else Path(rel_file)
            file_exists = full_path.is_file()
            text = ""
            if file_exists:
                try:
                    text = full_path.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    text = ""
            v_reps = []
            for rep in reps:
                frm = str(rep.get("from") or "")
                to = str(rep.get("to") or "")
                section = str(rep.get("section") or "").strip() or None
                found = False
                if file_exists and frm:
                    if section:
                        span = self._section_body_span(text, section)
                        if span is not None:
                            found = frm in text[span[0]:span[1]]
                    else:
                        found = frm in text
                if not found:
                    warning_count += 1
                v_reps.append({"from": frm, "to": to, "section": section, "found": found})
            validation.append({"file": rel_file, "fileExists": file_exists, "replacements": v_reps})
        return validation, warning_count

    def _handle_get_replacements(self) -> None:
        entries = self._load_replacements_entries()
        validation, warning_count = self._validate_entries(entries)
        self._send_json(200, {
            "ok": True,
            "entries": entries,
            "validation": validation,
            "warningCount": warning_count,
            "path": str(self._replacements_path()),
        })

    def _handle_save_replacements(self) -> None:
        body = self._read_json_body()
        entries = body.get("entries")
        if not isinstance(entries, list):
            self._send_json(400, {"ok": False, "error": "entries must be a list"})
            return
        p = self._replacements_path()
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                json.dump(entries, f, indent=2)
                f.write("\n")
        except Exception as e:
            self._send_json(500, {"ok": False, "error": f"Save failed: {e}"})
            return
        self._send_json(200, {"ok": True, "count": len(entries), "path": str(p)})

    def _handle_validate_replacements(self) -> None:
        body = self._read_json_body()
        entries = body.get("entries") or []
        if not isinstance(entries, list):
            self._send_json(400, {"ok": False, "error": "entries must be a list"})
            return
        validation, warning_count = self._validate_entries(entries)
        self._send_json(200, {"ok": True, "validation": validation, "warningCount": warning_count})

    def _handle_apply_replacements(self) -> None:
        entries = self._load_replacements_entries()
        root = SETUP_ROOT
        if root is None and CONFIG_PATH is not None:
            root = CONFIG_PATH.parent.parent
        applied = 0
        rep_hits = 0
        details = []
        errors = []
        entries_changed = False
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            rel_file = str(entry.get("file") or "")
            reps = entry.get("replacements") or []
            full_path = (root / rel_file) if root else Path(rel_file)
            if not full_path.is_file():
                errors.append(f"{rel_file}: not found")
                continue
            try:
                original = full_path.read_text(encoding="utf-8", errors="ignore")
                text = original
                file_hits = 0
                for rep in reps:
                    frm = str(rep.get("from") or "")
                    to = str(rep.get("to") or "")
                    section = str(rep.get("section") or "").strip() or None
                    if not frm or frm == to:
                        # Nothing to change (empty from, or already at target).
                        continue
                    if section:
                        # Restrict substitution to the named INI [Section] body
                        # (same rules as tools/update-configfiles.sh).
                        span = self._section_body_span(text, section)
                        if span is None:
                            continue  # section missing → skip quietly
                        body = text[span[0]:span[1]]
                        if frm not in body:
                            continue
                        new_body = body.replace(frm, to)
                        text = text[:span[0]] + new_body + text[span[1]:]
                        file_hits += 1
                    else:
                        if frm not in text:
                            continue
                        text = text.replace(frm, to)
                        file_hits += 1
                    # Advance the anchor to what's now actually in the
                    # file. Without this, "from" stays pinned to the
                    # old text forever — the next time a port changes
                    # and you Sync + Apply again, "from" no longer
                    # matches anything in the file, so this row gets
                    # silently skipped and the .cfg file never picks
                    # up the new value.
                    rep["from"] = to
                    entries_changed = True
                if text != original:
                    full_path.write_text(text, encoding="utf-8")
                    applied += 1
                    rep_hits += file_hits
                    details.append({"file": rel_file, "replacements": file_hits})
            except Exception as e:
                errors.append(f"{rel_file}: {e}")

        if entries_changed:
            try:
                p = self._replacements_path()
                with open(p, "w", encoding="utf-8") as f:
                    json.dump(entries, f, indent=2)
                    f.write("\n")
            except Exception as e:
                errors.append(f"Could not persist updated 'from' anchors: {e}")

        self._send_json(200 if not errors else 207, {
            "ok": len(errors) == 0,
            "count": applied,
            "replacementCount": rep_hits,
            "details": details,
            "setupRoot": str(root) if root else None,
            "errors": errors,
        })


def open_browser(url: str, delay: float = 0.6) -> None:
    time.sleep(delay)
    try:
        webbrowser.open(url)
    except Exception as e:
        print(f"Could not open browser automatically: {e}")
        print(f"Open this URL manually: {url}")


def main() -> int:
    global CONFIG_PATH, SETUP_ROOT, SERVE_DIR, HTTP_SERVER

    parser = argparse.ArgumentParser(description="KD Config Dashboard server")
    parser.add_argument("--port", "-p", type=int, default=DEFAULT_PORT)
    parser.add_argument("--host", default=HOST)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument(
        "--setup-root",
        type=Path,
        default=None,
        help="KD setup root (contains config/). Example: /opt/kd-setup/idol-linux-setup",
    )
    parser.add_argument(
        "--config-path",
        type=Path,
        default=None,
        help="Full path to my-config.json (default: <setup-root>/config/my-config.json)",
    )
    args = parser.parse_args()

    SERVE_DIR = Path(__file__).resolve().parent
    dashboard = SERVE_DIR / DASHBOARD
    if not dashboard.is_file():
        print(f"ERROR: {DASHBOARD} not found in {SERVE_DIR}")
        return 1

    SETUP_ROOT = args.setup_root.resolve() if args.setup_root else None
    CONFIG_PATH = resolve_config_path(SETUP_ROOT, args.config_path)

    if SETUP_ROOT is None:
        # Infer setup root from config path parent/parent
        try:
            if CONFIG_PATH.parent.name.lower() == "config":
                SETUP_ROOT = CONFIG_PATH.parent.parent
        except Exception:
            pass

    global LOG_PATH
    LOG_PATH = _resolve_log_path()
    append_dashboard_log("INFO", f"Dashboard starting — log file: {LOG_PATH}")

    try:
        server = ThreadingHTTPServer((args.host, args.port), KDHandler)
        HTTP_SERVER = server
    except OSError as e:
        print(f"ERROR: cannot bind {args.host}:{args.port} — {e}")
        return 1

    # Prefer a browsable local URL even when binding 0.0.0.0 for external access
    bind_host = args.host
    browse_host = "127.0.0.1" if bind_host in ("0.0.0.0", "::", "") else bind_host
    url = f"http://{browse_host}:{args.port}/{DASHBOARD}"
    print()
    print("=" * 64)
    print("  KD Configuration Dashboard")
    print("=" * 64)
    print(f"  Serving  : {SERVE_DIR}")
    print(f"  Bind     : {bind_host}:{args.port}")
    print(f"  URL      : {url}")
    if bind_host in ("0.0.0.0", "::"):
        print(f"  External : http://<this-host-ip>:{args.port}/{DASHBOARD}")
    print(f"  Export   : {CONFIG_PATH}")
    if SETUP_ROOT:
        print(f"  Setup    : {SETUP_ROOT}")
    print("-" * 64)
    print("  Press Ctrl+C to stop, or use the Exit button in the UI.")
    print("=" * 64)
    print()

    if not args.no_browser:
        threading.Thread(target=open_browser, args=(url,), daemon=True).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down…")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())