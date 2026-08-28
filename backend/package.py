from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, asdict
from pathlib import Path


class PackageError(ValueError):
    pass


@dataclass(frozen=True)
class MetadataPackage:
    title_options: tuple[str, ...]
    description: str
    tags: tuple[str, ...]
    chapters: tuple[dict, ...]

    @classmethod
    def from_dict(cls, value: dict) -> "MetadataPackage":
        titles = tuple(str(title).strip() for title in value.get("title_options", []) if str(title).strip())
        if len(titles) != 3:
            raise PackageError("제목 후보는 정확히 3개여야 합니다.")
        if any(len(title) > 100 for title in titles):
            raise PackageError("YouTube 제목은 100자를 초과할 수 없습니다.")
        tags = tuple(str(tag).strip().lstrip("#") for tag in value.get("tags", []) if str(tag).strip())
        chapters = tuple(value.get("chapters", []))
        previous = -1.0
        for chapter in chapters:
            start = float(chapter["start_sec"])
            if start < previous:
                raise PackageError("챕터는 시작 시간 순서여야 합니다.")
            previous = start
        return cls(titles, str(value.get("description", "")).strip(), tags, chapters)


def timestamp(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def description_with_chapters(package: MetadataPackage) -> str:
    chapter_text = "\n".join(
        f"{timestamp(float(chapter['start_sec']))} {chapter['title']}" for chapter in package.chapters
    )
    return f"{package.description}\n\n{chapter_text}".strip()


def write_metadata_package(package: MetadataPackage, directory: str | Path) -> dict[str, str]:
    output = Path(directory).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    payload = {**asdict(package), "description_with_chapters": description_with_chapters(package)}
    json_path = output / "metadata.json"
    text_path = output / "metadata.txt"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    text_path.write_text(
        "제목 후보\n" + "\n".join(f"{index}. {title}" for index, title in enumerate(package.title_options, 1))
        + f"\n\n설명\n{payload['description_with_chapters']}\n\n태그\n"
        + ", ".join(f"#{tag}" for tag in package.tags), encoding="utf-8",
    )
    return {"json_path": str(json_path), "text_path": str(text_path)}


def build_thumbnail_commands(
    ffmpeg: str, video_path: str, timestamps: list[float], output_directory: str | Path
) -> list[list[str]]:
    output = Path(output_directory).expanduser().resolve()
    if not timestamps:
        raise PackageError("썸네일 후보 시각이 하나 이상 필요합니다.")
    commands = []
    for index, seconds in enumerate(timestamps, 1):
        if float(seconds) < 0:
            raise PackageError("썸네일 시각은 음수일 수 없습니다.")
        commands.append([
            ffmpeg, "-hide_banner", "-y", "-ss", f"{float(seconds):.3f}", "-i", video_path,
            "-frames:v", "1", "-q:v", "2", str(output / f"thumbnail-{index:02d}.jpg"),
        ])
    return commands


def extract_thumbnails(
    video_path: str, timestamps: list[float], output_directory: str | Path,
    runner=subprocess.run,
) -> list[str]:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise PackageError("ffmpeg가 설치되어 있지 않습니다.")
    output = Path(output_directory).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    commands = build_thumbnail_commands(ffmpeg, video_path, timestamps, output)
    for command in commands:
        result = runner(command, capture_output=True, text=True, check=False)
        if result.returncode:
            raise PackageError(result.stderr[-4000:] or "썸네일 추출에 실패했습니다.")
    return [command[-1] for command in commands]
