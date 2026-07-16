from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

from app.config import load_config
from app.plugins.registry import register_builtins
from app.sync_engine import SyncEngine


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[SyncEngine]:
    lock_dir = tmp_path / "lock"
    lock_dir.mkdir()
    target = tmp_path / "data"
    target.mkdir()

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "sync": {"interval": 3600, "lock_dir": str(lock_dir)},
                "server": {"host": "127.0.0.1", "port": 8099},
                "plugins": [
                    {
                        "type": "local_dir",
                        "name": "Local",
                        "slug": "local",
                        "config": {"target": str(target)},
                    }
                ],
            }
        )
    )
    load_config(str(config_path))
    register_builtins()
    eng = SyncEngine()
    eng.start()
    yield eng
    eng.stop()


def test_db_created(engine: SyncEngine, tmp_path: Path) -> None:
    db_path = tmp_path / "lock" / "mirrord.db"
    assert db_path.exists()


def test_plugin_stats_registered(engine: SyncEngine) -> None:
    assert engine.download_db is not None
    stats = engine.download_db.load_plugin_stats("local")
    assert stats is not None
    assert stats.plugin_name == "Local"
    assert stats.plugin_type == "local_dir"


def test_get_all_stats(engine: SyncEngine) -> None:
    stats = engine.get_all_stats()
    assert len(stats) == 1
    assert stats[0].slug == "local"


def test_record_download_persists(engine: SyncEngine) -> None:
    engine.record_download("local", "/file", 42)
    assert engine.download_db is not None
    summary = engine.download_db.get_summary("local")
    assert summary["total_downloads"] == 1
    assert summary["total_bytes_served"] == 42


def test_migration_sets_lock_dir(engine: SyncEngine, tmp_path: Path) -> None:
    # lock_dir must be resolved so the DB path is correct
    assert engine.config.sync.lock_dir == str(tmp_path / "lock")
