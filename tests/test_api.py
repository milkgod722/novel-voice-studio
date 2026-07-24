import io
import time
import wave

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def make_wav():
    out = io.BytesIO()
    with wave.open(out, "wb") as writer:
        writer.setnchannels(1); writer.setsampwidth(2); writer.setframerate(24000)
        writer.writeframes(b"\x00\x00" * 24000 * 4)
    return out.getvalue()


def test_http_end_to_end(tmp_path):
    app = create_app(Settings(tmp_path, "mock", None, None, allow_mock_jobs=True))
    with TestClient(app) as client:
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
        job = client.post("/api/jobs", json={"voice_id": voice.json()["id"], "book_id": book.json()["id"], "chapter_start": 0, "preview_chars": 50})
        assert job.status_code == 202
        assert job.json()["preview_chars"] == 50
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
