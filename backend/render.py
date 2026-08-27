from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


class RenderError(RuntimeError):
    pass


@dataclass(frozen=True)
class RenderPlan:
    input_path: str
    output_path: str
    cuts: tuple[dict, ...]
    width: int = 1920
    height: int = 1080
    video_codec: str = "libx264"
    audio_codec: str = "aac"


def _number(value: object, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise RenderError(f"{name} 값이 숫자가 아닙니다.") from error
    if number < 0:
        raise RenderError(f"{name} 값은 음수일 수 없습니다.")
    return number


def validate_plan(plan: RenderPlan) -> tuple[dict, ...]:
    if not plan.input_path:
        raise RenderError("입력 파일 경로가 필요합니다.")
    if not plan.output_path:
        raise RenderError("출력 파일 경로가 필요합니다.")
    active = tuple(cut for cut in plan.cuts if cut.get("pacing_mode") != "CUT")
    if not active:
        raise RenderError("렌더링할 활성 컷이 없습니다.")
    for expected, cut in enumerate(active, 1):
        start = _number(cut.get("source_start_sec"), "source_start_sec")
        end = _number(cut.get("source_end_sec"), "source_end_sec")
        if end <= start:
            raise RenderError(f"컷 {expected}의 종료 시각은 시작 시각보다 커야 합니다.")
    return active


def build_filter_graph(plan: RenderPlan) -> tuple[str, str, str]:
    cuts = validate_plan(plan)
    chains: list[str] = []
    concat_inputs: list[str] = []
    for index, cut in enumerate(cuts):
        start = float(cut["source_start_sec"])
        end = float(cut["source_end_sec"])
        duration = end - start
        fade = min(0.005, duration / 4)
        video_filters = [
            f"trim=start={start:.3f}:end={end:.3f}", "setpts=PTS-STARTPTS",
            f"scale={plan.width}:{plan.height}:force_original_aspect_ratio=decrease",
            f"pad={plan.width}:{plan.height}:(ow-iw)/2:(oh-ih)/2",
        ]
        effect = cut.get("visual_effect") or {}
        if isinstance(effect, str):
            effect = {"type": effect}
        if effect.get("type") == "zoom":
            ratio = min(max(float(effect.get("ratio", 1.08)), 1.0), 1.5)
            zoom_w, zoom_h = int(plan.width / ratio), int(plan.height / ratio)
            video_filters.append(f"crop={zoom_w}:{zoom_h}:(iw-ow)/2:(ih-oh)/2,scale={plan.width}:{plan.height}")
        chains.append(f"[0:v:0]{','.join(video_filters)}[v{index}]")
        chains.append(
            f"[0:a:0]atrim=start={start:.3f}:end={end:.3f},asetpts=PTS-STARTPTS,"
            f"afade=t=in:st=0:d={fade:.3f},afade=t=out:st={max(duration-fade,0):.3f}:d={fade:.3f}[a{index}]"
        )
        concat_inputs.append(f"[v{index}][a{index}]")
    chains.append(f"{''.join(concat_inputs)}concat=n={len(cuts)}:v=1:a=1[vout][aout]")
    return ";".join(chains), "[vout]", "[aout]"


def build_render_command(plan: RenderPlan, ffmpeg: str = "ffmpeg") -> list[str]:
    graph, video, audio = build_filter_graph(plan)
    return [
        ffmpeg, "-hide_banner", "-y", "-i", plan.input_path, "-filter_complex", graph,
        "-map", video, "-map", audio, "-c:v", plan.video_codec, "-preset", "medium",
        "-crf", "18", "-c:a", plan.audio_codec, "-b:a", "192k", "-movflags", "+faststart",
        plan.output_path,
    ]


def render(plan: RenderPlan, runner: Callable = subprocess.run) -> dict:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RenderError("ffmpeg가 설치되어 있지 않습니다.")
    output = Path(plan.output_path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    command = build_render_command(plan, ffmpeg)
    result = runner(command, capture_output=True, text=True, check=False)
    if result.returncode:
        raise RenderError(result.stderr[-4000:] or "FFmpeg 렌더링에 실패했습니다.")
    return {"output_path": str(output), "command": command, "cut_count": len(validate_plan(plan))}


def export_plan(plan: RenderPlan) -> str:
    graph, video, audio = build_filter_graph(plan)
    return json.dumps({
        "input_path": plan.input_path, "output_path": plan.output_path,
        "filter_graph": graph, "video_map": video, "audio_map": audio,
        "command": build_render_command(plan),
    }, ensure_ascii=False, indent=2)
