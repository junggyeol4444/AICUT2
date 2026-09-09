"""The production knowledge store (4.5).

What comes out of the reference loop is knowledge, not rules. 4.5 is explicit:
these patterns are consulted when planning a new video, never applied as fixed
law, and 7.1 has the planner compare a pattern against the actual content before
using it. So this store hands the planner evidence with support counts attached
and lets the judgement happen there.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ProductionKnowledge:
    """Patterns observed across references, with how much support each has."""

    # 4.5 lists what this store holds, and the original 8장 adds two more. Every
    # item there has a field here; a missing field is a question nobody can ask
    # the planner later.
    structure_patterns: list[dict[str, Any]] = field(default_factory=list)      # 콘텐츠 구성 패턴
    editing_patterns: list[dict[str, Any]] = field(default_factory=list)        # 편집 패턴
    storytelling_patterns: list[dict[str, Any]] = field(default_factory=list)   # 스토리텔링 패턴
    scene_selection_patterns: list[dict[str, Any]] = field(default_factory=list)  # 장면 선택 패턴
    pacing_patterns: list[dict[str, Any]] = field(default_factory=list)         # 영상 템포
    subtitle_patterns: list[dict[str, Any]] = field(default_factory=list)       # 자막 사용 패턴
    emphasis_patterns: list[dict[str, Any]] = field(default_factory=list)       # 반응 강조 방식
    people_patterns: list[dict[str, Any]] = field(default_factory=list)         # 4.3 인물
    content_type_patterns: list[dict[str, Any]] = field(default_factory=list)   # 콘텐츠별 특징 (8장)
    length_structure_patterns: list[dict[str, Any]] = field(default_factory=list)  # 영상 길이와 구성의 관계
    response_structure_patterns: list[dict[str, Any]] = field(default_factory=list)  # 시청자 반응과 영상 구성의 관계 (8장)
    title_patterns: list[str] = field(default_factory=list)                     # 제목 패턴
    thumbnail_patterns: list[str] = field(default_factory=list)                 # 썸네일 패턴
    production_logic: list[dict[str, Any]] = field(default_factory=list)        # 4.4 / 7장
    performance_learning: list[dict[str, Any]] = field(default_factory=list)    # 12.2
    source_output_rules: list[str] = field(default_factory=list)                # 12.3 B
    sample_size: int = 0

    def carry_over_learning(self, previous: "ProductionKnowledge") -> "ProductionKnowledge":
        """Keep what the other two loops learned when loop A rebuilds this file.

        12.3 runs three loops into one knowledge file. Loop A rebuilds its own
        patterns from every stored reference, which is right for A and wrong for
        the file: saving that fresh object dropped 12.3 B's inferred rules and
        12.2's performance learning, so a reference run silently undid every
        pair the operator had fed in.
        """
        self.source_output_rules = list(previous.source_output_rules)
        self.performance_learning = list(previous.performance_learning)
        return self

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProductionKnowledge":
        known = {f for f in cls().__dict__}
        return cls(**{k: v for k, v in data.items() if k in known})

    @classmethod
    def load(cls, path: str | Path) -> "ProductionKnowledge":
        file = Path(path)
        if not file.exists():
            return cls()
        return cls.from_dict(json.loads(file.read_text(encoding="utf-8")))

    def save(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
        return target

    def summary_for_planner(self, *, limit: int = 8) -> dict[str, Any]:
        """A compact view for the planner: patterns plus how well supported they are.

        The pattern lists come out of :func:`consolidate` ordered by support, so
        the first few are the best-supported ones. The two learning lists do not:
        loop B and loop C append to them run after run, so the front of those
        lists is the oldest thing ever learned. Slicing them from the front hid
        every later correction behind the first eight - the newest are taken.
        """
        return {
            "sample_size": self.sample_size,
            "structure": self.structure_patterns[:limit],
            "storytelling": self.storytelling_patterns[:limit],
            "editing": self.editing_patterns[:limit],
            "scene_selection": self.scene_selection_patterns[:limit],
            "pacing": self.pacing_patterns[:limit],
            "subtitles": self.subtitle_patterns[:limit],
            "emphasis": self.emphasis_patterns[:limit],
            "people": self.people_patterns[:limit],
            "content_type": self.content_type_patterns[:limit],
            "length_and_structure": self.length_structure_patterns[:limit],
            "response_and_structure": self.response_structure_patterns[:limit],
            "production_logic": self.production_logic[:limit],
            "titles": self.title_patterns[:limit],
            "thumbnails": self.thumbnail_patterns[:limit],
            "learned_from_own_performance": self.performance_learning[-limit:],
            "learned_from_human_edits": self.source_output_rules[-limit:],
            "caveat": "observed patterns, not rules; compare against this content before applying (4.5, 7.1)",
        }


def consolidate(analyses: list[dict[str, Any]]) -> ProductionKnowledge:
    """Fold per-video analyses into patterns, counting how often each recurs.

    4.6's point in code form: the value is in what several references share, not
    in reproducing any one of them.
    """
    knowledge = ProductionKnowledge(sample_size=len(analyses))

    def collect(key: str) -> list[dict[str, Any]]:
        counter: Counter[str] = Counter()
        for analysis in analyses:
            section = analysis.get(key)
            if isinstance(section, dict):
                for name, value in section.items():
                    counter[f"{name}={_stringify(value)}"] += 1
            elif isinstance(section, list):
                counter.update(_stringify(v) for v in section)
            elif section:
                counter[_stringify(section)] += 1
        return [
            {"pattern": pattern, "support": count, "share": round(count / max(1, len(analyses)), 3)}
            for pattern, count in counter.most_common(40)
        ]

    knowledge.structure_patterns = collect("structure")
    knowledge.editing_patterns = collect("editing")
    knowledge.storytelling_patterns = collect("storytelling")
    knowledge.scene_selection_patterns = collect("scene_selection")
    knowledge.pacing_patterns = collect("pacing")
    knowledge.subtitle_patterns = collect("subtitles")
    knowledge.emphasis_patterns = collect("emphasis")
    knowledge.people_patterns = collect("people")
    knowledge.content_type_patterns = collect("content_type")
    knowledge.length_structure_patterns = collect("length_and_structure")
    knowledge.response_structure_patterns = collect("response_and_structure")
    knowledge.production_logic = collect("production_logic")
    knowledge.title_patterns = [p["pattern"] for p in collect("title_pattern")]
    knowledge.thumbnail_patterns = [p["pattern"] for p in collect("thumbnail_pattern")]
    return knowledge


def _stringify(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)[:200]
    return str(value)[:200]
