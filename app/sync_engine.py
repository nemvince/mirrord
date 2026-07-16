import json
import logging
import os
import socket
import threading

from apscheduler.schedulers.background import BackgroundScheduler

from app.config import get_config, reload_config
from app.db import DownloadDB
from app.plugins.registry import get_plugin

logger = logging.getLogger("mirrord.sync")

CONTROL_SOCKET = os.environ.get("MIRRORD_SOCKET", "/tmp/mirrord/control.sock")


class SyncEngine:
    def __init__(self):
        self.config = get_config()
        self.scheduler = BackgroundScheduler()
        self._plugins: list = []
        self._plugins_lock = threading.Lock()
        self._plugin_locks: dict[str, threading.Lock] = {}
        self._stop_event = threading.Event()
        self._started_event = threading.Event()
        self._socket_thread = None
        self._config_mtime: float = 0
        self._reload_lock = threading.Lock()
        self.download_db: DownloadDB | None = None

    def _get_plugins_snapshot(self) -> list:
        """Return a snapshot of the current plugin list (thread-safe)."""
        with self._plugins_lock:
            return list(self._plugins)

    @property
    def plugins(self) -> list:
        """Thread-safe access to the current plugin list."""
        return self._get_plugins_snapshot()

    def start(self) -> None:
        if self._started_event.is_set():
            logger.warning("Sync engine already started, ignoring duplicate start()")
            return
        self._started_event.set()
        self._record_config_mtime()
        db_path = self.config.sync.database_path
        self.download_db = DownloadDB(db_path)
        if self.config.sync.lock_dir:
            self.download_db.migrate_from_json(self.config.sync.lock_dir)
        self._init_plugins()
        interval = self.config.sync.interval
        self.scheduler.add_job(
            self._run_all,
            "interval",
            seconds=interval,
            id="sync_all",
            max_instances=1,
        )
        self.scheduler.start()
        self._start_control_socket()
        self._start_config_watcher()
        with self._plugins_lock:
            count = len(self._plugins)
        logger.info(
            "Sync engine started (interval=%ds, plugins=%d)",
            interval,
            count,
        )

    def stop(self) -> None:
        self._stop_event.set()
        self.scheduler.shutdown(wait=False)
        if self._socket_thread and self._socket_thread.is_alive():
            self._socket_thread.join(timeout=2.0)

    def _init_plugins(self) -> None:
        new_plugins = []
        for pc in self.config.enabled_plugins:
            cls = get_plugin(pc.type)
            if cls is None:
                logger.warning("Unknown plugin type: %s", pc.type)
                continue
            plugin_config = {
                **pc.config,
                "name": pc.name,
                "slug": pc.slug,
                "description": pc.description,
            }
            plugin_config.setdefault("lock_dir", self.config.sync.lock_dir)
            plugin = cls(plugin_config)
            new_plugins.append(plugin)
            # Reuse existing lock if slug survived the reload, else create new
            if plugin.stats.slug not in self._plugin_locks:
                self._plugin_locks[plugin.stats.slug] = threading.Lock()
            # Register identity + restore cumulative stats from the DB
            db = self.download_db
            if db is not None:
                db.upsert_plugin(pc.slug, pc.name, pc.type, pc.description)
                saved = db.load_plugin_stats(pc.slug)
                if saved is not None:
                    plugin.stats = saved
            logger.info("Loaded plugin: %s (%s, slug=%s)", pc.name, pc.type, pc.slug)
        with self._plugins_lock:
            self._plugins = new_plugins

    # ── config hot-reload ──────────────────────────────────────────

    def _record_config_mtime(self) -> None:
        try:
            self._config_mtime = os.path.getmtime(self.config.path)
        except OSError:
            self._config_mtime = 0

    def _start_config_watcher(self) -> None:
        """Poll config.yaml every 10s; hot-reload when mtime changes."""

        def _watch() -> None:
            while not self._stop_event.wait(10):
                try:
                    mtime = os.path.getmtime(self.config.path)
                except OSError:
                    continue
                if mtime != self._config_mtime:
                    logger.info("Config file changed, reloading...")
                    self._reload_engine()

        thread = threading.Thread(target=_watch, daemon=True, name="config-watcher")
        thread.start()

    def _reload_engine(self) -> None:
        """Stop plugins, reload config, re-init with new settings."""
        with self._reload_lock:
            try:
                new_config = reload_config()
            except Exception as e:
                logger.error("Failed to reload config: %s", e)
                return

            # Snapshot the current plugins under the lock
            with self._plugins_lock:
                old_plugins = list(self._plugins)

            logger.info("Stopping %d plugins for reload...", len(old_plugins))
            for plugin in old_plugins:
                plugin.stop()

            # Note: we intentionally do NOT clear _plugin_locks here.
            # Locks are reused when a slug survives the reload.
            # This prevents orphaned sync threads from losing mutual exclusion.

            self.config = new_config
            self._record_config_mtime()
            self._init_plugins()

            active_slugs = {p.stats.slug for p in self._get_plugins_snapshot()}

            # Reschedule with potentially new interval
            try:
                self.scheduler.remove_job("sync_all")
            except Exception:
                pass  # job may not exist yet
            self.scheduler.add_job(
                self._run_all,
                "interval",
                seconds=self.config.sync.interval,
                id="sync_all",
                max_instances=1,
            )
            logger.info(
                "Config reloaded (interval=%ds, plugins=%d)",
                self.config.sync.interval,
                len(active_slugs),
            )

    def _run_all(self) -> None:
        for plugin in self._get_plugins_snapshot():
            thread = threading.Thread(
                target=self._run_plugin, args=(plugin,), daemon=True
            )
            thread.start()

    def _run_plugin(self, plugin) -> None:
        lock = self._plugin_locks.get(plugin.stats.slug)
        if lock is None:
            lock = threading.Lock()
            self._plugin_locks[plugin.stats.slug] = lock
        with lock:
            try:
                logger.info("Syncing %s...", plugin.stats.plugin_name)
                plugin.sync()
                if plugin.stats.status.value == "failed":
                    logger.error(
                        "Sync %s failed: %s (%.1fs)",
                        plugin.stats.plugin_name,
                        plugin.stats.last_error or "unknown",
                        plugin.stats.last_duration or 0,
                    )
                else:
                    logger.info(
                        "Sync %s: %s (%.1fs)",
                        plugin.stats.plugin_name,
                        plugin.stats.status.value,
                        plugin.stats.last_duration or 0,
                    )
            except Exception as e:
                logger.error("Sync %s failed: %s", plugin.stats.plugin_name, e)
            finally:
                if self.download_db is not None:
                    self.download_db.save_plugin_stats(plugin.stats.slug, plugin.stats)

    def get_all_stats(self) -> list:
        return [p.stats.snapshot() for p in self._get_plugins_snapshot()]

    def record_download(
        self,
        slug: str,
        path: str,
        size: int,
        ua: str | None = None,
        geocode: str | None = None,
    ) -> None:
        if self.download_db is not None:
            self.download_db.record(slug, path, size, ua=ua, geocode=geocode)

    def get_download_stats(self) -> dict[str, dict]:
        if self.download_db is None:
            return {}
        return self.download_db.get_all_snapshots()

    def get_next_sync_time(self) -> float | None:
        """Return the next scheduled sync timestamp, or None if not available."""
        job = self.scheduler.get_job("sync_all")
        if job and job.next_run_time:
            return job.next_run_time.timestamp()
        return None

    def get_plugin_by_name(self, name: str):
        for p in self._get_plugins_snapshot():
            if p.stats.plugin_name == name:
                return p
        return None

    def get_plugin_by_slug(self, slug: str):
        for p in self._get_plugins_snapshot():
            if p.stats.slug == slug:
                return p
        return None

    @property
    def plugin_names(self) -> list[str]:
        return [p.stats.plugin_name for p in self._get_plugins_snapshot()]

    def _find_plugin(self, key: str):
        return self.get_plugin_by_name(key) or self.get_plugin_by_slug(key)

    def trigger_sync(self, key: str) -> bool:
        plugin = self._find_plugin(key)
        if plugin is None:
            return False
        thread = threading.Thread(target=self._run_plugin, args=(plugin,), daemon=True)
        thread.start()
        return True

    def trigger_all(self) -> None:
        for plugin in self._get_plugins_snapshot():
            self.trigger_sync(plugin.stats.slug)

    def stop_sync(self, key: str) -> bool:
        plugin = self._find_plugin(key)
        if plugin is None:
            return False
        plugin.stop()
        return True

    def stop_all(self) -> None:
        for plugin in self._get_plugins_snapshot():
            plugin.stop()

    def _start_control_socket(self) -> None:
        self._stop_event.clear()
        self._socket_thread = threading.Thread(target=self._serve_control, daemon=True)
        self._socket_thread.start()

    def _serve_control(self) -> None:
        try:
            os.unlink(CONTROL_SOCKET)
        except OSError:
            pass

        dirname = os.path.dirname(CONTROL_SOCKET)
        if dirname:
            os.makedirs(dirname, exist_ok=True)

        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.bind(CONTROL_SOCKET)
            os.chmod(CONTROL_SOCKET, 0o600)
        except OSError as e:
            logger.error("Failed to bind control socket %s: %s", CONTROL_SOCKET, e)
            sock.close()
            return

        sock.settimeout(1.0)
        sock.listen(5)
        logger.info("Control socket listening on %s", CONTROL_SOCKET)

        try:
            while not self._stop_event.is_set():
                try:
                    conn, _ = sock.accept()
                except TimeoutError:
                    continue
                try:
                    data = b""
                    while not self._stop_event.is_set():
                        chunk = conn.recv(4096)
                        if not chunk:
                            break
                        data += chunk
                        if b"\n" in data:
                            break
                        # Cap at 64 KB to prevent OOM from malicious clients
                        if len(data) > 65536:
                            err = json.dumps(
                                {"ok": False, "error": "payload too large"}
                            )
                            conn.sendall((err + "\n").encode())
                            break
                    else:
                        data = b""
                    if data:
                        response = self._handle_control(data.decode().strip())
                        conn.sendall((json.dumps(response) + "\n").encode())
                except Exception as e:
                    try:
                        conn.sendall(
                            (json.dumps({"ok": False, "error": str(e)}) + "\n").encode()
                        )
                    except Exception:
                        pass
                finally:
                    conn.close()
        finally:
            sock.close()
            try:
                os.unlink(CONTROL_SOCKET)
            except OSError:
                pass

    def _handle_control(self, raw: str) -> dict:
        try:
            req = json.loads(raw)
        except json.JSONDecodeError:
            return {"ok": False, "error": "Invalid JSON"}
        action = req.get("action", "")
        key = req.get("plugin", "")

        if action == "trigger":
            if key:
                ok = self.trigger_sync(key)
                return {"ok": ok, "error": None if ok else f"Plugin not found: {key}"}
            self.trigger_all()
            return {"ok": True}
        elif action == "stop":
            if key:
                ok = self.stop_sync(key)
                return {"ok": ok, "error": None if ok else f"Plugin not found: {key}"}
            self.stop_all()
            return {"ok": True}
        elif action == "status":
            return {
                "ok": True,
                "plugins": [
                    {
                        "name": s.plugin_name,
                        "slug": s.slug,
                        "type": s.plugin_type,
                        "description": s.description,
                        "status": s.status.value,
                        "last_sync": s.last_sync,
                        "last_duration": s.last_duration,
                        "last_error": s.last_error,
                        "total_syncs": s.total_syncs,
                        "total_failures": s.total_failures,
                    }
                    for s in self.get_all_stats()
                ],
            }
        else:
            return {"ok": False, "error": f"Unknown action: {action}"}
