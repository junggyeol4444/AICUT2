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
    subtitle_path: str | None = None
    audio_mix: tuple[dict, ...] = ()


@dataclass(frozen=True)
class LoudnessTarget:
    integrated_lufs: float = -14.0
    true_peak_db: float = -1.0
    loudness_range: float = 11.0


@dataclass(frozen=True)
class LoudnessMeasurement:
    input_i: float
    input_tp: float
    input_lra: float
    input_thresh: float
    target_offset: float


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
    seen_tracks = set()
    for track in plan.audio_mix:
        try:
            index, volume = int(track["track_index"]), float(track.get("volume", 1.0))
        except (KeyError, TypeError, ValueError) as error:
            raise RenderError("오디오 믹스의 트랙 번호와 볼륨이 올바르지 않습니다.") from error
        if index < 0 or volume < 0 or index in seen_tracks:
            raise RenderError("오디오 믹스에는 중복되지 않은 트랙 번호와 음수가 아닌 볼륨이 필요합니다.")
        seen_tracks.add(index)
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
        if plan.audio_mix:
            audio_inputs = []
            for mix_index, track in enumerate(plan.audio_mix):
                label = f"am{index}_{mix_index}"
                chains.append(
                    f"[0:a:{int(track['track_index'])}]atrim=start={start:.3f}:end={end:.3f},"
                    f"asetpts=PTS-STARTPTS,volume={float(track.get('volume', 1.0)):.3f}[{label}]"
                )
                audio_inputs.append(f"[{label}]")
            chains.append(
                f"{''.join(audio_inputs)}amix=inputs={len(audio_inputs)}:duration=longest:normalize=0,"
                f"afade=t=in:st=0:d={fade:.3f},afade=t=out:st={max(duration-fade,0):.3f}:d={fade:.3f}[a{index}]"
            )
        else:
            chains.append(
                f"[0:a:0]atrim=start={start:.3f}:end={end:.3f},asetpts=PTS-STARTPTS,"
                f"afade=t=in:st=0:d={fade:.3f},afade=t=out:st={max(duration-fade,0):.3f}:d={fade:.3f}[a{index}]"
            )
        concat_inputs.append(f"[v{index}][a{index}]")
    video_output = "[vout]"
    concat_video = "[vconcat]" if plan.subtitle_path else video_output
    chains.append(f"{''.join(concat_inputs)}concat=n={len(cuts)}:v=1:a=1{concat_video}[aout]")
    if plan.subtitle_path:
        subtitle = Path(plan.subtitle_path).expanduser().resolve()
        if not subtitle.is_file():
            raise RenderError(f"ASS 자막 파일을 찾을 수 없습니다: {subtitle}")
        escaped = subtitle.as_posix().replace("\\", r"\\").replace(":", r"\:").replace("'", r"\'")
        chains.append(f"{concat_video}subtitles=filename='{escaped}'{video_output}")
    return ";".join(chains), "[vout]", "[aout]"


def build_render_command(
    plan: RenderPlan,
    ffmpeg: str = "ffmpeg",
    target: LoudnessTarget | None = None,
    measurement: LoudnessMeasurement | None = None,
) -> list[str]:
    graph, video, audio = build_filter_graph(plan)
    audio_map = audio
    if target and measurement:
        loudnorm = (
            f"loudnorm=I={target.integrated_lufs}:TP={target.true_peak_db}:LRA={target.loudness_range}:"
            f"measured_I={measurement.input_i}:measured_TP={measurement.input_tp}:"
            f"measured_LRA={measurement.input_lra}:measured_thresh={measurement.input_thresh}:"
            f"offset={measurement.target_offset}:linear=true:print_format=summary"
        )
        graph = f"{graph};{audio}{loudnorm}[anorm]"
        audio_map = "[anorm]"
    return [
        ffmpeg, "-hide_banner", "-y", "-i", plan.input_path, "-filter_complex", graph,
        "-map", video, "-map", audio_map, "-c:v", plan.video_codec, "-preset", "medium",
        "-crf", "18", "-c:a", plan.audio_codec, "-b:a", "192k", "-movflags", "+faststart",
        plan.output_path,
    ]


def build_measurement_command(
    plan: RenderPlan, target: LoudnessTarget, ffmpeg: str = "ffmpeg"
) -> list[str]:
    graph, _, audio = build_filter_graph(plan)
    graph = (
        f"{graph};{audio}loudnorm=I={target.integrated_lufs}:TP={target.true_peak_db}:"
        f"LRA={target.loudness_range}:print_format=json[ameasure]"
    )
    return [
        ffmpeg, "-hide_banner", "-nostats", "-i", plan.input_path,
        "-filter_complex", graph, "-map", "[ameasure]", "-f", "null", "-",
    ]


def parse_loudness_measurement(stderr: str) -> LoudnessMeasurement:
    start = stderr.rfind("{")
    end = stderr.rfind("}")
    if start < 0 or end <= start:
        raise RenderError("FFmpeg 라우드니스 측정 결과를 찾을 수 없습니다.")
    try:
        payload = json.loads(stderr[start:end + 1])
        return LoudnessMeasurement(
            input_i=float(payload["input_i"]), input_tp=float(payload["input_tp"]),
            input_lra=float(payload["input_lra"]), input_thresh=float(payload["input_thresh"]),
            target_offset=float(payload["target_offset"]),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise RenderError(f"라우드니스 측정 결과가 올바르지 않습니다: {error}") from error


def render(
    plan: RenderPlan,
    runner: Callable = subprocess.run,
    loudness_target: LoudnessTarget | None = None,
) -> dict:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RenderError("ffmpeg가 설치되어 있지 않습니다.")
    output = Path(plan.output_path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    target = loudness_target or LoudnessTarget()
    measurement_command = build_measurement_command(plan, target, ffmpeg)
    measurement_result = runner(measurement_command, capture_output=True, text=True, check=False)
    if measurement_result.returncode:
        raise RenderError(measurement_result.stderr[-4000:] or "라우드니스 1차 측정에 실패했습니다.")
    measurement = parse_loudness_measurement(measurement_result.stderr)
    command = build_render_command(plan, ffmpeg, target, measurement)
    result = runner(command, capture_output=True, text=True, check=False)
    if result.returncode:
        raise RenderError(result.stderr[-4000:] or "FFmpeg 렌더링에 실패했습니다.")
    return {
        "output_path": str(output), "command": command, "measurement_command": measurement_command,
        "loudness_measurement": measurement.__dict__, "cut_count": len(validate_plan(plan)),
    }


def export_plan(plan: RenderPlan, target: LoudnessTarget | None = None) -> str:
    graph, video, audio = build_filter_graph(plan)
    target = target or LoudnessTarget()
    return json.dumps({
        "input_path": plan.input_path, "output_path": plan.output_path,
        "subtitle_path": plan.subtitle_path,
        "audio_mix": plan.audio_mix,
        "filter_graph": graph, "video_map": video, "audio_map": audio,
        "loudness_target": target.__dict__,
        "measurement_command": build_measurement_command(plan, target),
        "final_command_requires_measurement": True,
    }, ensure_ascii=False, indent=2)
