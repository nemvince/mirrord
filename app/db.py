import glob
import json
import os
import sqlite3
import threading
import time
from datetime import datetime

from app.models import SyncJobStats, SyncStatus

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS downloads (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    slug    TEXT NOT NULL,
    path    TEXT NOT NULL,
    size    INTEGER NOT NULL,
    ts      REAL NOT NULL,
    ua      TEXT,
    geocode TEXT
);

CREATE INDEX IF NOT EXISTS idx_dl_slug    ON downloads(slug);
CREATE INDEX IF NOT EXISTS idx_dl_ts      ON downloads(ts);
CREATE INDEX IF NOT EXISTS idx_dl_slug_ts ON downloads(slug, ts);
CREATE INDEX IF NOT EXISTS idx_dl_path    ON downloads(path);

CREATE TABLE IF NOT EXISTS daily_dl (
    date    TEXT NOT NULL,
    slug    TEXT NOT NULL,
    count   INTEGER DEFAULT 0,
    bytes   INTEGER DEFAULT 0,
    PRIMARY KEY (date, slug)
);

CREATE TABLE IF NOT EXISTS plugin_stats (
    slug                     TEXT PRIMARY KEY,
    plugin_name             TEXT NOT NULL,
    plugin_type             TEXT NOT NULL,
    description             TEXT,
    status                  TEXT,
    last_sync               REAL,
    last_duration           REAL,
    last_size_bytes         INTEGER,
    last_error              TEXT,
    sync_started_at         REAL,
    progress_pct            INTEGER,
    dir_size                INTEGER,
    total_syncs             INTEGER DEFAULT 0,
    total_failures          INTEGER DEFAULT 0,
    total_bytes_transferred INTEGER DEFAULT 0
);
"""


class DownloadDB:
    """Central SQLite store for all persisted mirrord state.

    Holds per-download events (``downloads`` / ``daily_dl``) and per-plugin
    cumulative sync statistics (``plugin_stats``). A single connection guarded
    by one re-entrant lock is shared across all engine threads.
    """

    def __init__(self, db_path: str):
        self._db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._init_schema()

    # ── low-level helpers ──────────────────────────────────────────

    def _execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def _query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(SCHEMA_SQL)
            self._conn.commit()

    # ── downloads ──────────────────────────────────────────────────

    def record(
        self,
        slug: str,
        path: str,
        size: int,
        ua: str | None = None,
        geocode: str | None = None,
    ) -> None:
        ts = time.time()
        date = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
        with self._lock:
            self._conn.execute(
                "INSERT INTO downloads (slug, path, size, ts, ua, geocode) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (slug, path, size, ts, ua, geocode),
            )
            self._conn.execute(
                """INSERT INTO daily_dl (date, slug, count, bytes) VALUES (?, ?, 1, ?)
                   ON CONFLICT(date, slug) DO UPDATE SET
                       count = count + 1,
                       bytes = bytes + excluded.bytes""",
                (date, slug, size),
            )
            self._conn.commit()

    def get_daily_totals(self, slug: str | None = None, days: int = 30) -> list[dict]:
        if slug:
            rows = self._query(
                "SELECT date, count, bytes FROM daily_dl "
                "WHERE slug = ? ORDER BY date DESC LIMIT ?",
                (slug, days),
            )
        else:
            rows = self._query(
                "SELECT date, SUM(count) AS count, SUM(bytes) AS bytes "
                "FROM daily_dl GROUP BY date ORDER BY date DESC LIMIT ?",
                (days,),
            )
        return [dict(r) for r in rows]

    def get_top_files(
        self,
        slug: str | None = None,
        limit: int = 25,
        since_days: int | None = None,
    ) -> list[dict]:
        cutoff = time.time() - since_days * 86400 if since_days else 0
        if slug:
            col = "path, COUNT(*) AS count, SUM(size) AS bytes, MAX(ts) AS last_dl"
            if since_days:
                rows = self._query(
                    f"SELECT {col} FROM downloads WHERE slug = ? AND ts > ? "
                    "GROUP BY path ORDER BY count DESC LIMIT ?",
                    (slug, cutoff, limit),
                )
            else:
                rows = self._query(
                    f"SELECT {col} FROM downloads WHERE slug = ? "
                    "GROUP BY path ORDER BY count DESC LIMIT ?",
                    (slug, limit),
                )
        else:
            col = (
                "slug, path, COUNT(*) AS count, SUM(size) AS bytes, MAX(ts) AS last_dl"
            )
            if since_days:
                rows = self._query(
                    f"SELECT {col} FROM downloads WHERE ts > ? "
                    "GROUP BY slug, path ORDER BY count DESC LIMIT ?",
                    (cutoff, limit),
                )
            else:
                rows = self._query(
                    f"SELECT {col} FROM downloads "
                    "GROUP BY slug, path ORDER BY count DESC LIMIT ?",
                    (limit,),
                )
        return [dict(r) for r in rows]

    def get_overview(self) -> dict:
        total = self._query(
            "SELECT COUNT(*) AS count, COALESCE(SUM(size), 0) AS bytes FROM downloads"
        )[0]
        by_slug = self._query(
            "SELECT slug, COUNT(*) AS count, COALESCE(SUM(size), 0) AS bytes "
            "FROM downloads GROUP BY slug ORDER BY count DESC"
        )
        today = datetime.now().strftime("%Y-%m-%d")
        today_stats = self._query(
            "SELECT COALESCE(SUM(count), 0) AS count, "
            "COALESCE(SUM(bytes), 0) AS bytes FROM daily_dl WHERE date = ?",
            (today,),
        )[0]
        by_geocode = self._query(
            "SELECT geocode, COUNT(*) AS count FROM downloads "
            "WHERE geocode IS NOT NULL GROUP BY geocode ORDER BY count DESC"
        )
        unique_files = self._query(
            "SELECT COUNT(DISTINCT slug || ':' || path) AS c FROM downloads"
        )[0]
        return {
            "total_downloads": total["count"] or 0,
            "total_bytes": total["bytes"] or 0,
            "by_slug": [dict(r) for r in by_slug],
            "today_downloads": today_stats["count"] or 0,
            "today_bytes": today_stats["bytes"] or 0,
            "by_geocode": [dict(r) for r in by_geocode],
            "unique_files": unique_files["c"] or 0,
        }

    def get_recent(self, limit: int = 50) -> list[dict]:
        rows = self._query(
            "SELECT slug, path, size, ts, ua, geocode "
            "FROM downloads ORDER BY ts DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in rows]

    def get_summary(self, slug: str) -> dict:
        total = self._query(
            "SELECT COUNT(*) AS count, COALESCE(SUM(size), 0) AS bytes, "
            "MAX(ts) AS last_ts FROM downloads WHERE slug = ?",
            (slug,),
        )[0]
        return {
            "total_downloads": total["count"] or 0,
            "total_bytes_served": total["bytes"] or 0,
            "last_download": total["last_ts"],
        }

    def get_top_files_for_slug(
        self, slug: str, limit: int = 10
    ) -> list[tuple[str, int]]:
        rows = self.get_top_files(slug=slug, limit=limit)
        return [(r["path"], r["count"]) for r in rows]

    def has_data(self) -> bool:
        row = self._query("SELECT COUNT(*) AS c FROM downloads")[0]
        return row["c"] > 0

    # ── plugin stats ───────────────────────────────────────────────

    def upsert_plugin(
        self,
        slug: str,
        plugin_name: str,
        plugin_type: str,
        description: str = "",
    ) -> None:
        """Register/refresh a plugin's identity, preserving cumulative counters."""
        self._execute(
            """INSERT INTO plugin_stats
                   (slug, plugin_name, plugin_type, description)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(slug) DO UPDATE SET
                   plugin_name = excluded.plugin_name,
                   plugin_type = excluded.plugin_type,
                   description = excluded.description""",
            (slug, plugin_name, plugin_type, description),
        )

    def save_plugin_stats(self, slug: str, stats: SyncJobStats) -> None:
        """Persist the cumulative sync counters for a plugin."""
        self._execute(
            """UPDATE plugin_stats SET
                   status = ?,
                   last_sync = ?,
                   last_duration = ?,
                   last_size_bytes = ?,
                   last_error = ?,
                   sync_started_at = ?,
                   progress_pct = ?,
                   dir_size = ?,
                   total_syncs = ?,
                   total_failures = ?,
                   total_bytes_transferred = ?
               WHERE slug = ?""",
            (
                stats.status.value,
                stats.last_sync,
                stats.last_duration,
                stats.last_size_bytes,
                stats.last_error,
                stats.sync_started_at,
                stats.progress_pct,
                stats.dir_size,
                stats.total_syncs,
                stats.total_failures,
                stats.total_bytes_transferred,
                slug,
            ),
        )

    def load_plugin_stats(self, slug: str) -> SyncJobStats | None:
        """Load a plugin's identity + cumulative stats, or None if unknown."""
        row = self._query("SELECT * FROM plugin_stats WHERE slug = ?", (slug,))
        if not row:
            return None
        r = row[0]
        return SyncJobStats(
            plugin_name=r["plugin_name"],
            plugin_type=r["plugin_type"],
            slug=slug,
            description=r["description"] or "",
            status=SyncStatus(r["status"]) if r["status"] else SyncStatus.IDLE,
            last_sync=r["last_sync"],
            last_duration=r["last_duration"],
            last_size_bytes=r["last_size_bytes"] or 0,
            last_error=r["last_error"],
            sync_started_at=r["sync_started_at"],
            progress_pct=r["progress_pct"] or 0,
            dir_size=r["dir_size"] or 0,
            total_syncs=r["total_syncs"] or 0,
            total_failures=r["total_failures"] or 0,
            total_bytes_transferred=r["total_bytes_transferred"] or 0,
        )

    def get_all_plugin_stats(self) -> list[SyncJobStats]:
        rows = self._query("SELECT * FROM plugin_stats ORDER BY plugin_name")
        return [self._row_to_stats(r) for r in rows]

    def _row_to_stats(self, r: sqlite3.Row) -> SyncJobStats:
        return SyncJobStats(
            plugin_name=r["plugin_name"],
            plugin_type=r["plugin_type"],
            slug=r["slug"],
            description=r["description"] or "",
            status=SyncStatus(r["status"]) if r["status"] else SyncStatus.IDLE,
            last_sync=r["last_sync"],
            last_duration=r["last_duration"],
            last_size_bytes=r["last_size_bytes"] or 0,
            last_error=r["last_error"],
            sync_started_at=r["sync_started_at"],
            progress_pct=r["progress_pct"] or 0,
            dir_size=r["dir_size"] or 0,
            total_syncs=r["total_syncs"] or 0,
            total_failures=r["total_failures"] or 0,
            total_bytes_transferred=r["total_bytes_transferred"] or 0,
        )

    # ── live download aggregates (replaces in-memory DownloadTracker) ──

    def get_snapshot(self, slug: str) -> dict:
        return self.get_summary(slug)

    def get_all_snapshots(self) -> dict[str, dict]:
        slugs = self._query("SELECT DISTINCT slug FROM downloads")
        return {row["slug"]: self.get_summary(row["slug"]) for row in slugs}

    # ── migration from legacy JSON stats files ─────────────────────

    def migrate_from_json(self, lock_dir: str) -> None:
        """One-time backfill of per-plugin JSON stats into ``plugin_stats``.

        Existing ``<slug>.stats.json`` files are removed after a successful
        import. (Download JSON files were already migrated to the ``downloads``
        table in an earlier release.)
        """
        json_files = glob.glob(os.path.join(lock_dir, "*.stats.json"))
        for jf in json_files:
            slug = os.path.basename(jf).replace(".stats.json", "")
            try:
                with open(jf) as f:
                    data = json.load(f)
            except (OSError, json.JSONDecodeError):
                continue
            identity = self.load_plugin_stats(slug)
            self._execute(
                """INSERT INTO plugin_stats
                       (slug, plugin_name, plugin_type, description,
                        last_sync, last_duration, last_size_bytes, last_error,
                        dir_size, total_syncs, total_failures,
                        total_bytes_transferred)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(slug) DO UPDATE SET
                       last_sync = excluded.last_sync,
                       last_duration = excluded.last_duration,
                       last_size_bytes = excluded.last_size_bytes,
                       last_error = excluded.last_error,
                       dir_size = excluded.dir_size,
                       total_syncs = excluded.total_syncs,
                       total_failures = excluded.total_failures,
                       total_bytes_transferred = excluded.total_bytes_transferred""",
                (
                    slug,
                    data.get("plugin_name", slug),
                    data.get("plugin_type", ""),
                    data.get("description", ""),
                    data.get("last_sync"),
                    data.get("last_duration"),
                    data.get("last_size_bytes", 0),
                    data.get("last_error"),
                    data.get("dir_size", 0),
                    data.get("total_syncs", 0),
                    data.get("total_failures", 0),
                    data.get("total_bytes_transferred", 0),
                )
                if identity is None
                else (
                    slug,
                    identity.plugin_name,
                    identity.plugin_type,
                    identity.description,
                    data.get("last_sync"),
                    data.get("last_duration"),
                    data.get("last_size_bytes", 0),
                    data.get("last_error"),
                    data.get("dir_size", 0),
                    data.get("total_syncs", 0),
                    data.get("total_failures", 0),
                    data.get("total_bytes_transferred", 0),
                ),
            )
            try:
                os.remove(jf)
            except OSError:
                pass
