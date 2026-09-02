from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Callable


class VisionAnalyzerError(RuntimeError):
    pass


def run_vision_analyzer(
    executable: list[str], source: str | Path, output_path: str | Path, duration_sec: float, *,
    start_sec: float, end_sec: float, interval_sec: float, runner: Callable = subprocess.run,
) -> dict:
    """Run an external vision model using the source-time observation contract."""
    if not executable or not all(isinstance(value, str) and value for value in executable):
        raise VisionAnalyzerError("비전 분석 실행 명령은 비어 있지 않은 인자 배열이어야 합니다.")
    if start_sec < 0 or end_sec <= start_sec or end_sec > duration_sec or interval_sec <= 0:
        raise VisionAnalyzerError("비전 분석 시간 범위와 샘플 간격이 올바르지 않습니다.")
    output = Path(output_path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [
        *executable, "--input", str(Path(source).expanduser().resolve()), "--output", str(output),
        "--start-sec", str(start_sec), "--end-sec", str(end_sec), "--interval-sec", str(interval_sec),
    ]
    result = runner(command, capture_output=True, text=True, check=False)
    if result.returncode:
        raise VisionAnalyzerError(result.stderr[-4000:] or "외부 비전 분석에 실패했습니다.")
    if not output.is_file():
        raise VisionAnalyzerError(f"외부 비전 분석 결과가 생성되지 않았습니다: {output}")
    try:
        payload = json.loads(output.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise VisionAnalyzerError(f"외부 비전 분석 JSON을 읽을 수 없습니다: {error}") from error
    raw = payload.get("observations") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        raise VisionAnalyzerError("외부 비전 결과 observations는 배열이어야 합니다.")
    observations = [_normalize_observation(item, start_sec, end_sec) for item in raw]
    observations.sort(key=lambda item: (item["start_sec"], item["end_sec"], item["kind"]))
    return {"observations": observations, "command": command, "output_path": str(output)}


def _normalize_observation(item: dict, range_start: float, range_end: float) -> dict:
    if not isinstance(item, dict):
        raise VisionAnalyzerError("비전 관찰값은 객체여야 합니다.")
    start, end = float(item["start_sec"]), float(item["end_sec"])
    confidence = item.get("confidence")
    if start < range_start or end <= start or end > range_end:
        raise VisionAnalyzerError("비전 관찰값이 요청한 원본 시간 범위를 벗어났습니다.")
    if confidence is not None and not 0 <= float(confidence) <= 1:
        raise VisionAnalyzerError("비전 관찰 신뢰도는 0과 1 사이여야 합니다.")
    kind = str(item.get("kind", "")).strip()
    if not kind:
        raise VisionAnalyzerError("비전 관찰 kind가 필요합니다.")
    payload = item.get("payload", {})
    if not isinstance(payload, dict):
        raise VisionAnalyzerError("비전 관찰 payload는 객체여야 합니다.")
    return {
        "modality": "VISION", "kind": kind, "track_index": None,
        "start_sec": start, "end_sec": end,
        "confidence": None if confidence is None else float(confidence), "payload": payload,
    }
