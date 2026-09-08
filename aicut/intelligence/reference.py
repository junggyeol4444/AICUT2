"""Loop A: learning how videos are actually being made (4장, 12.3 A).

Scope is deliberately narrow at the start (4.1): the neighbourhood the channel
actually competes in, not YouTube at large.

The data policy of 4.6 is enforced by construction. This module reads metadata
and public metrics through the API, sends *that* to the analysis step, and stores
only the resulting patterns. It never downloads a reference video, and the
reference table has no column to keep one in. Anything an analysis needs beyond
metadata has to be supplied by the operator for material they are entitled to
analyse, and is discarded after the analysis returns.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Sequence

from pathlib import Path

from aicut.config import CalibrationProfile
from aicut.db.store import Store
from aicut.intelligence.knowledge import ProductionKnowledge, consolidate
from aicut.intelligence.youtube import YouTubeClient
from aicut.llm import Producer

log = logging.getLogger(__name__)

# 4.1: start narrow, widen later if it earns it.
DEFAULT_QUERIES = [
    "게임 스트리머 편집 영상",
    "생방송 하이라이트 편집",
    "합방 하이라이트",
    "스트리머 리액션 모음",
    "게임 방송 다시보기 편집",
]


def collect_references(
    client: YouTubeClient,
    queries: Sequence[str] = DEFAULT_QUERIES,
    *,
    per_query: int = 25,
    **search_params: Any,
) -> list[dict[str, Any]]:
    """Search, then fetch public metrics. Roughly 100 + 1 units per query (11.4)."""
    seen: set[str] = set()
    references: list[dict[str, Any]] = []
    for query in queries:
        try:
            ids = [vid for vid in client.search(query, max_results=per_query, **search_params) if vid not in seen]
        except Exception as exc:
            log.warning("reference search %r failed: %s", query, exc)
            continue
        seen.update(ids)
        for record in client.public_metrics(ids):
            record["found_by"] = query
            references.append(record)
    return references


def watch(
    path: str,
    profile: CalibrationProfile,
    *,
    frames_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Read one reference video so the analysis can look at it (4.2, 4.3).

    4.2 lists 영상 first among the things collected, and every item 4.3 asks
    about — 컷, 평균 장면 길이, 화면 전환, 자막, 강조, 효과 — is on the screen,
    not in the title.

    18장 decides what happens here. 영상 디코딩 and 파일 관리 are the program's,
    so this decodes the file and samples frames across it. 편집 의도 is the AI's,
    so nothing here counts a cut or scores a scene; the frames go to the
    analysis and it says what the editing is.

    4.6 governs the file: `analyze` deletes the frames once the answer is back.
    """
    from aicut.media import probe as probe_mod
    from aicut.media import vision as vision_mod

    media = probe_mod.probe(path)
    interval = profile.get_float("scan.pass1_frame_interval_sec")
    frames: list[str] = []
    if frames_dir is not None:
        samples = vision_mod.sample_frames(
            path, Path(frames_dir), interval_sec=interval, prefix="ref",
        )
        frames = [f.path for f in samples]
    return {"frames": frames, "duration_sec": media.duration_sec}


def analyze(
    producer: Producer,
    store: Store,
    references: Iterable[dict[str, Any]],
    *,
    extra_context: dict[str, dict[str, Any]] | None = None,
    watched: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Ask why each reference was made the way it was, and keep only the answer.

    ``extra_context`` carries anything the operator supplied for material they may
    analyse (their own transcript of a video, notes on its edit). It is passed to
    the analysis and never written to the database - 4.6.

    ``watched`` carries the frames :func:`watch` sampled from a video the
    operator supplied the file for. They are shown to the analysis and then
    deleted: 4.6 keeps the patterns and not the media.
    """
    analyses: list[dict[str, Any]] = []
    for reference in references:
        payload = {
            "video": {
                "title": reference.get("title", ""),
                "description": reference.get("description", "")[:2000],
                "tags": reference.get("tags", []),
                "duration": reference.get("duration", ""),
                "published_at": reference.get("published_at", ""),
            },
            "public_metrics": reference.get("public_metrics", {}),
            "context": (extra_context or {}).get(reference.get("video_id", ""), {}),
            "note": "public metrics only; retention and CTR are unavailable for other channels (4.2)",
        }
        seen = (watched or {}).get(reference.get("video_id", ""), {})
        frames = list(seen.get("frames", []))
        if frames:
            payload["watched"] = {"duration_sec": seen.get("duration_sec", 0.0),
                                  "frame_count": len(frames)}
            payload["note"] += ("; the images are frames sampled across this video, in order."
                                " 4.3 asks what its editing is - 컷, 평균 장면 길이, 화면 전환,"
                                " 자막, 강조, 효과 - and 4.4 asks why it was cut that way")
        try:
            analysis = producer.analyze_reference(payload, images=frames)
        except Exception as exc:
            log.warning("analysis failed for %s: %s", reference.get("video_id"), exc)
            continue
        finally:
            # 4.6: the analysis is kept, the media is not.
            _discard(frames)
        store.save_reference(
            reference.get("video_id", ""),
            reference.get("channel_id", ""),
            reference.get("public_metrics", {}),
            analysis,
        )
        analyses.append(analysis)
    return analyses


def _discard(frames: Iterable[str]) -> None:
    """Delete the frames a reference was read from (4.6)."""
    for path in frames:
        try:
            Path(path).unlink(missing_ok=True)
        except OSError as exc:
            log.warning("could not discard reference frame %s: %s", path, exc)


def build_knowledge(store: Store) -> ProductionKnowledge:
    """Consolidate every stored reference analysis into production knowledge (4.5)."""
    return consolidate([r["extracted_patterns"] for r in store.references() if r["extracted_patterns"]])
