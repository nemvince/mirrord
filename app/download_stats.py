import json
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Optional


MAX_TOP_FILES = 10  # keep only the top N most-downloaded files in memory


@dataclass
class DownloadStats:
    """Per-plugin download counters, persisted to disk."""

    total_downloads: int = 0
    total_bytes_served: int = 0
    last_download: Optional[float] = None
    top_files: dict[str, int] = field(default_factory=dict)  # path → count (in-memory only)
    _stats_path: str = ""

    def __post_init__(self):
        self._lock = threading.RLock()

    def record(self, path: str, size: int) -> None:
        with self._lock:
            self.total_downloads += 1
            self.total_bytes_served += size
            self.last_download = time.time()
            self.top_files[path] = self.top_files.get(path, 0) + 1

    def top_files_list(self) -> list[tuple[str, int]]:
        """Return top files sorted by download count, limited to MAX_TOP_FILES."""
        with self._lock:
            return sorted(
                self.top_files.items(), key=lambda x: x[1], reverse=True
            )[:MAX_TOP_FILES]

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "total_downloads": self.total_downloads,
                "total_bytes_served": self.total_bytes_served,
                "last_download": self.last_download,
                "top_files": self.top_files_list(),
            }

    def save(self) -> None:
        if not self._stats_path:
            return
        try:
            os.makedirs(os.path.dirname(self._stats_path), exist_ok=True)
            with open(self._stats_path, "w") as f:
                json.dump(
                    {
                        "total_downloads": self.total_downloads,
                        "total_bytes_served": self.total_bytes_served,
                        "last_download": self.last_download,
                    },
                    f,
                )
        except OSError:
            pass

    def load(self) -> None:
        if not self._stats_path or not os.path.isfile(self._stats_path):
            return
        try:
            with open(self._stats_path) as f:
                data = json.load(f)
            with self._lock:
                self.total_downloads = data.get("total_downloads", 0)
                self.total_bytes_served = data.get("total_bytes_served", 0)
                self.last_download = data.get("last_download")
        except (OSError, json.JSONDecodeError, KeyError):
            pass


class DownloadTracker:
    """Tracks HTTP download activity across all plugins."""

    def __init__(self):
        self._stats: dict[str, DownloadStats] = {}
        self._lock = threading.Lock()

    def ensure_plugin(self, slug: str, stats_path: str = "") -> DownloadStats:
        with self._lock:
            if slug not in self._stats:
                ds = DownloadStats(_stats_path=stats_path)
                ds.load()
                self._stats[slug] = ds
            return self._stats[slug]

    def record_download(self, slug: str, path: str, size: int) -> None:
        with self._lock:
            ds = self._stats.get(slug)
            if ds is None:
                return  # not initialised yet — should not happen after engine start
        ds.record(path, size)
        ds.save()

    def get_snapshot(self, slug: str) -> Optional[dict]:
        with self._lock:
            ds = self._stats.get(slug)
            if ds is None:
                return None
        return ds.snapshot()

    def get_all_snapshots(self) -> dict[str, dict]:
        with self._lock:
            return {slug: ds.snapshot() for slug, ds in self._stats.items()}

    def save_all(self) -> None:
        with self._lock:
            for ds in self._stats.values():
                ds.save()

    def load_all(self) -> None:
        """Load all existing stat files from disk — called during engine start."""
        # Individual loads happen in ensure_plugin
        pass

    def prune_stale(self, active_slugs: set[str]) -> None:
        """Remove entries for plugins that no longer exist in the config."""
        with self._lock:
            stale = [s for s in self._stats if s not in active_slugs]
            for s in stale:
                del self._stats[s]
