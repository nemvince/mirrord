import ipaddress
import os
import re
import threading

import yaml


def _slugify(name: str) -> str:
    s = name.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


# Networks whose requests are allowed to assert client identity via
# X-Forwarded-For / X-Real-IP / Forwarded. Anything outside these is treated
# as the actual client (spoofing the headers is ignored).
DEFAULT_TRUSTED_PROXIES = [
    "127.0.0.0/8",
    "::1/128",
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
]


class SyncConfig:
    def __init__(self, data: dict):
        self.interval: int = data.get("interval", 3600)
        self.lock_dir: str = data.get("lock_dir", "/tmp/mirrord")
        self.database_path: str = data.get(
            "database_path", os.path.join(self.lock_dir, "mirrord.db")
        )
        if self.interval <= 0:
            raise ValueError(f"sync.interval must be > 0, got {self.interval}")
        if not self.lock_dir.strip():
            raise ValueError("sync.lock_dir must not be empty")
        if not self.database_path.strip():
            raise ValueError("sync.database_path must not be empty")
        self.trusted_proxies: list[ipaddress._BaseNetwork] = [
            ipaddress.ip_network(n)
            for n in data.get("trusted_proxies", DEFAULT_TRUSTED_PROXIES)
        ]

    def is_trusted_proxy(self, ip: str) -> bool:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        return any(addr in net for net in self.trusted_proxies)


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
            _config = Config("config.yaml")
        return _config


def reload_config() -> Config:
    """Reload config from the same path used originally. Returns the new Config."""
    global _config
    with _config_lock:
        path = _config.path if _config else "config.yaml"
        _config = Config(path)
    return _config
