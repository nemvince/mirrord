import re
import threading

import yaml


def _slugify(name: str) -> str:
    s = name.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


class SyncConfig:
    def __init__(self, data: dict):
        self.interval: int = data.get("interval", 3600)
        self.lock_dir: str = data.get("lock_dir", "/tmp/mirrord")
        if self.interval <= 0:
            raise ValueError(f"sync.interval must be > 0, got {self.interval}")
        if not self.lock_dir.strip():
            raise ValueError("sync.lock_dir must not be empty")


class PluginConfig:
    def __init__(self, data: dict):
        self.type: str = data["type"]
        self.name: str = data.get("name", self.type)
        self.description: str = data.get("description", "")
        self.enabled: bool = data.get("enabled", True)
        self.config: dict = data.get("config", {})
        self.slug: str = data.get("slug", "") or _slugify(self.name)


class ServerConfig:
    def __init__(self, data: dict):
        self.host: str = data.get("host", "0.0.0.0")
        self.port: int = data.get("port", 8080)


class Config:
    def __init__(self, path: str = "config.yaml"):
        self.path = path
        try:
            with open(path, encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
        except FileNotFoundError:
            raise FileNotFoundError(
                f"Config file not found: {path}\n"
                f"Copy config.example.yaml to {path} to get started."
            )
        except yaml.YAMLError as e:
            raise yaml.YAMLError(f"Failed to parse {path}: {e}")

        self.server = ServerConfig(raw.get("server", {}))
        self.sync = SyncConfig(raw.get("sync", {}))
        self.plugins = [PluginConfig(p) for p in raw.get("plugins", [])]

    @property
    def enabled_plugins(self) -> list[PluginConfig]:
        return [p for p in self.plugins if p.enabled]


_config: Config | None = None
_config_lock = threading.Lock()


def load_config(path: str = "config.yaml") -> Config:
    global _config
    with _config_lock:
        _config = Config(path)
    return _config


def get_config() -> Config:
    global _config
    with _config_lock:
        if _config is None:
            _config = load_config()
        return _config


def reload_config() -> Config:
    """Reload config from the same path used originally. Returns the new Config."""
    global _config
    with _config_lock:
        path = _config.path if _config else "config.yaml"
        _config = Config(path)
    return _config
