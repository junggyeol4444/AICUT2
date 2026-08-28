from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Callable


class DiscoveryError(ValueError):
    pass


DECISIONS = {"MAKE", "COMBINE", "HOLD", "REJECT"}
PACING = {"KEEP", "TRIM", "CUT"}


def build_discovery_command(executable: list[str], input_path: str, output_path: str) -> list[str]:
    if not executable or not all(isinstance(value, str) and value for value in executable):
        raise DiscoveryError("콘텐츠 발견 실행기는 비어 있지 않은 인자 배열이어야 합니다.")
    return [*executable, "--input", input_path, "--output", output_path]


def validate_discovery_manifest(manifest: dict, duration_sec: float) -> dict:
    events = manifest.get("events", [])
    candidates = manifest.get("candidates", [])
    episodes = manifest.get("episodes", [])
    event_ids, candidate_ids = set(), set()
    for event in events:
        event_id = str(event.get("event_id", "")).strip()
        if not event_id or event_id in event_ids:
            raise DiscoveryError("사건 ID는 비어 있지 않고 중복되지 않아야 합니다.")
        event_ids.add(event_id)
        if not str(event.get("summary", "")).strip():
            raise DiscoveryError(f"사건 {event_id}에 요약이 필요합니다.")
        for mention in event.get("mentions", []):
            start, end = float(mention["start_sec"]), float(mention["end_sec"])
            if start < 0 or end <= start or end > duration_sec:
                raise DiscoveryError(f"사건 {event_id}의 언급 구간이 원본 범위를 벗어났습니다.")
    for candidate in candidates:
        candidate_id = str(candidate.get("candidate_id", "")).strip()
        if not candidate_id or candidate_id in candidate_ids:
            raise DiscoveryError("후보 ID는 비어 있지 않고 중복되지 않아야 합니다.")
        candidate_ids.add(candidate_id)
        unknown = set(candidate.get("event_ids", [])) - event_ids
        if unknown:
            raise DiscoveryError(f"후보 {candidate_id}가 존재하지 않는 사건을 참조합니다: {sorted(unknown)}")
        score = float(candidate["independence_score"])
        if not 0 <= score <= 1:
            raise DiscoveryError("후보 독립성 점수는 0과 1 사이여야 합니다.")
        if candidate.get("decision") not in DECISIONS:
            raise DiscoveryError(f"지원하지 않는 후보 결정입니다: {candidate.get('decision')}")
        if not str(candidate.get("decision_reason", "")).strip():
            raise DiscoveryError("후보 결정에는 설명 가능한 근거가 필요합니다.")
    for episode in episodes:
        unknown = set(episode.get("candidate_ids", [])) - candidate_ids
        if unknown:
            raise DiscoveryError(f"에피소드가 존재하지 않는 후보를 참조합니다: {sorted(unknown)}")
        timeline = episode.get("timeline", [])
        for cut in timeline:
            start, end = float(cut["source_start_sec"]), float(cut["source_end_sec"])
            if start < 0 or end <= start or end > duration_sec:
                raise DiscoveryError("편집 컷이 원본 범위를 벗어났습니다.")
            if cut.get("pacing_mode") not in PACING:
                raise DiscoveryError("편집 컷 호흡은 KEEP, TRIM 또는 CUT이어야 합니다.")
    return {"events": events, "candidates": candidates, "episodes": episodes}


def run_discovery(
    executable: list[str], analysis_windows: list[dict], duration_sec: float,
    work_directory: str | Path, runner: Callable = subprocess.run,
) -> dict:
    work = Path(work_directory).expanduser().resolve()
    work.mkdir(parents=True, exist_ok=True)
    input_path, output_path = work / "understanding-input.json", work / "discovery-output.json"
    input_path.write_text(json.dumps({
        "duration_sec": duration_sec, "analysis_windows": analysis_windows,
        "instructions": {
            "boundary": "event_not_screen_state", "allow_zero_candidates": True,
            "chronological_order_required": False, "explain_decisions": True,
        },
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    command = build_discovery_command(executable, str(input_path), str(output_path))
    result = runner(command, capture_output=True, text=True, check=False)
    if result.returncode:
        raise DiscoveryError(result.stderr[-4000:] or "콘텐츠 발견 실행에 실패했습니다.")
    if not output_path.is_file():
        raise DiscoveryError("콘텐츠 발견 결과 파일이 생성되지 않았습니다.")
    try:
        manifest = json.loads(output_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DiscoveryError(f"콘텐츠 발견 결과 JSON을 읽을 수 없습니다: {error}") from error
    return {"manifest": validate_discovery_manifest(manifest, duration_sec), "command": command}
