from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Callable

from .producer import validate_analysis_manifest


class PlanningError(RuntimeError):
    pass


def run_dynamic_planner(
    executable: list[str], analysis_input: dict, output_directory: str | Path,
    duration_sec: float, runner: Callable = subprocess.run,
) -> dict:
    """Generate non-linear edit episodes from validated candidates and retrieved scenes."""
    if not executable or not all(isinstance(value, str) and value for value in executable):
        raise PlanningError("동적 기획 AI 실행 명령은 비어 있지 않은 인자 배열이어야 합니다.")
    if not analysis_input.get("candidates"):
        raise PlanningError("동적 기획에는 하나 이상의 콘텐츠 후보가 필요합니다.")
    output = Path(output_directory).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    input_path, result_path = output / "planning-input.json", output / "planning-output.json"
    input_path.write_text(json.dumps(analysis_input, ensure_ascii=False, indent=2), encoding="utf-8")
    command = [*executable, "--input", str(input_path), "--output", str(result_path)]
    result = runner(command, capture_output=True, text=True, check=False)
    if result.returncode:
        raise PlanningError(result.stderr[-4000:] or "동적 콘텐츠 기획 AI 실행에 실패했습니다.")
    if not result_path.is_file():
        raise PlanningError(f"동적 기획 결과 파일이 생성되지 않았습니다: {result_path}")
    try:
        payload = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PlanningError(f"동적 기획 결과 JSON을 읽을 수 없습니다: {error}") from error
    episodes = payload.get("episodes") if isinstance(payload, dict) else None
    if not isinstance(episodes, list):
        raise PlanningError("동적 기획 결과 episodes는 배열이어야 합니다.")
    manifest = {"events": analysis_input.get("events", []),
                "candidates": analysis_input.get("candidates", []), "episodes": episodes}
    try:
        manifest = validate_analysis_manifest(manifest, duration_sec)
    except ValueError as error:
        raise PlanningError(str(error)) from error
    return {"manifest": manifest, "episodes": episodes, "command": command,
            "input_path": str(input_path), "output_path": str(result_path)}
