"""Talking to the AI Engine from inside an editor (36장 9번 AI Engine Connector).

The plugin does not analyse anything. 35장: AI Engine은 영상의 "두뇌"이고
Plugin은 편집기와 AI를 연결하는 "손"이다. This is the wire between them.

The engine is the local `aicut ui` server. Everything here is one HTTP call:

    check()                     the engine is up, and which workspace it holds
    submit(source)              hand it the broadcast the editor has open
    job(job_id)                 what it is doing now (26장's progress screen)
    episodes(project_id)        what it decided to make
    edit_model(episode_id)      the Common Edit Model for one of them (37장)

Standard library only, and 3.6 syntax: this runs inside the editor's own
interpreter. urllib rather than requests, because an editor's Python has
whatever Blackmagic or Adobe shipped and nothing else.
"""

import json
import os

try:                                   # py3
    from urllib.request import Request, urlopen
    from urllib.error import HTTPError, URLError
except ImportError:                    # pragma: no cover - py2 editors
    from urllib2 import Request, urlopen, HTTPError, URLError

DEFAULT_ENGINE = "http://127.0.0.1:8765"


class EngineError(Exception):
    """The engine could not be reached, or refused."""


class Engine(object):
    """One aicut engine, addressed over HTTP.

    ``api_key`` is only needed when the operator started the server with one;
    `aicut ui` is localhost-only and unauthenticated by default.
    """

    def __init__(self, base_url=None, api_key=None, timeout=30):
        self.base_url = (base_url or os.environ.get("AICUT_ENGINE") or DEFAULT_ENGINE).rstrip("/")
        self.api_key = api_key or os.environ.get("AICUT_API_KEY") or ""
        self.timeout = timeout

    # -- the calls ---------------------------------------------------------
    def check(self):
        """Is the engine there. Returns its profile line, or raises."""
        return self._call("GET", "/api/profiles")

    def submit(self, source_path, **options):
        """Hand the engine the file the editor has open. Returns the job.

        This is 4장's button: the person pressed it, and from here the engine
        does 5장's whole list on its own.
        """
        body = {"source": source_path}
        body.update(options)
        return self._call("POST", "/api/projects", body)

    def job(self, job_id):
        """What the engine is doing, for 26장's progress panel."""
        return self._call("GET", "/api/jobs/" + str(job_id))

    def episodes(self, project_id):
        return self._call("GET", "/api/projects/{}/episodes".format(project_id))

    def edit_model(self, episode_id, mode="new_sequence"):
        """The Common Edit Model for one episode (37장).

        ``mode`` is 25장's: `new_sequence` leaves the operator's own timeline
        alone, `edit_current` does not. It is theirs to choose, so the adapter
        passes through what they picked rather than defaulting silently.
        """
        return self._call(
            "POST", "/api/episodes/{}/edit-model".format(episode_id), {"mode": mode},
        )

    # -- the wire ----------------------------------------------------------
    def _call(self, method, path, body=None):
        url = self.base_url + path
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        request = Request(url, data=data, headers=headers)
        request.get_method = lambda: method
        try:
            handle = urlopen(request, timeout=self.timeout)
        except HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:400]
            except Exception:
                pass
            raise EngineError("{} {} -> HTTP {} {}".format(method, path, exc.code, detail))
        except URLError as exc:
            # The common case by a distance: the engine was never started.
            raise EngineError(
                "no aicut engine at {} ({}). Start it with `aicut ui`, or set "
                "AICUT_ENGINE to where it is running.".format(self.base_url, exc.reason)
            )
        try:
            raw = handle.read().decode("utf-8")
        finally:
            handle.close()
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except ValueError:
            raise EngineError("{} {} did not return JSON".format(method, path))
