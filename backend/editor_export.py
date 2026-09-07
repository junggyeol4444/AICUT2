from __future__ import annotations

import csv
import xml.etree.ElementTree as ET
from pathlib import Path


class EditorExportError(ValueError):
    pass


def _active_cuts(cuts: list[dict]) -> list[dict]:
    active = [cut for cut in cuts if cut.get("pacing_mode") != "CUT"]
    if not active:
        raise EditorExportError("편집기로 내보낼 활성 컷이 없습니다.")
    for cut in active:
        start, end = float(cut["source_start_sec"]), float(cut["source_end_sec"])
        if start < 0 or end <= start:
            raise EditorExportError("컷의 원본 시간 범위가 올바르지 않습니다.")
    return active


def _timecode(seconds: float, fps: int) -> str:
    frames = round(seconds * fps)
    hours, frames = divmod(frames, fps * 3600)
    minutes, frames = divmod(frames, fps * 60)
    secs, frames = divmod(frames, fps)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}:{frames:02d}"


def export_editor_bundle(
    input_path: str, cuts: list[dict], output_directory: str | Path, *,
    title: str = "AICUT Timeline", fps: int = 30,
) -> dict:
    """Export one edit decision into open interchange files used by major NLEs."""
    source = Path(input_path).expanduser().resolve()
    if not source.is_file():
        raise EditorExportError(f"원본 영상 파일을 찾을 수 없습니다: {source}")
    if fps not in {24, 25, 30, 50, 60}:
        raise EditorExportError("editor export fps는 24/25/30/50/60 중 하나여야 합니다.")
    active = _active_cuts(cuts)
    output = Path(output_directory).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)

    total_ms = round(sum(float(c["source_end_sec"]) - float(c["source_start_sec"]) for c in active) * 1000)
    root = ET.Element("fcpxml", version="1.10")
    resources = ET.SubElement(root, "resources")
    ET.SubElement(resources, "format", id="r1", name=f"FFVideoFormat{fps}p", frameDuration=f"1/{fps}s")
    asset = ET.SubElement(resources, "asset", id="r2", name=source.name, start="0s", hasVideo="1", hasAudio="1")
    ET.SubElement(asset, "media-rep", kind="original-media", src=source.as_uri())
    library = ET.SubElement(root, "library")
    event = ET.SubElement(library, "event", name="AICUT")
    project = ET.SubElement(event, "project", name=title)
    sequence = ET.SubElement(project, "sequence", format="r1", duration=f"{total_ms}/1000s", tcStart="0s")
    spine = ET.SubElement(sequence, "spine")
    offset_ms = 0
    for index, cut in enumerate(active, 1):
        start_ms = round(float(cut["source_start_sec"]) * 1000)
        duration_ms = round((float(cut["source_end_sec"]) - float(cut["source_start_sec"])) * 1000)
        ET.SubElement(spine, "asset-clip", {
            "name": str(cut.get("cut_id") or f"Cut {index}"), "ref": "r2",
            "offset": f"{offset_ms}/1000s", "start": f"{start_ms}/1000s",
            "duration": f"{duration_ms}/1000s",
        })
        offset_ms += duration_ms
    fcpxml_path = output / "aicut-timeline.fcpxml"
    ET.ElementTree(root).write(fcpxml_path, encoding="utf-8", xml_declaration=True)

    edl_path = output / "aicut-timeline.edl"
    record = 0.0
    lines = [f"TITLE: {title}", "FCM: NON-DROP FRAME", ""]
    for index, cut in enumerate(active, 1):
        start, end = float(cut["source_start_sec"]), float(cut["source_end_sec"])
        duration = end - start
        lines.extend([
            f"{index:03d}  AX       V     C        {_timecode(start, fps)} {_timecode(end, fps)} "
            f"{_timecode(record, fps)} {_timecode(record + duration, fps)}",
            f"* FROM CLIP NAME: {source.name}", "",
        ])
        record += duration
    edl_path.write_text("\n".join(lines), encoding="utf-8")

    csv_path = output / "aicut-timeline.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=(
            "order", "cut_id", "source_file", "source_in_sec", "source_out_sec",
            "duration_sec", "pacing_mode", "scene_role", "reason",
        ))
        writer.writeheader()
        for index, cut in enumerate(active, 1):
            start, end = float(cut["source_start_sec"]), float(cut["source_end_sec"])
            writer.writerow({
                "order": index, "cut_id": cut.get("cut_id", ""), "source_file": str(source),
                "source_in_sec": start, "source_out_sec": end, "duration_sec": end - start,
                "pacing_mode": cut.get("pacing_mode", "KEEP"), "scene_role": cut.get("scene_role", ""),
                "reason": cut.get("pacing_reason", ""),
            })
    return {
        "fcpxml": str(fcpxml_path), "edl": str(edl_path), "csv": str(csv_path),
        "active_cut_count": len(active), "duration_sec": total_ms / 1000,
    }
