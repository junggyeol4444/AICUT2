from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Callable


class PacingError(RuntimeError):
    pass


def run_smart_pacing(
    executable: list[str], analysis_input: dict, output_directory: str | Path,
    runner: Callable = subprocess.run,
) -> dict:
    """Classify each planned cut as KEEP/TRIM/CUT using its multimodal context."""
    if not executable or not all(isinstance(value, str) and value for value in executable):
        raise PacingError("스마트 페이싱 실행 명령은 비어 있지 않은 인자 배열이어야 합니다.")
    episodes = analysis_input.get("episodes", [])
    cuts = {(episode["episode_id"], cut["sequence_order"])
            for episode in episodes for cut in episode.get("timeline", [])}
    if not cuts:
        raise PacingError("스마트 페이싱을 적용할 기획 컷이 없습니다.")
    output = Path(output_directory).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    input_path, result_path = output / "pacing-input.json", output / "pacing-output.json"
    input_path.write_text(json.dumps(analysis_input, ensure_ascii=False, indent=2), encoding="utf-8")
    command = [*executable, "--input", str(input_path), "--output", str(result_path)]
    result = runner(command, capture_output=True, text=True, check=False)
    if result.returncode:
        raise PacingError(result.stderr[-4000:] or "스마트 페이싱 AI 실행에 실패했습니다.")
    if not result_path.is_file():
        raise PacingError(f"스마트 페이싱 결과 파일이 생성되지 않았습니다: {result_path}")
    try:
        payload = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PacingError(f"스마트 페이싱 결과 JSON을 읽을 수 없습니다: {error}") from error
    raw = payload.get("decisions") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        raise PacingError("스마트 페이싱 decisions는 배열이어야 합니다.")
    decisions, seen = [], set()
    for item in raw:
        key = (str(item.get("episode_id", "")), int(item.get("sequence_order", 0)))
        if key not in cuts or key in seen:
            raise PacingError("페이싱 결과가 존재하지 않거나 중복된 컷을 참조합니다.")
        mode, reason = item.get("pacing_mode"), str(item.get("reason", "")).strip()
        if mode not in {"KEEP", "TRIM", "CUT"} or not reason:
            raise PacingError("페이싱 모드와 판단 근거가 올바르지 않습니다.")
        seen.add(key)
        decisions.append({"episode_id": key[0], "sequence_order": key[1],
                          "pacing_mode": mode, "reason": reason})
    return {"decisions": decisions, "command": command, "input_path": str(input_path),
            "output_path": str(result_path)}
