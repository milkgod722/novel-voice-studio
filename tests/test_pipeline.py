import io
import threading
import time
import wave
from pathlib import Path

from app.service import StudioService
from app.storage import Store
from app.synth import MockSynthesizer


class BlockingSynthesizer(MockSynthesizer):
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()

    def synthesize(self, reference, text, emotion, output):
        self.started.set()
        assert self.release.wait(5), "test synthesizer was not released"
        super().synthesize(reference, text, emotion, output)


class FailOnceSynthesizer(MockSynthesizer):
    def __init__(self):
        self.calls = 0

    def synthesize(self, reference, text, emotion, output):
        self.calls += 1
        if self.calls == 2:
            raise RuntimeError("temporary provider failure")
        super().synthesize(reference, text, emotion, output)


class BlockOnSecondSynthesizer(MockSynthesizer):
    def __init__(self):
        self.calls = 0
        self.second_started = threading.Event()
        self.release = threading.Event()

    def synthesize(self, reference, text, emotion, output):
        self.calls += 1
        if self.calls == 2:
            self.second_started.set()
            assert self.release.wait(5), "second segment was not released"
        super().synthesize(reference, text, emotion, output)


def wav_bytes(seconds=3.2, rate=24000):
    stream = io.BytesIO()
    with wave.open(stream, "wb") as writer:
        writer.setnchannels(1); writer.setsampwidth(2); writer.setframerate(rate)
        writer.writeframes(b"\x00\x00" * int(seconds * rate))
    stream.seek(0)
    return stream


def test_complete_audiobook_pipeline(tmp_path: Path):
    service = StudioService(Store(tmp_path), MockSynthesizer(), chunk_chars=18, allow_mock_jobs=True)
    try:
        voice = service.add_voice(wav_bytes(), "wechat.wav", "测试者", True)
        book = service.add_book(io.BytesIO("第1章 相遇\n她惊喜地笑了！\n第2章 离别\n他流着泪说再见。".encode()), "novel.txt", "小城")
        job = service.create_job(
            voice["id"], book["id"], 0, 1, 0.65, output_format="wav"
        )
        deadline = time.time() + 10
        while time.time() < deadline:
            job = service.store.load_meta("jobs", job["id"])
            if job["status"] in {"completed", "failed"}: break
            time.sleep(0.05)
        assert job["status"] == "completed", job.get("error")
        output = tmp_path / "jobs" / job["id"] / "audiobook.wav"
        assert output.stat().st_size > 1000
        assert job["output_format"] == "wav"
        assert job["output_bytes"] == output.stat().st_size
        assert len(job["segments"]) == 2
        assert all(
            (output.parent / segment["output"]).exists()
            for segment in job["segments"]
        )
        assert not (output.parent / "parts").exists()
        assert (output.parent / "provenance.json").exists()
    finally:
        service.shutdown()


def test_first_chapter_is_playable_while_later_segment_is_still_running(tmp_path: Path):
    synth = BlockOnSecondSynthesizer()
    service = StudioService(
        Store(tmp_path),
        synth,
        chunk_chars=20,
        segment_chars=20,
        allow_mock_jobs=True,
    )
    try:
        voice = service.add_voice(wav_bytes(), "voice.wav", "测试者", True)
        book = service.add_book(
            io.BytesIO("第一章\n第一段。\n第二章\n第二段。".encode()),
            "book.txt",
            "分段测试",
        )
        job = service.create_job(
            voice["id"], book["id"], 0, 1, 0.65, output_format="wav"
        )
        assert synth.second_started.wait(3)
        running = service.store.load_meta("jobs", job["id"])
        assert running["status"] == "running"
        assert running["ready_segments"] == 1
        assert running["total_segments"] == 2
        first = tmp_path / "jobs" / job["id"] / running["segments"][0]["output"]
        assert first.exists()
        assert not (first.parent.parent / "parts" / "00000.wav").exists()
        synth.release.set()
    finally:
        synth.release.set()
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


def test_mock_engine_rejects_real_jobs(tmp_path: Path):
    service = StudioService(Store(tmp_path), MockSynthesizer())
    try:
        try:
            service.create_job("voice", "book", 0, 0, 0.65)
            assert False, "expected mock engine guard"
        except ValueError as exc:
            assert "演示引擎" in str(exc)
    finally:
        service.shutdown()


def test_duplicate_active_job_is_rejected(tmp_path: Path):
    service = StudioService(Store(tmp_path), MockSynthesizer(), allow_mock_jobs=True)
    try:
        voice = service.add_voice(wav_bytes(), "voice.wav", "测试者", True)
        book = service.add_book(io.BytesIO("第1章\n测试内容。".encode()), "book.txt", "测试书")
        first = service.create_job(voice["id"], book["id"], 0, 0, 0.65, 50)
        try:
            service.create_job(voice["id"], book["id"], 0, 0, 0.65, 50)
            assert False, "expected duplicate task guard"
        except ValueError as exc:
            assert "重复提交" in str(exc)
        assert first["status"] in {"queued", "running"}
    finally:
        service.shutdown()


def test_running_job_can_be_cancelled(tmp_path: Path):
    synth = BlockingSynthesizer()
    service = StudioService(Store(tmp_path), synth, allow_mock_jobs=True)
    try:
        voice = service.add_voice(wav_bytes(), "voice.wav", "测试者", True)
        book = service.add_book(io.BytesIO("第1章\n测试内容。".encode()), "book.txt", "测试书")
        job = service.create_job(voice["id"], book["id"], 0, 0, 0.65, 50, "wav")
        assert synth.started.wait(2)
        cancelled = service.cancel_job(job["id"])
        assert cancelled["cancel_requested"] is True
        assert "正在取消" in cancelled["stage"]
        try:
            service.delete_job(job["id"])
            assert False, "expected active job deletion guard"
        except ValueError as exc:
            assert "正在生成" in str(exc)
        synth.release.set()
        deadline = time.time() + 3
        while time.time() < deadline:
            cancelled = service.store.load_meta("jobs", job["id"])
            if cancelled["status"] == "cancelled":
                break
            time.sleep(0.03)
        assert cancelled["status"] == "cancelled"
        job_folder = tmp_path / "jobs" / job["id"]
        service.delete_job(job["id"])
        assert not job_folder.exists()
    finally:
        synth.release.set()
        service.shutdown()


def test_failed_job_resumes_from_cached_parts(tmp_path: Path):
    service = StudioService(
        Store(tmp_path), FailOnceSynthesizer(), chunk_chars=4, allow_mock_jobs=True
    )
    try:
        voice = service.add_voice(wav_bytes(), "voice.wav", "测试者", True)
        book = service.add_book(io.BytesIO("第1章\n测试内容。".encode()), "book.txt", "测试书")
        job = service.create_job(
            voice["id"], book["id"], 0, 0, 0.65, 50, output_format="wav"
        )
        deadline = time.time() + 3
        while time.time() < deadline:
            job = service.store.load_meta("jobs", job["id"])
            if job["status"] == "completed":
                break
            time.sleep(0.03)
        part = next((tmp_path / "jobs" / job["id"] / "parts").glob("*.wav"))
        original_mtime = part.stat().st_mtime_ns
        job.update(status="failed", stage="生成失败", error="模拟网络中断")
        service.store.save_meta("jobs", job["id"], job)
        resumed = service.retry_job(job["id"])
        assert resumed["status"] == "queued"
        deadline = time.time() + 3
        while time.time() < deadline:
            resumed = service.store.load_meta("jobs", job["id"])
            if resumed["status"] == "completed":
                break
            time.sleep(0.03)
        assert resumed["status"] == "completed"
        assert service.synth.calls == 3
        assert original_mtime > 0
        assert not part.parent.exists()
    finally:
        service.shutdown()


def test_retry_skips_already_published_segments(tmp_path: Path):
    synth = FailOnceSynthesizer()
    service = StudioService(
        Store(tmp_path),
        synth,
        chunk_chars=20,
        segment_chars=20,
        allow_mock_jobs=True,
    )
    try:
        voice = service.add_voice(wav_bytes(), "voice.wav", "测试者", True)
        book = service.add_book(
            io.BytesIO("第一章\n第一段。\n第二章\n第二段。".encode()),
            "book.txt",
            "断点测试",
        )
        job = service.create_job(
            voice["id"], book["id"], 0, 1, 0.65, output_format="wav"
        )
        deadline = time.time() + 3
        while time.time() < deadline:
            failed = service.store.load_meta("jobs", job["id"])
            if failed["status"] == "failed":
                break
            time.sleep(0.03)
        assert failed["status"] == "failed"
        assert failed["ready_segments"] == 1
        assert synth.calls == 2
        first_output = (
            tmp_path / "jobs" / job["id"] / failed["segments"][0]["output"]
        )
        first_mtime = first_output.stat().st_mtime_ns
        service.retry_job(job["id"])
        deadline = time.time() + 3
        while time.time() < deadline:
            resumed = service.store.load_meta("jobs", job["id"])
            if resumed["status"] == "completed":
                break
            time.sleep(0.03)
        assert resumed["status"] == "completed", resumed.get("error")
        assert synth.calls == 3
        assert first_output.stat().st_mtime_ns == first_mtime
    finally:
        service.shutdown()
