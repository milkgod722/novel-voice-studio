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
    allow_mock_jobs: bool = False
    mimo_api_key: str | None = None
    mimo_base_url: str = "https://api.xiaomimimo.com/v1"
    mimo_use_system_proxy: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        root = Path(__file__).resolve().parents[1]
        path = os.getenv("INDEXTTS_PATH")
        model = os.getenv("INDEXTTS_MODEL_DIR")
        mimo_api_key = os.getenv("MIMO_API_KEY")
        engine = os.getenv("NVS_ENGINE") or ("mimo" if mimo_api_key else "mock")
        return cls(
            data_dir=Path(os.getenv("NVS_DATA_DIR", root / "data")).resolve(),
            engine=engine.lower(),
            indextts_path=Path(path).resolve() if path else None,
            model_dir=Path(model).resolve() if model else None,
            max_upload_mb=int(os.getenv("NVS_MAX_UPLOAD_MB", "200")),
            chunk_chars=int(os.getenv("NVS_CHUNK_CHARS", "110")),
            allow_mock_jobs=os.getenv("NVS_ALLOW_MOCK_JOBS", "").lower() in {"1", "true", "yes"},
            mimo_api_key=mimo_api_key,
            mimo_base_url=os.getenv("MIMO_BASE_URL", "https://api.xiaomimimo.com/v1").rstrip("/"),
            mimo_use_system_proxy=os.getenv("MIMO_USE_SYSTEM_PROXY", "").lower() in {"1", "true", "yes"},
        )
