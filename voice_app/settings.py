from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from .schemas import DesktopSettings


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_ROOT = Path(os.environ.get("VOICE_APP_DATA_DIR", PROJECT_ROOT))
DEFAULT_SETTINGS_PATH = DATA_ROOT / "outputs" / "settings.json"


class SettingsStore:
    def __init__(self, path: Path = DEFAULT_SETTINGS_PATH):
        self.path = Path(path)
        self._lock = threading.Lock()

    def load(self) -> DesktopSettings:
        with self._lock:
            if not self.path.is_file():
                return DesktopSettings()
            try:
                return DesktopSettings.model_validate_json(
                    self.path.read_text(encoding="utf-8")
                )
            except (OSError, ValueError, json.JSONDecodeError):
                return DesktopSettings()

    def save(self, settings: DesktopSettings) -> DesktopSettings:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(settings.model_dump_json(indent=2), encoding="utf-8")
            temporary.replace(self.path)
        return settings
