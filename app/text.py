from __future__ import annotations

import html
import posixpath
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from charset_normalizer import from_bytes

SUPPORTED_BOOKS = {".txt", ".md", ".epub"}
_CHAPTER_RE = re.compile(
    r"^\s*(?:第[零〇一二三四五六七八九十百千万两\d]+[章节回卷部篇]|chapter\s+\d+)\s*[^\n]{0,50}$",
    re.IGNORECASE,
)
_SENTENCE_RE = re.compile(r"(?<=[。！？!?；;…])|(?<=\.{3})")


def decode_text(raw: bytes) -> str:
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig")
    best = from_bytes(raw).best()
    if best is None:
        raise ValueError("无法识别文本编码，请转换为 UTF-8 后重试")
    return str(best)


def extract_book(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_BOOKS:
        raise ValueError("仅支持 TXT、Markdown 和 EPUB 小说")
    if suffix != ".epub":
        return clean_text(decode_text(path.read_bytes()))
    return clean_text(_extract_epub(path))


def _extract_epub(path: Path) -> str:
    pieces: list[str] = []
    with zipfile.ZipFile(path) as archive:
        names = _epub_spine_names(archive)
        if not names:
            names = [n for n in archive.namelist() if n.lower().endswith((".xhtml", ".html", ".htm"))]
        for name in names:
            raw = archive.read(name)
            try:
                root = ElementTree.fromstring(raw)
                text = "\n".join("".join(node.itertext()) for node in root.iter() if node.tag.split("}")[-1] in {"h1", "h2", "h3", "p"})
            except ElementTree.ParseError:
                text = re.sub(r"<[^>]+>", " ", decode_text(raw))
            if text.strip():
                pieces.append(html.unescape(text))
    if not pieces:
        raise ValueError("EPUB 中没有找到可朗读的正文")
    return "\n".join(pieces)


def _epub_spine_names(archive: zipfile.ZipFile) -> list[str]:
    """Return EPUB documents in declared reading order, or an empty fallback."""
    try:
        container = ElementTree.fromstring(archive.read("META-INF/container.xml"))
        rootfile = next(node for node in container.iter() if node.tag.split("}")[-1] == "rootfile")
        opf_name = rootfile.attrib["full-path"]
        package = ElementTree.fromstring(archive.read(opf_name))
        manifest = {
            node.attrib["id"]: node.attrib["href"]
            for node in package.iter()
            if node.tag.split("}")[-1] == "item" and "id" in node.attrib and "href" in node.attrib
        }
        base = posixpath.dirname(opf_name)
        return [
            posixpath.normpath(posixpath.join(base, manifest[node.attrib["idref"]])).lstrip("/")
            for node in package.iter()
            if node.tag.split("}")[-1] == "itemref" and node.attrib.get("idref") in manifest
        ]
    except (KeyError, StopIteration, ElementTree.ParseError):
        return []


def clean_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\u3000", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    return text.strip()


def split_chapters(text: str) -> list[dict[str, str | int]]:
    lines = text.splitlines()
    chapters: list[dict[str, str | int]] = []
    current_title = "正文"
    current: list[str] = []
    for line in lines:
        if _CHAPTER_RE.match(line.strip()):
            if "\n".join(current).strip():
                body = "\n".join(current).strip()
                chapters.append({"index": len(chapters), "title": current_title, "text": body})
            current_title = line.strip()
            current = []
        else:
            current.append(line)
    body = "\n".join(current).strip()
    if body or not chapters:
        chapters.append({"index": len(chapters), "title": current_title, "text": body})
    return chapters


def chunk_text(text: str, max_chars: int = 110) -> list[str]:
    """Split on natural boundaries, while never losing or reordering content."""
    text = clean_text(text)
    if not text:
        return []
    text = re.sub(r"(?<![。！？!?；;…])\n+", "。", text)
    text = text.replace("\n", "")
    sentences = [p.strip() for p in _SENTENCE_RE.split(text) if p.strip()]
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        while len(sentence) > max_chars:
            split_at = max(sentence.rfind(mark, 0, max_chars + 1) for mark in "，、：,: ")
            split_at = split_at + 1 if split_at >= max_chars // 2 else max_chars
            head, sentence = sentence[:split_at].strip(), sentence[split_at:].strip()
            if current:
                chunks.append(current)
                current = ""
            if head:
                chunks.append(head)
        if not sentence:
            continue
        if current and len(current) + len(sentence) > max_chars:
            chunks.append(current)
            current = sentence
        else:
            current += sentence
    if current:
        chunks.append(current)
    return chunks
