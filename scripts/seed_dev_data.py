#!/usr/bin/env python
"""Seed the mirrord SQLite DB with dummy download data for local development.

Fills the ``downloads`` / ``daily_dl`` tables (and registers a couple of
plugin identities) with backdated, randomised traffic so every chart on the
``/stats/`` page renders with realistic-looking data.

Usage:
    uv run python scripts/seed_dev_data.py                 # uses config.yaml
    uv run python scripts/seed_dev_data.py --db /tmp/x.db  # explicit DB path
    uv run python scripts/seed_dev_data.py --days 30 --count 4000 --clear

Timestamps are written directly (bypassing DownloadDB.record, which always
stamps "now") so the daily chart shows a real history.
"""

from __future__ import annotations

import argparse
import os
import random
import sys
import time
from datetime import datetime

# Allow running as `python scripts/seed_dev_data.py` from the repo root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import DownloadDB  # noqa: E402

# (slug, human name, plugin_type) — registered so the "Downloads per Mirror"
# chart shows friendly names.
_MIRRORS = [
    ("arch-kernel", "Arch Linux (kernel.org)", "arch_rsync"),
    ("debian-http", "Debian (HTTP)", "debmirror"),
    ("fedora-mirror", "Fedora", "local_dir"),
    ("ubuntu-ports", "Ubuntu Ports", "local_dir"),
]

# Popular files per mirror, with a rough relative weight to make "Top Files"
# interesting.
_FILES: dict[str, list[tuple[str, int]]] = {
    "arch-kernel": [
        ("/core/os/x86_64/linux-6.9.arch1-1-x86_64.pkg.tar.zst", 40),
        ("/extra/os/x86_64/firefox-127.0-1-x86_64.pkg.tar.zst", 30),
        ("/core/os/x86_64/glibc-2.39-3-x86_64.pkg.tar.zst", 25),
        ("/extra/os/x86_64/gcc-14.1.1-1-x86_64.pkg.tar.zst", 15),
        ("/core/os/x86_64/systemd-256-1-x86_64.pkg.tar.zst", 12),
    ],
    "debian-http": [
        ("/dists/bookworm/main/binary-amd64/Packages.gz", 35),
        ("/pool/main/l/linux/linux-image-6.1.0-amd64.deb", 28),
        ("/pool/main/f/firefox-esr/firefox-esr_115_amd64.deb", 20),
        ("/dists/bookworm/Release", 18),
    ],
    "fedora-mirror": [
        ("/releases/40/Everything/x86_64/os/repodata/repomd.xml", 22),
        ("/releases/40/Workstation/x86_64/iso/Fedora-40.iso", 10),
    ],
    "ubuntu-ports": [
        ("/dists/noble/main/binary-arm64/Packages.xz", 14),
        ("/pool/main/l/linux/linux-image-generic_arm64.deb", 9),
    ],
}

# Country codes weighted so the geography chart has a clear ranking.
_GEO = (
    ["US"] * 30
    + ["DE"] * 22
    + ["GB"] * 14
    + ["FR"] * 11
    + ["HU"] * 9
    + ["JP"] * 7
    + ["BR"] * 5
    + ["IN"] * 5
    + ["AU"] * 3
    + [None] * 6  # some downloads have no geocode (GeoIP disabled / private IP)
)

_UAS = [
    "pacman/6.1.0",
    "curl/8.7.1",
    "Wget/1.24.5",
    "APT-HTTP/1.3 (2.6.1)",
    "libdnf (Fedora 40)",
    "Mozilla/5.0 (X11; Linux x86_64)",
]


def _resolve_db_path(explicit: str | None) -> str:
    if explicit:
        return explicit
    env = os.environ.get("MIRRORD_DATABASE_PATH")
    if env:
        return env
    try:
        from app.config import get_config

        return get_config().sync.database_path
    except Exception as exc:  # pragma: no cover - dev convenience fallback
        fallback = "/tmp/mirrord/mirrord.db"
        print(f"! Could not load config ({exc}); using {fallback}")
        return fallback


def _clear(db: DownloadDB) -> None:
    with db._lock:
        db._conn.execute("DELETE FROM downloads")
        db._conn.execute("DELETE FROM daily_dl")
        db._conn.commit()


def _insert(
    db: DownloadDB,
    slug: str,
    path: str,
    size: int,
    ts: float,
    ua: str | None,
    geocode: str | None,
) -> None:
    """Insert a backdated download into both tables (mirrors record())."""
    date = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
    with db._lock:
        db._conn.execute(
            "INSERT INTO downloads (slug, path, size, ts, ua, geocode) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (slug, path, size, ts, ua, geocode),
        )
        db._conn.execute(
            """INSERT INTO daily_dl (date, slug, count, bytes) VALUES (?, ?, 1, ?)
               ON CONFLICT(date, slug) DO UPDATE SET
                   count = count + 1,
                   bytes = bytes + excluded.bytes""",
            (date, slug, size),
        )


def seed(db: DownloadDB, days: int, count: int, seed_val: int) -> None:
    rng = random.Random(seed_val)

    # Register plugin identities so slug -> name labels resolve on the chart.
    for slug, name, ptype in _MIRRORS:
        db.upsert_plugin(slug, name, ptype, description=f"Seeded dev data for {name}")

    # Weight mirrors so the per-mirror chart has a clear ordering.
    mirror_weights = [50, 35, 20, 10]
    slugs = [m[0] for m in _MIRRORS]

    now = time.time()
    span = days * 86400

    with db._lock:
        for _ in range(count):
            slug = rng.choices(slugs, weights=mirror_weights, k=1)[0]
            files = _FILES[slug]
            paths, weights = zip(*files, strict=True)
            path = rng.choices(paths, weights=weights, k=1)[0]

            # Bias recent days a little heavier than old ones.
            age_frac = rng.random() ** 1.5  # skew toward 0 (recent)
            ts = now - age_frac * span

            size = rng.randint(50_000, 350_000_000)
            ua = rng.choice(_UAS)
            geocode = rng.choice(_GEO)

            _insert(db, slug, path, size, ts, ua, geocode)
        db._conn.commit()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", help="SQLite DB path (default: from config.yaml)")
    ap.add_argument("--days", type=int, default=30, help="history window (default 30)")
    ap.add_argument(
        "--count", type=int, default=4000, help="number of downloads (default 4000)"
    )
    ap.add_argument("--seed", type=int, default=1337, help="RNG seed (default 1337)")
    ap.add_argument(
        "--clear",
        action="store_true",
        help="wipe existing downloads/daily_dl before seeding",
    )
    args = ap.parse_args()

    db_path = _resolve_db_path(args.db)
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    print(f"→ seeding {db_path}")

    db = DownloadDB(db_path)
    if args.clear:
        print("  clearing existing download data")
        _clear(db)

    seed(db, days=args.days, count=args.count, seed_val=args.seed)

    ov = db.get_overview()
    print(f"✓ {ov['total_downloads']} downloads across {len(ov['by_slug'])} mirrors")
    print(f"  {len(ov['by_geocode'])} distinct geocodes")
    print(f"  {ov['unique_files']} unique files")
    print("  open /stats/ to see the charts")


if __name__ == "__main__":
    main()
