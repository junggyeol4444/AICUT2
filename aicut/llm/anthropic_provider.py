"""Anthropic-backed reasoning provider.

Only the transport lives here; every judgement is defined by the task prompts in
:mod:`aicut.llm.prompts`. Requests are retried on transient failures and the raw
exchange can be logged to disk, because a production judgement that a human
reviewer disagrees with (15.4) is only auditable if the payload that produced it
was kept.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Sequence

from aicut.errors import ProviderError
from aicut.llm.base import Producer, parse_json_block

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-sonnet-5"


#: What a frame is sent as. The API takes base64 JPEG/PNG inline.
_MEDIA_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                ".webp": "image/webp"}

#: Bytes above which a single frame is skipped rather than sent. A 6-hour pass
#: is many calls; one oversized frame that fails the request would take a whole
#: window's judgement with it.
_MAX_IMAGE_BYTES = 4 * 1024 * 1024


def _image_blocks(images: Sequence[str]) -> list[dict[str, Any]]:
    """Turn frame paths into inline image blocks, skipping what cannot be sent."""
    blocks: list[dict[str, Any]] = []
    for path in images:
        target = Path(path)
        media_type = _MEDIA_TYPES.get(target.suffix.lower())
        if not media_type:
            log.warning("frame %s is not a sendable image type; skipped", target)
            continue
        try:
            raw = target.read_bytes()
        except OSError as exc:
            log.warning("could not read frame %s: %s", target, exc)
            continue
        if len(raw) > _MAX_IMAGE_BYTES:
            log.warning("frame %s is %d bytes; skipped", target, len(raw))
            continue
        blocks.append({
            "type": "image",
            "source": {"type": "base64", "media_type": media_type,
                       "data": base64.standard_b64encode(raw).decode("ascii")},
        })
    return blocks


class AnthropicProducer(Producer):
    name = "anthropic"

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        *,
        api_key: str | None = None,
        max_tokens: int = 8000,
        max_retries: int = 3,
        transcript_dir: str | os.PathLike[str] | None = None,
    ):
        try:
            import anthropic  # optional dependency
        except ImportError as exc:  # pragma: no cover - optional dep
            raise ProviderError(
                "the anthropic package is not installed; install aicut[llm] or use --producer mock"
            ) from exc
        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise ProviderError("ANTHROPIC_API_KEY is not set")
        self._client = anthropic.Anthropic(api_key=key)
        self._errors = anthropic
        self.model = model
        self.max_tokens = max_tokens
        self.max_retries = max_retries
        self.transcript_dir = Path(transcript_dir) if transcript_dir else None
        if self.transcript_dir:
            self.transcript_dir.mkdir(parents=True, exist_ok=True)

    def complete_json(
        self, task: str, system: str, payload: dict[str, Any],
        *, images: Sequence[str] = (),
    ) -> Any:
        body = json.dumps(payload, ensure_ascii=False, default=str)
        content = _image_blocks(images) + [{"type": "text", "text": body}]
        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                response = self._client.messages.create(
                    model=self.model,
                    max_tokens=self.max_tokens,
                    system=system,
                    # Pictures first, then the numbers and the words about
                    # them: 5.2 has the passes read screen and sound together,
                    # and a caption that arrives before its picture is read as
                    # a description instead of a question about one.
                    messages=[{"role": "user", "content": content}],
                )
            except Exception as exc:  # transport / rate limit / overload
                if not self._worth_retrying(exc):
                    # A bad key or a malformed request fails the same way three
                    # times. Retrying it wastes the caller's minutes and buries
                    # the one line that says what to fix.
                    raise ProviderError(f"task {task!r}: {self._explain(exc)}") from exc
                last_error = exc
                if attempt == self.max_retries - 1:
                    break
                wait = 2 ** (attempt + 1)
                log.warning("task %s failed (%s); retrying in %ss", task, exc, wait)
                time.sleep(wait)
                continue
            text = "".join(block.text for block in response.content if getattr(block, "type", "") == "text")
            self._log_exchange(task, body, text)
            if getattr(response, "stop_reason", None) == "max_tokens":
                # The reply was cut mid-JSON. Without this the failure surfaces
                # as "no parsable JSON", which sends the reader looking at the
                # prompt instead of at the limit that actually stopped it.
                raise ProviderError(
                    f"task {task!r}: the reply hit the {self.max_tokens}-token limit and was cut "
                    "off mid-answer. Raise max_tokens, or give the task less at once "
                    "(a shorter broadcast, or fewer candidates per call)."
                )
            return parse_json_block(text)
        raise ProviderError(f"task {task!r} failed after {self.max_retries} attempts: {last_error}")

    def _worth_retrying(self, exc: Exception) -> bool:
        """Only failures that a second identical request could survive.

        Overload, rate limits and dropped connections pass; a rejected key or a
        request the API refused to parse will be rejected again just as fast.
        """
        for name in ("AuthenticationError", "PermissionDeniedError", "NotFoundError",
                     "BadRequestError", "UnprocessableEntityError"):
            kind = getattr(self._errors, name, None)
            if kind is not None and isinstance(exc, kind):
                return False
        return True

    def _explain(self, exc: Exception) -> str:
        """Say what to do about it, not only that it happened."""
        auth = getattr(self._errors, "AuthenticationError", None)
        denied = getattr(self._errors, "PermissionDeniedError", None)
        missing = getattr(self._errors, "NotFoundError", None)
        if auth is not None and isinstance(exc, auth):
            return f"the API rejected ANTHROPIC_API_KEY ({exc})"
        if denied is not None and isinstance(exc, denied):
            return f"this key is not allowed to use {self.model} ({exc})"
        if missing is not None and isinstance(exc, missing):
            return (
                f"no model named {self.model!r}. Pass a model this key can reach, "
                f"or leave it unset to use {DEFAULT_MODEL}. ({exc})"
            )
        return str(exc)

    def _log_exchange(self, task: str, request: str, reply: str) -> None:
        if not self.transcript_dir:
            return
        path = self.transcript_dir / f"{int(time.time() * 1000)}_{task}.json"
        path.write_text(
            json.dumps({"task": task, "model": self.model, "request": request, "reply": reply}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
