"""EVALUATING: which candidates are actually worth making (6.3).

Four verdicts, all normal: produce it, combine it with a related event, hold it,
reject it. Rejection is not a system failure and neither is rejecting everything -
this stage exists precisely so the system can decline, which is the difference
between a producer and a clip extractor.

The thresholds the producer is shown come from the profile and are still
provisional; 17.3's "false positive rate - did the system promote something a
human would throw away" is the measurement that settles them.
"""

from __future__ import annotations

from aicut.models import ContentCandidate, Decision
from aicut.pipeline.context import RunContext


def run(ctx: RunContext, candidates: list[ContentCandidate]) -> list[ContentCandidate]:
    if not candidates:
        return []

    events = {e.event_id: e for e in ctx.store.events(ctx.project.project_id)}
    thresholds = ctx.profile.get("discovery")
    verdicts = ctx.producer.evaluate_candidates({
        "candidates": [
            {
                "candidate_id": c.candidate_id,
                # 6.3 judges a candidate on 사건 완결 and 맥락, so it needs the
                # whole of 6.1, not the scores. 결말 없음 is `outcome`; 강한 반응
                # is in 주요 변화 and the scenes.
                "core_summary": c.core_summary,
                "people": c.people,
                "scenes": c.scenes,
                "start_point": c.start_point,
                "key_changes": c.key_changes,
                "outcome": c.outcome,
                "event_relations": c.event_relations,
                "suggested_form": c.suggested_form,
                "required_context": c.required_context,
                "required_context_sec": c.required_context_sec,
                "independence_score": c.independence_score,
                "density_score": c.density_score,
                "has_resolution": c.has_resolution,
                "events": [
                    {"summary": events[eid].summary, "mention_count": len(events[eid].mentions)}
                    for eid in c.related_event_ids if eid in events
                ],
            }
            for c in candidates
        ],
        "thresholds": thresholds,
        "length_hint_sec": ctx.project.length_hint_sec,
    })

    by_id = {c.candidate_id: c for c in candidates}
    for verdict in verdicts:
        candidate = by_id.get(verdict.get("candidate_id", ""))
        if candidate is None:
            continue
        try:
            candidate.decision = Decision(verdict.get("decision", "hold"))
        except ValueError:
            candidate.decision = Decision.HOLD
        candidate.decision_reason = verdict.get("reason", "")
        candidate.combine_with = [
            cid for cid in (verdict.get("combine_with") or []) if cid in by_id and cid != candidate.candidate_id
        ]

    ctx.store.upsert_candidates(ctx.project.project_id, candidates)
    counts = {d.value: sum(1 for c in candidates if c.decision is d) for d in Decision}
    ctx.note("decisions", counts)
    ctx.note("rejections", [
        {"summary": c.core_summary[:80], "reason": c.decision_reason}
        for c in candidates if c.decision is Decision.REJECT
    ])
    return [c for c in candidates if c.decision in (Decision.PRODUCE, Decision.COMBINE)]


def group_for_production(candidates: list[ContentCandidate]) -> list[list[ContentCandidate]]:
    """Merge combine-linked candidates into the groups that become episodes.

    6.3's candidate B - funny but with no ending - becomes a video only when it
    is welded to the event that resolves it. A combine candidate that nothing
    picks up is left out rather than shipped unresolved.
    """
    by_id = {c.candidate_id: c for c in candidates}
    seen: set[str] = set()
    groups: list[list[ContentCandidate]] = []

    for candidate in candidates:
        if candidate.candidate_id in seen:
            continue
        group = [candidate]
        seen.add(candidate.candidate_id)
        queue = list(candidate.combine_with)
        while queue:
            other_id = queue.pop()
            other = by_id.get(other_id)
            if other is None or other.candidate_id in seen:
                continue
            seen.add(other.candidate_id)
            group.append(other)
            queue.extend(other.combine_with)
        if any(c.decision is Decision.PRODUCE for c in group) or len(group) > 1:
            groups.append(group)
    return groups
