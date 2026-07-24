from __future__ import annotations

from array import array
import json
import shutil
import subprocess
import sys
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


def concatenate_wavs(parts: list[Path], output: Path, crossfade_ms: int = 40) -> None:
    if not parts:
        raise ValueError("没有可拼接的音频片段")
    output.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(parts[0]), "rb") as first:
        params = first.getparams()
    if params.sampwidth != 2:
        raise ValueError("音频拼接仅支持 16-bit PCM WAV")
    overlap_frames = max(0, int(params.framerate * crossfade_ms / 1000))
    expected = (params.nchannels, params.sampwidth, params.framerate)

    def pcm_bytes(samples: array) -> bytes:
        if sys.byteorder == "little":
            return samples.tobytes()
        encoded = array("h", samples)
        encoded.byteswap()
        return encoded.tobytes()

    with wave.open(str(output), "wb") as writer:
        writer.setparams(params)
        pending: array | None = None
        for path in parts:
            with wave.open(str(path), "rb") as reader:
                actual = (reader.getnchannels(), reader.getsampwidth(), reader.getframerate())
                if actual != expected:
                    raise ValueError(f"音频片段格式不一致: {path.name}")
                samples = array("h")
                samples.frombytes(reader.readframes(reader.getnframes()))
                if sys.byteorder != "little":
                    samples.byteswap()
            if pending is None:
                pending = samples
                continue
            frames = min(
                overlap_frames,
                len(pending) // params.nchannels,
                len(samples) // params.nchannels,
            )
            overlap_samples = frames * params.nchannels
            if not overlap_samples:
                writer.writeframes(pcm_bytes(pending))
                pending = samples
                continue
            writer.writeframes(pcm_bytes(pending[:-overlap_samples]))
            blended = array("h", pending[-overlap_samples:])
            for frame in range(frames):
                alpha = (frame + 1) / (frames + 1)
                for channel in range(params.nchannels):
                    offset = frame * params.nchannels + channel
                    blended[offset] = round(
                        blended[offset] * (1.0 - alpha) + samples[offset] * alpha
                    )
            writer.writeframes(pcm_bytes(blended))
            pending = samples[overlap_samples:]
        if pending is not None:
            writer.writeframes(pcm_bytes(pending))


def encode_mp3(source: Path, output: Path, bitrate: str = "64k") -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            ffmpeg_executable(), "-y", "-i", str(source), "-vn",
            "-ac", "1", "-ar", "24000", "-codec:a", "libmp3lame",
            "-b:a", bitrate, str(output),
        ],
        capture_output=True,
        text=True,
        timeout=3600,
    )
    if result.returncode or not output.exists() or output.stat().st_size == 0:
        output.unlink(missing_ok=True)
        detail = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "未知错误"
        raise RuntimeError(f"MP3 编码失败: {detail}")


def audio_media_type(output_format: str) -> str:
    return "audio/mpeg" if output_format == "mp3" else "audio/wav"


def write_provenance(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
