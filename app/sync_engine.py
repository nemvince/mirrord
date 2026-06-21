import json
import logging
import os
import socket
import threading

from apscheduler.schedulers.background import BackgroundScheduler

from app.config import get_config, reload_config
from app.download_stats import DownloadTracker
from app.plugins.registry import get_plugin

logger = logging.getLogger("mirrord.sync")

CONTROL_SOCKET = os.environ.get("MIRRORD_SOCKET", "/tmp/mirrord/control.sock")


class SyncEngine:
    def __init__(self):
        self.config = get_config()
        self.scheduler = BackgroundScheduler()
        self.plugins: list = []
        self._plugin_locks: dict[str, threading.Lock] = {}
        self._stop_event = threading.Event()
        self._socket_thread = None
        self._started = False
        self._config_mtime: float = 0
        self._reload_lock = threading.Lock()
        self.download_tracker = DownloadTracker()

    def start(self) -> None:
        if self._started:
            logger.warning("Sync engine already started, ignoring duplicate start()")
            return
        self._started = True
        self._record_config_mtime()
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
        logger.info(
            "Sync engine started (interval=%ds, plugins=%d)",
            interval,
            len(self.plugins),
        )

    def stop(self) -> None:
        self._stop_event.set()
        self.scheduler.shutdown(wait=False)
        if self._socket_thread and self._socket_thread.is_alive():
            self._socket_thread.join(timeout=2.0)

    def _init_plugins(self) -> None:
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
            self.plugins.append(plugin)
            self._plugin_locks[plugin.stats.slug] = threading.Lock()
            # Persist stats across restarts
            plugin.stats._stats_path = os.path.join(
                self.config.sync.lock_dir, f"{pc.slug}.stats.json"
            )
            plugin.stats.load()
            # Initialise download tracker for this plugin
            self.download_tracker.ensure_plugin(
                pc.slug,
                stats_path=os.path.join(
                    self.config.sync.lock_dir, f"{pc.slug}.download_stats.json"
                ),
            )
            logger.info("Loaded plugin: %s (%s, slug=%s)", pc.name, pc.type, pc.slug)

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

            logger.info("Stopping %d plugins for reload...", len(self.plugins))
            for plugin in self.plugins:
                plugin.stop()
            self.plugins.clear()
            self._plugin_locks.clear()

            self.config = new_config
            self._record_config_mtime()
            self._init_plugins()

            # Prune download stats for removed plugins
            active_slugs = {p.stats.slug for p in self.plugins}
            self.download_tracker.prune_stale(active_slugs)

            # Reschedule with potentially new interval
            self.scheduler.remove_job("sync_all")
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
                len(self.plugins),
            )

    def _run_all(self) -> None:
        for plugin in self.plugins:
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

    def get_all_stats(self) -> list:
        return [p.stats.snapshot() for p in self.plugins]

    def record_download(self, slug: str, path: str, size: int) -> None:
        self.download_tracker.record_download(slug, path, size)

    def get_download_stats(self) -> dict[str, dict]:
        return self.download_tracker.get_all_snapshots()

    def get_next_sync_time(self) -> float | None:
        """Return the next scheduled sync timestamp, or None if not available."""
        job = self.scheduler.get_job("sync_all")
        if job and job.next_run_time:
            return job.next_run_time.timestamp()
        return None

    def get_plugin_by_name(self, name: str):
        for p in self.plugins:
            if p.stats.plugin_name == name:
                return p
        return None

    def get_plugin_by_slug(self, slug: str):
        for p in self.plugins:
            if p.stats.slug == slug:
                return p
        return None

    @property
    def plugin_names(self) -> list[str]:
        return [p.stats.plugin_name for p in self.plugins]

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
        for plugin in self.plugins:
            self.trigger_sync(plugin.stats.slug)

    def stop_sync(self, key: str) -> bool:
        plugin = self._find_plugin(key)
        if plugin is None:
            return False
        plugin.stop()
        return True

    def stop_all(self) -> None:
        for plugin in self.plugins:
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
