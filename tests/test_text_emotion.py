from app.emotion import EMOTIONS, plan_emotion, plan_emotion_sequence
import zipfile

from app.text import chunk_text, extract_book, plan_progressive_segments, split_chapters


def test_chapters_and_chunks_preserve_order():
    text = "第1章 开始\n风吹过长街。她笑了！\n第2章 夜雨\n他独自回忆往事，叹了一口气。"
    chapters = split_chapters(text)
    assert [c["title"] for c in chapters] == ["第1章 开始", "第2章 夜雨"]
    chunks = chunk_text(str(chapters[0]["text"]), 8)
    assert chunks == ["风吹过长街。", "她笑了！"]


def test_emotion_vector_is_normalized_and_semantic():
    vector = plan_emotion("哈哈，我终于成功了！", 0.7)
    assert len(vector) == 8
    assert abs(sum(vector) - 1) < 0.001
    assert vector[EMOTIONS.index("happy")] > vector[EMOTIONS.index("sad")]


def test_emotion_sequence_reduces_abrupt_segment_changes():
    texts = ["!!", "??", "calm narration."]
    raw = [plan_emotion(text, 0.9) for text in texts]
    smoothed = plan_emotion_sequence(texts, 0.9)
    raw_jump = sum(abs(left - right) for left, right in zip(raw[0], raw[1]))
    smooth_jump = sum(abs(left - right) for left, right in zip(smoothed[0], smoothed[1]))
    assert smooth_jump < raw_jump
    assert all(abs(sum(vector) - 1) < 0.001 for vector in smoothed)


def test_progressive_segments_keep_chapter_order_and_split_long_chapters():
    chapters = [
        {"index": 0, "title": "第一章", "text": "甲" * 12},
        {"index": 1, "title": "第二章", "text": "乙" * 4},
    ]
    segments = plan_progressive_segments(chapters, chunk_chars=4, segment_chars=8)
    assert [segment["chapter_index"] for segment in segments] == [0, 0, 1]
    assert segments[0]["title"] == "第一章 · 1/2"
    assert "".join(
        chunk for segment in segments for chunk in segment["chunks"]
    ) == "甲" * 12 + "乙" * 4


def test_half_million_character_book_has_bounded_progressive_plan():
    text = "甲" * 500_000
    segments = plan_progressive_segments(
        [{"index": 0, "title": "长篇", "text": text}],
        chunk_chars=120,
        segment_chars=2000,
    )
    assert 240 <= len(segments) <= 270
    assert max(segment["chars"] for segment in segments) <= 2040
    assert sum(segment["chars"] for segment in segments) == len(text)


def test_epub_uses_declared_spine_order(tmp_path):
    book = tmp_path / "order.epub"
    container = '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OPS/book.opf"/></rootfiles></container>'
    opf = '<package xmlns="http://www.idpf.org/2007/opf"><manifest><item id="a" href="a.xhtml"/><item id="b" href="b.xhtml"/></manifest><spine><itemref idref="b"/><itemref idref="a"/></spine></package>'
    with zipfile.ZipFile(book, "w") as archive:
        archive.writestr("META-INF/container.xml", container)
        archive.writestr("OPS/book.opf", opf)
        archive.writestr("OPS/a.xhtml", "<html><body><p>后读</p></body></html>")
        archive.writestr("OPS/b.xhtml", "<html><body><p>先读</p></body></html>")
    assert extract_book(book) == "先读\n后读"
