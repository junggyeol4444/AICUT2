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
    ranges: list[dict] | None = None,
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
            for selected in ranges or [{"start_sec": 0, "end_sec": duration_sec, "reason": "full_coverage"}]:
                range_start = max(0.0, float(selected["start_sec"]))
                range_end = min(duration_sec, float(selected["end_sec"]))
                stream.setpos(min(stream.getnframes(), round(range_start * rate)))
                start = range_start
                while start < range_end:
                    raw = stream.readframes(min(round(rate * (range_end - start)), max(1, round(rate * window_sec))))
                    if not raw:
                        break
                    mono = _downmix(_pcm_samples(raw, width), channels)
                    if not mono:
                        break
                    end = min(range_end, start + len(mono) / rate)
                    observations.append(_audio_observation(mono, width, track_index, start, end, selected.get("reason")))
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


def _downmix(samples: array, channels: int) -> list[float]:
    return [sum(samples[index:index + channels]) / channels for index in range(0, len(samples) - channels + 1, channels)]


def _audio_observation(mono: list[float], width: int, track: int, start: float, end: float, reason: str | None) -> dict:
    peak = max(abs(sample) for sample in mono)
    rms = math.sqrt(sum(sample * sample for sample in mono) / len(mono))
    crossings = sum(1 for left, right in zip(mono, mono[1:]) if (left < 0) != (right < 0))
    return {
        "modality": "AUDIO", "kind": "SIGNAL_WINDOW", "track_index": track,
        "start_sec": start, "end_sec": end, "confidence": None,
        "payload": {"rms_dbfs": _dbfs(rms, float((1 << (width * 8 - 1)) - 1)),
                    "peak_dbfs": _dbfs(peak, float((1 << (width * 8 - 1)) - 1)),
                    "zero_crossing_rate": crossings / max(1, len(mono) - 1), "sample_count": len(mono),
                    "selection_reason": reason},
    }


_META = re.compile(
    r"(?:frame:\d+\s+)?pts:-?\d+\s+pts_time:(?P<time>-?[\d.]+)"
    r"|lavfi\.(?P<key>[\w.]+)=(?P<value>[^\s]+)"
)


def analyze_video(
    source: str | Path, duration_sec: float, *, frame_interval_sec: float = 5.0,
    runner: Callable = subprocess.run, start_sec: float = 0.0, end_sec: float | None = None,
) -> dict:
    """Sample the complete source with FFmpeg and emit timestamped visual signals."""
    if frame_interval_sec <= 0:
        raise ValueError("frame_interval_sec must be positive")
    if start_sec < 0 or (end_sec is not None and end_sec <= start_sec):
        raise ValueError("video precision range must be positive")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise AnalysisError("ffmpeg가 설치되어 있지 않습니다.")
    end_sec = duration_sec if end_sec is None else min(duration_sec, end_sec)
    command = [
        ffmpeg, "-hide_banner", "-nostdin", "-ss", str(start_sec), "-t", str(end_sec - start_sec),
        "-i", str(Path(source).expanduser().resolve()),
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
            start = start_sec + float(match.group("time"))
            if start < 0 or start >= duration_sec:
                current = None
                continue
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


def analyze_precision_ranges(
    source: str | Path, audio_paths: list[str | Path], duration_sec: float, ranges: list[dict], *,
    audio_window_sec: float, vision_interval_sec: float,
) -> dict:
    """Actually execute denser audio and vision passes only inside selected source ranges."""
    audio = analyze_audio_tracks(audio_paths, duration_sec, window_sec=audio_window_sec, ranges=ranges) if audio_paths else {"observations": []}
    vision = []
    for selected in ranges:
        result = analyze_video(source, duration_sec, frame_interval_sec=vision_interval_sec,
                               start_sec=float(selected["start_sec"]), end_sec=float(selected["end_sec"]))
        for item in result["observations"]:
            item["payload"]["selection_reason"] = selected.get("reason")
            vision.append(item)
    observations = audio["observations"] + vision
    for item in observations:
        item["kind"] = f"PRECISION_{item['kind']}"
    return {"observations": observations, "ranges": ranges}
