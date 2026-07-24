from __future__ import annotations

import shutil
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO

from .audio import concatenate_wavs, normalize_reference, write_provenance
from .emotion import plan_emotion
from .storage import Store
from .synth import Synthesizer
from .text import SUPPORTED_BOOKS, chunk_text, extract_book, split_chapters


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobCancelled(Exception):
    pass


class StudioService:
    def __init__(
        self,
        store: Store,
        synth: Synthesizer,
        max_upload_mb: int = 200,
        chunk_chars: int = 110,
        allow_mock_jobs: bool = False,
    ):
        self.store = store
        self.synth = synth
        self.max_upload_bytes = max_upload_mb * 1024 * 1024
        self.chunk_chars = chunk_chars
        self.allow_mock_jobs = allow_mock_jobs
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="nvs-render")
        self._mark_interrupted_jobs()

    def _save_job(self, meta: dict) -> None:
        meta["updated_at"] = now_iso()
        self.store.save_meta("jobs", meta["id"], meta)

    def _mark_interrupted_jobs(self) -> None:
        for meta in self.store.list_meta("jobs"):
            if meta.get("status") not in {"queued", "running"}:
                continue
            meta.update(
                status="failed",
                stage="服务曾重启，原任务已中断",
                error="服务重启导致任务中断，请重新生成",
                completed_at=now_iso(),
            )
            self._save_job(meta)

    def list_jobs(self) -> list[dict]:
        jobs = self.store.list_meta("jobs")
        queued = sorted(
            (job for job in jobs if job.get("status") == "queued"),
            key=lambda job: job.get("created_at", ""),
        )
        queue_positions = {job["id"]: index + 1 for index, job in enumerate(queued)}
        for job in jobs:
            if job.get("status") == "queued":
                job["queue_position"] = queue_positions[job["id"]]
        return jobs

    def cancel_job(self, job_id: str) -> dict:
        meta = self.store.load_meta("jobs", job_id)
        if meta.get("status") not in {"queued", "running"}:
            raise ValueError("该任务已经结束，无法取消")
        meta["cancel_requested"] = True
        if meta["status"] == "queued":
            meta.update(status="cancelled", stage="已取消", completed_at=now_iso())
        else:
            meta["stage"] = "正在取消：等待当前 MiMo 请求返回"
        self._save_job(meta)
        return meta

    def retry_job(self, job_id: str) -> dict:
        meta = self.store.load_meta("jobs", job_id)
        if meta.get("status") not in {"failed", "cancelled"}:
            raise ValueError("只有失败或已取消的任务可以继续生成")
        if meta.get("engine") != self.synth.name:
            raise ValueError(f"请先启用任务原使用的语音引擎：{meta.get('engine')}")
        meta.update(
            status="queued",
            stage="等待从已有分片继续生成",
            error=None,
            retry_count=int(meta.get("retry_count", 0)) + 1,
        )
        meta.pop("cancel_requested", None)
        meta.pop("completed_at", None)
        self._save_job(meta)
        self.executor.submit(self._render, job_id)
        return meta

    def delete_job(self, job_id: str) -> None:
        meta = self.store.load_meta("jobs", job_id)
        if meta.get("status") in {"queued", "running"}:
            raise ValueError("任务正在生成，请先取消并等待任务结束后再删除")
        self.store.delete("jobs", job_id)

    def _raise_if_cancelled(self, job_id: str, meta: dict) -> None:
        current = self.store.load_meta("jobs", job_id)
        if current.get("cancel_requested") or current.get("status") == "cancelled":
            meta["cancel_requested"] = True
            raise JobCancelled

    def _copy_limited(self, source: BinaryIO, target: Path) -> int:
        size = 0
        with target.open("wb") as output:
            while block := source.read(1024 * 1024):
                size += len(block)
                if size > self.max_upload_bytes:
                    output.close()
                    target.unlink(missing_ok=True)
                    raise ValueError("上传文件过大")
                output.write(block)
        return size

    def add_voice(
        self,
        source: BinaryIO,
        filename: str,
        name: str,
        consent: bool,
        transcript: str = "",
    ) -> dict:
        if not consent:
            raise ValueError("必须确认已取得声音本人明确授权")
        item_id = self.store.new_id()
        folder = self.store.directory("voices", item_id)
        raw = folder / ("source" + Path(filename or "voice.wav").suffix.lower())
        self._copy_limited(source, raw)
        info = normalize_reference(raw, folder / "reference.wav")
        normalized_transcript = " ".join(transcript.split())
        if len(normalized_transcript) > 2000:
            raise ValueError("参考语音文字不能超过 2000 个字符")
        (folder / "reference.txt").write_text(normalized_transcript, encoding="utf-8")
        meta = {
            "id": item_id,
            "name": name.strip() or "我的声音",
            "filename": filename,
            "consent": True,
            "transcript_chars": len(normalized_transcript),
            "created_at": now_iso(),
            **info,
        }
        self.store.save_meta("voices", item_id, meta)
        return meta

    def add_book(self, source: BinaryIO, filename: str, title: str) -> dict:
        suffix = Path(filename or "book.txt").suffix.lower()
        if suffix not in SUPPORTED_BOOKS:
            raise ValueError("仅支持 TXT、Markdown 和 EPUB 小说")
        item_id = self.store.new_id()
        folder = self.store.directory("books", item_id)
        raw = folder / ("source" + suffix)
        self._copy_limited(source, raw)
        text = extract_book(raw)
        if not text:
            raise ValueError("小说正文为空")
        (folder / "book.txt").write_text(text, encoding="utf-8")
        chapters = split_chapters(text)
        meta = {
            "id": item_id, "title": title.strip() or Path(filename).stem, "filename": filename,
            "chars": len(text), "chapter_count": len(chapters),
            "chapters": [{"index": c["index"], "title": c["title"], "chars": len(str(c["text"]))} for c in chapters],
            "created_at": now_iso(),
        }
        self.store.save_meta("books", item_id, meta)
        return meta

    def create_job(
        self,
        voice_id: str,
        book_id: str,
        chapter_start: int,
        chapter_end: int | None,
        emotion_strength: float,
        preview_chars: int | None = None,
    ) -> dict:
        if self.synth.name == "mock" and not self.allow_mock_jobs:
            raise ValueError("当前是演示引擎，不能生成真实语音。请先启用 MiMo、Qwen3-TTS 或 IndexTTS2")
        voice = self.store.load_meta("voices", voice_id)
        book = self.store.load_meta("books", book_id)
        count = int(book["chapter_count"])
        end = count - 1 if chapter_end is None else chapter_end
        if chapter_start < 0 or end < chapter_start or end >= count:
            raise ValueError("章节范围无效")
        normalized_strength = max(0.0, min(1.0, emotion_strength))
        for active in self.store.list_meta("jobs"):
            if active.get("status") not in {"queued", "running"}:
                continue
            same_request = (
                active.get("voice_id") == voice["id"]
                and active.get("book_id") == book["id"]
                and active.get("chapter_start") == chapter_start
                and active.get("chapter_end") == end
                and active.get("preview_chars") == preview_chars
                and active.get("emotion_strength") == normalized_strength
            )
            if same_request:
                raise ValueError("相同的任务已经在生成或排队，请勿重复提交")
        job_id = self.store.new_id()
        meta = {
            "id": job_id, "voice_id": voice["id"], "book_id": book["id"], "chapter_start": chapter_start,
            "chapter_end": end, "emotion_strength": normalized_strength,
            "preview_chars": preview_chars, "engine": self.synth.name, "status": "queued",
            "stage": "等待生成线程", "progress": 0, "created_at": now_iso(), "error": None,
        }
        self._save_job(meta)
        self.executor.submit(self._render, job_id)
        return meta

    def set_synthesizer(self, synth: Synthesizer) -> None:
        active = [job for job in self.store.list_meta("jobs") if job.get("status") in {"queued", "running"}]
        if active:
            raise ValueError("有任务正在生成，暂时不能切换语音引擎")
        self.synth = synth

    def _render(self, job_id: str) -> None:
        meta = self.store.load_meta("jobs", job_id)
        if meta.get("status") == "cancelled" or meta.get("cancel_requested"):
            return
        folder = self.store.directory("jobs", job_id)
        try:
            meta.update(status="running", stage="正在准备文本和参考音频", started_at=now_iso())
            self._save_job(meta)
            book_dir = self.store.directory("books", meta["book_id"])
            voice_dir = self.store.directory("voices", meta["voice_id"])
            chapters = split_chapters((book_dir / "book.txt").read_text(encoding="utf-8"))
            selected = chapters[meta["chapter_start"] : meta["chapter_end"] + 1]
            chunk_limit = self.synth.preferred_chunk_chars or self.chunk_chars
            if meta.get("preview_chars"):
                preview_text = "\n".join(str(chapter["text"]) for chapter in selected)[: int(meta["preview_chars"])]
                chunks = chunk_text(preview_text, chunk_limit)
            else:
                chunks = [chunk for chapter in selected for chunk in chunk_text(str(chapter["text"]), chunk_limit)]
            if not chunks:
                raise ValueError("所选章节没有可朗读内容")
            parts: list[Path] = []
            manifest = []
            for index, text in enumerate(chunks):
                self._raise_if_cancelled(job_id, meta)
                part = folder / "parts" / f"{index:05d}.wav"
                emotion = plan_emotion(text, meta["emotion_strength"])
                if not part.exists():
                    if self.synth.provider == "voice-clone":
                        meta["stage"] = f"正在等待语音克隆 API 返回第 {index + 1}/{len(chunks)} 段"
                    elif self.synth.provider == "qwen3-tts" and index == 0:
                        meta["stage"] = "正在加载 Qwen3-TTS（首次运行会下载模型）"
                    else:
                        meta["stage"] = f"正在生成第 {index + 1}/{len(chunks)} 段"
                    self._save_job(meta)
                    self.synth.synthesize(voice_dir / "reference.wav", text, emotion, part)
                    self._raise_if_cancelled(job_id, meta)
                parts.append(part)
                manifest.append({"index": index, "text": text, "emotion": emotion, "file": part.name})
                meta["progress"] = int((index + 1) * 95 / len(chunks))
                meta["stage"] = f"已完成 {index + 1}/{len(chunks)} 段"
                self._save_job(meta)
            meta["stage"] = "正在合并音频"
            self._save_job(meta)
            output = folder / "audiobook.wav"
            concatenate_wavs(parts, output)
            write_provenance(folder / "provenance.json", {
                "generated_at": now_iso(), "synthetic_audio": True, "consent_confirmed": True,
                "engine": self.synth.name, "voice_id": meta["voice_id"], "book_id": meta["book_id"], "segments": manifest,
            })
            meta.update(
                status="completed", stage="可以在线播放", progress=100,
                completed_at=now_iso(), output="audiobook.wav",
            )
        except JobCancelled:
            meta.update(status="cancelled", stage="已取消", completed_at=now_iso(), error=None)
        except Exception as exc:
            meta.update(status="failed", stage="生成失败", error=str(exc), completed_at=now_iso())
        self._save_job(meta)

    def shutdown(self) -> None:
        self.executor.shutdown(wait=True, cancel_futures=False)
