"""Ollama-backed reasoning provider — the judgement runs on the operator's own machine.

18장 says where the boundary is, not where the AI lives. 20.1's stack names no
model service at all, so running it locally is as much in-spec as calling out,
and it is the only option for an operator who will not send hours of their own
broadcast to somebody else's server.

**The model has to take images.** 5.2 says the passes read 화면 and 소리 together
and 4.3 asks what is on the screen; a text-only model turns this back into the
speech-only editor 1.2 rejects. `llava`, `qwen2.5vl`, `gemma3` and `llama3.2-vision`
take images; `llama3`, `qwen2.5` and `mistral` do not. :meth:`check` says which
one is loaded before a six-hour run finds out the hard way.

Transport is stdlib urllib: the engine runs on the standard library and ffmpeg,
and a local HTTP call does not justify a dependency.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Sequence

from aicut.errors import ProviderError
from aicut.llm.base import Producer, parse_json_block

log = logging.getLogger(__name__)

DEFAULT_HOST = "http://localhost:11434"
DEFAULT_MODEL = "qwen2.5vl:7b"

#: Substrings of model names known to accept images. Checked against the name
#: the operator passed, so a tag or a size suffix does not defeat it. A model
#: not on this list is not refused - the list is what is known, not what exists
#: - but it is called out, because the failure it prevents is silent: an image
#: sent to a text-only model is dropped and the pass answers from the words.
_VISION_HINTS = (
    "llava", "bakllava", "moondream", "vision", "-vl", "qwen2.5vl", "qwen2-vl",
    "gemma3", "minicpm-v", "internvl", "pixtral", "granite3.2-vision",
)


def _image_data(images: Sequence[str]) -> list[str]:
    """Frames as base64, which is how Ollama's /api/chat takes them."""
    out: list[str] = []
    for path in images:
        target = Path(path)
        try:
            out.append(base64.standard_b64encode(target.read_bytes()).decode("ascii"))
        except OSError as exc:
            log.warning("could not read frame %s: %s", target, exc)
    return out


def looks_like_a_vision_model(model: str) -> bool:
    lowered = model.lower()
    return any(hint in lowered for hint in _VISION_HINTS)


class OllamaProducer(Producer):
    name = "ollama"

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        *,
        host: str | None = None,
        max_retries: int = 3,
        timeout_sec: float = 600.0,
        num_ctx: int | None = None,
        transcript_dir: str | os.PathLike[str] | None = None,
        warn_on_text_only: bool = True,
    ):
        self.model = model
        self.host = (host or os.environ.get("OLLAMA_HOST") or DEFAULT_HOST).rstrip("/")
        if "://" not in self.host:                 # OLLAMA_HOST is often bare host:port
            self.host = f"http://{self.host}"
        self.max_retries = max_retries
        self.timeout_sec = timeout_sec
        self.num_ctx = num_ctx
        self.transcript_dir = Path(transcript_dir) if transcript_dir else None
        if self.transcript_dir:
            self.transcript_dir.mkdir(parents=True, exist_ok=True)
        if warn_on_text_only and not looks_like_a_vision_model(model):
            log.warning(
                "%r is not a model this knows to take images. 5.2 has the passes read screen "
                "and sound together; a text-only model silently drops the frames and judges "
                "from the words alone, which is the editor 1.2 rejects. Try llava, "
                "qwen2.5vl or gemma3 if that is not what you want.", model,
            )

    # -- transport -----------------------------------------------------------
    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{self.host}{path}",
            data=json.dumps(body, ensure_ascii=False, default=str).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=self.timeout_sec) as response:
            return json.loads(response.read().decode("utf-8"))

    def check(self) -> dict[str, Any]:
        """Is Ollama up, is the model pulled, and does it take images?

        Called before a run rather than during one: a six-hour broadcast should
        not reach its first window before finding out the model was never
        pulled.
        """
        try:
            with urllib.request.urlopen(f"{self.host}/api/tags", timeout=15) as response:
                tags = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            raise ProviderError(
                f"no Ollama at {self.host} ({exc}). Start it with `ollama serve`, or point "
                "--ollama-host / OLLAMA_HOST at the machine running it."
            ) from exc
        names = [m.get("name", "") for m in tags.get("models", [])]
        loaded = any(n == self.model or n.split(":")[0] == self.model.split(":")[0] for n in names)
        if not loaded:
            raise ProviderError(
                f"Ollama at {self.host} has no model {self.model!r}. Pull it with "
                f"`ollama pull {self.model}`. Present: {', '.join(names) or 'none'}"
            )
        return {
            "host": self.host,
            "model": self.model,
            "available_models": names,
            "takes_images": looks_like_a_vision_model(self.model),
        }

    # -- the one method every task goes through ------------------------------
    def complete_json(
        self, task: str, system: str, payload: dict[str, Any],
        *, images: Sequence[str] = (),
    ) -> Any:
        body_text = json.dumps(payload, ensure_ascii=False, default=str)
        message: dict[str, Any] = {"role": "user", "content": body_text}
        data = _image_data(images)
        if data:
            message["images"] = data
        request: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, message],
            "stream": False,
            # Ollama can constrain the reply to JSON. Every task here returns
            # JSON, so this removes a whole class of failure rather than
            # catching it afterwards.
            "format": "json",
            "options": {"temperature": 0.2},
        }
        if self.num_ctx:
            request["options"]["num_ctx"] = self.num_ctx

        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                reply = self._post("/api/chat", request)
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:400]
                if exc.code == 404:
                    raise ProviderError(
                        f"task {task!r}: Ollama has no model {self.model!r} "
                        f"(pull it with `ollama pull {self.model}`). {detail}"
                    ) from exc
                if 400 <= exc.code < 500:
                    # A request this server refuses to parse will be refused
                    # again just as fast; retrying buries the line that says why.
                    raise ProviderError(f"task {task!r}: Ollama rejected the request: {detail}") from exc
                last_error = exc
            except Exception as exc:                    # connection, timeout
                last_error = exc
            else:
                text = (reply.get("message") or {}).get("content", "")
                self._log_exchange(task, body_text, text)
                if not text.strip():
                    raise ProviderError(
                        f"task {task!r}: {self.model} returned an empty reply. A model that "
                        "cannot hold this much context often answers with nothing - try "
                        "--ollama-num-ctx, or a smaller window (scan.pass1_window_sec)."
                    )
                return parse_json_block(text)
            if attempt == self.max_retries - 1:
                break
            wait = 2 ** (attempt + 1)
            log.warning("task %s failed (%s); retrying in %ss", task, last_error, wait)
            time.sleep(wait)
        raise ProviderError(
            f"task {task!r} failed after {self.max_retries} attempts against {self.host}: {last_error}"
        )

    def _log_exchange(self, task: str, request: str, reply: str) -> None:
        if not self.transcript_dir:
            return
        path = self.transcript_dir / f"{int(time.time() * 1000)}_{task}.json"
        path.write_text(
            json.dumps({"task": task, "model": self.model, "host": self.host,
                        "request": request, "reply": reply}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
