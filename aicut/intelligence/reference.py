"""Loop A: learning how videos are actually being made (4장, 12.3 A).

Scope is deliberately narrow at the start (4.1): the neighbourhood the channel
actually competes in, not YouTube at large.

4.6 says the media policy has to be settled before MVP 1 starts, and leaves the
decision to the operator. It is settled: **the media is kept.** The operator
supplies the reference material or has the system fetch it, holds the legal
question themselves, and nothing here deletes a file after reading it. What 4.6
rules out is still ruled out - reproducing one video. That is 4.5's job and it
consolidates across references.

So this module fetches metadata and public metrics, reads the video and its
thumbnail when they are available, hands all of it to the analysis, and keeps
what it downloaded under the workspace.
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

# 4.1 names the neighbourhood to start in, and these are its terms, not a set
# invented here. The original spec (6.1) adds 스트리머 콘텐츠 to the same list.
DEFAULT_QUERIES = [
    "게임 스트리머",
    "인터넷 방송",
    "생방송 편집 영상",
    "게임 하이라이트",
    "합방",
    "리액션",
    "토크",
    "예능형 콘텐츠",
    "스트리머 콘텐츠",
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
    # The scan interval is for the source being edited, where every window is
    # looked at separately. A reference is read in one go, so the same interval
    # put a whole video's frames into a single call: 36 minutes at 5s is 432
    # images, and a long stream is thousands. The cap spreads the same coverage
    # over as many frames as the analysis can actually be shown, by widening the
    # interval rather than decoding frames and then throwing them away.
    cap = profile.get_int("scan.reference_frame_cap")
    if cap > 0 and media.duration_sec > 0:
        interval = max(interval, media.duration_sec / cap)
    frames: list[str] = []
    if frames_dir is not None:
        samples = vision_mod.sample_frames(
            path, Path(frames_dir),
            start_sec=0.0, duration_sec=media.duration_sec,
            interval_sec=interval, prefix="ref",
        )
        frames = [f.path for f in samples]
    return {
        "frames": frames,
        "duration_sec": media.duration_sec,
        "frame_interval_sec": interval,
    }


def watch_all(
    references: Sequence[dict[str, Any]],
    profile: CalibrationProfile,
    workspace: str | Path,
    *,
    files: dict[str, str] | None = None,
    download: bool = False,
    thumbnails: bool = True,
    comments: bool = False,
    comment_limit: int | None = None,
) -> dict[str, dict[str, Any]]:
    """Get hold of what 4.2 collects for each reference, and read it.

    The operator settled 4.6 both ways: they hand over files, and the system
    fetches the rest. ``files`` is what they handed over, ``download`` turns the
    fetcher on for everything else. Neither is required - a reference with no
    video still gets analysed from its metadata, which is all 4.2 guarantees for
    another channel's video anyway.
    """
    from aicut.intelligence import fetch as fetch_mod

    supplied = dict(files or {})
    root = Path(workspace) / "references"
    watched: dict[str, dict[str, Any]] = {}
    for reference in references:
        video_id = reference.get("video_id", "")
        if not video_id:
            continue
        seen: dict[str, Any] = {}
        if thumbnails:
            # 4.5 asks for a 썸네일 패턴, and that needs the picture.
            shot = fetch_mod.fetch_thumbnail(
                reference.get("thumbnails", {}), root / video_id, video_id=video_id,
            )
            if shot:
                seen["thumbnail"] = shot
        if comments:
            # 4.2 collects 댓글, and the original 6.2 widens that to 시청자 반응
            # 데이터. Read through the browser, not the API, so this does not
            # eat the quota 11.4 rations for search and upload.
            from aicut.intelligence import comments as comments_mod
            try:
                said = comments_mod.fetch(video_id, limit=comment_limit)
                if said:
                    seen["comments"] = said
            except Exception as exc:
                log.warning("could not read comments for %s: %s", video_id, exc)
        path = supplied.get(video_id)
        if path is None and download:
            try:
                path = fetch_mod.fetch_video(video_id, root / video_id)
            except Exception as exc:
                log.warning("could not fetch %s: %s", video_id, exc)
                path = None
        if path:
            try:
                seen.update(watch(path, profile, frames_dir=root / video_id / "frames"))
                seen["video_path"] = path
            except Exception as exc:
                log.warning("could not read %s: %s", path, exc)
        if seen:
            watched[video_id] = seen
    return watched


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

    ``watched`` carries what :func:`watch` read from a video the operator
    supplied or had fetched: frames from the video, and the thumbnail. Both are
    shown to the analysis and both stay on disk afterwards - see the module
    docstring for the 4.6 decision.
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
            "channel": {
                "channel_id": reference.get("channel_id", ""),
                "channel_title": reference.get("channel_title", ""),
            },
            "category_id": reference.get("category_id", ""),
            "public_metrics": reference.get("public_metrics", {}),
            "context": (extra_context or {}).get(reference.get("video_id", ""), {}),
            "note": "public metrics only; retention and CTR are unavailable for other channels (4.2)",
        }
        seen = (watched or {}).get(reference.get("video_id", ""), {})
        # The thumbnail leads: 4.5 asks how it relates to the video behind it,
        # which is a question about the order they are seen in.
        said = seen.get("comments", [])
        if said:
            # 원본 8장 asks about 시청자 반응과 영상 구성의 관계, which needs what
            # viewers actually said, not how many of them said something.
            payload["comments"] = {
                "count_read": len(said),
                "comments": [
                    {"text": c.get("text", "")[:600], "likes": c.get("likes", "")}
                    for c in said
                ],
            }
        thumbnail = seen.get("thumbnail", "")
        frames = list(seen.get("frames", []))
        images = ([thumbnail] if thumbnail else []) + frames
        if images:
            payload["watched"] = {
                "duration_sec": seen.get("duration_sec", 0.0),
                "thumbnail": bool(thumbnail),
                "frame_count": len(frames),
            }
            payload["note"] += (
                "; the images are this video's own material."
                + (" The first is its thumbnail." if thumbnail else "")
                + (f" The remaining {len(frames)} are frames sampled across the video"
                   f" every {seen.get('frame_interval_sec', 0.0):.1f}s, in time order."
                   if frames else "")
                + " 4.3 asks what its editing is - 컷, 평균 장면 길이, 확대, 크롭, 화면 전환,"
                  " 자막, 강조, 효과, 효과음, BGM, 이미지, 밈, 리플레이 - and who the video"
                  " is about. 4.4 asks why it was made that way"
            )
        try:
            analysis = producer.analyze_reference(payload, images=images)
        except Exception as exc:
            log.warning("analysis failed for %s: %s", reference.get("video_id"), exc)
            continue
        store.save_reference(
            reference.get("video_id", ""),
            reference.get("channel_id", ""),
            reference.get("public_metrics", {}),
            analysis,
        )
        analyses.append(analysis)
    return analyses


#: 19장 MVP 1 is scored on whether a person who watched the video says the
#: analysis matches why it was made. Agree and disagree are the whole answer;
#: "unsure" is not a third judgement, it is not having made one, so it is left
#: unrecorded rather than stored as a value that quietly dilutes the rate.
REFERENCE_VERDICTS = ("agree", "disagree")


def record_reference_verdict(store: Store, ref_id: str, verdict: str, note: str = "") -> None:
    """19장 MVP 1: 사람이 봤을 때 분석이 실제 제작 의도와 일치하는가."""
    if verdict not in REFERENCE_VERDICTS:
        raise ValueError(f"verdict must be one of {', '.join(REFERENCE_VERDICTS)}")
    store.set_reference_verdict(ref_id, verdict if not note else f"{verdict}: {note}")


def reference_agreement(store: Store) -> dict[str, Any]:
    """The MVP 1 gate: the 비율 19장 asks for, and nothing more.

    19장 says "일치한다고 판단되는 비율 확보" without naming the ratio that
    counts as secured. Deciding one here would be inventing the gate rather
    than measuring it.
    """
    references = store.references()
    judged = [r for r in references if r.get("human_verdict")]
    agreed = sum(1 for r in judged if str(r["human_verdict"]).startswith("agree"))
    return {
        "analysed": len(references),
        "judged": len(judged),
        "agreed": agreed,
        "agreement": round(agreed / len(judged), 3) if judged else None,
    }


def build_knowledge(store: Store) -> ProductionKnowledge:
    """Consolidate every stored reference analysis into production knowledge (4.5)."""
    return consolidate([r["extracted_patterns"] for r in store.references() if r["extracted_patterns"]])
