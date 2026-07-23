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
    app = create_app(Settings(tmp_path, "mock", None, None))
    with TestClient(app) as client:
        health = client.get("/api/health").json()
        assert health == {"status": "ok", "engine": "mock", "real_voice_cloning": False}
        voice = client.post("/api/voices", files={"file": ("chat.wav", make_wav(), "audio/wav")}, data={"name": "我", "consent": "true"})
        assert voice.status_code == 201, voice.text
        book = client.post("/api/books", files={"file": ("book.txt", "第1章\n你好，世界！".encode(), "text/plain")}, data={"title": "测试书"})
        assert book.status_code == 201, book.text
        job = client.post("/api/jobs", json={"voice_id": voice.json()["id"], "book_id": book.json()["id"], "chapter_start": 0})
        assert job.status_code == 202
        for _ in range(100):
            state = client.get(f"/api/jobs/{job.json()['id']}").json()
            if state["status"] == "completed": break
            time.sleep(0.03)
        audio = client.get(f"/api/jobs/{job.json()['id']}/audio")
        assert audio.status_code == 200
        assert audio.headers["content-type"] == "audio/wav"
