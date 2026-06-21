import logging
import re
import subprocess
import threading
import time
from pathlib import Path

from app.plugins.base import BaseSyncPlugin

logger = logging.getLogger("mirrord.plugin.ftpsync")

# ftpsync echoes lines like "sync:stage1", "sync:stage2", etc.
_STAGE_RE = re.compile(r"stage\s*(\d)", re.IGNORECASE)
_SYNC_ALL_RE = re.compile(r"sync:all", re.IGNORECASE)


def _conf_value(value: str) -> str:
    """Escape a value for safe interpolation into a double-quoted shell string.

    ftpsync config files are sourced as shell snippets.  A value like
    ``foo"; rm -rf /; echo "`` would break out of the quotes and execute
    arbitrary commands.  We use ``shlex.quote`` to produce a safely quoted
    shell token, then strip the outer single-quotes so the caller can wrap
    in double-quotes as ftpsync expects.

    For values that are purely alphanumeric with limited punctuation (the
    common case for hostnames, paths, and archive names) this is a no-op.
    """
    # Reject values containing null bytes outright
    if "\x00" in value:
        raise ValueError("config value contains null byte")
    # Escape characters that are meaningful inside double-quoted shell strings:
    # " closes the quote, $ starts variable expansion, ` starts command
    # substitution, \ is an escape character.
    return (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("$", "\\$")
        .replace("`", "\\`")
    )


class FtpSyncPlugin(BaseSyncPlugin):
    """Mirror a Debian archive via the ``ftpsync`` tool (from ``archvsync``).

    ftpsync performs a **two-stage rsync** designed for public mirrors:

    * **Stage 1** — sync everything *except* metadata indices (Packages,
      Sources, Release, Contents, …).  No deletions happen in this stage.
    * **Stage 2** — sync the remaining metadata and atomically swap it in
      using ``--delay-updates``.  Deletions happen here with a safety cap.

    This guarantees that apt clients never see a half-updated mirror.

    Requires the ``ftpsync`` Debian package (``apt install ftpsync``).
    See https://www.debian.org/mirror/ftpmirror for background.
    """

    plugin_type = "ftpsync"

    # Progress bookkeeping: stage 1 is the bulk (pool data), stage 2 is
    # fast metadata swap.  We use coarse buckets so the progress bar
    # moves meaningfully without needing to parse rsync's --quiet output.
    _STAGE_PROGRESS = {
        "starting": 0,
        "stage1": 5,
        "stage2": 70,
        "done": 100,
    }

    def __init__(self, config: dict):
        super().__init__(config)
        self.target = Path(self.config["target"])

        # ── connection ───────────────────────────────────────────
        self.host = self.config.get("host", "ftp.debian.org")
        self.rsync_path = self.config.get("rsync_path", "debian")
        self.rsync_transport = self.config.get("rsync_transport", "")

        # ── filtering ────────────────────────────────────────────
        self.archs = self.config.get("archs", "")
        self.include_source = self.config.get("include_source", False)
        self.extra_excludes: list[str] = self.config.get("excludes", [])

        # ── bandwidth ────────────────────────────────────────────
        self.bwlimit = self.config.get("bwlimit", 0)

        # ── identity (appears in trace files) ─────────────────────
        self.mirrorname = self.config.get("mirrorname", "")

        # ── lock / logging ───────────────────────────────────────
        self.slug = self.config.get("slug", "ftpsync")
        self.conf_dir = Path(
            self.config.get(
                "conf_dir", str(Path.home() / ".config" / "ftpsync")
            )
        )
        self.lock_path = (
            Path(self.config.get("lock_dir", "/tmp/mirrord")) / f"{self.slug}.lck"
        )
        self._proc_lock = threading.Lock()
        self._validate_config()

    # ── config validation ──────────────────────────────────────

    def _validate_config(self) -> None:
        """Validate config types and ranges before use."""
        name = self.config.get("name", self.plugin_type)

        if not self.host.strip():
            raise ValueError(f"Plugin '{name}': host must not be empty")

        if not self.rsync_path.strip():
            raise ValueError(f"Plugin '{name}': rsync_path must not be empty")

        if not isinstance(self.bwlimit, (int, float)) or self.bwlimit < 0:
            raise ValueError(
                f"Plugin '{name}': bwlimit must be a non-negative number, "
                f"got: {self.bwlimit!r}"
            )

    # ── public API (BaseSyncPlugin) ──────────────────────────────

    def sync(self) -> None:
        self.stats.start()
        start = time.time()

        try:
            self._ensure_target()
            self._write_config()
            self._run_ftpsync()
            elapsed = time.time() - start
            self.stats.success(elapsed)
            self._update_dir_size()
        except Exception as e:
            logger.error("Sync %s failed: %s", self.stats.plugin_name, e)
            self.stats.failed(str(e))

    # ── config generation ────────────────────────────────────────

    def _ensure_target(self) -> None:
        self.target.mkdir(parents=True, exist_ok=True)
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)

    def _config_path(self) -> Path:
        """Return the path to the ftpsync.conf we generate."""
        return self.conf_dir / f"ftpsync-{self.slug}.conf"

    def _write_config(self) -> None:
        """Render a ``ftpsync.conf`` from the current plugin config.

        ftpsync reads config from ``/etc/ftpsync/ftpsync-<archive>.conf``
        when invoked as ``ftpsync sync:archive:<archive>``.
        """
        esc = _conf_value  # shorthand
        lines = [
            "# Auto-generated by mirrord — do not edit by hand.",
            "",
            "# ── connection ──",
            f'RSYNC_HOST="{esc(self.host)}"',
            f'RSYNC_PATH="{esc(self.rsync_path)}"',
            "",
            "# ── mail (ftpsync requires MAILTO or LOGNAME) ──",
            'MAILTO="root"',
        ]

        if self.rsync_transport:
            lines.append(f'RSYNC_TRANSPORT="{esc(self.rsync_transport)}"')

        lines += [
            "",
            "# ── destination ──",
            f'TO="{esc(str(self.target))}/"',
        ]

        if self.mirrorname:
            lines.append(f'MIRRORNAME="{esc(self.mirrorname)}"')

        # ── architecture filtering ───────────────────────────
        arch_value = self.archs.replace(",", " ").strip()
        if arch_value:
            if self.include_source:
                arch_value += " source"
            lines += [
                "",
                "# ── architecture filter ──",
                f'ARCH_INCLUDE="{esc(arch_value)}"',
            ]

        # ── bandwidth ────────────────────────────────────────
        if self.bwlimit > 0:
            lines += [
                "",
                "# ── bandwidth limit (KB/s) ──",
                f'RSYNC_BW="{int(self.bwlimit)}"',
            ]

        # ── extra excludes (raw rsync --exclude rules) ───────
        # Note: ftpsync docs warn against using EXCLUDE for
        # architecture or suite filtering — use ARCH_INCLUDE/
        # ARCH_EXCLUDE instead.
        if self.extra_excludes:
            lines.append("")
            lines.append("# ── extra excludes ──")
            for exc in self.extra_excludes:
                lines.append(f'EXCLUDE="{esc(exc)}"')

        conf_path = self._config_path()
        conf_path.parent.mkdir(parents=True, exist_ok=True)
        conf_path.write_text("\n".join(lines) + "\n")
        logger.info("Wrote ftpsync config → %s", conf_path)

    # ── ftpsync execution ────────────────────────────────────────

    def _ftpsync_args(self) -> list[str]:
        """Build the ftpsync command line.

        Uses ``sync:archive:<slug>`` so ftpsync reads
        ``/etc/ftpsync/ftpsync-<slug>.conf``.
        """
        return ["ftpsync", f"sync:archive:{self.slug}"]

    def _run_ftpsync(self) -> None:
        args = self._ftpsync_args()
        logger.info("Running ftpsync: %s", " ".join(args))

        self.stats.set_progress(self._STAGE_PROGRESS["starting"])

        with self._proc_lock:
            proc = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            self._process = proc

        start = time.time()
        progress_stop = threading.Event()

        def _log_periodic() -> None:
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

        try:
            lines: list[str] = []
            buf = ""
            while True:
                ch = proc.stdout.read(1)
                if not ch:
                    break
                if ch in ("\r", "\n"):
                    self._parse_stage(buf)
                    stripped = buf.strip()
                    if stripped:
                        lines.append(stripped)
                    buf = ""
                else:
                    buf += ch
            if buf:
                self._parse_stage(buf)
                lines.append(buf.strip())
        finally:
            progress_stop.set()
            proc.wait()
            progress_thread.join(timeout=5)
            with self._proc_lock:
                self._process = None

        elapsed = time.time() - start
        if proc.returncode != 0:
            for line in lines:
                logger.error("ftpsync: %s", line)
            raise RuntimeError(f"ftpsync failed with exit code {proc.returncode}")

        self.stats.set_progress(self._STAGE_PROGRESS["done"])
        logger.info("ftpsync finished in %.0fs", elapsed)

    def _parse_stage(self, text: str) -> None:
        """Detect ftpsync stage transitions from stdout lines."""
        m = _STAGE_RE.search(text)
        if m:
            stage_num = int(m.group(1))
            key = f"stage{stage_num}"
            if key in self._STAGE_PROGRESS:
                self.stats.set_progress(self._STAGE_PROGRESS[key])
                logger.info("ftpsync entered stage %d", stage_num)

    # ── helpers ──────────────────────────────────────────────────

    def _update_dir_size(self) -> None:
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
                "Failed to compute dir size for %s: %s",
                self.stats.plugin_name,
                e,
            )
