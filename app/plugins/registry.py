from typing import Type

from app.plugins.arch_rsync import ArchRsyncPlugin
from app.plugins.base import BaseSyncPlugin
from app.plugins.debmirror import DebMirrorPlugin

_registry: dict[str, Type[BaseSyncPlugin]] = {}


def register(plugin_type: str, cls: Type[BaseSyncPlugin]) -> None:
    _registry[plugin_type] = cls


def get_plugin(plugin_type: str) -> Type[BaseSyncPlugin] | None:
    return _registry.get(plugin_type)


def get_all_types() -> list[str]:
    return list(_registry.keys())


def register_builtins() -> None:
    register("arch_rsync", ArchRsyncPlugin)
    register("debmirror", DebMirrorPlugin)
