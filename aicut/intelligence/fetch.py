"""Getting the reference material 4.2 asks for: the video, and the thumbnail.

4.2 lists 영상 and 썸네일 among what loop A collects, and 4.5 asks for a
썸네일 패턴. Neither is answerable from a URL string.

4.6 requires the media policy to be settled before MVP 1 and leaves the choice
to the operator. It is settled: what is fetched is kept, under the workspace,
and the operator holds the legal question. Nothing here deletes.

Both fetchers are optional. The engine runs on the stdlib and ffmpeg; a machine
that only renders needs neither, so a missing dependency says what to install
rather than crashing somewhere further in.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import urllib.request
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: 4.2 wants the thumbnail as the viewer saw it, so prefer the largest.
THUMBNAIL_PREFERENCE = ("maxres", "standard", "high", "medium", "default")


class FetchUnavailable(RuntimeError):
    """A fetcher was asked for and its dependency is not installed."""


def thumbnail_url(thumbnails: dict[str, Any]) -> str:
    """The largest thumbnail YouTube published for this video."""
    for size in THUMBNAIL_PREFERENCE:
        url = (thumbnails.get(size) or {}).get("url")
        if url:
            return url
    return ""


def fetch_thumbnail(thumbnails: dict[str, Any], into: str | Path, *, video_id: str) -> str:
    """Download the thumbnail image. Returns the path, or "" if there is none."""
    url = thumbnail_url(thumbnails)
    if not url:
        return ""
    target = Path(into) / f"{video_id}_thumbnail.jpg"
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with urllib.request.urlopen(url, timeout=30) as response:  # noqa: S310 - youtube i.ytimg.com
            target.write_bytes(response.read())
    except Exception as exc:
        log.warning("thumbnail for %s could not be fetched: %s", video_id, exc)
        return ""
    return str(target)


def have_downloader() -> bool:
    return shutil.which("yt-dlp") is not None


def fetch_video(video_id: str, into: str | Path, *, quality: str = "best[height<=720]") -> str:
    """Download one reference video with yt-dlp. Returns the path on disk.

    The operator chose to have the system fetch reference material as well as
    accept files by hand, so both paths exist. This is the fetched one.
    """
    if not have_downloader():
        raise FetchUnavailable(
            "yt-dlp is not on PATH. Install it (pip install 'aicut[download]') or "
            "pass the file yourself with --file ID=PATH."
        )
    directory = Path(into)
    directory.mkdir(parents=True, exist_ok=True)
    template = str(directory / f"{video_id}.%(ext)s")
    result = subprocess.run(
        ["yt-dlp", "-f", quality, "-o", template, "--no-playlist",
         f"https://www.youtube.com/watch?v={video_id}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"yt-dlp failed for {video_id}: {result.stderr.strip()[:400]}")
    found = sorted(directory.glob(f"{video_id}.*"))
    playable = [p for p in found if p.suffix.lower() not in {".json", ".txt", ".part"}]
    if not playable:
        raise RuntimeError(f"yt-dlp reported success but wrote nothing for {video_id}")
    return str(playable[0])

