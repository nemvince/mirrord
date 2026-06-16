import json
import os
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class SyncStatus(str, Enum):
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
    last_sync: Optional[float] = None
    last_duration: Optional[float] = None
    last_size_bytes: int = 0
    last_error: Optional[str] = None
    sync_started_at: Optional[float] = None
    progress_pct: int = 0
    dir_size: int = 0
    total_syncs: int = 0
    total_failures: int = 0
    total_bytes_transferred: int = 0
    _stats_path: str = ""

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
        self._save()

    def failed(self, error: str):
        with self._lock:
            self.status = SyncStatus.FAILED
            self.last_sync = time.time()
            self.last_duration = None
            self.last_error = error
            self.total_failures += 1
            self.sync_started_at = None
        self._save()

    def skipped(self):
        with self._lock:
            self.status = SyncStatus.SKIPPED
            self.last_sync = time.time()
            self.last_duration = None
            self.sync_started_at = None
        self._save()

    def set_progress(self, pct: int):
        """Called by plugins during sync to report progress (0-100)."""
        self.progress_pct = pct

    def _save(self) -> None:
        """Persist cumulative stats to a JSON file so they survive restarts."""
        if not self._stats_path:
            return
        try:
            os.makedirs(os.path.dirname(self._stats_path), exist_ok=True)
            with open(self._stats_path, "w") as f:
                json.dump(
                    {
                        "total_syncs": self.total_syncs,
                        "total_failures": self.total_failures,
                        "total_bytes_transferred": self.total_bytes_transferred,
                        "dir_size": self.dir_size,
                        "last_sync": self.last_sync,
                        "last_duration": self.last_duration,
                        "last_size_bytes": self.last_size_bytes,
                        "last_error": self.last_error,
                    },
                    f,
                )
        except OSError:
            pass

    def load(self) -> None:
        """Restore cumulative stats from a previously saved JSON file."""
        if not self._stats_path or not os.path.isfile(self._stats_path):
            return
        try:
            with open(self._stats_path) as f:
                data = json.load(f)
            self.total_syncs = data.get("total_syncs", 0)
            self.total_failures = data.get("total_failures", 0)
            self.total_bytes_transferred = data.get("total_bytes_transferred", 0)
            self.dir_size = data.get("dir_size", 0)
            self.last_sync = data.get("last_sync")
            self.last_duration = data.get("last_duration")
            self.last_size_bytes = data.get("last_size_bytes", 0)
            self.last_error = data.get("last_error")
        except (OSError, json.JSONDecodeError, KeyError):
            pass

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
