from __future__ import annotations

import math
from collections import defaultdict

from .performance import PerformanceError


def aggregate_edit_strategies(snapshots: list[dict], profile: dict) -> dict:
    """Aggregate correlation-only cut observations into guarded strategy proposals."""
    required = {"min_snapshots", "min_observations", "confidence_z", "minimum_effect"}
    if not required.issubset(profile):
        raise PerformanceError("전략 집계 기준은 채널 프로파일에서 모두 제공해야 합니다.")
    minimum_snapshots = int(profile["min_snapshots"])
    minimum_observations = int(profile["min_observations"])
    z_score = float(profile["confidence_z"])
    minimum_effect = float(profile["minimum_effect"])
    if minimum_snapshots < 1 or minimum_observations < 2 or z_score <= 0 or minimum_effect < 0:
        raise PerformanceError("전략 집계 프로파일 값이 올바르지 않습니다.")
    groups = defaultdict(list)
    snapshot_ids = defaultdict(set)
    for snapshot in snapshots:
        metrics = snapshot.get("metrics", snapshot)
        attribution = metrics.get("cut_attribution", {})
        if attribution.get("status") != "READY":
            continue
        snapshot_id = snapshot.get("performance_id") or metrics.get("snapshot_label") or id(snapshot)
        for item in attribution.get("observations", []):
            key = (str(item.get("scene_role", "UNKNOWN")), str(item.get("pacing_mode", "UNKNOWN")))
            groups[key].append(float(item["retention_change"]))
            snapshot_ids[key].add(snapshot_id)
    proposals = []
    for (scene_role, pacing_mode), values in sorted(groups.items()):
        sample_count, video_count = len(values), len(snapshot_ids[(scene_role, pacing_mode)])
        if sample_count < minimum_observations or video_count < minimum_snapshots:
            decision, reason = "HOLD", "INSUFFICIENT_SAMPLE"
            mean = sum(values) / sample_count
            lower = upper = None
        else:
            mean = sum(values) / sample_count
            variance = sum((value - mean) ** 2 for value in values) / (sample_count - 1)
            margin = z_score * math.sqrt(variance / sample_count)
            lower, upper = mean - margin, mean + margin
            if lower >= minimum_effect:
                decision, reason = "PROMOTE", "POSITIVE_INTERVAL"
            elif upper <= -minimum_effect:
                decision, reason = "ROLLBACK", "NEGATIVE_INTERVAL"
            else:
                decision, reason = "HOLD", "UNCERTAIN_INTERVAL"
        proposals.append({
            "scene_role": scene_role, "pacing_mode": pacing_mode, "decision": decision, "reason": reason,
            "mean_retention_change": mean, "interval": {"lower": lower, "upper": upper},
            "observation_count": sample_count, "snapshot_count": video_count,
            "interpretation": "CORRELATION_ONLY_REQUIRES_EXPERIMENT",
        })
    return {"proposals": proposals, "profile": dict(profile)}
