from pathlib import Path

import pytest

from app.db import DownloadDB
from app.models import SyncJobStats, SyncStatus


@pytest.fixture
def lock_dir(tmp_path: Path) -> str:
    d = tmp_path / "lock"
    d.mkdir()
    return str(d)


@pytest.fixture
def db(lock_dir: str) -> DownloadDB:
    return DownloadDB(str(Path(lock_dir) / "mirrord.db"))


def test_schema_initialized(db: DownloadDB) -> None:
    rows = db._query("SELECT name FROM sqlite_master WHERE type='table'")
    names = {r["name"] for r in rows}
    assert {"downloads", "daily_dl", "plugin_stats"} <= names


def test_record_and_summary(db: DownloadDB) -> None:
    db.record("arch", "/iso/arch.iso", 1000, ua="curl", geocode="US")
    db.record("arch", "/iso/arch.iso", 500)
    summary = db.get_summary("arch")
    assert summary["total_downloads"] == 2
    assert summary["total_bytes_served"] == 1500
    assert summary["last_download"] is not None


def test_get_all_snapshots_empty(db: DownloadDB) -> None:
    assert db.get_all_snapshots() == {}


def test_get_all_snapshots(db: DownloadDB) -> None:
    db.record("a", "/x", 10)
    db.record("b", "/y", 20)
    snaps = db.get_all_snapshots()
    assert set(snaps) == {"a", "b"}
    assert snaps["a"]["total_bytes_served"] == 10


def test_daily_totals_rollup(db: DownloadDB) -> None:
    db.record("arch", "/iso", 100)
    daily = db.get_daily_totals(slug="arch", days=30)
    assert len(daily) == 1
    assert daily[0]["count"] == 1
    assert daily[0]["bytes"] == 100


def test_overview(db: DownloadDB) -> None:
    db.record("arch", "/iso", 100, geocode="US")
    overview = db.get_overview()
    assert overview["total_downloads"] == 1
    assert overview["total_bytes"] == 100
    assert overview["by_slug"][0]["slug"] == "arch"
    assert overview["by_geocode"][0]["geocode"] == "US"


def test_plugin_stats_round_trip(db: DownloadDB) -> None:
    db.upsert_plugin("arch", "Arch Linux", "arch_rsync", "mirror")
    stats = SyncJobStats(
        plugin_name="Arch Linux",
        plugin_type="arch_rsync",
        slug="arch",
        description="mirror",
        status=SyncStatus.SUCCESS,
        last_sync=123.0,
        total_syncs=3,
        total_failures=1,
        total_bytes_transferred=9000,
    )
    db.save_plugin_stats("arch", stats)
    loaded = db.load_plugin_stats("arch")
    assert loaded is not None
    assert loaded.slug == "arch"
    assert loaded.total_syncs == 3
    assert loaded.total_failures == 1
    assert loaded.status == SyncStatus.SUCCESS


def test_load_unknown_plugin_returns_none(db: DownloadDB) -> None:
    assert db.load_plugin_stats("nope") is None


def test_upsert_preserves_counters(db: DownloadDB) -> None:
    db.upsert_plugin("arch", "Arch", "arch_rsync")
    db.save_plugin_stats(
        "arch",
        SyncJobStats("Arch", "arch_rsync", slug="arch", total_syncs=5),
    )
    # Re-register (e.g. on reload) must not wipe total_syncs
    db.upsert_plugin("arch", "Arch Renamed", "arch_rsync")
    loaded = db.load_plugin_stats("arch")
    assert loaded is not None
    assert loaded.total_syncs == 5
    assert loaded.plugin_name == "Arch Renamed"


def test_migrate_from_json(lock_dir: str, db: DownloadDB) -> None:
    (Path(lock_dir) / "arch.stats.json").write_text(
        '{"total_syncs": 7, "total_failures": 2, '
        '"total_bytes_transferred": 1234, "dir_size": 100, '
        '"last_sync": 99.0, "last_duration": 1.5, '
        '"last_size_bytes": 10, "last_error": null}'
    )
    db.migrate_from_json(lock_dir)
    loaded = db.load_plugin_stats("arch")
    assert loaded is not None
    assert loaded.total_syncs == 7
    assert loaded.total_failures == 2
    # JSON file consumed after migration
    assert not (Path(lock_dir) / "arch.stats.json").exists()
