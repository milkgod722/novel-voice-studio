import base64
import io
import json
import wave
from http.client import IncompleteRead

from app.config import Settings
from app.synth import MiMoVoiceCloneSynthesizer


def make_wav() -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(24000)
        writer.writeframes(b"\x00\x00" * 2400)
    return output.getvalue()


class FakeResponse:
    def __init__(self, data: bytes):
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def read(self):
        return self.data


def test_mimo_voice_clone_request_and_wav_response(tmp_path, monkeypatch):
    reference = tmp_path / "reference.wav"
    reference.write_bytes(make_wav())
    generated = make_wav()
    captured = {}

    def fake_urlopen(request, timeout):
        captured["headers"] = dict(request.header_items())
        captured["payload"] = json.loads(request.data)
        captured["timeout"] = timeout
        response = {"choices": [{"message": {"audio": {"data": base64.b64encode(generated).decode()}}}]}
        return FakeResponse(json.dumps(response).encode())

    synth = MiMoVoiceCloneSynthesizer("sk-test-key", "https://example.test/v1")
    synth._open = fake_urlopen
    output = tmp_path / "out.wav"
    synth.synthesize(reference, "她惊喜地笑了！", [0.8, 0, 0, 0, 0, 0, 0.1, 0.1], output)

    assert output.read_bytes() == generated
    assert captured["payload"]["model"] == "mimo-v2.5-tts-voiceclone"
    assert captured["payload"]["messages"][1] == {"role": "assistant", "content": "她惊喜地笑了！"}
    assert captured["payload"]["audio"]["voice"].startswith("data:audio/wav;base64,")
    headers = {key.lower(): value for key, value in captured["headers"].items()}
    assert headers["api-key"] == "sk-test-key"


def test_mimo_uses_custom_model_name(tmp_path):
    reference = tmp_path / "reference.wav"
    reference.write_bytes(make_wav())
    generated = make_wav()
    captured = {}

    def fake_urlopen(request, timeout):
        captured["payload"] = json.loads(request.data)
        response = {"choices": [{"message": {"audio": {"data": base64.b64encode(generated).decode()}}}]}
        return FakeResponse(json.dumps(response).encode())

    synth = MiMoVoiceCloneSynthesizer(
        "gateway-secret",
        "https://gateway.example.test/v1",
        model="mimo-custom-voiceclone",
        auth_mode="bearer",
    )
    synth._open = fake_urlopen
    synth.synthesize(reference, "自定义模型。", [0, 0, 0, 0, 0, 0, 0, 1], tmp_path / "out.wav")

    assert synth.name == "mimo-custom-voiceclone"
    assert synth.api_url == "https://gateway.example.test/v1/chat/completions"
    assert synth.auth_mode == "bearer"
    assert captured["payload"]["model"] == "mimo-custom-voiceclone"


def test_custom_endpoint_and_bearer_auth_are_sent(tmp_path):
    reference = tmp_path / "reference.wav"
    reference.write_bytes(make_wav())
    generated = make_wav()
    captured = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["headers"] = {key.lower(): value for key, value in request.header_items()}
        response = {"choices": [{"message": {"audio": {"data": base64.b64encode(generated).decode()}}}]}
        return FakeResponse(json.dumps(response).encode())

    synth = MiMoVoiceCloneSynthesizer(
        "gateway-secret",
        "http://127.0.0.1:9000/custom/voice-clone",
        model="local-clone-model",
        auth_mode="bearer",
    )
    synth._open = fake_urlopen
    synth.synthesize(reference, "网关测试。", [0, 0, 0, 0, 0, 0, 0, 1], tmp_path / "out.wav")

    assert captured["url"] == "http://127.0.0.1:9000/custom/voice-clone"
    assert captured["headers"]["authorization"] == "Bearer gateway-secret"
    assert "api-key" not in captured["headers"]


def test_mimo_rejects_token_plan_key_for_non_coding_app():
    try:
        MiMoVoiceCloneSynthesizer("tp-not-valid-for-this-app")
        assert False, "expected Token Plan guard"
    except RuntimeError as exc:
        assert "Token Plan" in str(exc)
        assert "sk-" in str(exc)


def test_mimo_normalizes_bearer_prefix():
    synth = MiMoVoiceCloneSynthesizer("Bearer sk-example")
    assert synth._api_key == "sk-example"


def test_mimo_rejects_invalid_model_name():
    try:
        MiMoVoiceCloneSynthesizer("sk-example", model="bad model name")
        assert False, "expected model validation"
    except RuntimeError as exc:
        assert "模型名称格式无效" in str(exc)


def test_voice_clone_rejects_invalid_api_url():
    try:
        MiMoVoiceCloneSynthesizer("gateway-secret", "file:///tmp/voice")
        assert False, "expected URL validation"
    except RuntimeError as exc:
        assert "API URL" in str(exc)


def test_mimo_model_can_come_from_environment(monkeypatch):
    monkeypatch.setenv("VOICE_CLONE_MODEL", "mimo-env-voiceclone")
    monkeypatch.setenv("VOICE_CLONE_API_URL", "http://127.0.0.1:9000/v1")
    monkeypatch.setenv("VOICE_CLONE_AUTH_MODE", "bearer")
    settings = Settings.from_env()
    assert settings.mimo_model == "mimo-env-voiceclone"
    assert settings.mimo_base_url == "http://127.0.0.1:9000/v1"
    assert settings.mimo_auth_mode == "bearer"


def test_token_plan_environment_key_does_not_auto_enable_mimo(monkeypatch):
    monkeypatch.delenv("NVS_ENGINE", raising=False)
    monkeypatch.setenv("MIMO_API_KEY", "tp-environment-key")
    assert Settings.from_env().engine == "mock"


def test_mimo_retries_incomplete_response(tmp_path, monkeypatch):
    reference = tmp_path / "reference.wav"
    reference.write_bytes(make_wav())
    generated = make_wav()
    response = {"choices": [{"message": {"audio": {"data": base64.b64encode(generated).decode()}}}]}
    attempts = 0

    def flaky_open(request, timeout):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise IncompleteRead(b'{"choices":', 68666)
        return FakeResponse(json.dumps(response).encode())

    monkeypatch.setattr("app.synth.time.sleep", lambda _: None)
    synth = MiMoVoiceCloneSynthesizer("sk-test-key", "https://example.test/v1")
    synth._open = flaky_open
    output = tmp_path / "out.wav"
    synth.synthesize(reference, "断线后重试。", [0, 0, 0, 0, 0, 0, 0, 1], output)

    assert attempts == 2
    assert output.read_bytes() == generated
