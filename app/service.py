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


class StudioService:
    def __init__(self, store: Store, synth: Synthesizer, max_upload_mb: int = 200, chunk_chars: int = 110):
        self.store = store
        self.synth = synth
        self.max_upload_bytes = max_upload_mb * 1024 * 1024
        self.chunk_chars = chunk_chars
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="nvs-render")

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

    def add_voice(self, source: BinaryIO, filename: str, name: str, consent: bool) -> dict:
        if not consent:
            raise ValueError("必须确认已取得声音本人明确授权")
        item_id = self.store.new_id()
        folder = self.store.directory("voices", item_id)
        raw = folder / ("source" + Path(filename or "voice.wav").suffix.lower())
        self._copy_limited(source, raw)
        info = normalize_reference(raw, folder / "reference.wav")
        meta = {"id": item_id, "name": name.strip() or "我的声音", "filename": filename, "consent": True, "created_at": now_iso(), **info}
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

    def create_job(self, voice_id: str, book_id: str, chapter_start: int, chapter_end: int | None, emotion_strength: float) -> dict:
        voice = self.store.load_meta("voices", voice_id)
        book = self.store.load_meta("books", book_id)
        count = int(book["chapter_count"])
        end = count - 1 if chapter_end is None else chapter_end
        if chapter_start < 0 or end < chapter_start or end >= count:
            raise ValueError("章节范围无效")
        job_id = self.store.new_id()
        meta = {
            "id": job_id, "voice_id": voice["id"], "book_id": book["id"], "chapter_start": chapter_start,
            "chapter_end": end, "emotion_strength": max(0.0, min(1.0, emotion_strength)),
            "engine": self.synth.name, "status": "queued", "progress": 0, "created_at": now_iso(), "error": None,
        }
        self.store.save_meta("jobs", job_id, meta)
        self.executor.submit(self._render, job_id)
        return meta

    def _render(self, job_id: str) -> None:
        meta = self.store.load_meta("jobs", job_id)
        folder = self.store.directory("jobs", job_id)
        try:
            meta.update(status="running", started_at=now_iso())
            self.store.save_meta("jobs", job_id, meta)
            book_dir = self.store.directory("books", meta["book_id"])
            voice_dir = self.store.directory("voices", meta["voice_id"])
            chapters = split_chapters((book_dir / "book.txt").read_text(encoding="utf-8"))
            selected = chapters[meta["chapter_start"] : meta["chapter_end"] + 1]
            chunks = [chunk for chapter in selected for chunk in chunk_text(str(chapter["text"]), self.chunk_chars)]
            if not chunks:
                raise ValueError("所选章节没有可朗读内容")
            parts: list[Path] = []
            manifest = []
            for index, text in enumerate(chunks):
                part = folder / "parts" / f"{index:05d}.wav"
                emotion = plan_emotion(text, meta["emotion_strength"])
                if not part.exists():
                    self.synth.synthesize(voice_dir / "reference.wav", text, emotion, part)
                parts.append(part)
                manifest.append({"index": index, "text": text, "emotion": emotion, "file": part.name})
                meta["progress"] = int((index + 1) * 95 / len(chunks))
                self.store.save_meta("jobs", job_id, meta)
            output = folder / "audiobook.wav"
            concatenate_wavs(parts, output)
            write_provenance(folder / "provenance.json", {
                "generated_at": now_iso(), "synthetic_audio": True, "consent_confirmed": True,
                "engine": self.synth.name, "voice_id": meta["voice_id"], "book_id": meta["book_id"], "segments": manifest,
            })
            meta.update(status="completed", progress=100, completed_at=now_iso(), output="audiobook.wav")
        except Exception as exc:
            meta.update(status="failed", error=str(exc), completed_at=now_iso())
        self.store.save_meta("jobs", job_id, meta)

    def shutdown(self) -> None:
        self.executor.shutdown(wait=True, cancel_futures=False)
