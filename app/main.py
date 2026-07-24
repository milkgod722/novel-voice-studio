from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import Settings
from .audio import audio_media_type
from .service import StudioService
from .storage import Store
from .synth import (
    DEFAULT_MIMO_MODEL,
    MiMoVoiceCloneSynthesizer,
    REMOTE_DEFAULTS,
    RemoteProviderSynthesizer,
    build_synthesizer,
)


class JobRequest(BaseModel):
    voice_id: str
    book_id: str
    chapter_start: int = Field(default=0, ge=0)
    chapter_end: int | None = Field(default=None, ge=0)
    emotion_strength: float = Field(default=0.65, ge=0, le=1)
    preview_chars: int | None = Field(default=None, ge=50, le=2000)
    output_format: Literal["mp3", "wav"] = "mp3"


class VoiceCloneConfigRequest(BaseModel):
    protocol: Literal[
        "mimo-chat",
        "aliyun-cosyvoice",
        "tencent-tts",
        "baidu-voice-clone",
        "google-cloud-tts",
        "openai-tts",
        "indextts-url",
    ] = "mimo-chat"
    api_url: str | None = Field(
        default="https://api.xiaomimimo.com/v1/chat/completions",
        max_length=2048,
    )
    api_key: str | None = Field(default=None, max_length=4096)
    api_secret: str | None = Field(default=None, max_length=4096)
    model: str | None = Field(
        default=None,
        min_length=1,
        max_length=512,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/\\-]{0,511}$",
    )
    voice_id: str | None = Field(default=None, max_length=16384)
    project_id: str | None = Field(default=None, max_length=256)
    language: str = Field(
        default="zh-CN",
        min_length=2,
        max_length=32,
        pattern=r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})?$",
    )
    auth_mode: Literal["api-key", "bearer"] = "api-key"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    service = StudioService(
        Store(settings.data_dir),
        build_synthesizer(
            settings.engine,
            settings.mimo_api_key,
            settings.mimo_base_url,
            settings.mimo_use_system_proxy,
            settings.mimo_model,
            settings.mimo_auth_mode,
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
        payload = {
            "status": "ok",
            "engine": service.synth.name,
            "real_voice_cloning": real,
            "jobs_enabled": real or service.allow_mock_jobs,
        }
        if service.synth.provider == "voice-clone":
            payload.update(
                protocol=service.synth.protocol,
                api_url=service.synth.api_url,
                auth_mode=service.synth.auth_mode,
                voice_mode="reference-audio",
            )
        elif service.synth.provider == "remote-api":
            payload.update(
                protocol=service.synth.protocol,
                api_url=service.synth.api_url,
                auth_mode=service.synth.auth_mode,
                model=service.synth.model,
                voice_mode=(
                    "reference-audio"
                    if service.synth.protocol == "indextts-url"
                    else "registered-voice"
                ),
            )
        return payload

    @app.get("/api/health")
    def health():
        return health_payload()

    @app.post("/api/config/mimo")
    @app.post("/api/config/voice-clone")
    def configure_voice_clone(request: VoiceCloneConfigRequest):
        try:
            if request.protocol == "mimo-chat":
                if not request.api_key or len(request.api_key.strip()) < 8:
                    raise ValueError("远程语音克隆 API Key 至少需要 8 个字符")
                if not request.api_url:
                    raise ValueError("远程语音克隆必须填写 API URL")
                service.set_synthesizer(MiMoVoiceCloneSynthesizer(
                    request.api_key,
                    request.api_url,
                    use_system_proxy=settings.mimo_use_system_proxy,
                    model=request.model or DEFAULT_MIMO_MODEL,
                    auth_mode=request.auth_mode,
                ))
            else:
                defaults = REMOTE_DEFAULTS[request.protocol]
                service.set_synthesizer(RemoteProviderSynthesizer(
                    request.protocol,
                    request.api_url or defaults["api_url"],
                    request.api_key or "",
                    api_secret=request.api_secret or "",
                    model=request.model or defaults["model"],
                    voice_id=request.voice_id or "",
                    project_id=request.project_id or "",
                    language=request.language,
                    auth_mode=request.auth_mode,
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
    def add_voice(
        file: UploadFile = File(...),
        name: str = Form("我的声音"),
        consent: bool = Form(...),
        transcript: str = Form(""),
    ):
        try:
            return service.add_voice(
                file.file, file.filename or "voice.wav", name, consent, transcript
            )
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
        filename = job.get("output", "audiobook.wav")
        output_format = job.get("output_format", Path(filename).suffix.lstrip(".") or "wav")
        path = service.store.directory("jobs", job_id) / filename
        return FileResponse(path, media_type=audio_media_type(output_format))

    @app.get("/api/jobs/{job_id}/download")
    def download_audio(job_id: str):
        try:
            job = service.store.load_meta("jobs", job_id)
        except KeyError as exc:
            raise api_error(exc) from exc
        if job["status"] != "completed":
            raise HTTPException(status_code=409, detail="音频尚未生成完成")
        filename = job.get("output", "audiobook.wav")
        output_format = job.get("output_format", Path(filename).suffix.lstrip(".") or "wav")
        path = service.store.directory("jobs", job_id) / filename
        return FileResponse(
            path,
            media_type=audio_media_type(output_format),
            filename=f"novel-{job_id}.{output_format}",
        )

    static = Path(__file__).with_name("static")
    app.mount("/", StaticFiles(directory=static, html=True), name="static")
    return app


app = create_app()
