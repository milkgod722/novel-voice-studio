import io
import time
import wave

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import JobRequest, create_app


def make_wav():
    out = io.BytesIO()
    with wave.open(out, "wb") as writer:
        writer.setnchannels(1); writer.setsampwidth(2); writer.setframerate(24000)
        writer.writeframes(b"\x00\x00" * 24000 * 4)
    return out.getvalue()


def test_job_request_defaults_to_mp3():
    request = JobRequest(voice_id="voice", book_id="book")
    assert request.output_format == "mp3"
    assert request.segment_chars == 2000


def test_job_request_rejects_unsafe_segment_sizes():
    for value in (499, 10001):
        try:
            JobRequest(voice_id="voice", book_id="book", segment_chars=value)
            assert False, f"expected validation error for {value}"
        except ValueError:
            pass


def test_failed_uploads_leave_no_orphan_directories(tmp_path):
    app = create_app(Settings(tmp_path, "mock", max_upload_mb=1, allow_mock_jobs=True))
    with TestClient(app) as client:
        invalid_voice = client.post(
            "/api/voices",
            files={"file": ("broken.wav", b"not-a-wave", "audio/wav")},
            data={"name": "坏音频", "consent": "true"},
        )
        assert invalid_voice.status_code == 400
        assert not any((tmp_path / "voices").iterdir())

        oversized_voice = client.post(
            "/api/voices",
            files={"file": ("large.wav", b"0" * (1024 * 1024 + 1), "audio/wav")},
            data={"name": "超大音频", "consent": "true"},
        )
        assert oversized_voice.status_code == 400
        assert "上传文件过大" in oversized_voice.json()["detail"]
        assert not any((tmp_path / "voices").iterdir())

        empty_book = client.post(
            "/api/books",
            files={"file": ("empty.txt", b"", "text/plain")},
        )
        assert empty_book.status_code == 400
        assert not any((tmp_path / "books").iterdir())

        oversized_book = client.post(
            "/api/books",
            files={"file": ("large.txt", b"0" * (1024 * 1024 + 1), "text/plain")},
        )
        assert oversized_book.status_code == 400
        assert "上传文件过大" in oversized_book.json()["detail"]
        assert not any((tmp_path / "books").iterdir())


def test_http_end_to_end(tmp_path):
    app = create_app(Settings(tmp_path, "mock", allow_mock_jobs=True))
    with TestClient(app) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert "创作设置" in page.text
        assert "生成参数" in page.text
        assert "作品与进度" in page.text
        assert "请先上传参考声音" in page.text
        assert 'data-open-panel="book-upload-panel"' in page.text
        assert "语音引擎设置" in page.text
        assert "/app.js?v=9" in page.text
        health = client.get("/api/health").json()
        assert health == {
            "status": "ok",
            "engine": "mock",
            "real_voice_cloning": False,
            "jobs_enabled": True,
        }
        voice = client.post(
            "/api/voices",
            files={"file": ("chat.wav", make_wav(), "audio/wav")},
            data={"name": "我", "consent": "true", "transcript": "这是参考语音。"},
        )
        assert voice.status_code == 201, voice.text
        assert voice.json()["transcript_chars"] == 7
        reference_text = tmp_path / "voices" / voice.json()["id"] / "reference.txt"
        assert reference_text.read_text(encoding="utf-8") == "这是参考语音。"
        book = client.post("/api/books", files={"file": ("book.txt", "第1章\n你好，世界！".encode(), "text/plain")}, data={"title": "测试书"})
        assert book.status_code == 201, book.text
        job = client.post("/api/jobs", json={"voice_id": voice.json()["id"], "book_id": book.json()["id"], "chapter_start": 0, "preview_chars": 50, "output_format": "wav", "segment_chars": 1000})
        assert job.status_code == 202
        assert job.json()["preview_chars"] == 50
        assert job.json()["book_title"] == "测试书"
        assert job.json()["voice_name"] == "我"
        assert job.json()["segment_chars"] == 1000
        for _ in range(100):
            state = client.get(f"/api/jobs/{job.json()['id']}").json()
            if state["status"] == "completed": break
            time.sleep(0.03)
        audio = client.get(f"/api/jobs/{job.json()['id']}/audio")
        assert audio.status_code == 200
        assert audio.headers["content-type"] == "audio/wav"
        assert "content-disposition" not in audio.headers
        download = client.get(f"/api/jobs/{job.json()['id']}/download")
        assert download.status_code == 200
        assert download.headers["content-disposition"].startswith("attachment;")
        assert ".wav" in download.headers["content-disposition"]
        assert state["ready_segments"] == 1
        segment_audio = client.get(
            f"/api/jobs/{job.json()['id']}/segments/0/audio"
        )
        assert segment_audio.status_code == 200
        assert segment_audio.headers["content-type"] == "audio/wav"
        segment_download = client.get(
            f"/api/jobs/{job.json()['id']}/segments/0/download"
        )
        assert segment_download.status_code == 200
        assert "segment-1.wav" in segment_download.headers["content-disposition"]
        finished_cancel = client.post(f"/api/jobs/{job.json()['id']}/cancel")
        assert finished_cancel.status_code == 400
        removed = client.delete(f"/api/jobs/{job.json()['id']}")
        assert removed.status_code == 204
        assert client.get(f"/api/jobs/{job.json()['id']}").status_code == 404
        token_plan = client.post("/api/config/mimo", json={"api_key": "tp-test-api-key"})
        assert token_plan.status_code == 400
        assert "Token Plan" in token_plan.json()["detail"]
        invalid_model = client.post(
            "/api/config/mimo",
            json={"api_key": "sk-test-api-key", "model": "bad model"},
        )
        assert invalid_model.status_code == 422
        configured = client.post(
            "/api/config/voice-clone",
            json={
                "protocol": "mimo-chat",
                "api_url": "http://127.0.0.1:9000/v1",
                "api_key": "gateway-test-key",
                "model": "mimo-user-voiceclone",
                "auth_mode": "bearer",
            },
        )
        assert configured.status_code == 200
        assert configured.json()["engine"] == "mimo-user-voiceclone"
        assert configured.json()["real_voice_cloning"] is True
        assert configured.json()["api_url"] == "http://127.0.0.1:9000/v1/chat/completions"
        assert configured.json()["auth_mode"] == "bearer"
        assert configured.json()["protocol"] == "mimo-chat"
