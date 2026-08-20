"""
Structured logging with console + file output.
Uses rich when available for colored, readable output; falls back to plain print.

Long detail strings (paths, multi-part status) are split across lines so the
installer console stays readable instead of wrapping mid-path.
"""

from __future__ import annotations

import logging
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional

_log_file: Optional[Path] = None
_min_level = logging.INFO
_use_rich = False

try:
    from rich.console import Console
    from rich.logging import RichHandler
    from rich.theme import Theme

    _console = Console(
        theme=Theme(
            {
                "info": "cyan",
                "success": "bold green",
                "warning": "bold #FF8C00",  # true orange
                "error": "bold red",
                "debug": "dim",
            }
        )
    )
    _use_rich = True
except ImportError:
    _console = None


def chown_to_invoking_user(path: Path, *, recursive: bool = True) -> None:
    """
    Set ownership of path (file or directory) to the user who invoked the
    toolkit. When running under sudo, prefer SUDO_USER so BasePath / logs /
    extracted trees are not root-owned.

    Best-effort: ignores errors when chown is not permitted (e.g. non-root
    process, or files still held by a service).
    """
    try:
        import os
        import pwd
        import grp
        sudo_user = (os.environ.get("SUDO_USER") or "").strip()
        if sudo_user and sudo_user != "root":
            pw = pwd.getpwnam(sudo_user)
            uid, gid = pw.pw_uid, pw.pw_gid
        else:
            uid, gid = os.getuid(), os.getgid()
            # If we are root without SUDO_USER, leave as-is
            if uid == 0:
                return
        target = Path(path)
        if not target.exists():
            return
        os.chown(target, uid, gid)
        if recursive and target.is_dir():
            for child in target.rglob("*"):
                try:
                    os.chown(child, uid, gid)
                except Exception:
                    pass
    except Exception:
        pass


# Backwards-compatible private alias
_chown_to_invoking_user = chown_to_invoking_user


def _escape_markup(msg: str) -> str:
    """
    Escape Rich markup in log messages so paths like
    [/opt/KnowledgeDiscovery/NiFi/conf/login-identity-providers.xml]
    are not interpreted as style tags.
    """
    if not msg or not _use_rich:
        return msg
    try:
        from rich.markup import escape
        return escape(str(msg))
    except Exception:
        return str(msg).replace("[", "\\[").replace("]", "\\]")



def _split_detail(detail: str) -> List[str]:
    """
    Break a long detail string into readable lines.
    Splits on '; ' first (common for multi-part status), then soft-wraps
    very long remaining segments at path-friendly boundaries.
    """
    if not detail:
        return []
    # Prefer semantic breaks
    parts = [p.strip() for p in re.split(r";\s*", detail) if p.strip()]
    lines: List[str] = []
    for part in parts:
        if len(part) <= 100:
            lines.append(part)
            continue
        # Soft-wrap long paths / sentences at backslash or space near 90 chars
        remaining = part
        while len(remaining) > 100:
            cut = -1
            window = remaining[:100]
            for sep in ("\\", "/", " ", "-"):
                idx = window.rfind(sep)
                if idx > 40:
                    cut = idx + (0 if sep in (" ",) else 1)
                    break
            if cut <= 0:
                cut = 100
            lines.append(remaining[:cut].rstrip())
            remaining = remaining[cut:].lstrip()
        if remaining:
            lines.append(remaining)
    return lines


class KDLogger:
    """Thin wrapper around stdlib logging + optional rich."""

    def __init__(self) -> None:
        self.logger = logging.getLogger("kd-installer")
        self.logger.setLevel(logging.DEBUG)
        self.logger.handlers.clear()
        self.logger.propagate = False

    def initialize(self, log_directory: str | Path, min_level: str = "INFO") -> Path:
        global _log_file, _min_level
        log_dir = Path(log_directory)
        log_dir.mkdir(parents=True, exist_ok=True)
        # Prefer the invoking user (SUDO_USER) so logs are not root-owned
        _chown_to_invoking_user(log_dir)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        _log_file = log_dir / f"kd-install-{stamp}.log"
        _min_level = getattr(logging, min_level.upper(), logging.INFO)

        # File handler (always plain)
        # Ensure log file exists then fix ownership (FileHandler creates it)
        _log_file.touch(exist_ok=True)
        _chown_to_invoking_user(_log_file)
        fh = logging.FileHandler(_log_file, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(
            logging.Formatter(
                "[%(asctime)s] [%(levelname)s] %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        self.logger.addHandler(fh)

        # Console handler
        if _use_rich:
            rh = RichHandler(
                console=_console,
                show_time=True,
                show_path=False,
                rich_tracebacks=True,
                markup=True,
            )
            rh.setLevel(_min_level)
            self.logger.addHandler(rh)
        else:
            ch = logging.StreamHandler(sys.stdout)
            ch.setLevel(_min_level)
            ch.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
            self.logger.addHandler(ch)

        self.info(f"Log file: {_log_file}")
        return _log_file

    def _before_log(self) -> None:
        """End any live status line so the next log row starts on a new line."""
        try:
            status_line_end()
        except Exception:
            pass

    def debug(self, msg: str) -> None:
        self._before_log()
        self.logger.debug(msg)

    def info(self, msg: str) -> None:
        self._before_log()
        # Section / phase headers (e.g. "=== Phase 1: ...") in bold yellow
        if _use_rich and msg.startswith("==="):
            self.logger.info(f"[bold yellow]{_escape_markup(msg)}[/bold yellow]")
        else:
            self.logger.info(_escape_markup(msg))

    def info_orange(self, msg: str) -> None:
        """INFO-level line with the entire message in orange (rich) or plain."""
        self._before_log()
        if _use_rich:
            self.logger.info(f"[bold #FF8C00]{_escape_markup(msg)}[/bold #FF8C00]")
        else:
            self.logger.info(msg)

    def success(self, msg: str) -> None:
        self._before_log()
        if _use_rich:
            self.logger.info(f"[success]{_escape_markup(msg)}[/success]")
        else:
            self.logger.info(f"[OK] {msg}")

    def warn(self, msg: str) -> None:
        self._before_log()
        self.logger.warning(_escape_markup(msg))

    def error(self, msg: str) -> None:
        self._before_log()
        self.logger.error(_escape_markup(msg))

    def step_result(
        self,
        step: str,
        success: bool,
        detail: str = "",
        *,
        warning: bool = False,
    ) -> None:
        """
        Log a step outcome. Long multi-part details are printed on following
        indented lines so paths and status fragments stay readable.

        When ``warning=True`` (soft success / degraded), the headline is
        ``[WARN]`` and the message is emitted in orange via ``warn`` instead
        of green OK or red FAILED.
        """
        if warning:
            status = "WARN"
        elif success:
            status = "OK"
        else:
            status = "FAILED"
        headline = f"[{status}] {step}"
        detail_lines = _split_detail(detail)

        def _emit(msg: str) -> None:
            if warning:
                self.warn(msg)
            elif success:
                self.success(msg)
            else:
                self.error(msg)

        if not detail_lines:
            _emit(headline)
            return

        # Single short detail stays on one line
        if len(detail_lines) == 1 and len(detail_lines[0]) <= 100:
            _emit(f"{headline} - {detail_lines[0]}")
            return

        # Multi-line: headline, then indented detail rows
        _emit(headline)
        for line in detail_lines:
            self.info(f"         {line}")


def format_elapsed(seconds: float) -> str:
    """Format seconds as H:MM:SS or M:SS for console clock display."""
    total = max(0, int(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


# ---------------------------------------------------------------------------
# Single-line live status (overwrite in place with \\r + clear-to-EOL)
# ---------------------------------------------------------------------------
_status_lock = threading.Lock()
_status_width = 0  # last printed width, for clearing
# ANSI helpers for the live status row (raw stdout; avoid rich markup here)
_ANSI_RESET = "\033[0m"
_ANSI_CYAN = "\033[36m"
_ANSI_GREEN = "\033[32m"
_ANSI_YELLOW = "\033[33m"
_ANSI_BOLD = "\033[1m"
_ANSI_DIM = "\033[2m"
_ANSI_MAGENTA = "\033[35m"
_ANSI_CLEAR_EOL = "\033[K"  # clear from cursor to end of line (reliable overwrite)


def _status_supports_color() -> bool:
    """True when the live status stream is an interactive TTY that accepts ANSI."""
    stream = getattr(sys, "__stdout__", None) or sys.stdout
    try:
        return bool(getattr(stream, "isatty", lambda: False)())
    except Exception:
        return False


def status_line(msg: str) -> None:
    """
    Rewrite one console status line in place (carriage return + clear-to-EOL).
    Does not append a newline, so only the time / progress text changes.
    Safe to call from background threads (clock, NiFi wait).

    Uses ``\\r\\033[K`` so the previous content is fully erased even when the
    new message is shorter — this prevents the multi-line “ghost” output that
    appears when plain ``\\r`` + padding is used on some terminals / under sudo.
    """
    global _status_width
    text = (msg or "").replace("\r", " ").replace("\n", " ").rstrip()
    # Prefer raw stdout so rich handlers do not force a new log line
    stream = getattr(sys, "__stdout__", None) or sys.stdout
    with _status_lock:
        try:
            # Clear entire previous content, then write the new text.
            # \033[K is far more reliable than space-padding across terminals.
            stream.write("\r" + _ANSI_CLEAR_EOL + text)
            stream.flush()
            # Track visible width (strip ANSI for length estimate)
            visible = re.sub(r"\033\[[0-9;]*m", "", text)
            _status_width = max(len(visible), _status_width)
        except Exception:
            pass


def status_line_clear() -> None:
    """
    Erase the in-place status line and park the cursor at column 0
    (same line). Prefer status_line_end() before normal log output so
    the next message starts on a new line.
    """
    global _status_width
    stream = getattr(sys, "__stdout__", None) or sys.stdout
    with _status_lock:
        try:
            if _status_width > 0:
                stream.write("\r" + _ANSI_CLEAR_EOL)
                stream.flush()
            _status_width = 0
        except Exception:
            pass


def status_line_end() -> None:
    """
    End the live status line so the next print/log starts on a fresh line.
    Clears residual characters, then writes a newline when a status was active.
    """
    global _status_width
    stream = getattr(sys, "__stdout__", None) or sys.stdout
    with _status_lock:
        try:
            if _status_width > 0:
                stream.write("\r" + _ANSI_CLEAR_EOL + "\n")
                stream.flush()
                _status_width = 0
        except Exception:
            pass


def status_line_finish(final_msg: str = "") -> None:
    """End the live line, then print a normal finished message with newline."""
    status_line_end()
    if final_msg:
        stream = getattr(sys, "__stdout__", None) or sys.stdout
        try:
            stream.write(final_msg.rstrip() + "\n")
            stream.flush()
        except Exception:
            pass




class ElapsedClock:
    """
    Background elapsed-time clock with optional progress-based ETA.

    Usage::

        with ElapsedClock("Install", interval=15, total_units=10) as clock:
            for item in items:
                work(item)
                clock.advance(1)   # or clock.set_progress(done, total)

    Ticks every ``interval`` seconds, e.g.::

        [clock] Install still running... elapsed 2:45 | 3/10 components | ~ETA 6:25 left

    ETA = elapsed * (total/done - 1) once at least one unit is complete.
    """

    def __init__(
        self,
        label: str = "Setup",
        interval: float = 15.0,
        total_units: int = 0,
        unit_label: str = "steps",
    ) -> None:
        self.label = label or "Setup"
        self.interval = max(5.0, float(interval))
        self.unit_label = unit_label or "steps"
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._t0: float = 0.0
        self._lock = threading.Lock()
        self._done: float = 0.0
        self._total: float = float(max(0, total_units))
        self._current_task: str = ""

    def __enter__(self) -> "ElapsedClock":
        self._t0 = time.monotonic()
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name=f"kd-clock-{self.label}",
            daemon=True,
        )
        self._thread.start()
        total_note = (
            f", {int(self._total)} {self.unit_label} planned"
            if self._total > 0
            else ""
        )
        log.info(
            f"  [clock] {self.label} started (clock every {int(self.interval)}s{total_note})"
        )
        return self

    @property
    def elapsed_seconds(self) -> float:
        if not self._t0:
            return 0.0
        return time.monotonic() - self._t0

    def set_total(self, total: int) -> None:
        with self._lock:
            self._total = float(max(0, total))

    def set_progress(self, done: float, total: Optional[float] = None) -> None:
        """Set absolute progress (e.g. components finished / total)."""
        with self._lock:
            self._done = max(0.0, float(done))
            if total is not None:
                self._total = float(max(0, total))

    def advance(self, units: float = 1.0) -> None:
        with self._lock:
            self._done = max(0.0, self._done + float(units))

    def set_task(self, name: str) -> None:
        """Optional label for the current unit of work (shown on ticks)."""
        with self._lock:
            self._current_task = (name or "").strip()

    def eta_seconds(self) -> Optional[float]:
        """
        Estimate seconds remaining from linear progress.
        Needs done > 0 and total > done; returns None if unknown.
        """
        with self._lock:
            done, total = self._done, self._total
        if done <= 0 or total <= 0 or done >= total:
            return 0.0 if total > 0 and done >= total else None
        elapsed = self.elapsed_seconds
        if elapsed < 1.0:
            return None
        # elapsed / done = rate per unit → remaining units * rate
        remaining_units = total - done
        return (elapsed / done) * remaining_units

    @staticmethod
    def _fmt_progress(done: float, total: float) -> str:
        if total <= 0:
            return ""
        pct = min(100.0, (done / total) * 100.0)
        # show integers when values are whole
        if abs(done - round(done)) < 1e-6 and abs(total - round(total)) < 1e-6:
            return f"{int(round(done))}/{int(round(total))} ({pct:.0f}%)"
        return f"{done:.1f}/{total:.0f} ({pct:.0f}%)"

    def _tick_message(self) -> str:
        """
        Build a single-line colored progress / statistics row that reflects
        the current setup status (elapsed, done/total, %, ETA, current task).
        """
        elapsed = self.elapsed_seconds
        with self._lock:
            done, total = self._done, self._total
            task = self._current_task
        use_color = _status_supports_color()
        R = _ANSI_RESET if use_color else ""
        C = _ANSI_CYAN if use_color else ""
        G = _ANSI_GREEN if use_color else ""
        Y = _ANSI_YELLOW if use_color else ""
        B = _ANSI_BOLD if use_color else ""
        D = _ANSI_DIM if use_color else ""
        M = _ANSI_MAGENTA if use_color else ""

        # Header
        parts = [
            f"  {B}{C}[clock]{R} {B}{self.label}{R} still running... "
            f"elapsed {G}{format_elapsed(elapsed)}{R}"
        ]
        if total > 0:
            pct = min(100.0, (done / total) * 100.0) if total else 0.0
            prog = self._fmt_progress(done, total)
            # Color the progress numbers green when advancing, yellow near start
            prog_color = G if done > 0 else Y
            parts.append(f"{prog_color}{prog}{R} {D}{self.unit_label}{R}")
        eta = self.eta_seconds()
        if eta is not None and eta > 0:
            parts.append(f"{Y}~ETA {format_elapsed(eta)} left{R}")
        elif total > 0 and done >= total:
            parts.append(f"{G}ETA complete{R}")
        elif total > 0 and done <= 0:
            parts.append(f"{D}ETA calculating...{R}")
        if task:
            parts.append(f"{M}now: {task}{R}")
        head, *rest = parts
        if not rest:
            return head
        return head + f" {D}|{R} " + f" {D}|{R} ".join(rest)

    def __exit__(self, exc_type, exc, tb) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval + 2)
            self._thread = None
        elapsed = time.monotonic() - self._t0
        status = "failed" if exc_type else "finished"
        with self._lock:
            done, total = self._done, self._total
        progress = ""
        if total > 0:
            progress = f" | {self._fmt_progress(done, total)}"
        # End live line, then one permanent log line on a new row
        status_line_end()
        log.info(
            f"  [clock] {self.label} {status} - total elapsed {format_elapsed(elapsed)}{progress}"
        )

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                # Single updating line (no new log rows every 15s)
                status_line(self._tick_message())
            except Exception:
                pass




# Singleton used by the rest of the package
log = KDLogger()

