from __future__ import annotations

import hmac
from collections.abc import Mapping


class ApiKeyGuard:
    """Optional bearer-token guard for the local API; disabled when no key is configured."""

    exempt_paths = frozenset({"/api/health", "/api/youtube/oauth/callback"})

    def __init__(self, api_key: str | None):
        self._api_key = (api_key or "").strip()

    @property
    def enabled(self) -> bool:
        return bool(self._api_key)

    def authorized(self, path: str, headers: Mapping[str, str]) -> bool:
        if not path.startswith("/api/") or path in self.exempt_paths or not self.enabled:
            return True
        authorization = headers.get("Authorization", "")
        scheme, separator, credential = authorization.partition(" ")
        if not separator or scheme.lower() != "bearer" or not credential:
            return False
        return hmac.compare_digest(credential, self._api_key)
