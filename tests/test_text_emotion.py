from app.emotion import EMOTIONS, plan_emotion, plan_emotion_sequence
import zipfile

from app.text import chunk_text, extract_book, split_chapters


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
