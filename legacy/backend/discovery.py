from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Callable

from .producer import validate_analysis_manifest


class DiscoveryError(RuntimeError):
    pass


def run_content_discovery(
    executable: list[str], analysis_input: dict, output_directory: str | Path,
    duration_sec: float, runner: Callable = subprocess.run,
) -> dict:
    """Run event-centric autonomous discovery before edit planning."""
    if not executable or not all(isinstance(value, str) and value for value in executable):
        raise DiscoveryError("콘텐츠 발견 AI 실행 명령은 비어 있지 않은 인자 배열이어야 합니다.")
    output = Path(output_directory).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    input_path, result_path = output / "discovery-input.json", output / "discovery-output.json"
    input_path.write_text(json.dumps(analysis_input, ensure_ascii=False, indent=2), encoding="utf-8")
    command = [*executable, "--input", str(input_path), "--output", str(result_path)]
    result = runner(command, capture_output=True, text=True, check=False)
    if result.returncode:
        raise DiscoveryError(result.stderr[-4000:] or "콘텐츠 자율 발견 AI 실행에 실패했습니다.")
    if not result_path.is_file():
        raise DiscoveryError(f"콘텐츠 발견 결과 파일이 생성되지 않았습니다: {result_path}")
    try:
        payload = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DiscoveryError(f"콘텐츠 발견 결과 JSON을 읽을 수 없습니다: {error}") from error
    if payload.get("episodes"):
        raise DiscoveryError("콘텐츠 발견 단계에서는 편집 에피소드를 생성할 수 없습니다.")
    try:
        manifest = validate_analysis_manifest({**payload, "episodes": []}, duration_sec)
    except ValueError as error:
        raise DiscoveryError(str(error)) from error
    return {"manifest": manifest, "command": command, "input_path": str(input_path),
            "output_path": str(result_path)}
