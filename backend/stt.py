from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .understanding import UnderstandingError, validate_transcript_segments


class SttError(RuntimeError):
    pass


@dataclass(frozen=True)
class SttJob:
    audio_path: str
    track_index: int
    output_path: str
    language: str | None = None


def build_stt_command(executable: list[str], job: SttJob) -> list[str]:
    if not executable:
        raise SttError("STT 실행 명령이 필요합니다.")
    command = [*executable, job.audio_path, "--output_format", "json", "--output_dir", str(Path(job.output_path).parent)]
    if job.language:
        command.extend(["--language", job.language])
    return command


def normalize_whisperx(payload: dict, track_index: int, duration_sec: float) -> list[dict]:
    raw_segments = []
    for segment in payload.get("segments", []):
        words = []
        for word in segment.get("words", []):
            if "start" not in word or "end" not in word:
                continue
            words.append({
                "start_sec": float(word["start"]), "end_sec": float(word["end"]),
                "word": str(word.get("word", "")).strip(), "score": word.get("score"),
            })
        scores = [float(word["score"]) for word in words if word.get("score") is not None]
        raw_segments.append({
            "track_index": track_index, "start_sec": segment["start"], "end_sec": segment["end"],
            "speaker_tag": segment.get("speaker", "UNKNOWN"), "text": segment.get("text", ""),
            "confidence": sum(scores) / len(scores) if scores else None, "words": words,
        })
    try:
        return validate_transcript_segments(raw_segments, duration_sec)
    except UnderstandingError as error:
        raise SttError(str(error)) from error


def transcribe_tracks(
    executable: list[str], audio_paths: list[str], duration_sec: float,
    output_directory: str | Path, language: str | None = None,
    runner: Callable = subprocess.run,
) -> dict:
    output = Path(output_directory).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    all_segments, commands = [], []
    for track_index, audio_path in enumerate(audio_paths):
        json_path = output / f"audio-track-{track_index:02d}.json"
        job = SttJob(audio_path, track_index, str(json_path), language)
        command = build_stt_command(executable, job)
        commands.append(command)
        result = runner(command, capture_output=True, text=True, check=False)
        if result.returncode:
            raise SttError(result.stderr[-4000:] or f"오디오 트랙 {track_index} STT에 실패했습니다.")
        if not json_path.is_file():
            raise SttError(f"STT 결과 파일이 생성되지 않았습니다: {json_path}")
        try:
            payload = json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise SttError(f"STT 결과 JSON을 읽을 수 없습니다: {error}") from error
        all_segments.extend(normalize_whisperx(payload, track_index, duration_sec))
    all_segments.sort(key=lambda item: (item["start_sec"], item["track_index"]))
    return {"segments": all_segments, "commands": commands}
