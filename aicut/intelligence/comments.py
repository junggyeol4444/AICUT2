"""Reading a reference video's comments (4.2, 원본 6.2).

4.2 lists 댓글 among what loop A collects, and the original 6.2 widens it to
"가능한 범위의 시청자 반응 데이터". The Data API can return comment threads, but
the operator ruled that out: the comments are read through a browser instead,
so the collection does not consume the quota 11.4 has to ration for search and
upload.

There is no cap by default. The operator asked for all of them, and a video
with a million comments has a million comments; ``limit`` exists so a caller
that only wants a sample can say so.

Playwright is an optional dependency, like every other stage. A missing one
says what to install rather than failing somewhere further in.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

#: YouTube loads comments as the page scrolls. This is how long to wait for a
#: batch to arrive before deciding the end has been reached.
_SETTLE_MS = 1500
_QUIET_ROUNDS = 3


class BrowserUnavailable(RuntimeError):
    """Playwright is not installed."""


def available() -> bool:
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return False
    return True


def fetch(
    video_id: str,
    *,
    limit: int | None = None,
    headless: bool = True,
    timeout_sec: float = 600.0,
) -> list[dict[str, Any]]:
    """Every comment on one video, read from the page.

    Returns dicts of author, text, likes and time, in the order the page served
    them - which is YouTube's own "top comments" order, so a caller taking a
    sample gets the ones viewers actually saw.
    """
    if not available():
        raise BrowserUnavailable(
            "playwright is not installed. pip install 'aicut[browser]' and, if the "
            "browser itself is missing, python -m playwright install chromium"
        )
    from playwright.sync_api import sync_playwright

    collected: list[dict[str, Any]] = []
    with sync_playwright() as engine:
        browser = engine.chromium.launch(headless=headless)
        try:
            page = browser.new_page()
            page.set_default_timeout(timeout_sec * 1000)
            page.goto(f"https://www.youtube.com/watch?v={video_id}")
            _dismiss_consent(page)
            page.wait_for_selector("ytd-comments", timeout=timeout_sec * 1000)
            quiet = 0
            seen = 0
            while quiet < _QUIET_ROUNDS:
                page.mouse.wheel(0, 4000)
                page.wait_for_timeout(_SETTLE_MS)
                count = page.locator("ytd-comment-thread-renderer").count()
                if count == seen:
                    quiet += 1
                else:
                    quiet = 0
                    seen = count
                if limit is not None and count >= limit:
                    break
            collected = _read_threads(page, limit)
        finally:
            browser.close()
    log.info("read %d comments for %s", len(collected), video_id)
    return collected


def _dismiss_consent(page) -> None:
    """The cookie wall sits in front of the comments in some regions."""
    for label in ("Accept all", "모두 수락", "I agree"):
        try:
            button = page.get_by_role("button", name=label)
            if button.count():
                button.first.click(timeout=3000)
                return
        except Exception:  # the wall is absent, which is the common case
            continue


def _read_threads(page, limit: int | None) -> list[dict[str, Any]]:
    threads = page.locator("ytd-comment-thread-renderer")
    total = threads.count() if limit is None else min(limit, threads.count())
    out: list[dict[str, Any]] = []
    for index in range(total):
        thread = threads.nth(index)
        out.append({
            "author": _text(thread, "#author-text"),
            "text": _text(thread, "#content-text"),
            "likes": _text(thread, "#vote-count-middle"),
            "published": _text(thread, ".published-time-text"),
        })
    return out


def _text(scope, selector: str) -> str:
    try:
        found = scope.locator(selector)
        return found.first.inner_text().strip() if found.count() else ""
    except Exception:
        return ""
