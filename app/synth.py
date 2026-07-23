from __future__ import annotations

import base64
import json
import math
import socket
import struct
import sys
import threading
import time
import wave
from abc import ABC, abstractmethod
from http.client import IncompleteRead
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener, urlopen


class Synthesizer(ABC):
    name = "unknown"
    preferred_chunk_chars: int | None = None

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


class MiMoVoiceCloneSynthesizer(Synthesizer):
    name = "mimo-v2.5-tts-voiceclone"
    preferred_chunk_chars = 400

    _STYLE = {
        0: "明快喜悦，声音带笑意，节奏轻盈但吐字清楚",
        1: "压住怒意但有力量，重音清晰，避免持续喊叫",
        2: "悲伤克制，语速稍慢，带轻微哽咽感但保持清晰",
        3: "紧张恐惧，呼吸和停顿自然，关键处略微加快",
        4: "厌恶和排斥感明显，语气冷淡，重音克制",
        5: "忧郁低沉，节奏舒缓，像在回忆往事",
        6: "惊讶明显，音高和节奏有自然变化，不要夸张失真",
        7: "平静自然，像专业有声书旁白，节奏从容",
    }

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.xiaomimimo.com/v1",
        timeout: int = 300,
        max_attempts: int = 3,
        use_system_proxy: bool = False,
    ):
        key = api_key.strip()
        if key.lower().startswith("bearer "):
            key = key[7:].strip()
        if not key:
            raise RuntimeError("MIMO_API_KEY 未配置")
        if key.startswith("tp-"):
            raise RuntimeError(
                "检测到 Token Plan（tp-）Key。该 Key 不能用于小说 TTS 应用；"
                "请在 MiMo 开放平台“API Keys”页面创建按量调用的 sk- Key"
            )
        if not key.startswith("sk-"):
            raise RuntimeError("MiMo 小说 TTS 仅接受开放平台按量调用的 sk- Key")
        self._api_key = key
        self._endpoint = f"{base_url.rstrip('/')}/chat/completions"
        self._timeout = timeout
        self._max_attempts = max(1, max_attempts)
        self._open = urlopen if use_system_proxy else build_opener(ProxyHandler({})).open
        self._voice_cache: dict[tuple[str, int, int], str] = {}
        self._lock = threading.Lock()

    def _voice_data_uri(self, reference: Path) -> str:
        stat = reference.stat()
        cache_key = (str(reference.resolve()), stat.st_mtime_ns, stat.st_size)
        cached = self._voice_cache.get(cache_key)
        if cached:
            return cached
        raw = reference.read_bytes()
        encoded = base64.b64encode(raw).decode("ascii")
        if len(encoded) > 10 * 1024 * 1024:
            raise ValueError("MiMo 参考音频 Base64 后不能超过 10MB")
        value = f"data:audio/wav;base64,{encoded}"
        self._voice_cache = {cache_key: value}
        return value

    def _instruction(self, emotion: list[float]) -> str:
        dominant = max(range(len(emotion)), key=emotion.__getitem__)
        strength = max(emotion)
        style = self._STYLE[dominant]
        intensity = "情感自然克制" if strength < 0.55 else "情感鲜明但不要损失音色相似度"
        return (
            f"请以专业中文有声书的方式演播。{style}，{intensity}。"
            "根据标点自然停顿，区分旁白与对白，保持参考音色一致，不添加原文之外的内容。"
        )

    def synthesize(self, reference: Path, text: str, emotion: list[float], output: Path) -> None:
        payload = {
            "model": self.name,
            "messages": [
                {"role": "user", "content": self._instruction(emotion)},
                {"role": "assistant", "content": text},
            ],
            "audio": {"format": "wav", "voice": self._voice_data_uri(reference)},
            "temperature": 0.6,
        }
        request = Request(
            self._endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "api-key": self._api_key, "User-Agent": "novel-voice-studio/0.1"},
            method="POST",
        )
        transient_error: Exception | None = None
        with self._lock:
            for attempt in range(1, self._max_attempts + 1):
                try:
                    with self._open(request, timeout=self._timeout) as response:
                        body = json.loads(response.read())
                    audio_data = body["choices"][0]["message"]["audio"]["data"]
                    audio_bytes = base64.b64decode(audio_data, validate=True)
                    break
                except HTTPError as exc:
                    detail = exc.read(1000).decode("utf-8", errors="replace")
                    if exc.code not in {429, 500, 502, 503, 504}:
                        raise RuntimeError(f"MiMo API 请求失败（HTTP {exc.code}）：{detail}") from exc
                    transient_error = RuntimeError(f"MiMo API 暂时不可用（HTTP {exc.code}）：{detail}")
                except (IncompleteRead, ConnectionResetError, TimeoutError, socket.timeout, URLError) as exc:
                    transient_error = exc
                except (KeyError, IndexError, ValueError, json.JSONDecodeError) as exc:
                    raise RuntimeError("MiMo API 返回了无法识别的音频响应") from exc
                if attempt < self._max_attempts:
                    time.sleep(2 ** (attempt - 1))
            else:
                detail = str(transient_error) if transient_error else "未知网络错误"
                raise RuntimeError(f"MiMo 响应传输中断，自动重试 {self._max_attempts} 次后仍失败：{detail}") from transient_error
        if not audio_bytes.startswith(b"RIFF") or audio_bytes[8:12] != b"WAVE":
            raise RuntimeError("MiMo API 返回的内容不是有效 WAV")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(audio_bytes)


def build_synthesizer(
    engine: str,
    source_path: Path | None,
    model_dir: Path | None,
    mimo_api_key: str | None = None,
    mimo_base_url: str = "https://api.xiaomimimo.com/v1",
    mimo_use_system_proxy: bool = False,
) -> Synthesizer:
    if engine == "mock":
        return MockSynthesizer()
    if engine == "indextts2" and source_path and model_dir:
        return IndexTTS2Synthesizer(source_path, model_dir)
    if engine == "mimo" and mimo_api_key:
        return MiMoVoiceCloneSynthesizer(
            mimo_api_key,
            mimo_base_url,
            use_system_proxy=mimo_use_system_proxy,
        )
    raise RuntimeError("NVS_ENGINE 必须为 mock、mimo，或正确配置的 indextts2")
