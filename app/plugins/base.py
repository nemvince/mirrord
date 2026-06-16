import subprocess
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

from app.models import SyncJobStats


class BaseSyncPlugin(ABC):
    plugin_type: str = ""

    def __init__(self, config: dict):
        self.config = config
        target = config.get("target", "")
        if not target:
            raise ValueError(
                f"Plugin '{config.get('name', self.plugin_type)}' is missing required 'target' config"
            )
        self.stats = SyncJobStats(
            plugin_name=config.get("name", self.plugin_type),
            plugin_type=self.plugin_type,
            slug=config.get("slug", ""),
            description=config.get("description", ""),
        )
        self._process: Optional[subprocess.Popen] = None

    @property
    def target_dir(self) -> Path:
        return Path(self.config["target"])

    def stop(self) -> None:
        if self._process and self._process.returncode is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait()

    @abstractmethod
    def sync(self) -> None: ...
