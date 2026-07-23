from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    engine: str
    indextts_path: Path | None
    model_dir: Path | None
    max_upload_mb: int = 200
    chunk_chars: int = 110

    @classmethod
    def from_env(cls) -> "Settings":
        root = Path(__file__).resolve().parents[1]
        path = os.getenv("INDEXTTS_PATH")
        model = os.getenv("INDEXTTS_MODEL_DIR")
        return cls(
            data_dir=Path(os.getenv("NVS_DATA_DIR", root / "data")).resolve(),
            engine=os.getenv("NVS_ENGINE", "mock").lower(),
            indextts_path=Path(path).resolve() if path else None,
            model_dir=Path(model).resolve() if model else None,
            max_upload_mb=int(os.getenv("NVS_MAX_UPLOAD_MB", "200")),
            chunk_chars=int(os.getenv("NVS_CHUNK_CHARS", "110")),
        )
