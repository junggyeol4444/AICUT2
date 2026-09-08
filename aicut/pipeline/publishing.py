"""Upload and publication (11.3, 11.4, 16장).

The order is fixed: upload private -> a person reviews -> the video goes public.
An unapproved episode cannot reach the public path from here; that is the gate,
expressed in code rather than in a policy document.

When the quota runs out the episode is not lost. It is kept locally, queued, and
scheduled against the next PT midnight (11.4).
"""

from __future__ import annotations

import logging
from typing import Any

from aicut.errors import ConfigError, QuotaExceeded
from aicut.intelligence.quota import QuotaLedger
from aicut.intelligence.youtube import YouTubeClient
from aicut.models import Episode
from aicut.pipeline.context import RunContext
from aicut.pipeline.states import State

log = logging.getLogger(__name__)


#: 11.3 fixes the order: upload non-public, a person reviews, then it goes
#: public. Anything else uploaded straight to `public` would skip the gate
#: entirely - `publish_approved` never runs, so its refusal never fires.
PRE_REVIEW_PRIVACY = ("private", "unlisted")


def _pre_review_privacy(value: Any) -> str:
    """The visibility an upload may have before a person has approved it.

    Read from the calibration profile, which is editable JSON with no schema, so
    it is checked here rather than trusted. 11.3 allows enabling automatic
    publication later, but that is a step *after* review; it is not licence to
    upload public in the first place.
    """
    privacy = str(value or "").strip().lower()
    if privacy not in PRE_REVIEW_PRIVACY:
        raise ConfigError(
            f"upload.privacy_on_upload is {value!r}; 11.3 requires an upload to be "
            f"{' or '.join(PRE_REVIEW_PRIVACY)} until a person has approved it. "
            "Publication happens through `aicut upload --publish` after review."
        )
    return privacy


def upload_episode(
    ctx: RunContext,
    episode: Episode,
    client: YouTubeClient,
    *,
    thumbnail_path: str | None = None,
) -> dict[str, Any]:
    """Upload one episode privately and record the result."""
    if not episode.output_mp4_path:
        raise ValueError(f"episode {episode.episode_id} has not been rendered")

    privacy = _pre_review_privacy(ctx.profile.get("upload.privacy_on_upload"))
    # 원본 24장's 업로드 정보, written by the packaging step. The profile
    # supplies the fallback category so nothing is hardcoded in the client (2.3).
    upload_info = episode.metadata.get("upload") or {}
    metadata = {
        "title": (episode.title_candidates or ["untitled"])[0],
        "description": episode.metadata.get("description", ""),
        "tags": episode.metadata.get("tags", []),
        # Defaulted rather than required: a profile written before these keys
        # existed still uploads, and an absent category means YouTube's own
        # default rather than a refusal.
        "category_id": upload_info.get("category_id")
        or ctx.profile.get("upload.default_category_id", ""),
        "language": upload_info.get("language")
        or ctx.profile.get("upload.default_language", ""),
    }
    try:
        result = client.upload(episode.output_mp4_path, metadata, privacy_status=privacy)
    except QuotaExceeded as exc:
        ctx.store.enqueue_upload(
            episode.episode_id,
            retry_after=exc.reset_at.isoformat() if exc.reset_at else None,
            error=str(exc),
        )
        episode.review_status = "upload_queued"
        ctx.store.save_episode(episode)
        ctx.report.setdefault("upload_queue", []).append({
            "episode_id": episode.episode_id,
            "retry_after": exc.reset_at.isoformat() if exc.reset_at else None,
            "reason": str(exc),
        })
        log.warning("quota exhausted; %s stays local and is queued", episode.episode_id)
        raise

    # Record the video before anything optional is attempted. A thumbnail that
    # fails - the quota can run out between the two calls, since thumbnails.set
    # books its own units - must not lose the id of a video that now exists:
    # the retry would upload a second copy and spend another 1,600 units on it.
    episode.metadata = dict(episode.metadata)
    episode.metadata["youtube"] = {
        "video_id": result.video_id,
        "url": result.url,
        "privacy_status": result.privacy_status,
    }
    episode.review_status = "pending" if ctx.profile.get("upload.require_human_review") else "approved"
    ctx.store.save_episode(episode)

    chosen = thumbnail_path or (episode.thumbnail_candidates[0] if episode.thumbnail_candidates else None)
    if chosen:
        try:
            client.set_thumbnail(result.video_id, chosen)
        except Exception as exc:
            # The upload stands and is recorded; say what is missing from it.
            log.warning("uploaded %s but could not set its thumbnail: %s", episode.episode_id, exc)
            ctx.report.setdefault("degraded", []).append({
                "episode_id": episode.episode_id,
                "reason": "thumbnail_not_set",
                "detail": f"{type(exc).__name__}: {exc}",
                "video_id": result.video_id,
            })
        else:
            episode.thumbnail_path = chosen
            ctx.store.save_episode(episode)
    return episode.metadata["youtube"]


def publish_approved(ctx: RunContext, episode: Episode, client: YouTubeClient) -> Episode:
    """Make a reviewed episode public. Refuses anything the gate has not passed."""
    if episode.review_status != "approved":
        raise PermissionError(
            f"episode {episode.episode_id} is '{episode.review_status}', not 'approved'; "
            "the human review gate has not been passed (11.3)"
        )
    youtube = episode.metadata.get("youtube", {})
    video_id = youtube.get("video_id")
    if not video_id:
        raise ValueError(f"episode {episode.episode_id} has not been uploaded yet")

    client.set_privacy(video_id, "public")
    episode.metadata = dict(episode.metadata)
    episode.metadata["youtube"] = {**youtube, "privacy_status": "public"}
    episode.review_status = "published"
    ctx.store.save_episode(episode)
    _advance_project_if_settled(ctx)
    return episode


#: Review outcomes that mean nobody is waiting on this episode any more. A
#: rejected episode is finished too - it is not going out, and holding the
#: project open for it would leave a project that can never close.
_SETTLED = {"published", "rejected"}


def _advance_project_if_settled(ctx: RunContext) -> None:
    """Move the project to PUBLISHED once every episode has been dealt with.

    14장 lists PUBLISHED as the end of the walk, and nothing ever set it: the
    episode's own review_status changed and `tb_project` stayed REVIEW_PENDING
    for good. `aicut status`, the project list and the UI therefore showed
    finished work as still awaiting review, and the terminal state was
    unreachable.

    Only when *every* episode is settled: a project with two episodes, one
    published and one still pending, is still waiting on a person.
    """
    episodes = ctx.store.episodes(ctx.project.project_id)
    if not episodes:
        return
    if any(e.review_status not in _SETTLED for e in episodes):
        return
    if not any(e.review_status == "published" for e in episodes):
        # Everything was rejected. That is a finished project, but calling it
        # PUBLISHED would say something untrue about what is on the channel.
        return
    ctx.store.set_status(
        ctx.project.project_id, State.PUBLISHED.value,
        f"{sum(1 for e in episodes if e.review_status == 'published')} of "
        f"{len(episodes)} episodes published",
    )


def process_retry_queue(ctx: RunContext, client: YouTubeClient, ledger: QuotaLedger) -> list[str]:
    """Retry queued uploads once the PT day has actually rolled over (11.4)."""
    from datetime import datetime

    now = ledger.pt_now()
    done: list[str] = []
    # This context carries one project's profile, and upload_episode reads it
    # for privacy, language and category. Draining the whole queue through it
    # would publish other projects' episodes under this project's settings.
    for row in ctx.store.upload_queue(project_id=ctx.project.project_id):
        retry_after = row.get("retry_after")
        if retry_after and datetime.fromisoformat(retry_after) > now:
            continue
        episode = ctx.store.get_episode(row["episode_id"])
        if episode is None:
            ctx.store.set_queue_state(row["queue_id"], "abandoned", "episode no longer exists")
            continue
        if episode.project_id != ctx.project.project_id:   # belt and braces
            continue
        try:
            upload_episode(ctx, episode, client)
        except QuotaExceeded as exc:
            ctx.store.set_queue_state(row["queue_id"], "RETRY_QUEUED", str(exc))
            break               # the day's allowance is gone again; stop trying
        else:
            ctx.store.set_queue_state(row["queue_id"], "uploaded")
            done.append(episode.episode_id)
    return done
