import fcntl
import logging
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

from app.plugins.base import BaseSyncPlugin

logger = logging.getLogger("mirrord.plugin.arch_rsync")

# Regex to extract to-chk/ir-chk from rsync --progress lines.
# Example: " (xfr#5, to-chk=169/396)"  →  files_remaining=169, total=396
# Progress = (total - remaining) / total * 100
_PROGRESS_RE = re.compile(r"(?:to|ir)-chk=(\d+)/(\d+)")

# Allowed URL schemes for rsync sources.
_VALID_SOURCE_SCHEMES = ("rsync://", "https://", "http://")


class ArchRsyncPlugin(BaseSyncPlugin):
    plugin_type = "arch_rsync"

    def __init__(self, config: dict):
        super().__init__(config)
        self.target = Path(self.config.get("target", ""))
        self.source_url = self.config.get("source_url", "")
        self.lastupdate_url = self.config.get("lastupdate_url", "")
        self.tls = self.config.get("tls", True)
        self.bwlimit = self.config.get("bwlimit", 0)
        self.excludes = self.config.get("excludes", [])
        self.lock_path = (
            Path(self.config.get("lock_dir", "/tmp/mirrord"))
            / f"{self.config.get('slug', 'arch_rsync')}.lck"
        )
        self._proc_lock = threading.Lock()
        self._validate_config()

    def _validate_config(self) -> None:
        """Validate config values to prevent injection and misconfiguration."""
        name = self.config.get("name", self.plugin_type)

        # Validate source_url scheme
        if self.source_url and not self.source_url.startswith(_VALID_SOURCE_SCHEMES):
            raise ValueError(
                f"Plugin '{name}': source_url must start with one of "
                f"{_VALID_SOURCE_SCHEMES}, got: {self.source_url!r}"
            )

        # Validate lastupdate_url scheme (if provided)
        if self.lastupdate_url and not self.lastupdate_url.startswith(
            ("https://", "http://")
        ):
            raise ValueError(
                f"Plugin '{name}': lastupdate_url must be http(s), "
                f"got: {self.lastupdate_url!r}"
            )

        # Validate bwlimit is a non-negative integer
        if not isinstance(self.bwlimit, int) or self.bwlimit < 0:
            raise ValueError(
                f"Plugin '{name}': bwlimit must be a non-negative integer, "
                f"got: {self.bwlimit!r}"
            )

        # Validate excludes are non-empty strings without shell metacharacters
        for exc in self.excludes:
            if not isinstance(exc, str) or not exc.strip():
                raise ValueError(
                    f"Plugin '{name}': exclude entries must be non-empty strings"
                )

    def sync(self) -> None:
        self.stats.start()
        start = time.time()

        try:
            self._ensure_target()
            with self._acquire_lock():
                self._cleanup_temp()
                self._run_sync()
            elapsed = time.time() - start
            self.stats.success(elapsed)
            self._update_dir_size()
        except Exception as e:
            logger.error("Sync %s failed: %s", self.stats.plugin_name, e)
            self.stats.failed(str(e))

    def _update_dir_size(self) -> None:
        """Compute the size of the mirror target directory using du -sb."""
        try:
            result = subprocess.run(
                ["du", "-sb", str(self.target)],
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode == 0:
                size_str = result.stdout.split()[0]
                self.stats.dir_size = int(size_str)
        except Exception as e:
            logger.warning(
                "Failed to compute dir size for %s: %s", self.stats.plugin_name, e
            )

    def _ensure_target(self) -> None:
        self.target.mkdir(parents=True, exist_ok=True)
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)

    class _Lock:
        def __init__(self, path: Path):
            self.path = path
            self._fd = None

        def __enter__(self):
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._fd = open(self.path, "w")
            try:
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self._fd.close()
                raise RuntimeError("Another sync is already running")
            return self

        def __exit__(self, *args):
            if self._fd:
                self._fd.close()

    def _acquire_lock(self) -> "_Lock":
        return self._Lock(self.lock_path)

    def _cleanup_temp(self) -> None:
        for tmp in self.target.rglob(".~tmp~*"):
            try:
                if tmp.is_dir():
                    shutil.rmtree(tmp)
                else:
                    tmp.unlink()
            except OSError as e:
                logger.warning("Failed to remove temp file %s: %s", tmp, e)

    def _run_sync(self) -> None:
        lastupdate_local = self.target / "lastupdate"
        is_tty = sys.stderr.isatty()

        if not is_tty and self.lastupdate_url and lastupdate_local.exists():
            try:
                with urllib.request.urlopen(self.lastupdate_url, timeout=30) as resp:
                    remote = resp.read().decode().strip()
                local = lastupdate_local.read_text().strip()
                if remote == local:
                    self._rsync(
                        f"{self.source_url}/lastsync", str(self.target / "lastsync")
                    )
                    self.stats.skipped()
                    return
            except Exception as e:
                logger.warning(
                    "Lastupdate check failed for %s: %s", self.stats.plugin_name, e
                )

        args = self._build_rsync_args()
        args.extend([self.source_url, str(self.target)])
        self._run_rsync(args)

    def _build_rsync_args(self) -> list[str]:
        if self.tls:
            cmd = ["rsync-ssl", "--type=openssl"]
        else:
            cmd = ["rsync"]

        cmd += [
            "-rlptH",
            "--safe-links",
            "--delete-delay",
            "--delay-updates",
            "--timeout=600",
            "--no-motd",
        ]

        is_tty = sys.stderr.isatty()
        if is_tty:
            cmd += ["-h", "-v", "--progress"]
        else:
            # Non-TTY: use --progress so we can parse percentage;
            # suppress the per-file list with --out-format to keep
            # output volume manageable.
            cmd += ["--progress", "--out-format=%i %n%L"]

        if self.bwlimit > 0:
            cmd.append(f"--bwlimit={self.bwlimit}")

        for exc in self.excludes:
            cmd.append(f"--exclude={exc}")

        return cmd

    def _rsync(self, src: str, dst: str) -> None:
        args = self._build_rsync_args()
        args.extend([src, dst])
        self._run_rsync(args)

    # ── progress parsing ────────────────────────────────────────────

    def _parse_progress(self, text: str) -> None:
        """Extract to-chk/ir-chk from an rsync progress line and compute overall %."""
        m = _PROGRESS_RE.search(text)
        if m:
            remaining = int(m.group(1))
            total = int(m.group(2))
            if total > 0:
                pct = int((total - remaining) / total * 100)
                self.stats.set_progress(pct)

    def _run_rsync(self, args: list[str]) -> None:
        logger.info("Running rsync: %s", " ".join(args))

        with self._proc_lock:
            proc = subprocess.Popen(
                args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
            )
            self._process = proc

        start = time.time()
        progress_stop = threading.Event()

        def _log_periodic() -> None:
            """Log sync progress every 60 seconds while rsync runs."""
            while not progress_stop.wait(60):
                elapsed = time.time() - start
                pct = self.stats.progress_pct
                logger.info(
                    "Sync %s: %d%% complete, elapsed %.0fs",
                    self.stats.plugin_name,
                    pct,
                    elapsed,
                )

        progress_thread = threading.Thread(target=_log_periodic, daemon=True)
        progress_thread.start()

        # Read stderr in a background thread so we don't block on it.
        stderr_chunks: list[str] = []

        def _drain_stderr() -> None:
            for line in proc.stderr:
                stderr_chunks.append(line)

        stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
        stderr_thread.start()

        # Read stdout byte-by-byte to handle \r-delimited progress lines.
        try:
            buf = ""
            while True:
                ch = proc.stdout.read(1)
                if not ch:
                    break
                if ch == "\r":
                    self._parse_progress(buf)
                    buf = ""
                elif ch == "\n":
                    self._parse_progress(buf)
                    buf = ""
                else:
                    buf += ch
            if buf:
                self._parse_progress(buf)
        finally:
            progress_stop.set()
            proc.wait()
            progress_thread.join(timeout=5)
            stderr_thread.join(timeout=10)
            with self._proc_lock:
                self._process = None

        elapsed = time.time() - start
        stderr_text = "".join(stderr_chunks)
        if proc.returncode != 0:
            if proc.returncode < 0:
                raise RuntimeError(f"sync cancelled (signal {abs(proc.returncode)})")
            raise RuntimeError(f"rsync failed: {stderr_text.strip()}")
        logger.info("Rsync finished in %.0fs, return code %d", elapsed, proc.returncode)
