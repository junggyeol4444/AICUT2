"""Optional bearer-token guard for the local UI server (18장 "서버").

The plan calls the UI a single-user local tool (15장) and the server binds to
localhost, so the guard is OFF unless a key is configured — turning it on by
default would only teach the operator to paste a key they do not need.

Ported from the Codex build's `legacy/backend/auth.py`, with the hole that
version had left open. There, `authorized()` returned True for every path not
under `/api/`, and the static handler's document root was the repository
directory whenever `dist/` was absent. The two together meant a configured API
key protected the JSON API and nothing else. Measured against that build before
porting, with `AICUT_API_KEY` set and no `Authorization` header:

    GET /api/projects        401
    GET /package.json        200
    GET /backend/token_store.py  200
    GET /aicut.db            200, 249,856 bytes  ← the whole database

Here the static route can only ever reach `aicut/ui/static/`, which holds the
packaged page and nothing of the operator's; `tests/test_ui.py` pins that the
workspace is unreachable through it. So exempting the page — which a browser
must fetch before it can send any header — costs nothing, and every `/api/`
path stays behind the key.
"""

from __future__ import annotations

import hmac
import os
from collections.abc import Mapping

#: Read by :func:`guard_from_environment`. Unset means the guard stays off.
ENV_VAR = "AICUT_UI_API_KEY"


class ApiKeyGuard:
    """Checks `Authorization: Bearer <key>` on the API surface."""

    #: The page a browser fetches before it can attach a header, plus the
    #: liveness probe. Neither reveals anything the key protects.
    exempt_api_paths = frozenset({"/api/health"})

    def __init__(self, api_key: str | None):
        self._api_key = (api_key or "").strip()

    @property
    def enabled(self) -> bool:
        return bool(self._api_key)

    def protects(self, path: str) -> bool:
        """True when *path* needs the key. Static paths never do — see module docstring."""
        return self.enabled and path.startswith("/api/") and path not in self.exempt_api_paths

    def authorized(self, path: str, headers: Mapping[str, str]) -> bool:
        if not self.protects(path):
            return True
        scheme, separator, credential = headers.get("Authorization", "").partition(" ")
        if not separator or scheme.lower() != "bearer" or not credential:
            return False
        # Constant time: a local port is still reachable by anything else on the
        # machine, and a length- or prefix-leaking compare is free to exploit there.
        return hmac.compare_digest(credential, self._api_key)


def guard_from_environment(env: Mapping[str, str] | None = None) -> ApiKeyGuard:
    return ApiKeyGuard((env if env is not None else os.environ).get(ENV_VAR))
