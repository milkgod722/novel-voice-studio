from __future__ import annotations

import shutil
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO

from .audio import concatenate_mp3s, concatenate_wavs, encode_mp3, normalize_reference, write_provenance
from .emotion import plan_emotion_sequence
from .storage import Store
from .synth import Synthesizer
from .text import SUPPORTED_BOOKS, extract_book, plan_progressive_segments, split_chapters


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
        segment_chars: int = 2000,
    ):
        self.store = store
        self.synth = synth
        self.max_upload_bytes = max_upload_mb * 1024 * 1024
        self.chunk_chars = chunk_chars
        self.segment_chars = max(chunk_chars, segment_chars)
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
            meta["stage"] = "正在取消：等待当前语音请求返回"
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
        try:
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
        except Exception:
            shutil.rmtree(folder, ignore_errors=True)
            raise

    def add_book(self, source: BinaryIO, filename: str, title: str) -> dict:
        suffix = Path(filename or "book.txt").suffix.lower()
        if suffix not in SUPPORTED_BOOKS:
            raise ValueError("仅支持 TXT、Markdown 和 EPUB 小说")
        item_id = self.store.new_id()
        folder = self.store.directory("books", item_id)
        try:
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
        except Exception:
            shutil.rmtree(folder, ignore_errors=True)
            raise

    def create_job(
        self,
        voice_id: str,
        book_id: str,
        chapter_start: int,
        chapter_end: int | None,
        emotion_strength: float,
        preview_chars: int | None = None,
        output_format: str = "mp3",
        segment_chars: int | None = None,
    ) -> dict:
        if self.synth.name == "mock" and not self.allow_mock_jobs:
            raise ValueError("当前是演示引擎，不能生成真实语音。请先配置一个远程 TTS API")
        voice = self.store.load_meta("voices", voice_id)
        book = self.store.load_meta("books", book_id)
        count = int(book["chapter_count"])
        end = count - 1 if chapter_end is None else chapter_end
        if chapter_start < 0 or end < chapter_start or end >= count:
            raise ValueError("章节范围无效")
        normalized_strength = max(0.0, min(1.0, emotion_strength))
        normalized_segment_chars = max(
            self.chunk_chars,
            self.segment_chars if segment_chars is None else int(segment_chars),
        )
        if output_format not in {"mp3", "wav"}:
            raise ValueError("成品格式仅支持 MP3 或 WAV")
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
                and active.get("output_format", "mp3") == output_format
                and int(active.get("segment_chars", self.segment_chars))
                == normalized_segment_chars
            )
            if same_request:
                raise ValueError("相同的任务已经在生成或排队，请勿重复提交")
        job_id = self.store.new_id()
        meta = {
            "id": job_id, "voice_id": voice["id"], "book_id": book["id"], "chapter_start": chapter_start,
            "book_title": book["title"], "voice_name": voice["name"],
            "chapter_end": end, "emotion_strength": normalized_strength,
            "preview_chars": preview_chars, "output_format": output_format,
            "progressive": True, "segment_chars": normalized_segment_chars, "segments": [],
            "engine": self.synth.name, "status": "queued",
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
                source_segments = [{
                    "index": meta["chapter_start"],
                    "title": "试听样片",
                    "text": preview_text,
                }]
                segment_plan = plan_progressive_segments(
                    source_segments, chunk_limit, max(self.segment_chars, len(preview_text))
                )
            else:
                segment_plan = plan_progressive_segments(
                    selected, chunk_limit, int(meta.get("segment_chars", self.segment_chars))
                )
            chunks = [
                text
                for segment in segment_plan
                for text in segment["chunks"]
            ]
            if not chunks or not segment_plan:
                raise ValueError("所选章节没有可朗读内容")
            emotions = plan_emotion_sequence(chunks, meta["emotion_strength"])
            manifest = []
            output_format = meta.get("output_format", "mp3")
            ready = {
                int(segment["index"]): segment
                for segment in meta.get("segments", [])
                if (folder / str(segment.get("output", ""))).exists()
            }
            segment_outputs: list[Path] = []
            chunk_index = 0
            for segment_index, segment in enumerate(segment_plan):
                segment_dir = folder / "segments"
                segment_output = segment_dir / f"{segment_index:04d}.{output_format}"
                segment_parts: list[Path] = []
                segment_cache_parts: list[Path] = []
                for text in segment["chunks"]:
                    emotion = emotions[chunk_index]
                    self._raise_if_cancelled(job_id, meta)
                    part = folder / "parts" / f"{chunk_index:05d}.wav"
                    if not segment_output.exists() and not part.exists():
                        if self.synth.provider in {"voice-clone", "remote-api"}:
                            meta["stage"] = (
                                f"正在生成可播放分段 {segment_index + 1}/{len(segment_plan)}"
                                f" · API 片段 {chunk_index + 1}/{len(chunks)}"
                            )
                        else:
                            meta["stage"] = (
                                f"正在生成可播放分段 {segment_index + 1}/{len(segment_plan)}"
                                f" · 音频片段 {chunk_index + 1}/{len(chunks)}"
                            )
                        self._save_job(meta)
                        self.synth.synthesize(voice_dir / "reference.wav", text, emotion, part)
                        self._raise_if_cancelled(job_id, meta)
                    if not segment_output.exists():
                        segment_parts.append(part)
                    segment_cache_parts.append(part)
                    manifest.append({
                        "index": chunk_index,
                        "segment_index": segment_index,
                        "text": text,
                        "emotion": emotion,
                        "file": part.name,
                    })
                    chunk_index += 1
                    meta["progress"] = int(chunk_index * 95 / len(chunks))
                    self._save_job(meta)

                if not segment_output.exists():
                    meta["stage"] = f"正在发布可播放分段 {segment_index + 1}/{len(segment_plan)}"
                    self._save_job(meta)
                    if output_format == "mp3":
                        segment_wav = segment_dir / f".{segment_index:04d}.wav"
                        concatenate_wavs(segment_parts, segment_wav, crossfade_ms=40)
                        encode_mp3(segment_wav, segment_output)
                        segment_wav.unlink(missing_ok=True)
                    else:
                        concatenate_wavs(segment_parts, segment_output, crossfade_ms=40)
                entry = {
                    "index": segment_index,
                    "title": segment["title"],
                    "chapter_index": segment["chapter_index"],
                    "chars": segment["chars"],
                    "output": segment_output.relative_to(folder).as_posix(),
                    "output_format": output_format,
                    "output_bytes": segment_output.stat().st_size,
                    "ready": True,
                }
                ready[segment_index] = entry
                segment_outputs.append(segment_output)
                meta["segments"] = [ready[index] for index in sorted(ready)]
                meta["ready_segments"] = len(meta["segments"])
                meta["total_segments"] = len(segment_plan)
                meta["stage"] = (
                    f"已有 {len(meta['segments'])}/{len(segment_plan)} 个分段可播放"
                )
                self._save_job(meta)
                for part in segment_cache_parts:
                    part.unlink(missing_ok=True)

            meta["stage"] = "正在快速合并完整成品"
            self._save_job(meta)
            output = folder / f"audiobook.{output_format}"
            if output_format == "mp3":
                concatenate_mp3s(segment_outputs, output)
            else:
                concatenate_wavs(segment_outputs, output, crossfade_ms=40)
            write_provenance(folder / "provenance.json", {
                "generated_at": now_iso(), "synthetic_audio": True, "consent_confirmed": True,
                "engine": self.synth.name, "voice_id": meta["voice_id"], "book_id": meta["book_id"],
                "output_format": output_format,
                "continuity": {"emotion_smoothing": "15/70/15", "crossfade_ms": 40},
                "progressive_segments": meta["segments"], "chunks": manifest,
            })
            meta.update(
                status="completed", stage="可以在线播放", progress=100,
                completed_at=now_iso(), output=output.name,
                output_format=output_format, output_bytes=output.stat().st_size,
                render_version=3,
            )
            try:
                shutil.rmtree(folder / "parts")
            except OSError:
                # The finished file is valid even if antivirus/indexing briefly
                # keeps a cache handle open. A later delete still removes it.
                pass
        except JobCancelled:
            meta.update(status="cancelled", stage="已取消", completed_at=now_iso(), error=None)
        except Exception as exc:
            meta.update(status="failed", stage="生成失败", error=str(exc), completed_at=now_iso())
        self._save_job(meta)

    def shutdown(self) -> None:
        self.executor.shutdown(wait=True, cancel_futures=False)
