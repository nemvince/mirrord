from app.plugins.arch_rsync import ArchRsyncPlugin
from app.plugins.base import BaseSyncPlugin
from app.plugins.debmirror import DebMirrorPlugin
from app.plugins.local_dir import LocalDirPlugin

_registry: dict[str, type[BaseSyncPlugin]] = {}


def register(plugin_type: str, cls: type[BaseSyncPlugin]) -> None:
    _registry[plugin_type] = cls


def get_plugin(plugin_type: str) -> type[BaseSyncPlugin] | None:
    return _registry.get(plugin_type)


def get_all_types() -> list[str]:
    return list(_registry.keys())


def register_builtins() -> None:
    register("arch_rsync", ArchRsyncPlugin)
    register("debmirror", DebMirrorPlugin)
    register("local_dir", LocalDirPlugin)
