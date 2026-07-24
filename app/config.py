from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    engine: str
    max_upload_mb: int = 200
    chunk_chars: int = 110
    allow_mock_jobs: bool = False
    mimo_api_key: str | None = None
    mimo_base_url: str = "https://api.xiaomimimo.com/v1"
    mimo_use_system_proxy: bool = False
    mimo_model: str = "mimo-v2.5-tts-voiceclone"
    mimo_auth_mode: str = "api-key"

    @classmethod
    def from_env(cls) -> "Settings":
        root = Path(__file__).resolve().parents[1]
        mimo_api_key = os.getenv("VOICE_CLONE_API_KEY") or os.getenv("MIMO_API_KEY")
        normalized_key = (mimo_api_key or "").strip()
        if normalized_key.lower().startswith("bearer "):
            normalized_key = normalized_key[7:].strip()
        engine = os.getenv("NVS_ENGINE") or ("mimo" if normalized_key.startswith("sk-") else "mock")
        return cls(
            data_dir=Path(os.getenv("NVS_DATA_DIR", root / "data")).resolve(),
            engine=engine.lower(),
            max_upload_mb=int(os.getenv("NVS_MAX_UPLOAD_MB", "200")),
            chunk_chars=int(os.getenv("NVS_CHUNK_CHARS", "110")),
            allow_mock_jobs=os.getenv("NVS_ALLOW_MOCK_JOBS", "").lower() in {"1", "true", "yes"},
            mimo_api_key=mimo_api_key,
            mimo_base_url=(
                os.getenv("VOICE_CLONE_API_URL")
                or os.getenv("MIMO_API_URL")
                or os.getenv("MIMO_BASE_URL")
                or "https://api.xiaomimimo.com/v1"
            ).strip(),
            mimo_use_system_proxy=os.getenv("MIMO_USE_SYSTEM_PROXY", "").lower() in {"1", "true", "yes"},
            mimo_model=(
                os.getenv("VOICE_CLONE_MODEL")
                or os.getenv("MIMO_MODEL")
                or "mimo-v2.5-tts-voiceclone"
            ).strip(),
            mimo_auth_mode=(
                os.getenv("VOICE_CLONE_AUTH_MODE")
                or os.getenv("MIMO_AUTH_MODE")
                or "api-key"
            ).strip().lower(),
        )
