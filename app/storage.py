from __future__ import annotations

import json
import os
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Any


class Store:
    def __init__(self, root: Path):
        self.root = root
        self._lock = threading.RLock()
        for name in ("voices", "books", "jobs"):
            (root / name).mkdir(parents=True, exist_ok=True)

    @staticmethod
    def new_id() -> str:
        return uuid.uuid4().hex[:16]

    def directory(self, kind: str, item_id: str) -> Path:
        if kind not in {"voices", "books", "jobs"} or not item_id.isalnum():
            raise ValueError("invalid storage path")
        path = self.root / kind / item_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def save_meta(self, kind: str, item_id: str, data: dict[str, Any]) -> None:
        target = self.directory(kind, item_id) / "meta.json"
        with self._lock:
            fd, temp_name = tempfile.mkstemp(dir=target.parent, suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump(data, stream, ensure_ascii=False, indent=2)
                os.replace(temp_name, target)
            finally:
                if os.path.exists(temp_name):
                    os.unlink(temp_name)

    def load_meta(self, kind: str, item_id: str) -> dict[str, Any]:
        path = self.root / kind / item_id / "meta.json"
        if not path.exists():
            raise KeyError(item_id)
        with self._lock:
            return json.loads(path.read_text(encoding="utf-8"))

    def list_meta(self, kind: str) -> list[dict[str, Any]]:
        result = []
        base = self.root / kind
        for path in base.glob("*/meta.json"):
            try:
                result.append(json.loads(path.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError):
                continue
        return sorted(result, key=lambda item: item.get("created_at", ""), reverse=True)
