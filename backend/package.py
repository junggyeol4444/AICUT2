from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Callable


class PackageError(ValueError):
    pass


def run_packaging_model(
    executable: list[str], analysis_input: dict, output_directory: str | Path,
    runner: Callable = subprocess.run,
) -> dict:
    """Ask an external model for reviewable metadata and thumbnail timestamps."""
    if not executable or not all(isinstance(value, str) and value for value in executable):
        raise PackageError("패키징 AI 실행 명령은 비어 있지 않은 인자 배열이어야 합니다.")
    episodes = analysis_input.get("episodes", [])
    known = {str(episode.get("episode_id")): episode for episode in episodes}
    if not known:
        raise PackageError("패키징에는 하나 이상의 기획된 에피소드가 필요합니다.")
    output = Path(output_directory).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    input_path, result_path = output / "packaging-input.json", output / "packaging-output.json"
    input_path.write_text(json.dumps(analysis_input, ensure_ascii=False, indent=2), encoding="utf-8")
    command = [*executable, "--input", str(input_path), "--output", str(result_path)]
    result = runner(command, capture_output=True, text=True, check=False)
    if result.returncode:
        raise PackageError(result.stderr[-4000:] or "패키징 AI 실행에 실패했습니다.")
    if not result_path.is_file():
        raise PackageError(f"패키징 결과 파일이 생성되지 않았습니다: {result_path}")
    try:
        payload = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PackageError(f"패키징 결과 JSON을 읽을 수 없습니다: {error}") from error
    packages = payload.get("packages") if isinstance(payload, dict) else None
    if not isinstance(packages, list):
        raise PackageError("패키징 결과 packages는 배열이어야 합니다.")
    normalized, seen = [], set()
    for item in packages:
        episode_id = str(item.get("episode_id", "")) if isinstance(item, dict) else ""
        if episode_id not in known:
            raise PackageError(f"알 수 없는 에피소드의 패키징 결과입니다: {episode_id}")
        if episode_id in seen:
            raise PackageError(f"중복된 에피소드 패키징 결과입니다: {episode_id}")
        seen.add(episode_id)
        metadata = MetadataPackage.from_dict(item.get("metadata", {}))
        timestamps = [float(value) for value in item.get("thumbnail_timestamps", [])]
        if any(value < 0 for value in timestamps):
            raise PackageError("썸네일 시각은 음수일 수 없습니다.")
        timeline_duration = sum(
            float(cut["source_end_sec"]) - float(cut["source_start_sec"])
            for cut in known[episode_id].get("timeline", []) if cut.get("pacing_mode") != "CUT"
        )
        if any(value > timeline_duration for value in timestamps):
            raise PackageError(f"썸네일 시각이 에피소드 길이를 벗어났습니다: {episode_id}")
        normalized.append({
            "episode_id": episode_id, "metadata": asdict(metadata),
            "thumbnail_timestamps": timestamps,
        })
    missing = set(known) - seen
    if missing:
        raise PackageError(f"패키징 결과에서 에피소드가 누락되었습니다: {', '.join(sorted(missing))}")
    return {"packages": normalized, "command": command, "input_path": str(input_path),
            "output_path": str(result_path)}


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


def build_episode_package(
    metadata: dict, video_path: str, timestamps: list[float], output_directory: str | Path,
    runner: Callable = subprocess.run,
) -> dict:
    """Write metadata and extract the model-selected, reviewable thumbnail frames."""
    package = MetadataPackage.from_dict(metadata)
    paths = write_metadata_package(package, output_directory)
    thumbnails = extract_thumbnails(video_path, timestamps, output_directory, runner) if timestamps else []
    return {**paths, "thumbnails": thumbnails, "metadata": asdict(package)}
