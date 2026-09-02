from __future__ import annotations

from dataclasses import dataclass, asdict


class PerformanceError(ValueError):
    pass


@dataclass(frozen=True)
class RetentionPoint:
    second: float
    ratio: float


def validate_metrics(value: dict) -> dict:
    required = ("views", "likes", "comments", "shares")
    metrics = dict(value)
    for key in required:
        number = int(metrics.get(key, 0))
        if number < 0:
            raise PerformanceError(f"{key} 값은 음수일 수 없습니다.")
        metrics[key] = number
    for key in ("click_through_rate", "average_view_percentage"):
        number = float(metrics.get(key, 0))
        if not 0 <= number <= 1:
            raise PerformanceError(f"{key} 값은 0과 1 사이여야 합니다.")
        metrics[key] = number
    points = []
    previous = -1.0
    for raw in metrics.get("retention", []):
        point = RetentionPoint(float(raw["second"]), float(raw["ratio"]))
        if point.second < previous or point.second < 0:
            raise PerformanceError("유지율 시점은 음수가 아닌 시간순이어야 합니다.")
        if not 0 <= point.ratio <= 1:
            raise PerformanceError("유지율은 0과 1 사이여야 합니다.")
        previous = point.second
        points.append(asdict(point))
    metrics["retention"] = points
    return metrics


def performance_insights(metrics: dict, profile: dict) -> list[dict]:
    """Generate evidence records using channel-measured thresholds, never constants."""
    value = validate_metrics(metrics)
    required = {"early_window_sec", "early_drop_ratio", "peak_gain_ratio"}
    if not required.issubset(profile):
        raise PerformanceError("성과 판단 기준은 채널 프로파일에서 모두 제공해야 합니다.")
    points = [RetentionPoint(**point) for point in value["retention"]]
    insights = []
    if len(points) >= 2:
        start = points[0]
        early = [point for point in points if point.second <= float(profile["early_window_sec"])]
        if early:
            drop = start.ratio - early[-1].ratio
            if drop >= float(profile["early_drop_ratio"]):
                insights.append({
                    "kind": "EARLY_DROP", "at_sec": early[-1].second, "magnitude": drop,
                    "recommendation": "초반 정보 공개 순서와 맥락 전달 방식을 재검토하세요.",
                })
        for previous, current in zip(points, points[1:]):
            gain = current.ratio - previous.ratio
            if gain >= float(profile["peak_gain_ratio"]):
                insights.append({
                    "kind": "RETENTION_GAIN", "at_sec": current.second, "magnitude": gain,
                    "recommendation": "해당 시점의 장면 역할과 연출을 성공 패턴 후보로 검토하세요.",
                })
    return insights


def attribute_retention_to_cuts(metrics: dict, cuts: list[dict], profile: dict) -> dict:
    """Map measured retention changes to the non-linear output timeline without claiming causality."""
    value = validate_metrics(metrics)
    required = {"min_views", "min_retention_points", "meaningful_change_ratio"}
    if not required.issubset(profile):
        raise PerformanceError("컷 성과 귀속 기준은 채널 프로파일에서 모두 제공해야 합니다.")
    minimum_views = int(profile["min_views"])
    minimum_points = int(profile["min_retention_points"])
    change_threshold = float(profile["meaningful_change_ratio"])
    if minimum_views < 1 or minimum_points < 2 or change_threshold < 0:
        raise PerformanceError("컷 성과 귀속 프로파일 값이 올바르지 않습니다.")
    points = [RetentionPoint(**point) for point in value["retention"]]
    reasons = []
    if value["views"] < minimum_views:
        reasons.append("MINIMUM_VIEWS_NOT_MET")
    if len(points) < minimum_points:
        reasons.append("RETENTION_POINTS_NOT_MET")
    if reasons:
        return {"status": "INSUFFICIENT_SAMPLE", "reasons": reasons, "observations": []}
    output_cursor = 0.0
    ranges = []
    for cut in cuts:
        if cut.get("pacing_mode") == "CUT":
            continue
        duration = float(cut["source_end_sec"]) - float(cut["source_start_sec"])
        ranges.append({
            "output_start_sec": output_cursor, "output_end_sec": output_cursor + duration,
            "sequence_order": cut.get("sequence_order"), "scene_role": cut.get("scene_role"),
            "pacing_mode": cut.get("pacing_mode"), "source_start_sec": cut["source_start_sec"],
            "source_end_sec": cut["source_end_sec"],
        })
        output_cursor += duration
    observations = []
    for previous, current in zip(points, points[1:]):
        change = current.ratio - previous.ratio
        if abs(change) < change_threshold:
            continue
        cut = next((item for item in ranges
                    if item["output_start_sec"] <= current.second < item["output_end_sec"]), None)
        if cut:
            observations.append({
                **cut, "at_output_sec": current.second, "retention_change": change,
                "direction": "GAIN" if change > 0 else "DROP",
                "interpretation": "CORRELATION_ONLY",
            })
    return {"status": "READY", "reasons": [], "observations": observations,
            "sample": {"views": value["views"], "retention_points": len(points)}}
