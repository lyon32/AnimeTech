"""Published-state persistence keyed on episode_key (restart + duplicate protection)."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


class PublishedState:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict[str, Any] = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def is_published(self, episode_key: str) -> bool:
        return episode_key in self.data

    def get(self, episode_key: str) -> dict[str, Any] | None:
        return self.data.get(episode_key)

    def record(self, episode_key: str, meta: dict[str, Any]) -> None:
        self.data[episode_key] = meta
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, indent=2, ensure_ascii=False)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise