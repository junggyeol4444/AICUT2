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

    # A combine link is a statement that two candidates belong in one episode,
    # and it means that whichever of them is read first. Walking the list and
    # closing each candidate as it is reached made the result depend on the
    # order: a PRODUCE candidate visited before the COMBINE candidate that
    # points at it was emitted alone and the combine candidate was then dropped
    # as an unresolved single. The store returns candidates by independence
    # score, which puts PRODUCE first, so 6.3's candidate B - 재미는 있으나 결말
    # 없음 - was reliably the one thrown away.
    #
    # So the links are treated as undirected edges and the connected components
    # are built first. Reversing the input cannot change what groups exist.
    neighbours: dict[str, set[str]] = {c.candidate_id: set() for c in candidates}
    for candidate in candidates:
        for other_id in candidate.combine_with:
            if other_id in neighbours:
                neighbours[candidate.candidate_id].add(other_id)
                neighbours[other_id].add(candidate.candidate_id)

    seen: set[str] = set()
    groups: list[list[ContentCandidate]] = []
    for candidate in candidates:
        if candidate.candidate_id in seen:
            continue
        component: list[ContentCandidate] = []
        queue = [candidate.candidate_id]
        seen.add(candidate.candidate_id)
        while queue:
            current = queue.pop()
            component.append(by_id[current])
            for other_id in sorted(neighbours[current]):
                if other_id not in seen:
                    seen.add(other_id)
                    queue.append(other_id)
        # Order inside a group follows the input, so the episode's own ordering
        # is stable too.
        order = {c.candidate_id: i for i, c in enumerate(candidates)}
        component.sort(key=lambda c: order[c.candidate_id])
        if any(c.decision is Decision.PRODUCE for c in component) or len(component) > 1:
            groups.append(component)
    return groups
