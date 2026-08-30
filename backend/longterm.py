from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Callable


class LongTermUnderstandingError(RuntimeError):
    pass


def run_window_understanding(
    executable: list[str], window: dict, timeline: list[dict], memory: dict,
    output_path: str | Path, runner: Callable = subprocess.run,
) -> dict:
    """Run one cumulative long-term-understanding window against prior memory."""
    if not executable or not all(isinstance(value, str) and value for value in executable):
        raise LongTermUnderstandingError("장기 이해 AI 실행 명령은 비어 있지 않은 인자 배열이어야 합니다.")
    start, end = float(window["start_sec"]), float(window["end_sec"])
    if start < 0 or end <= start or not isinstance(memory, dict):
        raise LongTermUnderstandingError("장기 이해 입력 시간 범위 또는 메모리가 올바르지 않습니다.")
    output = Path(output_path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    input_path = output.with_suffix(".input.json")
    input_path.write_text(json.dumps({
        "window": window, "timeline": timeline, "memory": memory,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    command = [*executable, "--input", str(input_path), "--output", str(output)]
    result = runner(command, capture_output=True, text=True, check=False)
    if result.returncode:
        raise LongTermUnderstandingError(result.stderr[-4000:] or "장기 방송 이해 AI 실행에 실패했습니다.")
    if not output.is_file():
        raise LongTermUnderstandingError(f"장기 이해 결과 파일이 생성되지 않았습니다: {output}")
    try:
        payload = json.loads(output.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise LongTermUnderstandingError(f"장기 이해 결과 JSON을 읽을 수 없습니다: {error}") from error
    return _normalize(payload, start, end, command)


def _normalize(payload: dict, start: float, end: float, command: list[str]) -> dict:
    if not isinstance(payload, dict) or not str(payload.get("summary", "")).strip():
        raise LongTermUnderstandingError("장기 이해 결과에는 비어 있지 않은 summary가 필요합니다.")
    memory = payload.get("memory")
    if not isinstance(memory, dict):
        raise LongTermUnderstandingError("장기 이해 결과 memory는 객체여야 합니다.")
    precision = payload.get("precision_ranges", [])
    if not isinstance(precision, list):
        raise LongTermUnderstandingError("precision_ranges는 배열이어야 합니다.")
    normalized = []
    for item in precision:
        begin, finish = float(item["start_sec"]), float(item["end_sec"])
        if begin < start or finish <= begin or finish > end:
            raise LongTermUnderstandingError("장기 이해 정밀 구간이 현재 창을 벗어났습니다.")
        normalized.append({"start_sec": begin, "end_sec": finish,
                           "reason": str(item.get("reason", "understanding_uncertainty"))})
    return {"summary": str(payload["summary"]).strip(), "memory": memory,
            "precision_ranges": normalized, "command": command}
