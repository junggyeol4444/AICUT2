"""Loop C: performance feedback (12장).

Own-channel metrics only - retention and click-through do not exist for anyone
else's videos (4.2). What comes back is not applied as a rule; it becomes a
strategy update carrying a confidence, which the planner sees as knowledge and
may still override for a content that does not fit the pattern (7.1).
"""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from aicut.intelligence.youtube import YouTubeClient
from aicut.pipeline.context import RunContext

log = logging.getLogger(__name__)


#: 12.1's collected metrics, by the clause's own names, and where each one comes
#: from. Two of them are not metrics the API returns at all - they are places in
#: the video, and they have to be read off the retention curve.
METRICS_12_1 = {
    "조회수": "views",
    "클릭률": "impressionsClickThroughRate",
    "평균 시청 지속 시간": "averageViewDuration",
    "시청자 유지율": "averageViewPercentage",
    "이탈 구간": "dropoffs",
    "재시청 구간": "rewatches",
    "좋아요": "likes",
    "댓글": "comments",
    "공유": "shares",
}

#: Only for a caller with no profile. How far below its neighbours a point on
#: the retention curve has to sit before it is a 이탈 구간 rather than the
#: ordinary slope every video has, and how far above before it is a 재시청 구간.
#: These decide what gets reported as a place viewers left, so 17.1 keeps them in
#: the profile under `performance.*`; the constants here are the fallback.
DEFAULT_DROPOFF_FALL = 0.15
DEFAULT_REWATCH_RISE = 0.15


def retention_features(
    curve: list[dict[str, float]], profile: Any = None,
) -> dict[str, list[dict[str, float]]]:
    """이탈 구간 and 재시청 구간, read off the retention curve (12.1).

    `audienceWatchRatio` is how much of the audience was still watching at each
    elapsed ratio. A fall from one point to the next is people leaving; a rise
    is people going back. 12.1 collects both as their own items, and neither is
    a metric the API returns - the curve is, and these are where it turns.

    Arithmetic only. Which drop-off matters, and what to do about it, is 12.2's
    question and it goes to the model with everything else.

    The two ratios come from the profile (17.1): they decide what is reported as
    a place viewers left, and that is a judgement about this channel's videos.
    """
    fall = (
        profile.get_float("performance.dropoff_fall_ratio")
        if profile is not None else DEFAULT_DROPOFF_FALL
    )
    rise = (
        profile.get_float("performance.rewatch_rise_ratio")
        if profile is not None else DEFAULT_REWATCH_RISE
    )
    points = [
        (float(p.get("elapsedVideoTimeRatio", 0.0)), float(p.get("audienceWatchRatio", 0.0)))
        for p in curve
        if p.get("audienceWatchRatio") is not None
    ]
    points.sort(key=lambda p: p[0])
    if len(points) < 3:
        # Two points describe a line; a line has no turn in it to report.
        return {"dropoffs": [], "rewatches": []}
    mean = sum(v for _, v in points) / len(points)
    if mean <= 0:
        return {"dropoffs": [], "rewatches": []}

    dropoffs: list[dict[str, float]] = []
    rewatches: list[dict[str, float]] = []
    for (at, before), (next_at, after) in zip(points, points[1:]):
        change = (after - before) / mean
        if change <= -fall:
            dropoffs.append({"at_ratio": round(at, 4), "to_ratio": round(next_at, 4),
                             "fall": round(-change, 4)})
        elif change >= rise:
            rewatches.append({"at_ratio": round(at, 4), "to_ratio": round(next_at, 4),
                              "rise": round(change, 4)})
    return {"dropoffs": dropoffs, "rewatches": rewatches}


def missing_metrics(metrics: dict[str, Any]) -> list[str]:
    """Which of 12.1's nine items this collection did not get.

    A metric that came back empty is not the same as one nobody asked for, and
    12.2 reasons from whatever is here - so what is absent has to be visible
    rather than inferred from a strategy update that reads oddly.

    An empty 이탈 구간 is an answer: this video has no sharp drop. What makes
    those two missing is having no retention curve to read them off, not the
    reading coming back empty. A zero view count is likewise a number, not a
    gap - only an absent key is.
    """
    absent: list[str] = []
    has_curve = bool(metrics.get("retention_curve"))
    for name, key in METRICS_12_1.items():
        if key in ("dropoffs", "rewatches"):
            if not has_curve:
                absent.append(name)
            continue
        if metrics.get(key) is None:
            absent.append(name)
    return absent


def collect(ctx: RunContext, client: YouTubeClient, *, days: int = 28) -> list[dict[str, Any]]:
    """Pull metrics for every published episode of this project."""
    end = date.today()
    start = end - timedelta(days=days)
    collected: list[dict[str, Any]] = []

    for episode in ctx.store.episodes(ctx.project.project_id):
        video_id = episode.metadata.get("youtube", {}).get("video_id")
        if not video_id or episode.review_status != "published":
            continue
        metrics = client.analytics(video_id, start.isoformat(), end.isoformat())
        curve = client.audience_retention(video_id, start.isoformat(), end.isoformat())
        metrics["retention_curve"] = curve
        # 12.1 collects 이탈 구간 and 재시청 구간 as their own items; the API
        # returns the curve, not its turns.
        metrics.update(retention_features(curve, ctx.profile))
        metrics["structure"] = episode.planned_structure.get("structure_name", "")
        metrics["target_type"] = episode.target_type
        absent = missing_metrics(metrics)
        if absent:
            log.warning(
                "%s: 12.1 asks for %s and this collection has none of them",
                episode.episode_id, ", ".join(absent),
            )
            metrics["missing_12_1"] = absent
        ctx.store.save_performance(episode.episode_id, metrics)
        collected.append({"episode_id": episode.episode_id, "metrics": metrics})
    if collected:
        ctx.note("performance_missing_metrics", {
            row["episode_id"]: row["metrics"]["missing_12_1"]
            for row in collected if row["metrics"].get("missing_12_1")
        })
    return collected


def learn(ctx: RunContext, knowledge_path: str | Path | None = None) -> dict[str, Any]:
    """Turn collected metrics into strategy updates and fold them into knowledge."""
    records = []
    for episode in ctx.store.episodes(ctx.project.project_id):
        rows = sorted(
            ctx.store.performance(episode.episode_id),
            key=lambda r: r.get("collected_at") or "",
        )
        if not rows:
            continue
        # One snapshot per episode: the newest. Every collection inserts a row,
        # and two runs over the same rolling --days window measure the same
        # views and the same retention curve. Passing both made one episode's
        # evidence count twice and pulled 12.2's strategy updates toward
        # whichever episode had been collected most often - not toward whatever
        # the viewers actually did. History stays in the table; what the
        # judgement sees is the current state of each video.
        latest = rows[-1]
        records.append({
            "episode_id": episode.episode_id,
            "structure": episode.planned_structure.get("structure_name", ""),
            "target_type": episode.target_type,
            "duration_sec": episode.planned_duration_sec,
            "cut_count": len(episode.timeline),
            "metrics": latest["metrics"],
            "collected_at": latest.get("collected_at"),
            "snapshots": len(rows),
        })
    if not records:
        return {"observations": [], "strategy_updates": []}

    result = ctx.producer.learn_from_performance({"episodes": records})
    if knowledge_path:
        path = Path(knowledge_path)
        existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        existing.setdefault("performance_learning", []).extend(result.get("strategy_updates", []))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8")
    return result
