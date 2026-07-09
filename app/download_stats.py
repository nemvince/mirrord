import threading
import time
from dataclasses import dataclass, field

MAX_TOP_FILES = 10


@dataclass
class DownloadStats:
    total_downloads: int = 0
    total_bytes_served: int = 0
    last_download: float | None = None
    top_files: dict[str, int] = field(default_factory=dict)

    def __post_init__(self):
        self._lock = threading.RLock()

    def record(self, path: str, size: int) -> None:
        with self._lock:
            self.total_downloads += 1
            self.total_bytes_served += size
            self.last_download = time.time()
            self.top_files[path] = self.top_files.get(path, 0) + 1

    def top_files_list(self) -> list[tuple[str, int]]:
        with self._lock:
            return sorted(
                self.top_files.items(),
                key=lambda x: x[1],
                reverse=True,
            )[:MAX_TOP_FILES]

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "total_downloads": self.total_downloads,
                "total_bytes_served": self.total_bytes_served,
                "last_download": self.last_download,
                "top_files": self.top_files_list(),
            }

    def load_from(self, data: dict) -> None:
        with self._lock:
            self.total_downloads = data.get("total_downloads", 0)
            self.total_bytes_served = data.get("total_bytes_served", 0)
            self.last_download = data.get("last_download")


class DownloadTracker:
    def __init__(self, db=None):
        self._stats: dict[str, DownloadStats] = {}
        self._lock = threading.Lock()
        self._db = db

    def ensure_plugin(self, slug: str, db_summary: dict | None = None) -> DownloadStats:
        with self._lock:
            if slug not in self._stats:
                ds = DownloadStats()
                if db_summary:
                    ds.load_from(db_summary)
                self._stats[slug] = ds
            return self._stats[slug]

    def record_download(
        self,
        slug: str,
        path: str,
        size: int,
        ua: str | None = None,
        geocode: str | None = None,
    ) -> None:
        with self._lock:
            ds = self._stats.get(slug)
            if ds is None:
                return
        ds.record(path, size)
        if self._db is not None:
            self._db.record(slug, path, size, ua=ua, geocode=geocode)

    def get_snapshot(self, slug: str) -> dict | None:
        with self._lock:
            ds = self._stats.get(slug)
            if ds is None:
                return None
        return ds.snapshot()

    def get_all_snapshots(self) -> dict[str, dict]:
        with self._lock:
            return {slug: ds.snapshot() for slug, ds in self._stats.items()}

    def prune_stale(self, active_slugs: set[str]) -> None:
        with self._lock:
            stale = [s for s in self._stats if s not in active_slugs]
            for s in stale:
                del self._stats[s]

    def load_from_db(self, slug: str) -> None:
        if self._db is None:
            return
        summary = self._db.get_summary(slug)
        self.ensure_plugin(slug, db_summary=summary)
