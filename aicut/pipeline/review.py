"""REVIEW_PENDING: the human gate (11.3), and the candidate review screen (15.4).

11.3 replaces the zero-touch design with a mandatory checkpoint: a finished video
is uploaded private and stays private until a person releases it. Automatic
release exists only as an explicit opt-in for a system that has already proven
itself, and the opt-in is recorded on the decision so it is never ambiguous who
allowed a video out.

The other half of this module is 15.4: the reviewer's agreement or disagreement
with each discovery decision is captured, because that record is training data
for loop B (12.3) and the raw material of the MVP 3 acceptance test.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from aicut.models import Decision, Episode
from aicut.pipeline.context import RunContext


@dataclass
class ReviewItem:
    episode_id: str
    titles: list[str]
    thumbnail_candidates: list[str]
    output_path: str | None
    duration_sec: float
    structure: dict[str, Any]
    plan_path: str
    notes: str = ""
    provisional_parameters: list[str] = field(default_factory=list)


def pending(ctx: RunContext, episodes: list[Episode]) -> list[ReviewItem]:
    """Everything waiting on a person, with what they need to judge it."""
    items = []
    for episode in episodes:
        episode.review_status = "pending"
        ctx.store.save_episode(episode)
        items.append(ReviewItem(
            episode_id=episode.episode_id,
            titles=episode.title_candidates,
            thumbnail_candidates=episode.thumbnail_candidates,
            output_path=episode.output_mp4_path,
            duration_sec=episode.planned_duration_sec,
            structure=episode.planned_structure,
            plan_path=str(ctx.project_dir / "plans" / f"{episode.episode_id}.json"),
            notes=episode.notes,
            provisional_parameters=ctx.profile.touched_provisional(),
        ))
    return items


def approve(ctx: RunContext, episode_id: str, *, reviewer: str, note: str = "") -> Episode:
    """Release one episode for publication. Without this, nothing goes public."""
    episode = ctx.store.get_episode(episode_id)
    if episode is None:
        raise KeyError(f"unknown episode {episode_id}")
    episode.review_status = "approved"
    episode.metadata = dict(episode.metadata)
    episode.metadata["review"] = {
        "by": reviewer,
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "note": note,
        "auto": False,
    }
    ctx.store.save_episode(episode)
    return episode


def reject(ctx: RunContext, episode_id: str, *, reviewer: str, reason: str) -> Episode:
    episode = ctx.store.get_episode(episode_id)
    if episode is None:
        raise KeyError(f"unknown episode {episode_id}")
    episode.review_status = "rejected"
    episode.metadata = dict(episode.metadata)
    episode.metadata["review"] = {
        "by": reviewer,
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "note": reason,
        "auto": False,
    }
    ctx.store.save_episode(episode)
    return episode


def record_candidate_verdict(ctx: RunContext, candidate_id: str, verdict: str, note: str = "") -> None:
    """15.4: a person agrees or disagrees with a discovery decision.

    Kept verbatim - agreement and disagreement are equally informative, and this
    is the only source of ground truth the system gets about 6장's judgements
    short of a full source/output pair.
    """
    if verdict not in ("agree", "disagree"):
        raise ValueError("verdict must be 'agree' or 'disagree'")
    ctx.store.set_human_verdict(candidate_id, verdict if not note else f"{verdict}: {note}")


#: 원본 32장 lists what a person evaluates about each candidate, and 19장 puts
#: the same four under MVP 3 as 항목별 평가. They are four different questions -
#: a candidate can find the right scenes and still fail to connect two times of
#: day - so one agree/disagree cannot stand in for them. Keys are the clause's
#: own words; nothing here interprets them.
ASSESSMENT_ITEMS = (
    "관련 장면을 제대로 찾는가",
    "서로 다른 시간대의 장면을 연결하는가",
    "사건의 시작과 결과를 이해하는가",
    "불필요한 장면을 제외하는가",
)

#: What a person may answer. 원본 32장 asks a question, not for a score, and an
#: item a reviewer could not judge is not a failure - it is unanswered, and the
#: rate below leaves it out rather than counting it either way.
ASSESSMENT_VERDICTS = ("yes", "no", "unclear")


def record_candidate_assessment(
    ctx: RunContext, candidate_id: str, assessment: dict[str, str],
) -> None:
    """19장 MVP 3: score one candidate on the four items of 원본 32장.

    Partial answers are allowed - a reviewer works through the items one at a
    time, and refusing a partial answer would mean losing the ones they gave.
    """
    unknown = [key for key in assessment if key not in ASSESSMENT_ITEMS]
    if unknown:
        raise ValueError(
            f"unknown assessment item(s) {unknown}; 원본 32장 names "
            + ", ".join(ASSESSMENT_ITEMS)
        )
    bad = {k: v for k, v in assessment.items() if v not in ASSESSMENT_VERDICTS}
    if bad:
        raise ValueError(
            f"assessment answers must be one of {', '.join(ASSESSMENT_VERDICTS)}; got {bad}"
        )
    ctx.store.set_human_assessment(candidate_id, assessment)


def assessment_rates(ctx: RunContext) -> dict[str, Any]:
    """The MVP 3 gate, item by item.

    Reported separately per item because that is how 원본 32장 asks it. The
    numbers are the reviewers' answers counted - nothing here decides whether
    the gate passed; 19장 leaves that to the person reading it.
    """
    candidates = ctx.store.candidates(ctx.project.project_id)
    answered = [c for c in candidates if c.human_assessment]
    items: dict[str, Any] = {}
    for item in ASSESSMENT_ITEMS:
        given = [c.human_assessment.get(item) for c in answered]
        given = [g for g in given if g in ASSESSMENT_VERDICTS]
        judged = [g for g in given if g != "unclear"]
        items[item] = {
            "answered": len(given),
            "unclear": len(given) - len(judged),
            "yes": sum(1 for g in judged if g == "yes"),
            "no": sum(1 for g in judged if g == "no"),
            "rate": round(sum(1 for g in judged if g == "yes") / len(judged), 3) if judged else None,
        }
    return {
        "candidates": len(candidates),
        "assessed": len(answered),
        "items": items,
    }


def candidate_review(ctx: RunContext) -> list[dict[str, Any]]:
    """The 15.4 screen: every candidate, the decision, and why."""
    return [
        {
            # 15.4 asks the reviewer whether they agree with the decision, and
            # 19장 scores MVP 3 on that agreement. They cannot judge a summary
            # and two scores, so the whole of 6.1 is on the screen.
            "candidate_id": c.candidate_id,
            "core_summary": c.core_summary,
            "people": c.people,
            "scenes": c.scenes,
            "start_point": c.start_point,
            "start_sec": c.start_sec,
            "key_changes": c.key_changes,
            "outcome": c.outcome,
            "event_relations": c.event_relations,
            "suggested_form": c.suggested_form,
            "decision": c.decision.value,
            "reason": c.decision_reason,
            "independence_score": c.independence_score,
            "density_score": c.density_score,
            "has_resolution": c.has_resolution,
            "required_context": c.required_context,
            "human_verdict": c.human_verdict,
            # 19장 MVP 3 asks four questions per candidate; the screen shows
            # which of them this candidate has been answered on.
            "human_assessment": c.human_assessment,
            "events": c.related_event_ids,
        }
        for c in ctx.store.candidates(ctx.project.project_id)
    ]


def agreement_rate(ctx: RunContext) -> dict[str, float]:
    """How often the human agreed - the number MVP 3 is judged on."""
    candidates = [c for c in ctx.store.candidates(ctx.project.project_id) if c.human_verdict]
    if not candidates:
        return {"reviewed": 0, "agreement": 0.0}
    agreed = sum(1 for c in candidates if c.human_verdict.startswith("agree"))
    produced = [c for c in candidates if c.decision is Decision.PRODUCE]
    false_positives = sum(1 for c in produced if c.human_verdict.startswith("disagree"))
    return {
        "reviewed": len(candidates),
        "agreement": round(agreed / len(candidates), 3),
        "false_positive_rate": round(false_positives / len(produced), 3) if produced else 0.0,
    }
