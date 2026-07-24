from __future__ import annotations

import base64
import importlib.util
import json
import math
import re
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
from urllib.parse import urlsplit, urlunsplit
from urllib.request import ProxyHandler, Request, build_opener, urlopen


DEFAULT_MIMO_MODEL = "mimo-v2.5-tts-voiceclone"
DEFAULT_QWEN_MODEL = "Qwen/Qwen3-TTS-12Hz-1.7B-Base"
MIMO_MODEL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
QWEN_MODEL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/\\-]{0,511}$")
VOICE_CLONE_AUTH_MODES = {"api-key", "bearer"}


class Synthesizer(ABC):
    name = "unknown"
    provider = "unknown"
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
    name = DEFAULT_MIMO_MODEL
    provider = "voice-clone"
    protocol = "mimo-chat"
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
        model: str = DEFAULT_MIMO_MODEL,
        auth_mode: str = "api-key",
    ):
        key = api_key.strip()
        if key.lower().startswith("bearer "):
            key = key[7:].strip()
        if not key:
            raise RuntimeError("语音克隆 API Key 未配置")
        model = model.strip()
        if not MIMO_MODEL_PATTERN.fullmatch(model):
            raise RuntimeError("语音克隆模型名称格式无效：仅允许字母、数字、点、下划线、冒号、斜杠和连字符")
        auth_mode = auth_mode.strip().lower()
        if auth_mode not in VOICE_CLONE_AUTH_MODES:
            raise RuntimeError("鉴权方式必须为 api-key 或 bearer")
        endpoint = self._normalize_endpoint(base_url)
        hostname = (urlsplit(endpoint).hostname or "").lower()
        if hostname == "xiaomimimo.com" or hostname.endswith(".xiaomimimo.com"):
            if key.startswith("tp-"):
                raise RuntimeError(
                    "检测到 Token Plan（tp-）Key。该 Key 不能用于小说 TTS 应用；"
                    "请在 MiMo 开放平台“API Keys”页面创建按量调用的 sk- Key"
                )
            if not key.startswith("sk-"):
                raise RuntimeError("小米 MiMo 官方接口仅接受开放平台按量调用的 sk- Key")
        self.name = model
        self._api_key = key
        self.api_url = endpoint
        self.auth_mode = auth_mode
        self._endpoint = endpoint
        self._timeout = timeout
        self._max_attempts = max(1, max_attempts)
        self._open = urlopen if use_system_proxy else build_opener(ProxyHandler({})).open
        self._voice_cache: dict[tuple[str, int, int], str] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _normalize_endpoint(value: str) -> str:
        raw = value.strip()
        parts = urlsplit(raw)
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            raise RuntimeError("语音克隆 API URL 必须是有效的 http:// 或 https:// 地址")
        if parts.username or parts.password or parts.fragment:
            raise RuntimeError("语音克隆 API URL 不能包含账号、密码或片段标识")
        path = parts.path.rstrip("/")
        if not path or path.endswith("/v1"):
            path = f"{path}/chat/completions"
        return urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))

    def _voice_data_uri(self, reference: Path) -> str:
        stat = reference.stat()
        cache_key = (str(reference.resolve()), stat.st_mtime_ns, stat.st_size)
        cached = self._voice_cache.get(cache_key)
        if cached:
            return cached
        raw = reference.read_bytes()
        encoded = base64.b64encode(raw).decode("ascii")
        if len(encoded) > 10 * 1024 * 1024:
            raise ValueError("语音克隆参考音频 Base64 后不能超过 10MB")
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
        headers = {"Content-Type": "application/json", "User-Agent": "novel-voice-studio/0.1"}
        if self.auth_mode == "bearer":
            headers["Authorization"] = f"Bearer {self._api_key}"
        else:
            headers["api-key"] = self._api_key
        request = Request(
            self._endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
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
                        raise RuntimeError(f"语音克隆 API 请求失败（HTTP {exc.code}）：{detail}") from exc
                    transient_error = RuntimeError(f"语音克隆 API 暂时不可用（HTTP {exc.code}）：{detail}")
                except (IncompleteRead, ConnectionResetError, TimeoutError, socket.timeout, URLError) as exc:
                    transient_error = exc
                except (KeyError, IndexError, ValueError, json.JSONDecodeError) as exc:
                    raise RuntimeError("语音克隆 API 返回了无法识别的音频响应") from exc
                if attempt < self._max_attempts:
                    time.sleep(2 ** (attempt - 1))
            else:
                detail = str(transient_error) if transient_error else "未知网络错误"
                raise RuntimeError(f"语音克隆响应传输中断，自动重试 {self._max_attempts} 次后仍失败：{detail}") from transient_error
        if not audio_bytes.startswith(b"RIFF") or audio_bytes[8:12] != b"WAVE":
            raise RuntimeError("语音克隆 API 返回的内容不是有效 WAV")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(audio_bytes)


class Qwen3TTSSynthesizer(Synthesizer):
    """Local Qwen3-TTS Base adapter with a cached voice-clone prompt."""

    provider = "qwen3-tts"
    protocol = "qwen3-tts-local"
    preferred_chunk_chars = 150

    def __init__(
        self,
        model: str = DEFAULT_QWEN_MODEL,
        device: str = "auto",
        *,
        model_instance=None,
        soundfile_module=None,
    ):
        model = model.strip()
        if not QWEN_MODEL_PATTERN.fullmatch(model):
            raise RuntimeError("Qwen3-TTS 模型名称或本地路径格式无效")
        normalized_device = device.strip().lower()
        if not re.fullmatch(r"auto|cpu|cuda(?::\d+)?", normalized_device):
            raise RuntimeError("Qwen3-TTS 设备必须为 auto、cpu、cuda 或 cuda:N")
        if model_instance is None:
            missing = [
                package
                for package in ("torch", "soundfile", "qwen_tts")
                if importlib.util.find_spec(package) is None
            ]
            if missing:
                raise RuntimeError(
                    "缺少 Qwen3-TTS 本地依赖："
                    + "、".join(missing)
                    + "。请运行 scripts/setup_qwen3_tts.ps1，并用 .venv-qwen 启动服务"
                )
        self.name = model
        self.device = normalized_device
        self._model = model_instance
        self._soundfile = soundfile_module
        self._prompt_cache: dict[tuple[str, int, int, str], object] = {}
        self._lock = threading.Lock()

    def _ensure_model(self) -> None:
        if self._model is not None and self._soundfile is not None:
            return
        try:
            import soundfile
            import torch
            from qwen_tts import Qwen3TTSModel
        except ImportError as exc:
            raise RuntimeError(
                "Qwen3-TTS 依赖未安装；请运行 scripts/setup_qwen3_tts.ps1，"
                "然后用 .venv-qwen 启动服务"
            ) from exc

        device = self.device
        if device == "auto":
            device = "cuda:0" if torch.cuda.is_available() else "cpu"
        if device.startswith("cuda") and not torch.cuda.is_available():
            raise RuntimeError("已选择 CUDA，但 PyTorch 当前无法使用 NVIDIA GPU")
        load_options = {
            "device_map": device,
            "dtype": torch.bfloat16 if device.startswith("cuda") else torch.float32,
        }
        if device.startswith("cuda") and importlib.util.find_spec("flash_attn") is not None:
            load_options["attn_implementation"] = "flash_attention_2"
        self._model = Qwen3TTSModel.from_pretrained(self.name, **load_options)
        self._soundfile = soundfile
        self.device = device

    @staticmethod
    def _reference_transcript(reference: Path) -> str:
        transcript_path = reference.with_name("reference.txt")
        if not transcript_path.exists():
            return ""
        return transcript_path.read_text(encoding="utf-8").strip()

    def _voice_prompt(self, reference: Path):
        transcript = self._reference_transcript(reference)
        stat = reference.stat()
        cache_key = (
            str(reference.resolve()),
            stat.st_mtime_ns,
            stat.st_size,
            transcript,
        )
        cached = self._prompt_cache.get(cache_key)
        if cached is not None:
            return cached
        prompt = self._model.create_voice_clone_prompt(
            ref_audio=str(reference),
            ref_text=transcript or None,
            x_vector_only_mode=not bool(transcript),
        )
        self._prompt_cache = {cache_key: prompt}
        return prompt

    def synthesize(self, reference: Path, text: str, emotion: list[float], output: Path) -> None:
        del emotion  # The Base checkpoint has no instruction-control interface.
        output.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._ensure_model()
            prompt = self._voice_prompt(reference)
            wavs, sample_rate = self._model.generate_voice_clone(
                text=text,
                language="Chinese",
                voice_clone_prompt=prompt,
            )
            if not wavs:
                raise RuntimeError("Qwen3-TTS 未返回音频")
            audio = wavs[0]
            if hasattr(audio, "detach"):
                audio = audio.detach().cpu().float().numpy()
            self._soundfile.write(str(output), audio, sample_rate)


def build_synthesizer(
    engine: str,
    source_path: Path | None,
    model_dir: Path | None,
    mimo_api_key: str | None = None,
    mimo_base_url: str = "https://api.xiaomimimo.com/v1",
    mimo_use_system_proxy: bool = False,
    mimo_model: str = DEFAULT_MIMO_MODEL,
    mimo_auth_mode: str = "api-key",
    qwen_model: str = DEFAULT_QWEN_MODEL,
    qwen_device: str = "auto",
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
            model=mimo_model,
            auth_mode=mimo_auth_mode,
        )
    if engine in {"qwen", "qwen3-tts"}:
        return Qwen3TTSSynthesizer(qwen_model, qwen_device)
    raise RuntimeError("NVS_ENGINE 必须为 mock、mimo、qwen，或正确配置的 indextts2")
