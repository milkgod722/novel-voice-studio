from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.synth import DEFAULT_QWEN_MODEL, Qwen3TTSSynthesizer


class FakeQwenModel:
    def __init__(self):
        self.prompt_calls = []
        self.generate_calls = []

    def create_voice_clone_prompt(self, **kwargs):
        self.prompt_calls.append(kwargs)
        return {"prompt": len(self.prompt_calls)}

    def generate_voice_clone(self, **kwargs):
        self.generate_calls.append(kwargs)
        return [[0.0, 0.25, -0.25]], 24000


class FakeSoundFile:
    def __init__(self):
        self.calls = []

    def write(self, path, audio, sample_rate):
        self.calls.append((path, audio, sample_rate))
        Path(path).write_bytes(b"fake-wave")


def test_qwen_uses_transcript_and_caches_prompt(tmp_path):
    reference = tmp_path / "reference.wav"
    reference.write_bytes(b"reference")
    (tmp_path / "reference.txt").write_text("你好，世界。", encoding="utf-8")
    model = FakeQwenModel()
    soundfile = FakeSoundFile()
    synth = Qwen3TTSSynthesizer(
        DEFAULT_QWEN_MODEL,
        model_instance=model,
        soundfile_module=soundfile,
    )

    synth.synthesize(reference, "第一句。", [0] * 8, tmp_path / "one.wav")
    synth.synthesize(reference, "第二句。", [0] * 8, tmp_path / "two.wav")

    assert len(model.prompt_calls) == 1
    assert model.prompt_calls[0] == {
        "ref_audio": str(reference),
        "ref_text": "你好，世界。",
        "x_vector_only_mode": False,
    }
    assert model.generate_calls[0]["language"] == "Chinese"
    assert model.generate_calls[0]["voice_clone_prompt"] == {"prompt": 1}
    assert soundfile.calls[0][2] == 24000


def test_qwen_falls_back_to_x_vector_without_transcript(tmp_path):
    reference = tmp_path / "reference.wav"
    reference.write_bytes(b"reference")
    model = FakeQwenModel()
    synth = Qwen3TTSSynthesizer(
        model_instance=model,
        soundfile_module=FakeSoundFile(),
    )

    synth.synthesize(reference, "没有转写。", [0] * 8, tmp_path / "out.wav")

    assert model.prompt_calls[0]["ref_text"] is None
    assert model.prompt_calls[0]["x_vector_only_mode"] is True


def test_qwen_local_config_endpoint(tmp_path, monkeypatch):
    class ConfiguredQwen:
        provider = "qwen3-tts"
        protocol = "qwen3-tts-local"
        preferred_chunk_chars = 150

        def __init__(self, model, device):
            self.name = model
            self.device = "cuda:0" if device == "auto" else device

        def synthesize(self, reference, text, emotion, output):
            raise AssertionError("not used")

    monkeypatch.setattr("app.main.Qwen3TTSSynthesizer", ConfiguredQwen)
    app = create_app(Settings(tmp_path, "mock", None, None))
    with TestClient(app) as client:
        response = client.post(
            "/api/config/voice-clone",
            json={"protocol": "qwen3-tts-local", "device": "auto"},
        )

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "engine": DEFAULT_QWEN_MODEL,
        "real_voice_cloning": True,
        "jobs_enabled": True,
        "protocol": "qwen3-tts-local",
        "device": "cuda:0",
    }


def test_web_form_keeps_xiaomi_mimo_as_default():
    html = Path("app/static/index.html").read_text(encoding="utf-8")
    protocol = '<option value="mimo-chat">MiMo / Chat Completions VoiceClone 兼容协议</option>'
    qwen = '<option value="qwen3-tts-local">Qwen3-TTS 本地模型</option>'

    assert html.index(protocol) < html.index(qwen)
    assert 'value="mimo-v2.5-tts-voiceclone"' in html
