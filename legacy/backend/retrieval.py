from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Callable


class RetrievalError(RuntimeError):
    pass


def run_scene_retrieval(
    executable: list[str], analysis_input: dict, output_directory: str | Path,
    duration_sec: float, runner: Callable = subprocess.run,
) -> dict:
    """Run hybrid semantic scene retrieval and validate every returned source range."""
    if not executable or not all(isinstance(value, str) and value for value in executable):
        raise RetrievalError("장면 검색 실행 명령은 비어 있지 않은 인자 배열이어야 합니다.")
    output = Path(output_directory).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    input_path, result_path = output / "retrieval-input.json", output / "retrieval-output.json"
    input_path.write_text(json.dumps(analysis_input, ensure_ascii=False, indent=2), encoding="utf-8")
    command = [*executable, "--input", str(input_path), "--output", str(result_path)]
    result = runner(command, capture_output=True, text=True, check=False)
    if result.returncode:
        raise RetrievalError(result.stderr[-4000:] or "장면 검색 실행에 실패했습니다.")
    if not result_path.is_file():
        raise RetrievalError(f"장면 검색 결과 파일이 생성되지 않았습니다: {result_path}")
    try:
        payload = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RetrievalError(f"장면 검색 결과 JSON을 읽을 수 없습니다: {error}") from error
    raw = payload.get("scenes") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        raise RetrievalError("장면 검색 결과 scenes는 배열이어야 합니다.")
    candidate_ids = {item["candidate_id"] for item in analysis_input.get("candidates", [])}
    scenes = [_normalize(item, duration_sec, candidate_ids) for item in raw]
    scenes.sort(key=lambda item: (item["candidate_id"], -item["score"], item["start_sec"]))
    return {"scenes": scenes, "command": command, "input_path": str(input_path), "output_path": str(result_path)}


def _normalize(item: dict, duration: float, candidate_ids: set[str]) -> dict:
    if not isinstance(item, dict):
        raise RetrievalError("검색 장면은 객체여야 합니다.")
    candidate_id = str(item.get("candidate_id", ""))
    if candidate_id not in candidate_ids:
        raise RetrievalError("검색 장면이 존재하지 않는 콘텐츠 후보를 참조합니다.")
    start, end, score = float(item["start_sec"]), float(item["end_sec"]), float(item["score"])
    if start < 0 or end <= start or end > duration:
        raise RetrievalError("검색 장면이 원본 방송 시간 범위를 벗어났습니다.")
    if not 0 <= score <= 1:
        raise RetrievalError("장면 검색 점수는 0과 1 사이여야 합니다.")
    reasons = item.get("reasons", [])
    if not isinstance(reasons, list) or not reasons or not all(str(reason).strip() for reason in reasons):
        raise RetrievalError("장면 검색에는 비어 있지 않은 선택 이유가 필요합니다.")
    return {"candidate_id": candidate_id, "query": str(item.get("query", "")).strip(),
            "start_sec": start, "end_sec": end, "score": score,
            "scene_role": str(item.get("scene_role", "supporting")), "reasons": [str(value) for value in reasons]}
