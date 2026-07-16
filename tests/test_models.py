from app.models import SyncJobStats, SyncStatus


def test_success_updates_counters() -> None:
    s = SyncJobStats("Arch", "arch_rsync", slug="arch")
    s.start()
    s.success(duration=1.5, bytes_transferred=100)
    assert s.status == SyncStatus.SUCCESS
    assert s.total_syncs == 1
    assert s.total_bytes_transferred == 100
    assert s.last_duration == 1.5
    assert s.last_error is None


def test_failed_updates_counters() -> None:
    s = SyncJobStats("Arch", "arch_rsync", slug="arch")
    s.failed("boom")
    assert s.status == SyncStatus.FAILED
    assert s.total_failures == 1
    assert s.last_error == "boom"


def test_skipped_sets_status() -> None:
    s = SyncJobStats("Arch", "arch_rsync", slug="arch")
    s.skipped()
    assert s.status == SyncStatus.SKIPPED


def test_set_progress_under_lock() -> None:
    s = SyncJobStats("Arch", "arch_rsync", slug="arch")
    s.set_progress(42)
    assert s.progress_pct == 42


def test_snapshot_is_copy() -> None:
    s = SyncJobStats("Arch", "arch_rsync", slug="arch")
    s.total_syncs = 3
    snap = s.snapshot()
    snap.total_syncs = 99
    assert s.total_syncs == 3
    assert snap.total_syncs == 99
