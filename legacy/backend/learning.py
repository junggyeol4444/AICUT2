from __future__ import annotations

from dataclasses import dataclass, asdict


class MappingError(ValueError):
    pass


@dataclass(frozen=True)
class Segment:
    start_sec: float
    end_sec: float

    @property
    def duration(self) -> float:
        return self.end_sec - self.start_sec


def _segment(value: dict) -> Segment:
    start, end = float(value["source_start_sec"]), float(value["source_end_sec"])
    if start < 0 or end <= start:
        raise MappingError("원본 구간의 종료 시각은 시작 시각보다 커야 합니다.")
    return Segment(start, end)


def merge_intervals(segments: list[Segment]) -> list[Segment]:
    merged: list[Segment] = []
    for segment in sorted(segments, key=lambda item: (item.start_sec, item.end_sec)):
        if not merged or segment.start_sec > merged[-1].end_sec:
            merged.append(segment)
        else:
            previous = merged[-1]
            merged[-1] = Segment(previous.start_sec, max(previous.end_sec, segment.end_sec))
    return merged


def complement(segments: list[Segment], duration_sec: float) -> list[Segment]:
    cursor = 0.0
    removed: list[Segment] = []
    for segment in merge_intervals(segments):
        if segment.start_sec > cursor:
            removed.append(Segment(cursor, segment.start_sec))
        cursor = max(cursor, segment.end_sec)
    if cursor < duration_sec:
        removed.append(Segment(cursor, duration_sec))
    return removed


def overlap_seconds(left: Segment, right: Segment) -> float:
    return max(0.0, min(left.end_sec, right.end_sec) - max(left.start_sec, right.start_sec))


def analyze_source_output(source_duration_sec: float, output_cuts: list[dict]) -> dict:
    duration = float(source_duration_sec)
    if duration <= 0:
        raise MappingError("원본 길이는 0보다 커야 합니다.")
    if not output_cuts:
        return {
            "source_duration_sec": duration, "output_duration_sec": 0,
            "selected_segments": [], "removed_segments": [asdict(Segment(0, duration))],
            "reordered_cuts": [], "repeated_sources": [], "emphasized_cuts": [],
            "selection_ratio": 0,
        }
    segments = [_segment(cut) for cut in output_cuts]
    if any(segment.end_sec > duration for segment in segments):
        raise MappingError("선택 구간이 원본 길이를 초과합니다.")
    merged = merge_intervals(segments)
    reordered = []
    for index in range(1, len(segments)):
        if segments[index].start_sec < segments[index - 1].start_sec:
            reordered.append({"output_order": index + 1, "from_source_sec": segments[index].start_sec})
    repeated = []
    for index, segment in enumerate(segments):
        for other_index in range(index):
            overlap = overlap_seconds(segment, segments[other_index])
            if overlap > 0:
                repeated.append({"output_order": index + 1, "previous_order": other_index + 1, "overlap_sec": overlap})
    emphasized = []
    for index, cut in enumerate(output_cuts, 1):
        effect = cut.get("visual_effect") or {}
        if isinstance(effect, str):
            effect = {"type": effect}
        if effect and effect.get("type", "none") != "none":
            emphasized.append({"output_order": index, "effect": effect})
    selected_duration = sum(segment.duration for segment in merged)
    return {
        "source_duration_sec": duration,
        "output_duration_sec": sum(segment.duration for segment in segments),
        "selected_segments": [asdict(segment) for segment in merged],
        "removed_segments": [asdict(segment) for segment in complement(segments, duration)],
        "reordered_cuts": reordered, "repeated_sources": repeated, "emphasized_cuts": emphasized,
        "selection_ratio": selected_duration / duration,
    }
