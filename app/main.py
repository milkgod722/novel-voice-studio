from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import Settings
from .service import StudioService
from .storage import Store
from .synth import MiMoVoiceCloneSynthesizer, build_synthesizer


class JobRequest(BaseModel):
    voice_id: str
    book_id: str
    chapter_start: int = Field(default=0, ge=0)
    chapter_end: int | None = Field(default=None, ge=0)
    emotion_strength: float = Field(default=0.65, ge=0, le=1)
    preview_chars: int | None = Field(default=None, ge=50, le=2000)


class MiMoConfigRequest(BaseModel):
    api_key: str = Field(min_length=8, max_length=512)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    service = StudioService(
        Store(settings.data_dir),
        build_synthesizer(
            settings.engine,
            settings.indextts_path,
            settings.model_dir,
            settings.mimo_api_key,
            settings.mimo_base_url,
            settings.mimo_use_system_proxy,
        ),
        settings.max_upload_mb, settings.chunk_chars, settings.allow_mock_jobs,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        service.shutdown()

    app = FastAPI(title="小说声工坊", version="0.1.0", lifespan=lifespan)
    app.state.service = service

    def api_error(exc: Exception) -> HTTPException:
        return HTTPException(status_code=404 if isinstance(exc, KeyError) else 400, detail=str(exc).strip("'"))

    def health_payload():
        real = service.synth.name != "mock"
        return {
            "status": "ok",
            "engine": service.synth.name,
            "real_voice_cloning": real,
            "jobs_enabled": real or service.allow_mock_jobs,
        }

    @app.get("/api/health")
    def health():
        return health_payload()

    @app.post("/api/config/mimo")
    def configure_mimo(request: MiMoConfigRequest):
        try:
            service.set_synthesizer(MiMoVoiceCloneSynthesizer(
                request.api_key,
                settings.mimo_base_url,
                use_system_proxy=settings.mimo_use_system_proxy,
            ))
            return health_payload()
        except (ValueError, RuntimeError) as exc:
            raise api_error(exc) from exc

    @app.get("/api/library")
    def library():
        return {
            "voices": service.store.list_meta("voices"),
            "books": service.store.list_meta("books"),
            "jobs": service.list_jobs(),
        }

    @app.post("/api/voices", status_code=201)
    def add_voice(file: UploadFile = File(...), name: str = Form("我的声音"), consent: bool = Form(...)):
        try:
            return service.add_voice(file.file, file.filename or "voice.wav", name, consent)
        except (ValueError, RuntimeError) as exc:
            raise api_error(exc) from exc

    @app.post("/api/books", status_code=201)
    def add_book(file: UploadFile = File(...), title: str = Form("")):
        try:
            return service.add_book(file.file, file.filename or "book.txt", title)
        except (ValueError, RuntimeError) as exc:
            raise api_error(exc) from exc

    @app.post("/api/jobs", status_code=202)
    def create_job(request: JobRequest):
        try:
            return service.create_job(**request.model_dump())
        except (ValueError, KeyError) as exc:
            raise api_error(exc) from exc

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str):
        try:
            return service.store.load_meta("jobs", job_id)
        except KeyError as exc:
            raise api_error(exc) from exc

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel_job(job_id: str):
        try:
            return service.cancel_job(job_id)
        except (KeyError, ValueError) as exc:
            raise api_error(exc) from exc

    @app.post("/api/jobs/{job_id}/retry")
    def retry_job(job_id: str):
        try:
            return service.retry_job(job_id)
        except (KeyError, ValueError) as exc:
            raise api_error(exc) from exc

    @app.delete("/api/jobs/{job_id}", status_code=204)
    def delete_job(job_id: str):
        try:
            service.delete_job(job_id)
            return Response(status_code=204)
        except (KeyError, ValueError) as exc:
            raise api_error(exc) from exc

    @app.get("/api/jobs/{job_id}/audio")
    def get_audio(job_id: str):
        try:
            job = service.store.load_meta("jobs", job_id)
        except KeyError as exc:
            raise api_error(exc) from exc
        if job["status"] != "completed":
            raise HTTPException(status_code=409, detail="音频尚未生成完成")
        path = service.store.directory("jobs", job_id) / "audiobook.wav"
        return FileResponse(path, media_type="audio/wav")

    @app.get("/api/jobs/{job_id}/download")
    def download_audio(job_id: str):
        try:
            job = service.store.load_meta("jobs", job_id)
        except KeyError as exc:
            raise api_error(exc) from exc
        if job["status"] != "completed":
            raise HTTPException(status_code=409, detail="音频尚未生成完成")
        path = service.store.directory("jobs", job_id) / "audiobook.wav"
        return FileResponse(path, media_type="audio/wav", filename=f"novel-{job_id}.wav")

    static = Path(__file__).with_name("static")
    app.mount("/", StaticFiles(directory=static, html=True), name="static")
    return app


app = create_app()
