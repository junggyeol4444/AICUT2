"""AI Timeline 수정 (플러그인 기획안 30장, MVP 7).

    사용자 요청: "초반을 20초 정도 줄여줘"
        -> 현재 Timeline 분석
        -> 불필요한 부분 탐색
        -> 새로운 편집 계획 생성
        -> Timeline 수정

The request is in ordinary words and which part of the video answers it is a
judgement about this content, so 18장 puts that with the AI: this module gives
the model the timeline as it stands and takes back a whole new one. What it does
not do is trust it. Every span that comes back is checked against the broadcast
before a single frame of the plan changes - a revision that reached past the end
of the source, or emptied the video, would be a worse answer than none.

The previous plan is kept. 22.5 lets a person disagree with a decision, and a
revision they did not want has to be undoable.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from aicut.errors import AicutError
from aicut.models import Cut, Episode
from aicut.pipeline.context import RunContext
from aicut.render.editplan import EditPlan
from aicut.render.timeline import Timeline

log = logging.getLogger(__name__)


class RevisionRefused(AicutError):
    """The request was not carried out, and the timeline is untouched."""


def plan_path_for(ctx: RunContext, episode: Episode) -> Path:
    return ctx.project_dir / "plans" / f"{episode.episode_id}.json"


def _payload(plan: EditPlan, episode: Episode, request: str, source_sec: float) -> dict[str, Any]:
    timeline = Timeline.from_cuts(plan.cuts)
    starts = timeline.cut_starts()
    return {
        "request": request,
        "structure": plan.structure or episode.planned_structure,
        "target_type": plan.target_type or episode.target_type,
        "source_duration_sec": round(source_sec, 3),
        "timeline_duration_sec": round(timeline.duration, 3),
        "cuts": [
            {
                "sequence_order": cut.sequence_order,
                "source_start_sec": round(cut.source_start_sec, 3),
                "source_end_sec": round(cut.source_end_sec, 3),
                "output_start_sec": round(starts.get(cut.sequence_order, 0.0), 3),
                "role": cut.scene_role,
                "speaker": cut.speaker_tag,
                "remove_spans": [[round(float(a), 3), round(float(b), 3)]
                                 for a, b in (tuple(s) for s in cut.remove_spans)],
                "pacing_reason": cut.pacing_reason,
            }
            for cut in sorted(plan.cuts, key=lambda c: c.sequence_order)
        ],
        # The words are what the video says; a request about "그 부분" is about
        # something somebody said, and without this the model is choosing spans
        # from numbers alone.
        "subtitles": [
            {"at_sec": round(line.start_sec, 2), "text": line.text}
            for line in sorted(plan.subtitles, key=lambda s: s.start_sec)[:400]
        ],
    }


def _validated_cuts(answer: dict[str, Any], source_sec: float) -> list[Cut]:
    """The model's timeline, checked against the broadcast it came from.

    Anything outside the source is not a cut of this video, and a span that ends
    before it starts is not a span. Both are refused rather than clamped: a
    clamped cut is a different edit from the one that was asked for, made
    silently by this function.
    """
    rows = answer.get("cuts")
    if not isinstance(rows, list) or not rows:
        raise RevisionRefused(
            "the revision came back with no timeline at all, so nothing was changed"
        )

    cuts: list[Cut] = []
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise RevisionRefused(f"cut {index} of the revision is not an object")
        try:
            start = float(row["source_start_sec"])
            end = float(row["source_end_sec"])
        except (KeyError, TypeError, ValueError):
            raise RevisionRefused(
                f"cut {index} of the revision does not say where in the broadcast it is"
            )
        if not end > start:
            raise RevisionRefused(
                f"cut {index} of the revision ends at {end:.3f}s and starts at "
                f"{start:.3f}s, which is not a span of video"
            )
        if start < 0 or (source_sec and end > source_sec + 0.001):
            raise RevisionRefused(
                f"cut {index} of the revision runs to {end:.3f}s, past the end of "
                f"a {source_sec:.3f}s broadcast - that material does not exist"
            )
        removals = []
        for span in row.get("remove_spans") or []:
            try:
                a, b = float(span[0]), float(span[1])
            except (TypeError, ValueError, IndexError):
                raise RevisionRefused(f"cut {index} has a removal that is not a span")
            if b > a and a >= start - 0.001 and b <= end + 0.001:
                removals.append([a, b])
        cuts.append(Cut(
            sequence_order=int(row.get("sequence_order", index)),
            source_start_sec=start,
            source_end_sec=end,
            speaker_tag=str(row.get("speaker") or ""),
            scene_role=str(row.get("role") or ""),
            pacing_reason=str(row.get("reason") or ""),
            remove_spans=removals,
        ))

    ordered = sorted(cuts, key=lambda c: c.sequence_order)
    if not Timeline.from_cuts(ordered).segments:
        raise RevisionRefused(
            "every cut in the revision is shorter than what pacing removes from "
            "it, so the video would be empty"
        )
    return ordered


def revise(ctx: RunContext, episode: Episode, request: str,
           *, plan_path: str | Path | None = None) -> dict[str, Any]:
    """Carry out one request against this episode's timeline (30장).

    Returns what changed, and raises `RevisionRefused` without touching anything
    when the request cannot be carried out - including when the model itself
    says so.
    """
    request = (request or "").strip()
    if not request:
        raise RevisionRefused("say what to change")

    path = Path(plan_path) if plan_path else plan_path_for(ctx, episode)
    if not path.exists():
        raise RevisionRefused(
            f"episode {episode.episode_id} has no saved plan at {path}; there is "
            "nothing to revise yet"
        )
    plan = EditPlan.load(path)
    before = Timeline.from_cuts(plan.cuts)
    source_sec = float(ctx.project.duration_sec or 0.0)

    answer = ctx.producer.revise_timeline(_payload(plan, episode, request, source_sec))
    refusal = (answer.get("refusal") or "").strip()
    if refusal:
        # 2.6: a departure is reported rather than papered over. The model
        # saying it will not do this is an answer, not a failure.
        raise RevisionRefused(refusal)

    cuts = _validated_cuts(answer, source_sec)
    after = Timeline.from_cuts(cuts)

    # The plan that was there is kept beside the new one: 22.5 lets a person
    # disagree, and they cannot if the old timeline is gone.
    kept = path.with_suffix(".before-revision.json")
    plan.save(kept)

    plan.cuts = cuts
    plan.provenance = dict(plan.provenance or {})
    revisions = list(plan.provenance.get("revisions") or [])
    revisions.append({
        "request": request,
        "rationale": str(answer.get("rationale") or ""),
        "cuts_before": len(before.cut_starts()),
        "cuts_after": len(after.cut_starts()),
        "duration_before_sec": round(before.duration, 3),
        "duration_after_sec": round(after.duration, 3),
        "previous_plan": kept.name,
    })
    plan.provenance["revisions"] = revisions
    plan.planned_duration_sec = after.duration
    plan.save(path)

    episode.timeline = cuts
    ctx.store.save_episode(episode)

    result = {
        "episode_id": episode.episode_id,
        "request": request,
        "rationale": str(answer.get("rationale") or ""),
        "duration_before_sec": round(before.duration, 3),
        "duration_after_sec": round(after.duration, 3),
        "cuts_before": len(before.cut_starts()),
        "cuts_after": len(after.cut_starts()),
        "plan": str(path),
        "previous_plan": str(kept),
        # 30장 has the person check the result in their own editor, and the
        # rendered file is now older than the plan. Saying so beats letting them
        # watch the version they asked to change.
        "rendered_output_is_stale": bool(episode.output_mp4_path),
    }
    log.info(
        "revised %s: %d cuts (%.1fs) -> %d cuts (%.1fs)",
        episode.episode_id, result["cuts_before"], result["duration_before_sec"],
        result["cuts_after"], result["duration_after_sec"],
    )
    ctx.report.setdefault("revisions", []).append(result)
    return result
