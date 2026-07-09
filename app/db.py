import glob
import json
import os
import sqlite3
import threading
import time
from datetime import datetime

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
"""


class DownloadDB:
    def __init__(self, db_path: str):
        self._db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._init_schema()

    def _init_schema(self):
        with self._lock:
            self._conn.executescript(SCHEMA_SQL)
            self._conn.commit()

    def record(
        self,
        slug: str,
        path: str,
        size: int,
        ua: str | None = None,
        geocode: str | None = None,
    ) -> None:
        with self._lock:
            ts = time.time()
            self._conn.execute(
                "INSERT INTO downloads (slug, path, size, ts, ua, geocode) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (slug, path, size, ts, ua, geocode),
            )
            date = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
            self._conn.execute(
                """INSERT INTO daily_dl (date, slug, count, bytes) VALUES (?, ?, 1, ?)
                   ON CONFLICT(date, slug) DO UPDATE SET
                       count = count + 1,
                       bytes = bytes + excluded.bytes""",
                (date, slug, size),
            )
            self._conn.commit()

    def get_daily_totals(self, slug: str | None = None, days: int = 30) -> list[dict]:
        with self._lock:
            if slug:
                rows = self._conn.execute(
                    "SELECT date, count, bytes FROM daily_dl "
                    "WHERE slug = ? ORDER BY date DESC LIMIT ?",
                    (slug, days),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT date, SUM(count) AS count, SUM(bytes) AS bytes "
                    "FROM daily_dl GROUP BY date ORDER BY date DESC LIMIT ?",
                    (days,),
                ).fetchall()
            return [dict(r) for r in rows]

    def get_top_files(
        self,
        slug: str | None = None,
        limit: int = 25,
        since_days: int | None = None,
    ) -> list[dict]:
        with self._lock:
            cutoff = time.time() - since_days * 86400 if since_days else 0
            if slug:
                if since_days:
                    col = (
                        "path, COUNT(*) AS count, SUM(size) AS bytes, "
                        "MAX(ts) AS last_dl"
                    )
                    rows = self._conn.execute(
                        f"SELECT {col} "
                        "FROM downloads WHERE slug = ? AND ts > ? "
                        "GROUP BY path ORDER BY count DESC LIMIT ?",
                        (slug, cutoff, limit),
                    ).fetchall()
                else:
                    col = (
                        "path, COUNT(*) AS count, SUM(size) AS bytes, "
                        "MAX(ts) AS last_dl"
                    )
                    rows = self._conn.execute(
                        f"SELECT {col} "
                        "FROM downloads WHERE slug = ? "
                        "GROUP BY path ORDER BY count DESC LIMIT ?",
                        (slug, limit),
                    ).fetchall()
            else:
                if since_days:
                    col = (
                        "slug, path, COUNT(*) AS count, SUM(size) AS bytes, "
                        "MAX(ts) AS last_dl"
                    )
                    rows = self._conn.execute(
                        f"SELECT {col} "
                        "FROM downloads WHERE ts > ? "
                        "GROUP BY slug, path ORDER BY count DESC LIMIT ?",
                        (cutoff, limit),
                    ).fetchall()
                else:
                    col = (
                        "slug, path, COUNT(*) AS count, SUM(size) AS bytes, "
                        "MAX(ts) AS last_dl"
                    )
                    rows = self._conn.execute(
                        f"SELECT {col} "
                        "FROM downloads "
                        "GROUP BY slug, path ORDER BY count DESC LIMIT ?",
                        (limit,),
                    ).fetchall()
            return [dict(r) for r in rows]

    def get_overview(self) -> dict:
        with self._lock:
            total = self._conn.execute(
                "SELECT COUNT(*) AS count, COALESCE(SUM(size), 0) AS bytes "
                "FROM downloads"
            ).fetchone()
            by_slug = self._conn.execute(
                "SELECT slug, COUNT(*) AS count, COALESCE(SUM(size), 0) AS bytes "
                "FROM downloads GROUP BY slug ORDER BY count DESC"
            ).fetchall()
            today = datetime.now().strftime("%Y-%m-%d")
            today_stats = self._conn.execute(
                "SELECT COALESCE(SUM(count), 0) AS count, "
                "COALESCE(SUM(bytes), 0) AS bytes "
                "FROM daily_dl WHERE date = ?",
                (today,),
            ).fetchone()
            by_geocode = self._conn.execute(
                "SELECT geocode, COUNT(*) AS count FROM downloads "
                "WHERE geocode IS NOT NULL GROUP BY geocode ORDER BY count DESC"
            ).fetchall()
            unique_files = self._conn.execute(
                "SELECT COUNT(DISTINCT slug || ':' || path) AS c FROM downloads"
            ).fetchone()
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
        with self._lock:
            rows = self._conn.execute(
                "SELECT slug, path, size, ts, ua, geocode "
                "FROM downloads ORDER BY ts DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_summary(self, slug: str) -> dict:
        with self._lock:
            total = self._conn.execute(
                "SELECT COUNT(*) AS count, COALESCE(SUM(size), 0) AS bytes, "
                "MAX(ts) AS last_ts FROM downloads WHERE slug = ?",
                (slug,),
            ).fetchone()
            return {
                "total_downloads": total["count"] or 0,
                "total_bytes_served": total["bytes"] or 0,
                "last_download": total["last_ts"],
            }

    def get_top_files_for_slug(
        self,
        slug: str,
        limit: int = 10,
    ) -> list[tuple[str, int]]:
        rows = self.get_top_files(slug=slug, limit=limit)
        return [(r["path"], r["count"]) for r in rows]

    def has_data(self) -> bool:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS c FROM downloads").fetchone()
            return row["c"] > 0

    def migrate_from_json(self, lock_dir: str) -> None:
        json_files = glob.glob(os.path.join(lock_dir, "*.download_stats.json"))
        for jf in json_files:
            slug = os.path.basename(jf).replace(".download_stats.json", "")
            try:
                with open(jf) as f:
                    data = json.load(f)
            except (OSError, json.JSONDecodeError):
                continue
            total = data.get("total_downloads", 0)
            last_ts = data.get("last_download")
            bytes_served = data.get("total_bytes_served", 0)
            if total and last_ts:
                with self._lock:
                    self._conn.execute(
                        "INSERT INTO downloads (slug, path, size, ts, ua, geocode) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (slug, "__legacy__", bytes_served, last_ts, None, None),
                    )
                    date = datetime.fromtimestamp(last_ts).strftime("%Y-%m-%d")
                    self._conn.execute(
                        "INSERT OR IGNORE INTO daily_dl (date, slug, count, bytes) "
                        "VALUES (?, ?, ?, ?)",
                        (date, slug, total, bytes_served),
                    )
                self._conn.commit()
