from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Callable

from .discovery import PACING


class PlanningError(ValueError):
    pass


TARGET_TYPES = {"LONG", "SHORTS"}


def build_planning_command(executable: list[str], input_path: str, output_path: str) -> list[str]:
    if not executable or not all(isinstance(item, str) and item for item in executable):
        raise PlanningError("기획 실행기는 비어 있지 않은 인자 배열이어야 합니다.")
    return [*executable, "--input", input_path, "--output", output_path]


def validate_edit_plan(plan: dict, candidate_ids: set[str], duration_sec: float) -> dict:
    episodes = plan.get("episodes", [])
    if not isinstance(episodes, list):
        raise PlanningError("episodes는 배열이어야 합니다.")
    episode_ids: set[str] = set()
    for episode in episodes:
        episode_id = str(episode.get("episode_id", "")).strip()
        if not episode_id or episode_id in episode_ids:
            raise PlanningError("에피소드 ID는 비어 있지 않고 중복되지 않아야 합니다.")
        episode_ids.add(episode_id)
        references = set(episode.get("candidate_ids", []))
        if not references or references - candidate_ids:
            raise PlanningError(f"에피소드 {episode_id}의 후보 참조가 올바르지 않습니다.")
        if episode.get("target_type") not in TARGET_TYPES:
            raise PlanningError("target_type은 LONG 또는 SHORTS여야 합니다.")
        if not isinstance(episode.get("structure"), dict) or not episode["structure"]:
            raise PlanningError("고정 템플릿이 아닌 콘텐츠별 structure가 필요합니다.")
        timeline = episode.get("timeline", [])
        if not timeline:
            raise PlanningError("에피소드에는 하나 이상의 편집 컷이 필요합니다.")
        active_duration = 0.0
        for cut in timeline:
            start, end = float(cut["source_start_sec"]), float(cut["source_end_sec"])
            if start < 0 or end <= start or end > duration_sec:
                raise PlanningError("편집 컷이 원본 범위를 벗어났습니다.")
            if cut.get("pacing_mode") not in PACING:
                raise PlanningError("편집 컷 호흡은 KEEP, TRIM 또는 CUT이어야 합니다.")
            if not str(cut.get("scene_role", "")).strip():
                raise PlanningError("모든 컷에는 장면 역할이 필요합니다.")
            if not str(cut.get("selection_reason", "")).strip():
                raise PlanningError("모든 컷에는 선택 근거가 필요합니다.")
            if not str(cut.get("pacing_reason", "")).strip():
                raise PlanningError("모든 컷에는 호흡 판단 근거가 필요합니다.")
            if cut["pacing_mode"] != "CUT":
                active_duration += end - start
        planned = float(episode.get("planned_duration_sec", active_duration))
        if planned <= 0:
            raise PlanningError("계획 길이는 0보다 커야 합니다.")
        episode["computed_active_duration_sec"] = round(active_duration, 3)
        episode["planned_duration_sec"] = planned
    return {"episodes": episodes}


def run_planning(
    executable: list[str], candidates: list[dict], events: list[dict],
    analysis_windows: list[dict], duration_sec: float, work_directory: str | Path,
    target_duration_hint: str | None = None, runner: Callable = subprocess.run,
) -> dict:
    work = Path(work_directory).expanduser().resolve()
    work.mkdir(parents=True, exist_ok=True)
    input_path, output_path = work / "planning-input.json", work / "edit-plan.json"
    eligible = [item for item in candidates if item.get("decision") in {"MAKE", "COMBINE"}]
    input_path.write_text(json.dumps({
        "duration_sec": duration_sec,
        "target_duration_hint": target_duration_hint,
        "candidates": eligible,
        "events": events,
        "analysis_windows": analysis_windows,
        "instructions": {
            "structure_is_dynamic": True,
            "chronological_order_required": False,
            "target_duration_is_hint_only": True,
            "require_cut_selection_and_pacing_reasons": True,
        },
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    command = build_planning_command(executable, str(input_path), str(output_path))
    result = runner(command, capture_output=True, text=True, check=False)
    if result.returncode:
        raise PlanningError(result.stderr[-4000:] or "동적 편집 기획 실행에 실패했습니다.")
    if not output_path.is_file():
        raise PlanningError("편집 계획 결과 파일이 생성되지 않았습니다.")
    try:
        payload = json.loads(output_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PlanningError(f"편집 계획 JSON을 읽을 수 없습니다: {error}") from error
    return {
        "plan": validate_edit_plan(payload, {item["candidate_id"] for item in eligible}, duration_sec),
        "command": command,
    }
