import logging
import subprocess
import time

from app.plugins.base import BaseSyncPlugin

logger = logging.getLogger("mirrord.plugin.local_dir")


class LocalDirPlugin(BaseSyncPlugin):
    """Mirror plugin that serves an existing local directory as-is.

    Unlike sync-based plugins (rsync, ftpsync), this does not fetch data
    from a remote source — it simply exposes a pre-existing directory on
    disk and reports its size/stats.  Useful for NFS mounts, manually
    maintained trees, or data populated by an external process.
    """

    plugin_type = "local_dir"

    def sync(self) -> None:
        self.stats.start()
        start = time.time()

        try:
            target = self.target_dir
            if not target.exists():
                raise FileNotFoundError(f"Target directory does not exist: {target}")
            if not target.is_dir():
                raise NotADirectoryError(f"Target path is not a directory: {target}")

            elapsed = time.time() - start
            self.stats.success(elapsed)
            self._update_dir_size()

            logger.info(
                "Local dir %s: %s (size reported asynchronously)",
                self.stats.plugin_name,
                target,
            )
        except Exception as e:
            logger.error("Local dir %s failed: %s", self.stats.plugin_name, e)
            self.stats.failed(str(e))

    def _update_dir_size(self) -> None:
        """Compute the size of the mirror target directory using du -sb."""
        try:
            result = subprocess.run(
                ["du", "-sb", str(self.target_dir)],
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
