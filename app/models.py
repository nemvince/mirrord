import threading
import time
from dataclasses import dataclass
from enum import StrEnum


class SyncStatus(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class SyncJobStats:
    plugin_name: str
    plugin_type: str
    slug: str = ""
    description: str = ""
    status: SyncStatus = SyncStatus.IDLE
    last_sync: float | None = None
    last_duration: float | None = None
    last_size_bytes: int = 0
    last_error: str | None = None
    sync_started_at: float | None = None
    progress_pct: int = 0
    dir_size: int = 0
    total_syncs: int = 0
    total_failures: int = 0
    total_bytes_transferred: int = 0

    def __post_init__(self):
        self._lock = threading.Lock()

    def start(self):
        with self._lock:
            self.status = SyncStatus.RUNNING
            self.sync_started_at = time.time()
            self.progress_pct = 0

    def success(self, duration: float, bytes_transferred: int = 0):
        with self._lock:
            self.status = SyncStatus.SUCCESS
            self.last_sync = time.time()
            self.last_duration = duration
            self.last_size_bytes = bytes_transferred
            self.total_bytes_transferred += bytes_transferred
            self.total_syncs += 1
            self.last_error = None
            self.sync_started_at = None

    def failed(self, error: str):
        with self._lock:
            self.status = SyncStatus.FAILED
            self.last_sync = time.time()
            self.last_duration = None
            self.last_error = error
            self.total_failures += 1
            self.sync_started_at = None

    def skipped(self):
        with self._lock:
            self.status = SyncStatus.SKIPPED
            self.last_sync = time.time()
            self.last_duration = None
            self.sync_started_at = None

    def set_progress(self, pct: int):
        """Called by plugins during sync to report progress (0-100)."""
        with self._lock:
            self.progress_pct = pct

    def snapshot(self) -> "SyncJobStats":
        """Return a thread-safe copy for external readers."""
        with self._lock:
            return SyncJobStats(
                plugin_name=self.plugin_name,
                plugin_type=self.plugin_type,
                slug=self.slug,
                description=self.description,
                status=self.status,
                last_sync=self.last_sync,
                last_duration=self.last_duration,
                last_size_bytes=self.last_size_bytes,
                last_error=self.last_error,
                sync_started_at=self.sync_started_at,
                progress_pct=self.progress_pct,
                dir_size=self.dir_size,
                total_syncs=self.total_syncs,
                total_failures=self.total_failures,
                total_bytes_transferred=self.total_bytes_transferred,
            )
