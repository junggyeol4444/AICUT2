from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Callable


class IntelligenceError(RuntimeError):
    pass


PATTERN_FIELDS = (
    "structure", "editing", "storytelling", "scene_selection", "pacing",
    "subtitles", "title_thumbnail_relationship", "production_logic",
)


def validate_reference_analysis(value: dict) -> dict:
    """Validate derived knowledge only; reference media is deliberately not persisted."""
    if not isinstance(value, dict):
        raise IntelligenceError("레퍼런스 분석 결과는 JSON 객체여야 합니다.")
    video_id = str(value.get("video_id", "")).strip()
    title = str(value.get("title", "")).strip()
    patterns = value.get("patterns")
    if not video_id or not title or not isinstance(patterns, dict):
        raise IntelligenceError("video_id, title, patterns가 필요합니다.")
    missing = [field for field in PATTERN_FIELDS if field not in patterns]
    if missing:
        raise IntelligenceError("제작 패턴 필드가 누락됐습니다: " + ", ".join(missing))
    normalized = {}
    for field in PATTERN_FIELDS:
        item = patterns[field]
        if not isinstance(item, (dict, list, str)) or not item:
            raise IntelligenceError(f"patterns.{field}는 비어 있지 않은 분석 결과여야 합니다.")
        normalized[field] = item
    metrics = value.get("public_metrics") or {}
    if not isinstance(metrics, dict):
        raise IntelligenceError("public_metrics는 객체여야 합니다.")
    forbidden = {"audience_retention", "click_through_rate", "rewatch_segments"} & metrics.keys()
    if forbidden:
        raise IntelligenceError("타 채널 레퍼런스에는 비공개 Analytics 지표를 저장할 수 없습니다.")
    return {
        "video_id": video_id, "title": title,
        "channel_ref": str(value.get("channel_ref", "")).strip() or None,
        "published_at": value.get("published_at"),
        "duration_sec": float(value["duration_sec"]) if value.get("duration_sec") is not None else None,
        "public_metrics": metrics, "patterns": normalized,
    }


def run_reference_analyzer(
    executable: list[str], metadata: dict, output_directory: str | Path,
    runner: Callable = subprocess.run,
) -> dict:
    if not executable or not all(isinstance(item, str) and item for item in executable):
        raise IntelligenceError("레퍼런스 분석 실행기는 비어 있지 않은 인자 배열이어야 합니다.")
    output = Path(output_directory).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    input_path, result_path = output / "reference-input.json", output / "reference-output.json"
    input_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    command = [*executable, "--input", str(input_path), "--output", str(result_path)]
    result = runner(command, capture_output=True, text=True, check=False)
    if result.returncode:
        raise IntelligenceError(result.stderr[-4000:] or "YouTube 레퍼런스 분석에 실패했습니다.")
    if not result_path.is_file():
        raise IntelligenceError("레퍼런스 분석 결과 파일이 생성되지 않았습니다.")
    try:
        analysis = validate_reference_analysis(json.loads(result_path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError) as error:
        raise IntelligenceError(f"레퍼런스 분석 JSON을 읽을 수 없습니다: {error}") from error
    return {"analysis": analysis, "command": command}
