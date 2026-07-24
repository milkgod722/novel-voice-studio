from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import re
import socket
import struct
import threading
import time
import uuid
import wave
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from http.client import IncompleteRead
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import ProxyHandler, Request, build_opener, urlopen


DEFAULT_MIMO_MODEL = "mimo-v2.5-tts-voiceclone"
MIMO_MODEL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
VOICE_CLONE_AUTH_MODES = {"api-key", "bearer"}
REMOTE_PROTOCOLS = {
    "aliyun-cosyvoice",
    "tencent-tts",
    "baidu-voice-clone",
    "google-cloud-tts",
    "openai-tts",
    "indextts-url",
}
REMOTE_DEFAULTS = {
    "aliyun-cosyvoice": {
        "api_url": "https://dashscope.aliyuncs.com/api/v1/services/audio/tts/SpeechSynthesizer",
        "model": "cosyvoice-v3.5-flash",
    },
    "tencent-tts": {
        "api_url": "https://tts.tencentcloudapi.com/",
        "model": "2019-08-23",
    },
    "baidu-voice-clone": {
        "api_url": "https://aip.baidubce.com/rest/2.0/speech/publiccloudspeech/v1/voice/clone/tts",
        "model": "voice-clone",
    },
    "google-cloud-tts": {
        "api_url": "https://texttospeech.googleapis.com/v1beta1/text:synthesize",
        "model": "chirp3-instant-custom-voice",
    },
    "openai-tts": {
        "api_url": "https://api.openai.com/v1/audio/speech",
        "model": "gpt-4o-mini-tts",
    },
    "indextts-url": {
        "api_url": "http://127.0.0.1:7860/tts",
        "model": "IndexTTS2",
    },
}


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
            "请让音量、音高、语速和叙述者状态与同一本书的前后片段保持一致。"
        )

    def synthesize(self, reference: Path, text: str, emotion: list[float], output: Path) -> None:
        payload = {
            "model": self.name,
            "messages": [
                {"role": "user", "content": self._instruction(emotion)},
                {"role": "assistant", "content": text},
            ],
            "audio": {"format": "wav", "voice": self._voice_data_uri(reference)},
            "temperature": 0.25,
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


class RemoteProviderSynthesizer(Synthesizer):
    """Direct adapters for hosted TTS APIs and a documented IndexTTS URL contract."""

    provider = "remote-api"

    _CHUNK_LIMITS = {
        "aliyun-cosyvoice": 450,
        "tencent-tts": 140,
        "baidu-voice-clone": 450,
        "google-cloud-tts": 1000,
        "openai-tts": 1000,
        "indextts-url": 150,
    }
    _GENERIC_EMOTIONS = (
        "happy", "angry", "sad", "fear", "disgusted", "sad", "amaze", "peaceful"
    )
    _BAIDU_EMOTIONS = (
        "happy", "angry", "down", "fear", "disgust", "down", "surprise", ""
    )

    def __init__(
        self,
        protocol: str,
        api_url: str,
        api_key: str = "",
        *,
        api_secret: str = "",
        model: str = "",
        voice_id: str = "",
        project_id: str = "",
        language: str = "zh-CN",
        auth_mode: str = "bearer",
        timeout: int = 300,
        max_attempts: int = 3,
    ):
        if protocol not in REMOTE_PROTOCOLS:
            raise RuntimeError(f"不支持的远程语音协议：{protocol}")
        parts = urlsplit(api_url.strip())
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            raise RuntimeError("API URL 必须是有效的 http:// 或 https:// 地址")
        if parts.username or parts.password or parts.fragment:
            raise RuntimeError("API URL 不能包含账号、密码或片段标识")
        defaults = REMOTE_DEFAULTS[protocol]
        self.protocol = protocol
        self.api_url = api_url.strip()
        self._endpoint = self.api_url
        self._api_key = api_key.strip()
        self._api_secret = api_secret.strip()
        self.model = (model or defaults["model"]).strip()
        self.voice_id = voice_id.strip()
        self.project_id = project_id.strip()
        self.language = language.strip() or "zh-CN"
        if protocol == "google-cloud-tts" and self.language.lower() == "zh-cn":
            self.language = "cmn-CN"
        self.auth_mode = auth_mode.strip().lower()
        self.name = f"{protocol}:{self.model}"
        self.preferred_chunk_chars = self._CHUNK_LIMITS[protocol]
        self._timeout = timeout
        self._max_attempts = max(1, max_attempts)
        self._open = urlopen
        self._lock = threading.Lock()
        self._validate_configuration()

    def _validate_configuration(self) -> None:
        if self.protocol != "indextts-url" and not self._api_key:
            raise RuntimeError("该厂商必须填写 API Key / Access Token")
        if self.protocol == "tencent-tts" and not self._api_secret:
            raise RuntimeError("腾讯云直连接口必须填写 SecretKey")
        if self.protocol in {
            "aliyun-cosyvoice",
            "tencent-tts",
            "baidu-voice-clone",
            "google-cloud-tts",
            "openai-tts",
        } and not self.voice_id:
            raise RuntimeError("该厂商必须填写已经创建或开通的音色 ID / Voice Key")
        if self.protocol == "google-cloud-tts" and not self.project_id:
            raise RuntimeError("Google Cloud 必须填写 Project ID")
        if self.auth_mode not in VOICE_CLONE_AUTH_MODES:
            raise RuntimeError("鉴权方式必须为 api-key 或 bearer")

    @staticmethod
    def _content_type(response) -> str:
        if hasattr(response, "headers"):
            return response.headers.get("Content-Type", "")
        if hasattr(response, "getheader"):
            return response.getheader("Content-Type", "")
        return ""

    def _request(self, request: Request) -> tuple[bytes, str]:
        transient_error: Exception | None = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                with self._open(request, timeout=self._timeout) as response:
                    return response.read(), self._content_type(response)
            except HTTPError as exc:
                detail = exc.read(1000).decode("utf-8", errors="replace")
                if exc.code not in {429, 500, 502, 503, 504}:
                    raise RuntimeError(
                        f"{self.protocol} 请求失败（HTTP {exc.code}）：{detail}"
                    ) from exc
                transient_error = RuntimeError(f"HTTP {exc.code}：{detail}")
            except (
                IncompleteRead,
                ConnectionResetError,
                TimeoutError,
                socket.timeout,
                URLError,
            ) as exc:
                transient_error = exc
            if attempt < self._max_attempts:
                time.sleep(2 ** (attempt - 1))
        detail = str(transient_error) if transient_error else "未知网络错误"
        raise RuntimeError(
            f"{self.protocol} 响应传输中断，重试 {self._max_attempts} 次后仍失败：{detail}"
        ) from transient_error

    def _json_post(
        self, payload: dict, headers: dict[str, str] | None = None
    ) -> tuple[bytes, str]:
        request_headers = {
            "Content-Type": "application/json",
            "User-Agent": "novel-voice-studio/0.2",
            **(headers or {}),
        }
        request = Request(
            self._endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=request_headers,
            method="POST",
        )
        return self._request(request)

    @staticmethod
    def _parse_json(raw: bytes, provider: str) -> dict:
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"{provider} 返回了无法识别的响应") from exc
        if not isinstance(value, dict):
            raise RuntimeError(f"{provider} 返回的 JSON 不是对象")
        return value

    @classmethod
    def _find_url(cls, value) -> str | None:
        if isinstance(value, dict):
            for key in ("url", "audio_url", "audioUrl"):
                candidate = value.get(key)
                if isinstance(candidate, str) and candidate.startswith(("http://", "https://")):
                    return candidate
            for child in value.values():
                result = cls._find_url(child)
                if result:
                    return result
        elif isinstance(value, list):
            for child in value:
                result = cls._find_url(child)
                if result:
                    return result
        elif isinstance(value, str) and value.startswith(("http://", "https://")):
            return value
        return None

    def _download(self, url: str) -> bytes:
        request = Request(url, headers={"User-Agent": "novel-voice-studio/0.2"})
        return self._request(request)[0]

    @staticmethod
    def _ensure_wav(audio: bytes, provider: str) -> bytes:
        if not audio.startswith(b"RIFF") or audio[8:12] != b"WAVE":
            raise RuntimeError(f"{provider} 没有返回有效 WAV；请确认输出格式配置为 wav")
        return audio

    @classmethod
    def _emotion(cls, emotion: list[float], baidu: bool = False) -> tuple[str, int]:
        dominant = max(range(len(emotion)), key=emotion.__getitem__)
        names = cls._BAIDU_EMOTIONS if baidu else cls._GENERIC_EMOTIONS
        return names[dominant], max(50, min(200, round(50 + max(emotion) * 150)))

    @classmethod
    def _instruction(cls, emotion: list[float]) -> str:
        name, intensity = cls._emotion(emotion)
        return (
            f"以{name}的情绪演播，情感强度约 {intensity}%。"
            "像专业有声书旁白一样区分叙述与对白，根据标点自然停顿，"
            "保持已注册音色稳定，不添加原文之外的内容。"
            "音量、音高、语速和叙述者状态须与同一本书的前后片段一致。"
        )

    def _aliyun(self, text: str, emotion: list[float]) -> bytes:
        payload = {
            "model": self.model,
            "input": {
                "text": text,
                "voice": self.voice_id,
                "format": "wav",
                "sample_rate": 24000,
                "instruction": self._instruction(emotion),
            },
        }
        raw, _ = self._json_post(
            payload, {"Authorization": f"Bearer {self._api_key}"}
        )
        body = self._parse_json(raw, "阿里云")
        url = self._find_url(body)
        if not url and isinstance(body.get("data"), str):
            try:
                url = self._find_url(json.loads(body["data"]))
            except json.JSONDecodeError:
                pass
        if not url:
            raise RuntimeError(f"阿里云未返回音频 URL：{str(body)[:500]}")
        return self._ensure_wav(self._download(url), "阿里云")

    @staticmethod
    def _hmac_sha256(key: bytes, message: str) -> bytes:
        return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()

    def _tencent_headers(self, payload: bytes, timestamp: int) -> dict[str, str]:
        parts = urlsplit(self._endpoint)
        host = parts.netloc
        canonical_uri = parts.path or "/"
        content_type = "application/json; charset=utf-8"
        canonical_headers = (
            f"content-type:{content_type}\n"
            f"host:{host}\n"
            "x-tc-action:texttovoice\n"
        )
        signed_headers = "content-type;host;x-tc-action"
        canonical_request = "\n".join(
            [
                "POST",
                canonical_uri,
                parts.query,
                canonical_headers,
                signed_headers,
                hashlib.sha256(payload).hexdigest(),
            ]
        )
        date = datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d")
        scope = f"{date}/tts/tc3_request"
        string_to_sign = "\n".join(
            [
                "TC3-HMAC-SHA256",
                str(timestamp),
                scope,
                hashlib.sha256(canonical_request.encode()).hexdigest(),
            ]
        )
        secret_date = self._hmac_sha256(("TC3" + self._api_secret).encode(), date)
        secret_service = self._hmac_sha256(secret_date, "tts")
        secret_signing = self._hmac_sha256(secret_service, "tc3_request")
        signature = hmac.new(
            secret_signing, string_to_sign.encode(), hashlib.sha256
        ).hexdigest()
        authorization = (
            "TC3-HMAC-SHA256 "
            f"Credential={self._api_key}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )
        return {
            "Authorization": authorization,
            "Content-Type": content_type,
            "Host": host,
            "X-TC-Action": "TextToVoice",
            "X-TC-Version": "2019-08-23",
            "X-TC-Timestamp": str(timestamp),
        }

    def _tencent(self, text: str, emotion: list[float]) -> bytes:
        category, intensity = self._emotion(emotion)
        payload = {
            "Text": text,
            "SessionId": uuid.uuid4().hex,
            "VoiceType": 200000000,
            "FastVoiceType": self.voice_id,
            "PrimaryLanguage": 1,
            "SampleRate": 24000,
            "Codec": "wav",
            "EmotionCategory": category,
            "EmotionIntensity": intensity,
        }
        encoded = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        headers = self._tencent_headers(encoded, int(time.time()))
        request = Request(self._endpoint, data=encoded, headers=headers, method="POST")
        raw, _ = self._request(request)
        body = self._parse_json(raw, "腾讯云")
        response = body.get("Response", {})
        if response.get("Error"):
            error = response["Error"]
            raise RuntimeError(
                f"腾讯云请求失败：{error.get('Code', '')} {error.get('Message', '')}"
            )
        try:
            audio = base64.b64decode(response["Audio"], validate=True)
        except (KeyError, ValueError) as exc:
            raise RuntimeError("腾讯云未返回有效的 Base64 音频") from exc
        return self._ensure_wav(audio, "腾讯云")

    def _baidu(self, text: str, emotion: list[float]) -> bytes:
        emotion_name, _ = self._emotion(emotion, baidu=True)
        payload = {
            "text": text,
            "voice_id": int(self.voice_id) if self.voice_id.isdigit() else self.voice_id,
            "lang": "ja" if self.language.lower().startswith("ja") else "zh",
            "media_type": "wav",
            "sample_rate": 24000,
            "speed": 5,
        }
        if emotion_name:
            payload["emotion"] = emotion_name
        raw, content_type = self._json_post(
            payload, {"Authorization": self._api_key}
        )
        if raw.startswith(b"RIFF"):
            return self._ensure_wav(raw, "百度智能云")
        body = self._parse_json(raw, "百度智能云")
        raise RuntimeError(
            f"百度智能云请求失败（{content_type or 'JSON'}）："
            f"{body.get('message') or body.get('error_msg') or str(body)[:500]}"
        )

    def _google(self, text: str) -> bytes:
        payload = {
            "input": {"text": text},
            "voice": {
                "languageCode": self.language,
                "voiceClone": {"voiceCloningKey": self.voice_id},
            },
            "audioConfig": {
                "audioEncoding": "LINEAR16",
                "sampleRateHertz": 24000,
            },
        }
        raw, _ = self._json_post(
            payload,
            {
                "Authorization": f"Bearer {self._api_key}",
                "x-goog-user-project": self.project_id,
            },
        )
        body = self._parse_json(raw, "Google Cloud")
        try:
            audio = base64.b64decode(body["audioContent"], validate=True)
        except (KeyError, ValueError) as exc:
            raise RuntimeError(f"Google Cloud 未返回音频：{str(body)[:500]}") from exc
        return self._ensure_wav(audio, "Google Cloud")

    def _openai(self, text: str, emotion: list[float]) -> bytes:
        voice: str | dict[str, str]
        voice = {"id": self.voice_id} if self.voice_id.startswith("voice_") else self.voice_id
        payload = {
            "model": self.model,
            "input": text,
            "voice": voice,
            "instructions": self._instruction(emotion),
            "response_format": "wav",
        }
        raw, content_type = self._json_post(
            payload, {"Authorization": f"Bearer {self._api_key}"}
        )
        if raw.startswith(b"RIFF"):
            return self._ensure_wav(raw, "OpenAI")
        body = self._parse_json(raw, "OpenAI")
        raise RuntimeError(
            f"OpenAI 请求失败（{content_type or 'JSON'}）："
            f"{body.get('error', {}).get('message') or str(body)[:500]}"
        )

    def _indextts(self, reference: Path, text: str, emotion: list[float]) -> bytes:
        boundary = f"----NovelVoiceStudio{uuid.uuid4().hex}"
        chunks: list[bytes] = []
        fields = {
            "text": text,
            "emo_vector": json.dumps(emotion),
            "emo_alpha": "0.65",
            "use_random": "false",
            "model": self.model,
        }
        for name, value in fields.items():
            chunks.append(
                (
                    f"--{boundary}\r\n"
                    f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                    f"{value}\r\n"
                ).encode("utf-8")
            )
        chunks.append(
            (
                f"--{boundary}\r\n"
                'Content-Disposition: form-data; name="spk_audio_prompt"; '
                'filename="reference.wav"\r\n'
                "Content-Type: audio/wav\r\n\r\n"
            ).encode("utf-8")
            + reference.read_bytes()
            + b"\r\n"
        )
        chunks.append(f"--{boundary}--\r\n".encode())
        headers = {
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "User-Agent": "novel-voice-studio/0.2",
        }
        if self._api_key:
            header = "Authorization" if self.auth_mode == "bearer" else "api-key"
            headers[header] = (
                f"Bearer {self._api_key}" if self.auth_mode == "bearer" else self._api_key
            )
        request = Request(
            self._endpoint, data=b"".join(chunks), headers=headers, method="POST"
        )
        raw, _ = self._request(request)
        if raw.startswith(b"RIFF"):
            return self._ensure_wav(raw, "IndexTTS URL")
        body = self._parse_json(raw, "IndexTTS URL")
        url = self._find_url(body)
        if url:
            return self._ensure_wav(self._download(url), "IndexTTS URL")
        for key in ("audio", "audio_data", "data"):
            value = body.get(key)
            if isinstance(value, str):
                if value.startswith("data:") and "," in value:
                    value = value.split(",", 1)[1]
                try:
                    return self._ensure_wav(
                        base64.b64decode(value, validate=True), "IndexTTS URL"
                    )
                except ValueError:
                    continue
        raise RuntimeError(f"IndexTTS URL 未返回 WAV、Base64 或音频 URL：{str(body)[:500]}")

    def synthesize(self, reference: Path, text: str, emotion: list[float], output: Path) -> None:
        with self._lock:
            if self.protocol == "aliyun-cosyvoice":
                audio = self._aliyun(text, emotion)
            elif self.protocol == "tencent-tts":
                audio = self._tencent(text, emotion)
            elif self.protocol == "baidu-voice-clone":
                audio = self._baidu(text, emotion)
            elif self.protocol == "google-cloud-tts":
                audio = self._google(text)
            elif self.protocol == "openai-tts":
                audio = self._openai(text, emotion)
            else:
                audio = self._indextts(reference, text, emotion)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(audio)


def build_synthesizer(
    engine: str,
    mimo_api_key: str | None = None,
    mimo_base_url: str = "https://api.xiaomimimo.com/v1",
    mimo_use_system_proxy: bool = False,
    mimo_model: str = DEFAULT_MIMO_MODEL,
    mimo_auth_mode: str = "api-key",
) -> Synthesizer:
    if engine == "mock":
        return MockSynthesizer()
    if engine == "mimo" and mimo_api_key:
        return MiMoVoiceCloneSynthesizer(
            mimo_api_key,
            mimo_base_url,
            use_system_proxy=mimo_use_system_proxy,
            model=mimo_model,
            auth_mode=mimo_auth_mode,
        )
    raise RuntimeError("NVS_ENGINE 必须为 mock 或已配置 API Key 的 mimo")
