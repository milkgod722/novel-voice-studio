import base64
import io
import json
import wave
from http.client import IncompleteRead

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
