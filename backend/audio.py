from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Callable


class AudioAnalyzerError(RuntimeError):
    pass


def run_audio_analyzer(
    executable: list[str], audio_paths: list[str | Path], output_path: str | Path, duration_sec: float, *,
    start_sec: float, end_sec: float, window_sec: float, runner: Callable = subprocess.run,
) -> dict:
    """Run a track-aware external audio event model and normalize its observations."""
    if not executable or not all(isinstance(value, str) and value for value in executable):
        raise AudioAnalyzerError("오디오 분석 실행 명령은 비어 있지 않은 인자 배열이어야 합니다.")
    if not audio_paths:
        raise AudioAnalyzerError("오디오 분석 입력 트랙이 필요합니다.")
    resolved_paths = [Path(path).expanduser().resolve() for path in audio_paths]
    missing = [str(path) for path in resolved_paths if not path.is_file()]
    if missing:
        raise AudioAnalyzerError(f"오디오 분석 입력 트랙을 찾을 수 없습니다: {missing}")
    if start_sec < 0 or end_sec <= start_sec or end_sec > duration_sec or window_sec <= 0:
        raise AudioAnalyzerError("오디오 분석 시간 범위와 창 길이가 올바르지 않습니다.")
    output = Path(output_path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [*executable, "--output", str(output), "--start-sec", str(start_sec),
               "--end-sec", str(end_sec), "--window-sec", str(window_sec)]
    for index, path in enumerate(resolved_paths):
        command.extend(["--audio", f"{index}:{path}"])
    result = runner(command, capture_output=True, text=True, check=False)
    if result.returncode:
        raise AudioAnalyzerError(result.stderr[-4000:] or "외부 오디오 사건 분석에 실패했습니다.")
    if not output.is_file():
        raise AudioAnalyzerError(f"외부 오디오 분석 결과가 생성되지 않았습니다: {output}")
    try:
        payload = json.loads(output.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise AudioAnalyzerError(f"외부 오디오 분석 JSON을 읽을 수 없습니다: {error}") from error
    raw = payload.get("observations") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        raise AudioAnalyzerError("외부 오디오 결과 observations는 배열이어야 합니다.")
    observations = [_normalize(item, len(audio_paths), start_sec, end_sec) for item in raw]
    observations.sort(key=lambda item: (item["start_sec"], item["track_index"], item["kind"]))
    return {"observations": observations, "command": command, "output_path": str(output)}


def _normalize(item: dict, track_count: int, range_start: float, range_end: float) -> dict:
    if not isinstance(item, dict):
        raise AudioAnalyzerError("오디오 관찰값은 객체여야 합니다.")
    start, end = float(item["start_sec"]), float(item["end_sec"])
    track = int(item.get("track_index", -1))
    confidence = item.get("confidence")
    if start < range_start or end <= start or end > range_end:
        raise AudioAnalyzerError("오디오 관찰값이 요청한 원본 시간 범위를 벗어났습니다.")
    if not 0 <= track < track_count:
        raise AudioAnalyzerError("오디오 관찰값이 존재하지 않는 트랙을 참조합니다.")
    if confidence is not None and not 0 <= float(confidence) <= 1:
        raise AudioAnalyzerError("오디오 관찰 신뢰도는 0과 1 사이여야 합니다.")
    kind, payload = str(item.get("kind", "")).strip(), item.get("payload", {})
    if not kind or not isinstance(payload, dict):
        raise AudioAnalyzerError("오디오 관찰 kind와 payload가 올바르지 않습니다.")
    return {"modality": "AUDIO", "kind": kind, "track_index": track, "start_sec": start, "end_sec": end,
            "confidence": None if confidence is None else float(confidence), "payload": payload}
