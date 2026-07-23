from __future__ import annotations

import math
import struct
import sys
import threading
import wave
from abc import ABC, abstractmethod
from pathlib import Path


class Synthesizer(ABC):
    name = "unknown"

    @abstractmethod
    def synthesize(self, reference: Path, text: str, emotion: list[float], output: Path) -> None:
        raise NotImplementedError


class MockSynthesizer(Synthesizer):
    """Deterministic audible output for pipeline tests; it is not speech."""

    name = "mock"

    def synthesize(self, reference: Path, text: str, emotion: list[float], output: Path) -> None:
        rate = 24000
        duration = max(0.18, min(1.5, len(text) * 0.018))
        frequency = 180 + int(emotion.index(max(emotion)) * 22)
        frames = bytearray()
        for i in range(int(rate * duration)):
            envelope = min(1.0, i / 240) * min(1.0, (rate * duration - i) / 240)
            sample = int(5000 * envelope * math.sin(2 * math.pi * frequency * i / rate))
            frames.extend(struct.pack("<h", sample))
        output.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(output), "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(rate)
            writer.writeframes(frames)


class IndexTTS2Synthesizer(Synthesizer):
    name = "indextts2"

    def __init__(self, source_path: Path, model_dir: Path):
        if not source_path.exists() or not model_dir.exists():
            raise RuntimeError("INDEXTTS_PATH 或 INDEXTTS_MODEL_DIR 不存在")
        sys.path.insert(0, str(source_path))
        try:
            from indextts.infer_v2 import IndexTTS2
        except ImportError as exc:
            raise RuntimeError("无法导入 IndexTTS2；请按 README 安装真实模型环境") from exc
        self._model = IndexTTS2(
            cfg_path=str(model_dir / "config.yaml"), model_dir=str(model_dir),
            use_fp16=True, use_cuda_kernel=False, use_deepspeed=False,
        )
        self._lock = threading.Lock()

    def synthesize(self, reference: Path, text: str, emotion: list[float], output: Path) -> None:
        output.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._model.infer(
                spk_audio_prompt=str(reference), text=text, output_path=str(output),
                emo_vector=emotion, emo_alpha=0.65, use_random=False, verbose=False,
            )


def build_synthesizer(engine: str, source_path: Path | None, model_dir: Path | None) -> Synthesizer:
    if engine == "mock":
        return MockSynthesizer()
    if engine == "indextts2" and source_path and model_dir:
        return IndexTTS2Synthesizer(source_path, model_dir)
    raise RuntimeError("NVS_ENGINE 必须为 mock，或同时配置 INDEXTTS_PATH 与 INDEXTTS_MODEL_DIR")
