from __future__ import annotations

import uuid
from typing import Any


class ReferenceError(ValueError):
    pass


PRIVATE_ANALYTICS = frozenset({
    "click_through_rate", "ctr", "audience_retention", "retention_curve",
    "average_view_duration", "average_view_percentage", "rewatch_segments", "drop_off_segments",
})
MEDIA_FIELDS = frozenset({"media_path", "source_media_path", "download_path", "local_video_path"})


def _nested_keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set(map(str, value)) | set().union(*(_nested_keys(item) for item in value.values()), set())
    if isinstance(value, list):
        return set().union(*(_nested_keys(item) for item in value), set())
    return set()


def validate_reference(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate one public YouTube reference analysis without retaining source media."""
    if not isinstance(payload, dict):
        raise ReferenceError("YouTube 레퍼런스는 객체여야 합니다.")
    forbidden = (PRIVATE_ANALYTICS | MEDIA_FIELDS).intersection(_nested_keys(payload))
    metrics = payload.get("public_metrics") or {}
    if not isinstance(metrics, dict):
        raise ReferenceError("public_metrics는 객체여야 합니다.")
    if forbidden:
        raise ReferenceError(f"공개 레퍼런스에 저장할 수 없는 필드입니다: {', '.join(sorted(forbidden))}")
    video_id = str(payload.get("video_id", "")).strip()
    if not video_id:
        raise ReferenceError("video_id가 필요합니다.")
    normalized_metrics = {}
    for key in ("views", "likes", "comments"):
        if key in metrics:
            value = int(metrics[key])
            if value < 0:
                raise ReferenceError(f"{key} 공개 지표는 음수일 수 없습니다.")
            normalized_metrics[key] = value
    metadata = payload.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise ReferenceError("metadata는 객체여야 합니다.")
    patterns = payload.get("patterns") or []
    if not isinstance(patterns, list):
        raise ReferenceError("patterns는 배열이어야 합니다.")
    normalized_patterns = []
    for index, pattern in enumerate(patterns):
        if not isinstance(pattern, dict):
            raise ReferenceError(f"patterns[{index}]는 객체여야 합니다.")
        title = str(pattern.get("title", "")).strip()
        kind = str(pattern.get("kind", "")).strip()
        description = str(pattern.get("description", "")).strip()
        confidence = float(pattern.get("confidence", 0))
        if not title or not kind or not description:
            raise ReferenceError(f"patterns[{index}]의 kind, title, description이 필요합니다.")
        if not 0 <= confidence <= 1:
            raise ReferenceError(f"patterns[{index}].confidence는 0과 1 사이여야 합니다.")
        evidence = pattern.get("evidence") or []
        if not isinstance(evidence, list) or not all(isinstance(item, dict) for item in evidence):
            raise ReferenceError(f"patterns[{index}].evidence는 객체 배열이어야 합니다.")
        normalized_patterns.append({
            "pattern_id": str(pattern.get("pattern_id") or uuid.uuid4()),
            "kind": kind, "title": title, "description": description,
            "confidence": confidence, "evidence": evidence,
        })
    return {
        "reference_id": str(payload.get("reference_id") or uuid.uuid4()),
        "video_id": video_id,
        "channel_ref": str(payload.get("channel_ref", "")).strip() or None,
        "metadata": metadata,
        "public_metrics": normalized_metrics,
        "patterns": normalized_patterns,
        "media_discarded": bool(payload.get("media_discarded", False)),
    }
