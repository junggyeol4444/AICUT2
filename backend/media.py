from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Callable


class MediaProbeError(RuntimeError):
    pass


@dataclass(frozen=True)
class MediaInfo:
    path: str
    duration_sec: float
    size_bytes: int
    format_name: str
    width: int
    height: int
    frame_rate: float
    video_codec: str
    audio_tracks: int
    audio_codecs: tuple[str, ...]

    def to_dict(self) -> dict:
        value = asdict(self)
        value["audio_codecs"] = list(self.audio_codecs)
        return value


def _rate(value: str | None) -> float:
    if not value or value == "0/0":
        return 0
    numerator, denominator = value.split("/", 1)
    return float(numerator) / float(denominator)


def probe_media(path: str | Path, runner: Callable = subprocess.run) -> MediaInfo:
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise MediaProbeError(f"미디어 파일을 찾을 수 없습니다: {source}")
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise MediaProbeError("ffprobe가 설치되어 있지 않습니다.")
    command = [ffprobe, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(source)]
    result = runner(command, capture_output=True, text=True, timeout=120, check=False)
    if result.returncode:
        raise MediaProbeError(result.stderr.strip() or "ffprobe 분석에 실패했습니다.")
    try:
        payload = json.loads(result.stdout)
        video = next(stream for stream in payload["streams"] if stream.get("codec_type") == "video")
        audio = [stream for stream in payload["streams"] if stream.get("codec_type") == "audio"]
        return MediaInfo(
            path=str(source), duration_sec=float(payload["format"].get("duration", 0)),
            size_bytes=int(payload["format"].get("size", source.stat().st_size)),
            format_name=payload["format"].get("format_name", "unknown"),
            width=int(video.get("width", 0)), height=int(video.get("height", 0)),
            frame_rate=_rate(video.get("avg_frame_rate")), video_codec=video.get("codec_name", "unknown"),
            audio_tracks=len(audio), audio_codecs=tuple(stream.get("codec_name", "unknown") for stream in audio),
        )
    except (KeyError, StopIteration, TypeError, ValueError, json.JSONDecodeError) as error:
        raise MediaProbeError(f"ffprobe 결과가 올바르지 않습니다: {error}") from error
