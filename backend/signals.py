from __future__ import annotations

import math
import wave
from array import array
from dataclasses import dataclass, asdict
from pathlib import Path


class SignalError(ValueError):
    pass


@dataclass(frozen=True)
class AudioFeature:
    track_index: int
    start_sec: float
    end_sec: float
    rms_dbfs: float
    peak_dbfs: float
    zero_crossing_rate: float


def _dbfs(amplitude: float, maximum: float) -> float:
    if amplitude <= 0:
        return float("-inf")
    return 20 * math.log10(amplitude / maximum)


def extract_pcm_features(path: str | Path, track_index: int, window_sec: float) -> list[dict]:
    if window_sec <= 0:
        raise SignalError("오디오 분석 창 길이는 양수여야 합니다.")
    source = Path(path)
    if not source.is_file():
        raise SignalError(f"오디오 파일을 찾을 수 없습니다: {source}")
    features = []
    with wave.open(str(source), "rb") as stream:
        if stream.getsampwidth() != 2:
            raise SignalError("현재 오디오 분석기는 16-bit PCM WAV를 요구합니다.")
        frame_rate, channels = stream.getframerate(), stream.getnchannels()
        frames_per_window = max(1, round(frame_rate * window_sec))
        cursor = 0
        while True:
            raw = stream.readframes(frames_per_window)
            if not raw:
                break
            samples = array("h")
            samples.frombytes(raw)
            mono = samples[::channels]
            if not mono:
                break
            square_mean = sum(value * value for value in mono) / len(mono)
            rms = math.sqrt(square_mean)
            peak = max(abs(value) for value in mono)
            crossings = sum((left < 0 <= right) or (right < 0 <= left) for left, right in zip(mono, mono[1:]))
            start = cursor / frame_rate
            end = start + len(mono) / frame_rate
            features.append(asdict(AudioFeature(
                track_index, start, end, _dbfs(rms, 32768), _dbfs(peak, 32768),
                crossings / max(1, len(mono) - 1),
            )))
            cursor += len(mono)
    return features


def validate_visual_observations(raw: list[dict], duration_sec: float) -> list[dict]:
    observations = []
    for value in raw:
        second = float(value["second"])
        if not 0 <= second <= duration_sec:
            raise SignalError("화면 관찰 시점이 원본 범위를 벗어났습니다.")
        observations.append({
            "second": second,
            "people": list(value.get("people", [])),
            "situation": value.get("situation"),
            "motion_score": value.get("motion_score"),
            "expression": value.get("expression"),
            "screen_event": value.get("screen_event"),
        })
    return sorted(observations, key=lambda item: item["second"])


def _overlaps(item: dict, start: float, end: float) -> bool:
    item_start = float(item.get("start_sec", item.get("second", 0)))
    item_end = float(item.get("end_sec", item_start))
    return item_start < end and item_end > start


def assemble_analysis_windows(
    scan_windows: list[dict], transcripts: list[dict], audio_features: list[dict],
    visual_observations: list[dict],
) -> list[dict]:
    result = []
    for window in scan_windows:
        start, end = float(window["start_sec"]), float(window["end_sec"])
        result.append({
            "pass_kind": window["pass_kind"], "start_sec": start, "end_sec": end,
            "reason": window.get("reason"),
            "transcript": [item for item in transcripts if _overlaps(item, start, end)],
            "audio": [item for item in audio_features if _overlaps(item, start, end)],
            "visual": [item for item in visual_observations if start <= float(item["second"]) < end],
        })
    return result
