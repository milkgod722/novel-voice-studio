import math
import pytest
from subprocess import CompletedProcess
import struct
import wave
from pathlib import Path

from app import audio
from app.audio import concatenate_mp3s, concatenate_wavs, encode_mp3


def write_tone(path: Path, frequency: int, seconds: float = 0.25, rate: int = 24000):
    frames = b"".join(
        struct.pack("<h", int(5000 * math.sin(2 * math.pi * frequency * index / rate)))
        for index in range(int(seconds * rate))
    )
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(frames)


def test_wav_segments_use_short_crossfade_without_inserted_silence(tmp_path):
    first = tmp_path / "first.wav"
    second = tmp_path / "second.wav"
    output = tmp_path / "joined.wav"
    write_tone(first, 220)
    write_tone(second, 330)
    concatenate_wavs([first, second], output, crossfade_ms=40)
    with wave.open(str(output), "rb") as reader:
        duration = reader.getnframes() / reader.getframerate()
    assert 0.45 < duration < 0.48


def test_mp3_encoding_uses_small_browser_compatible_profile(tmp_path, monkeypatch):
    source = tmp_path / "source.wav"
    output = tmp_path / "output.mp3"
    write_tone(source, 220, seconds=2)

    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        output.write_bytes(b"ID3" + b"\0" * 2048)
        return CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(audio, "ffmpeg_executable", lambda: "ffmpeg")
    monkeypatch.setattr(audio.subprocess, "run", fake_run)
    encode_mp3(source, output)
    assert output.read_bytes()[:3] == b"ID3"
    assert ["-ac", "1", "-ar", "24000"] == captured["command"][5:9]
    assert captured["command"][-3:-1] == ["-b:a", "64k"]


def test_real_mp3_encoding_is_smaller_than_pcm_when_ffmpeg_is_available(tmp_path):
    try:
        audio.ffmpeg_executable()
    except RuntimeError:
        pytest.skip("FFmpeg is installed with the project runtime, not this test interpreter")
    source = tmp_path / "source.wav"
    output = tmp_path / "output.mp3"
    write_tone(source, 220, seconds=2)
    encode_mp3(source, output)
    assert output.stat().st_size < source.stat().st_size / 4


def test_real_mp3_segments_can_be_fast_concatenated(tmp_path):
    try:
        audio.ffmpeg_executable()
    except RuntimeError:
        pytest.skip("FFmpeg is installed with the project runtime, not this test interpreter")
    first_wav = tmp_path / "first.wav"
    second_wav = tmp_path / "second.wav"
    first_mp3 = tmp_path / "first.mp3"
    second_mp3 = tmp_path / "second.mp3"
    output = tmp_path / "joined.mp3"
    write_tone(first_wav, 220, seconds=1)
    write_tone(second_wav, 330, seconds=1)
    encode_mp3(first_wav, first_mp3)
    encode_mp3(second_wav, second_mp3)
    concatenate_mp3s([first_mp3, second_mp3], output)
    assert output.stat().st_size > first_mp3.stat().st_size
    assert not output.with_suffix(".concat.txt").exists()
