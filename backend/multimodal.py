from __future__ import annotations

import math
import re
import shutil
import subprocess
import wave
from array import array
from pathlib import Path
from typing import Callable, Iterable


class AnalysisError(RuntimeError):
    pass


def analyze_audio_tracks(
    paths: Iterable[str | Path], duration_sec: float, *, window_sec: float = 1.0,
) -> dict:
    """Extract calibrated, model-independent PCM features on the source timeline."""
    if window_sec <= 0:
        raise ValueError("audio window_sec must be positive")
    observations = []
    for track_index, value in enumerate(paths):
        path = Path(value).expanduser().resolve()
        if not path.is_file():
            raise AnalysisError(f"오디오 분석 파일을 찾을 수 없습니다: {path}")
        with wave.open(str(path), "rb") as stream:
            channels, width, rate = stream.getnchannels(), stream.getsampwidth(), stream.getframerate()
            if width not in (1, 2, 4):
                raise AnalysisError(f"지원하지 않는 PCM sample width입니다: {width}")
            frames_per_window = max(1, round(rate * window_sec))
            start = 0.0
            while start < duration_sec:
                raw = stream.readframes(frames_per_window)
                if not raw:
                    break
                samples = _pcm_samples(raw, width)
                mono = samples[::channels]
                if not mono:
                    break
                peak = max(abs(sample) for sample in mono)
                rms = math.sqrt(sum(sample * sample for sample in mono) / len(mono))
                full_scale = float((1 << (width * 8 - 1)) - 1)
                crossings = sum(1 for left, right in zip(mono, mono[1:]) if (left < 0) != (right < 0))
                end = min(duration_sec, start + len(mono) / rate)
                observations.append({
                    "modality": "AUDIO", "kind": "SIGNAL_WINDOW", "track_index": track_index,
                    "start_sec": start, "end_sec": end, "confidence": None,
                    "payload": {
                        "rms_dbfs": _dbfs(rms, full_scale), "peak_dbfs": _dbfs(peak, full_scale),
                        "zero_crossing_rate": crossings / max(1, len(mono) - 1), "sample_count": len(mono),
                    },
                })
                start = end
    return {"observations": observations, "window_sec": window_sec}


def _pcm_samples(raw: bytes, width: int) -> array:
    typecode = {1: "B", 2: "h", 4: "i"}[width]
    values = array(typecode)
    values.frombytes(raw)
    if width == 1:
        return array("i", (value - 128 for value in values))
    if __import__("sys").byteorder != "little":
        values.byteswap()
    return values


def _dbfs(value: float, full_scale: float) -> float | None:
    return round(20 * math.log10(value / full_scale), 4) if value else None


_META = re.compile(r"(?:frame:\d+\s+)?pts:\d+\s+pts_time:(?P<time>[\d.]+)|lavfi\.(?P<key>[\w.]+)=(?P<value>[^\s]+)")


def analyze_video(
    source: str | Path, duration_sec: float, *, frame_interval_sec: float = 5.0,
    runner: Callable = subprocess.run,
) -> dict:
    """Sample the complete source with FFmpeg and emit timestamped visual signals."""
    if frame_interval_sec <= 0:
        raise ValueError("frame_interval_sec must be positive")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise AnalysisError("ffmpeg가 설치되어 있지 않습니다.")
    command = [
        ffmpeg, "-hide_banner", "-nostdin", "-i", str(Path(source).expanduser().resolve()),
        "-vf", f"fps=1/{frame_interval_sec},scale=320:-2,signalstats,scdet,metadata=print:file=-",
        "-an", "-f", "null", "-",
    ]
    result = runner(command, capture_output=True, text=True, timeout=None, check=False)
    if result.returncode:
        raise AnalysisError(result.stderr.strip() or "비전 특징 분석에 실패했습니다.")
    observations, current = [], None
    for line in result.stdout.splitlines():
        match = _META.search(line)
        if not match:
            continue
        if match.group("time") is not None:
            if current:
                observations.append(current)
            start = float(match.group("time"))
            current = {
                "modality": "VISION", "kind": "FRAME_SIGNAL", "track_index": None,
                "start_sec": start, "end_sec": min(duration_sec, start + frame_interval_sec),
                "confidence": None, "payload": {},
            }
        elif current:
            try:
                current["payload"][match.group("key")] = float(match.group("value"))
            except ValueError:
                current["payload"][match.group("key")] = match.group("value")
    if current:
        observations.append(current)
    return {"observations": observations, "frame_interval_sec": frame_interval_sec, "command": command}
