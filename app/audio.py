from __future__ import annotations

import json
import shutil
import subprocess
import wave
from pathlib import Path


def ffmpeg_executable() -> str:
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:
        raise RuntimeError("未找到 FFmpeg，请安装依赖或将 ffmpeg 加入 PATH") from exc


def normalize_reference(source: Path, target: Path) -> dict[str, float | int]:
    target.parent.mkdir(parents=True, exist_ok=True)
    # A canonical WAV needs no external transcoder. Besides being faster, this
    # keeps the core pipeline testable on offline machines; other containers go
    # through the bundled FFmpeg dependency below.
    if source.suffix.lower() == ".wav":
        try:
            with wave.open(str(source), "rb") as stream:
                duration = stream.getnframes() / stream.getframerate()
                canonical = stream.getnchannels() == 1 and stream.getsampwidth() == 2 and stream.getframerate() == 24000
            if canonical:
                shutil.copyfile(source, target)
                if duration < 3:
                    raise ValueError("参考语音太短，请提供至少 3 秒、建议 10–30 秒的清晰语音")
                if duration > 120:
                    raise ValueError("参考语音最长 120 秒")
                return {"duration_seconds": round(duration, 2), "sample_rate": 24000, "channels": 1}
        except (wave.Error, EOFError):
            pass
    cmd = [
        ffmpeg_executable(), "-y", "-i", str(source), "-ac", "1", "-ar", "24000",
        "-af", "highpass=f=60,lowpass=f=11000,loudnorm=I=-20:TP=-2:LRA=7", str(target),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode:
        raise ValueError("音频无法解析；微信 SILK 请先导出/转换为 WAV、MP3、M4A 或 OGG")
    with wave.open(str(target), "rb") as stream:
        duration = stream.getnframes() / stream.getframerate()
        info = {"duration_seconds": round(duration, 2), "sample_rate": stream.getframerate(), "channels": stream.getnchannels()}
    if duration < 3:
        raise ValueError("参考语音太短，请提供至少 3 秒、建议 10–30 秒的清晰语音")
    if duration > 120:
        raise ValueError("参考语音最长 120 秒")
    return info


def concatenate_wavs(parts: list[Path], output: Path, pause_ms: int = 180) -> None:
    if not parts:
        raise ValueError("没有可拼接的音频片段")
    output.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(parts[0]), "rb") as first:
        params = first.getparams()
    silence = b"\x00" * int(params.framerate * pause_ms / 1000) * params.nchannels * params.sampwidth
    with wave.open(str(output), "wb") as writer:
        writer.setparams(params)
        for index, path in enumerate(parts):
            with wave.open(str(path), "rb") as reader:
                if (reader.getnchannels(), reader.getsampwidth(), reader.getframerate()) != (params.nchannels, params.sampwidth, params.framerate):
                    raise ValueError(f"音频片段格式不一致: {path.name}")
                writer.writeframes(reader.readframes(reader.getnframes()))
            if index + 1 < len(parts):
                writer.writeframes(silence)


def write_provenance(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
