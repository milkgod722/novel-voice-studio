import io
import time
import wave
from pathlib import Path

from app.service import StudioService
from app.storage import Store
from app.synth import MockSynthesizer


def wav_bytes(seconds=3.2, rate=24000):
    stream = io.BytesIO()
    with wave.open(stream, "wb") as writer:
        writer.setnchannels(1); writer.setsampwidth(2); writer.setframerate(rate)
        writer.writeframes(b"\x00\x00" * int(seconds * rate))
    stream.seek(0)
    return stream


def test_complete_audiobook_pipeline(tmp_path: Path):
    service = StudioService(Store(tmp_path), MockSynthesizer(), chunk_chars=18)
    try:
        voice = service.add_voice(wav_bytes(), "wechat.wav", "测试者", True)
        book = service.add_book(io.BytesIO("第1章 相遇\n她惊喜地笑了！\n第2章 离别\n他流着泪说再见。".encode()), "novel.txt", "小城")
        job = service.create_job(voice["id"], book["id"], 0, 1, 0.65)
        deadline = time.time() + 10
        while time.time() < deadline:
            job = service.store.load_meta("jobs", job["id"])
            if job["status"] in {"completed", "failed"}: break
            time.sleep(0.05)
        assert job["status"] == "completed", job.get("error")
        output = tmp_path / "jobs" / job["id"] / "audiobook.wav"
        assert output.stat().st_size > 1000
        assert (output.parent / "provenance.json").exists()
    finally:
        service.shutdown()

def test_voice_requires_consent(tmp_path: Path):
    service = StudioService(Store(tmp_path), MockSynthesizer())
    try:
        try:
            service.add_voice(wav_bytes(), "voice.wav", "未授权", False)
            assert False, "expected consent validation"
        except ValueError as exc:
            assert "授权" in str(exc)
    finally:
        service.shutdown()
