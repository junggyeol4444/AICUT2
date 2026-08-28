from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Callable


class UnderstandingError(ValueError):
    pass


@dataclass(frozen=True)
class PreprocessPlan:
    source_path: str
    output_directory: str
    audio_tracks: int
    frame_interval_sec: float


@dataclass(frozen=True)
class ScanWindow:
    pass_kind: str
    start_sec: float
    end_sec: float
    reason: str


def validate_preprocess(plan: PreprocessPlan) -> None:
    if not plan.source_path:
        raise UnderstandingError("원본 파일 경로가 필요합니다.")
    if plan.audio_tracks < 0:
        raise UnderstandingError("오디오 트랙 수는 음수일 수 없습니다.")
    if plan.frame_interval_sec <= 0:
        raise UnderstandingError("프레임 샘플 간격은 측정된 양수여야 합니다.")


def build_preprocess_commands(plan: PreprocessPlan, ffmpeg: str = "ffmpeg") -> dict:
    validate_preprocess(plan)
    output = Path(plan.output_directory).expanduser().resolve()
    audio = []
    for index in range(plan.audio_tracks):
        audio.append([
            ffmpeg, "-hide_banner", "-y", "-i", plan.source_path, "-map", f"0:a:{index}",
            "-vn", "-acodec", "pcm_s16le", "-ar", "48000", str(output / f"audio-track-{index:02d}.wav"),
        ])
    frames = [
        ffmpeg, "-hide_banner", "-y", "-i", plan.source_path,
        "-vf", f"fps=1/{plan.frame_interval_sec}", "-q:v", "3", str(output / "frames" / "frame-%08d.jpg"),
    ]
    return {"audio": audio, "frames": frames}


def execute_preprocess(plan: PreprocessPlan, runner: Callable = subprocess.run) -> dict:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise UnderstandingError("ffmpeg가 설치되어 있지 않습니다.")
    output = Path(plan.output_directory).expanduser().resolve()
    (output / "frames").mkdir(parents=True, exist_ok=True)
    commands = build_preprocess_commands(plan, ffmpeg)
    artifacts = []
    for kind, command_list in commands.items():
        for command in command_list if kind == "audio" else [command_list]:
            result = runner(command, capture_output=True, text=True, check=False)
            if result.returncode:
                raise UnderstandingError(result.stderr[-4000:] or f"{kind} 전처리에 실패했습니다.")
            artifacts.append({"kind": kind.upper(), "path": command[-1], "command": command})
    return {"artifacts": artifacts, "commands": commands}


def build_scan_plan(
    duration_sec: float,
    coarse_window_sec: float,
    precision_ranges: list[dict] | None = None,
) -> list[ScanWindow]:
    duration, window = float(duration_sec), float(coarse_window_sec)
    if duration <= 0 or window <= 0:
        raise UnderstandingError("방송 길이와 1차 통과 구간 길이는 양수여야 합니다.")
    windows = []
    start = 0.0
    while start < duration:
        windows.append(ScanWindow("COARSE", start, min(start + window, duration), "full_coverage"))
        start += window
    precision = []
    for value in precision_ranges or []:
        begin = max(0.0, float(value["start_sec"]))
        end = min(duration, float(value["end_sec"]))
        if end <= begin:
            raise UnderstandingError("2차 정밀 통과 구간이 올바르지 않습니다.")
        precision.append((begin, end, str(value.get("reason", "coarse_signal"))))
    for begin, end, reason in sorted(precision):
        if windows and windows[-1].pass_kind == "PRECISION" and begin <= windows[-1].end_sec:
            previous = windows[-1]
            windows[-1] = ScanWindow("PRECISION", previous.start_sec, max(previous.end_sec, end), f"{previous.reason},{reason}")
        else:
            windows.append(ScanWindow("PRECISION", begin, end, reason))
    return windows


def validate_transcript_segments(raw_segments: list[dict], duration_sec: float) -> list[dict]:
    result = []
    for value in raw_segments:
        start, end = float(value["start_sec"]), float(value["end_sec"])
        confidence = value.get("confidence")
        if start < 0 or end <= start or end > duration_sec:
            raise UnderstandingError("자막 구간이 원본 시간 범위를 벗어났습니다.")
        if confidence is not None and not 0 <= float(confidence) <= 1:
            raise UnderstandingError("자막 신뢰도는 0과 1 사이여야 합니다.")
        words = value.get("words", [])
        if any(float(word["start_sec"]) < start or float(word["end_sec"]) > end for word in words):
            raise UnderstandingError("단어 타임스탬프가 자막 구간을 벗어났습니다.")
        result.append({
            "track_index": int(value.get("track_index", 0)), "start_sec": start, "end_sec": end,
            "speaker_tag": str(value.get("speaker_tag", "UNKNOWN")), "text": str(value["text"]).strip(),
            "confidence": None if confidence is None else float(confidence), "words": words,
        })
    return result
