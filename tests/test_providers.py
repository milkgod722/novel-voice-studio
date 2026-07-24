import base64
import io
import json
import wave
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.synth import RemoteProviderSynthesizer


def make_wav() -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(24000)
        writer.writeframes(b"\x00\x00" * 2400)
    return output.getvalue()


class FakeResponse:
    def __init__(self, data: bytes, content_type: str = "application/json"):
        self.data = data
        self.headers = {"Content-Type": content_type}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def read(self):
        return self.data


def test_aliyun_contract_downloads_wav(tmp_path):
    captured = {}
    responses = [
        FakeResponse(json.dumps({"output": {"audio": {"url": "https://audio.test/out.wav"}}}).encode()),
        FakeResponse(make_wav(), "audio/wav"),
    ]

    def fake_open(request, timeout):
        captured.setdefault("requests", []).append(request)
        return responses.pop(0)

    synth = RemoteProviderSynthesizer(
        "aliyun-cosyvoice",
        "https://dashscope.test/tts",
        "dashscope-key",
        model="cosyvoice-v3.5-flash",
        voice_id="voice-cloned",
    )
    synth._open = fake_open
    output = tmp_path / "ali.wav"
    synth.synthesize(tmp_path / "ref.wav", "开心地说。", [0.9, 0, 0, 0, 0, 0, 0, 0.1], output)

    payload = json.loads(captured["requests"][0].data)
    assert payload["input"]["voice"] == "voice-cloned"
    assert "happy" in payload["input"]["instruction"]
    assert captured["requests"][0].get_header("Authorization") == "Bearer dashscope-key"
    assert output.read_bytes() == make_wav()


def test_tencent_contract_is_tc3_signed(tmp_path):
    captured = {}
    response = {"Response": {"Audio": base64.b64encode(make_wav()).decode()}}

    def fake_open(request, timeout):
        captured["request"] = request
        return FakeResponse(json.dumps(response).encode())

    synth = RemoteProviderSynthesizer(
        "tencent-tts",
        "https://tts.tencentcloudapi.com/",
        "secret-id",
        api_secret="secret-key",
        voice_id="WCHN-custom",
    )
    synth._open = fake_open
    synth.synthesize(tmp_path / "ref.wav", "悲伤地说。", [0, 0, 0.9, 0, 0, 0, 0, 0.1], tmp_path / "tx.wav")

    headers = {key.lower(): value for key, value in captured["request"].header_items()}
    payload = json.loads(captured["request"].data)
    assert headers["authorization"].startswith("TC3-HMAC-SHA256 Credential=secret-id/")
    assert headers["x-tc-action"] == "TextToVoice"
    assert payload["VoiceType"] == 200000000
    assert payload["FastVoiceType"] == "WCHN-custom"
    assert payload["EmotionCategory"] == "sad"


def test_baidu_contract_returns_binary_wav(tmp_path):
    captured = {}

    def fake_open(request, timeout):
        captured["request"] = request
        return FakeResponse(make_wav(), "audio/wav")

    synth = RemoteProviderSynthesizer(
        "baidu-voice-clone",
        "https://aip.baidubce.test/voice/clone/tts",
        "baidu-api-key",
        voice_id="100001",
    )
    synth._open = fake_open
    synth.synthesize(tmp_path / "ref.wav", "惊讶地说。", [0, 0, 0, 0, 0, 0, 0.9, 0.1], tmp_path / "baidu.wav")

    payload = json.loads(captured["request"].data)
    assert payload["voice_id"] == 100001
    assert payload["emotion"] == "surprise"
    assert captured["request"].get_header("Authorization") == "baidu-api-key"


def test_google_contract_uses_cloning_key(tmp_path):
    captured = {}

    def fake_open(request, timeout):
        captured["request"] = request
        body = {"audioContent": base64.b64encode(make_wav()).decode()}
        return FakeResponse(json.dumps(body).encode())

    synth = RemoteProviderSynthesizer(
        "google-cloud-tts",
        "https://texttospeech.googleapis.test/v1beta1/text:synthesize",
        "oauth-token",
        voice_id="voice-cloning-key",
        project_id="project-123",
    )
    synth._open = fake_open
    synth.synthesize(tmp_path / "ref.wav", "你好。", [0] * 7 + [1], tmp_path / "google.wav")

    payload = json.loads(captured["request"].data)
    assert payload["voice"]["voiceClone"]["voiceCloningKey"] == "voice-cloning-key"
    assert payload["voice"]["languageCode"] == "cmn-CN"
    assert captured["request"].get_header("X-goog-user-project") == "project-123"


def test_openai_contract_supports_custom_voice_and_instructions(tmp_path):
    captured = {}

    def fake_open(request, timeout):
        captured["request"] = request
        return FakeResponse(make_wav(), "audio/wav")

    synth = RemoteProviderSynthesizer(
        "openai-tts",
        "https://api.openai.test/v1/audio/speech",
        "openai-key",
        model="gpt-4o-mini-tts",
        voice_id="voice_1234",
    )
    synth._open = fake_open
    synth.synthesize(tmp_path / "ref.wav", "愤怒地说。", [0, 0.9, 0, 0, 0, 0, 0, 0.1], tmp_path / "openai.wav")

    payload = json.loads(captured["request"].data)
    assert payload["voice"] == {"id": "voice_1234"}
    assert payload["response_format"] == "wav"
    assert "angry" in payload["instructions"]


def test_indextts_url_contract_posts_reference_and_emotion(tmp_path):
    captured = {}
    reference = tmp_path / "reference.wav"
    reference.write_bytes(make_wav())

    def fake_open(request, timeout):
        captured["request"] = request
        return FakeResponse(make_wav(), "audio/wav")

    synth = RemoteProviderSynthesizer(
        "indextts-url",
        "http://127.0.0.1:9000/tts",
        "",
        model="IndexTTS2",
    )
    synth._open = fake_open
    synth.synthesize(reference, "有感情地说。", [0.8] + [0] * 7, tmp_path / "index.wav")

    body = captured["request"].data
    assert b'name="spk_audio_prompt"; filename="reference.wav"' in body
    assert b'name="emo_vector"' in body
    assert "有感情地说。".encode() in body


def test_remote_provider_config_endpoint(tmp_path):
    app = create_app(Settings(tmp_path, "mock"))
    with TestClient(app) as client:
        response = client.post(
            "/api/config/voice-clone",
            json={
                "protocol": "openai-tts",
                "api_url": "https://api.openai.com/v1/audio/speech",
                "api_key": "test-openai-key",
                "model": "gpt-4o-mini-tts",
                "voice_id": "voice_1234",
            },
        )

    assert response.status_code == 200
    assert response.json()["protocol"] == "openai-tts"
    assert response.json()["model"] == "gpt-4o-mini-tts"
    assert response.json()["voice_mode"] == "registered-voice"
    assert "api_key" not in response.json()


def test_web_form_has_all_remote_providers_and_xiaomi_is_default():
    html = Path("app/static/index.html").read_text(encoding="utf-8")
    providers = [
        "mimo-chat",
        "aliyun-cosyvoice",
        "tencent-tts",
        "baidu-voice-clone",
        "google-cloud-tts",
        "openai-tts",
        "indextts-url",
    ]

    assert html.index('value="mimo-chat"') < html.index('value="aliyun-cosyvoice"')
    assert all(f'value="{provider}"' in html for provider in providers)
    assert "qwen3-tts-local" not in html
