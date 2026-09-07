from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class SubtitleError(ValueError):
    pass


@dataclass(frozen=True)
class AssStyle:
    font_name: str
    font_size: int
    primary_color: str
    outline_color: str
    margin_v: int

    @classmethod
    def from_dict(cls, value: dict) -> "AssStyle":
        required = ("font_name", "font_size", "primary_color", "outline_color", "margin_v")
        if any(key not in value for key in required):
            raise SubtitleError("ASS 스타일은 채널 프로파일의 폰트·크기·색상·여백을 모두 지정해야 합니다.")
        size, margin = int(value["font_size"]), int(value["margin_v"])
        if not str(value["font_name"]).strip() or size <= 0 or margin < 0:
            raise SubtitleError("ASS 스타일의 폰트, 크기 또는 여백이 올바르지 않습니다.")
        return cls(str(value["font_name"]).strip(), size, str(value["primary_color"]),
                   str(value["outline_color"]), margin)


def _time(seconds: float) -> str:
    centiseconds = round(float(seconds) * 100)
    hours, remainder = divmod(centiseconds, 360000)
    minutes, remainder = divmod(remainder, 6000)
    secs, fraction = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{fraction:02d}"


def write_ass_subtitles(
    cues: list[dict], style_profile: dict, output_path: str | Path, duration_sec: float,
    width: int = 1920, height: int = 1080,
) -> str:
    """Create an ASS file from output-timeline cues and a measured channel style profile."""
    style = AssStyle.from_dict(style_profile)
    previous_start = -1.0
    dialogue = []
    for cue in cues:
        start, end = float(cue["start_sec"]), float(cue["end_sec"])
        if start < 0 or end <= start or end > float(duration_sec) or start < previous_start:
            raise SubtitleError("자막 큐는 완성본 시간 범위 안에서 시작 시간 순으로 정렬되어야 합니다.")
        previous_start = start
        text = str(cue.get("text", "")).strip().replace("\n", r"\N")
        if not text:
            raise SubtitleError("빈 자막 큐는 저장할 수 없습니다.")
        speaker = str(cue.get("speaker_tag", "UNKNOWN")).strip() or "UNKNOWN"
        dialogue.append(f"Dialogue: 0,{_time(start)},{_time(end)},Default,{speaker},0,0,0,,{text}")
    output = Path(output_path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    content = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {int(width)}
PlayResY: {int(height)}

[V4+ Styles]
Format: Name,Fontname,Fontsize,PrimaryColour,OutlineColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding
Style: Default,{style.font_name},{style.font_size},{style.primary_color},{style.outline_color},0,0,0,0,100,100,0,0,1,2,0,2,40,40,{style.margin_v},1

[Events]
Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text
{"\n".join(dialogue)}
"""
    output.write_text(content, encoding="utf-8-sig")
    return str(output)
