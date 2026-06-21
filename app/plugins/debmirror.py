import logging
import re
import subprocess
import threading
import time
from pathlib import Path

from app.plugins.base import BaseSyncPlugin

logger = logging.getLogger("mirrord.plugin.debmirror")

# debmirror outputs progress like: "[  45%] Getting: pool/main/a/...  "
_PROGRESS_RE = re.compile(r"\[\s*(\d+)%\]")


class DebMirrorPlugin(BaseSyncPlugin):
    """Mirror Debian package repositories over HTTP via the `debmirror` tool.

    Requires `debmirror` installed on the host (apt install debmirror).
    Downloads indices, packages, and sources from an upstream HTTP mirror
    and produces a complete apt-usable repository tree.
    """

    plugin_type = "debmirror"

    def __init__(self, config: dict):
        super().__init__(config)
        self.target = Path(self.config["target"])
        self.host = self.config.get("host", "ftp.debian.org")
        self.root = self.config.get("root", "/debian")
        self.method = self.config.get("method", "http")
        self.dists = self.config.get("dists", "bookworm,bookworm-updates")
        self.sections = self.config.get(
            "sections", "main,contrib,non-free,non-free-firmware"
        )
        self.archs = self.config.get("archs", "amd64")
        self.source = self.config.get("source", False)
        self.ignore_release_gpg = self.config.get("ignore_release_gpg", True)
        self.lock_path = (
            Path(self.config.get("lock_dir", "/tmp/mirrord"))
            / f"{self.config.get('slug', 'debmirror')}.lck"
        )
        self._proc_lock = threading.Lock()

    def sync(self) -> None:
        self.stats.start()
        start = time.time()

        try:
            self._ensure_target()
            self._run_debmirror()
            elapsed = time.time() - start
            self.stats.success(elapsed)
            self._update_dir_size()
        except Exception as e:
            logger.error("Sync %s failed: %s", self.stats.plugin_name, e)
            self.stats.failed(str(e))

    def _ensure_target(self) -> None:
        self.target.mkdir(parents=True, exist_ok=True)
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)

    def _build_args(self) -> list[str]:
        args = [
            "debmirror",
            "--progress",
            "--verbose",
            "--host",
            self.host,
            "--root",
            self.root,
            "--method",
            self.method,
            "--dist",
            self.dists,
            "--section",
            self.sections,
            "--arch",
            self.archs,
            str(self.target),
        ]
        if not self.source:
            args.append("--nosource")
        if self.ignore_release_gpg:
            args.append("--ignore-release-gpg")
        return args

    def _run_debmirror(self) -> None:
        args = self._build_args()
        logger.info("Running debmirror: %s", " ".join(args))

        with self._proc_lock:
            proc = subprocess.Popen(
                args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
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
            buf = ""
            while True:
                ch = proc.stdout.read(1)
                if not ch:
                    break
                if ch in ("\r", "\n"):
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
            with self._proc_lock:
                self._process = None

        elapsed = time.time() - start
        if proc.returncode != 0:
            raise RuntimeError(f"debmirror failed with code {proc.returncode}")
        logger.info("debmirror finished in %.0fs", elapsed)

    def _parse_progress(self, text: str) -> None:
        m = _PROGRESS_RE.search(text)
        if m:
            self.stats.set_progress(int(m.group(1)))

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
                "Failed to compute dir size for %s: %s", self.stats.plugin_name, e
            )
