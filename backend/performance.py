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
