from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Callable


class ProducerError(ValueError):
    pass


def validate_analysis_manifest(payload: dict, duration_sec: float) -> dict:
    if not isinstance(payload, dict):
        raise ProducerError("AI 분석 결과는 JSON 객체여야 합니다.")
    events = payload.get("events", [])
    candidates = payload.get("candidates", [])
    episodes = payload.get("episodes", [])
    if not all(isinstance(items, list) for items in (events, candidates, episodes)):
        raise ProducerError("events, candidates, episodes는 배열이어야 합니다.")

    event_ids, candidate_ids, episode_ids = set(), set(), set()
    for event in events:
        event_id = _identifier(event, "event_id", "사건")
        if event_id in event_ids:
            raise ProducerError(f"중복 사건 ID입니다: {event_id}")
        event_ids.add(event_id)
        if not str(event.get("summary", "")).strip():
            raise ProducerError(f"사건 요약이 비어 있습니다: {event_id}")
        for mention in event.get("mentions", []):
            _source_range(mention, duration_sec, "start_sec", "end_sec")
            if not str(mention.get("role", "")).strip():
                raise ProducerError(f"사건 언급 역할이 비어 있습니다: {event_id}")

    for candidate in candidates:
        candidate_id = _identifier(candidate, "candidate_id", "후보")
        if candidate_id in candidate_ids:
            raise ProducerError(f"중복 후보 ID입니다: {candidate_id}")
        candidate_ids.add(candidate_id)
        unknown = set(candidate.get("event_ids", [])) - event_ids
        if unknown:
            raise ProducerError(f"후보가 존재하지 않는 사건을 참조합니다: {sorted(unknown)}")
        score = float(candidate.get("independence_score", -1))
        if not 0 <= score <= 1:
            raise ProducerError("독립성 점수는 0과 1 사이여야 합니다.")
        if candidate.get("decision", "HOLD") not in {"MAKE", "COMBINE", "HOLD", "REJECT"}:
            raise ProducerError("지원하지 않는 콘텐츠 후보 결정입니다.")
        if not str(candidate.get("decision_reason", "")).strip():
            raise ProducerError(f"후보 판단 근거가 비어 있습니다: {candidate_id}")

    for episode in episodes:
        episode_id = _identifier(episode, "episode_id", "에피소드")
        if episode_id in episode_ids:
            raise ProducerError(f"중복 에피소드 ID입니다: {episode_id}")
        episode_ids.add(episode_id)
        unknown = set(episode.get("candidate_ids", [])) - candidate_ids
        if unknown:
            raise ProducerError(f"에피소드가 존재하지 않는 후보를 참조합니다: {sorted(unknown)}")
        if episode.get("target_type") not in {"LONG", "SHORTS"}:
            raise ProducerError("에피소드 target_type은 LONG 또는 SHORTS여야 합니다.")
        for cut in episode.get("timeline", []):
            _source_range(cut, duration_sec, "source_start_sec", "source_end_sec")
            if cut.get("pacing_mode") not in {"KEEP", "TRIM", "CUT"}:
                raise ProducerError("컷 pacing_mode는 KEEP, TRIM 또는 CUT이어야 합니다.")
            if not str(cut.get("scene_role", "")).strip():
                raise ProducerError(f"컷 장면 역할이 비어 있습니다: {episode_id}")
    return payload


def run_producer(
    executable: list[str], analysis_input: dict, output_directory: str | Path,
    duration_sec: float, runner: Callable = subprocess.run,
) -> dict:
    if not executable or not all(isinstance(value, str) and value for value in executable):
        raise ProducerError("AI producer 실행 명령은 비어 있지 않은 인자 배열이어야 합니다.")
    output = Path(output_directory).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    input_path, result_path = output / "producer-input.json", output / "producer-output.json"
    input_path.write_text(json.dumps(analysis_input, ensure_ascii=False, indent=2), encoding="utf-8")
    command = [*executable, "--input", str(input_path), "--output", str(result_path)]
    result = runner(command, capture_output=True, text=True, check=False)
    if result.returncode:
        raise ProducerError(result.stderr[-4000:] or "AI producer 실행에 실패했습니다.")
    if not result_path.is_file():
        raise ProducerError(f"AI producer 결과 파일이 생성되지 않았습니다: {result_path}")
    try:
        payload = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ProducerError(f"AI producer 결과 JSON을 읽을 수 없습니다: {error}") from error
    return {"manifest": validate_analysis_manifest(payload, duration_sec), "command": command,
            "input_path": str(input_path), "output_path": str(result_path)}


def _identifier(value: dict, key: str, label: str) -> str:
    identifier = str(value.get(key, "")).strip()
    if not identifier:
        raise ProducerError(f"{label} ID가 필요합니다.")
    return identifier


def _source_range(value: dict, duration: float, start_key: str, end_key: str) -> None:
    start, end = float(value[start_key]), float(value[end_key])
    if start < 0 or end <= start or (duration > 0 and end > duration):
        raise ProducerError("AI 결과가 원본 방송 시간 범위를 벗어났습니다.")
