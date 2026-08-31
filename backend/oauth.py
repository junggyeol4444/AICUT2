from __future__ import annotations

import json
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .upload import UploadError, YouTubeResumableClient
from .token_store import EncryptedTokenStore


@dataclass
class OAuthTokens:
    access_token: str
    refresh_token: str
    expires_at: float


class YouTubeOAuth:
    authorization_endpoint = "https://accounts.google.com/o/oauth2/v2/auth"
    token_endpoint = "https://oauth2.googleapis.com/token"
    scope = " ".join((
        "https://www.googleapis.com/auth/youtube.upload",
        "https://www.googleapis.com/auth/youtube.readonly",
        "https://www.googleapis.com/auth/yt-analytics.readonly",
    ))

    def __init__(
        self, client_id: str, client_secret: str, redirect_uri: str, *, opener: Callable = urlopen,
        clock: Callable[[], float] = time.time, token_store: EncryptedTokenStore | None = None,
    ):
        if not all(str(value).strip() for value in (client_id, client_secret, redirect_uri)):
            raise UploadError("YouTube OAuth client id, client secret, redirect URI가 필요합니다.")
        self.client_id, self.client_secret, self.redirect_uri = client_id, client_secret, redirect_uri
        self.opener, self.clock = opener, clock
        self.token_store = token_store
        stored = token_store.load() if token_store else None
        self.tokens = OAuthTokens(**stored) if stored else None
        self._states: set[str] = set()
        self._lock = threading.Lock()

    def authorization_url(self) -> dict[str, str]:
        state = secrets.token_urlsafe(32)
        with self._lock:
            self._states.add(state)
        query = urlencode({
            "client_id": self.client_id, "redirect_uri": self.redirect_uri,
            "response_type": "code", "scope": self.scope, "access_type": "offline",
            "prompt": "consent", "state": state,
        })
        return {"authorization_url": f"{self.authorization_endpoint}?{query}", "state": state}

    def exchange_callback(self, code: str, state: str) -> OAuthTokens:
        with self._lock:
            if not state or state not in self._states:
                raise UploadError("YouTube OAuth state가 유효하지 않습니다.")
            self._states.remove(state)
        if not code:
            raise UploadError("YouTube OAuth callback code가 없습니다.")
        payload = self._token_request({
            "code": code, "client_id": self.client_id, "client_secret": self.client_secret,
            "redirect_uri": self.redirect_uri, "grant_type": "authorization_code",
        })
        self.tokens = self._tokens_from_payload(payload, payload.get("refresh_token", ""))
        if not self.tokens.refresh_token:
            raise UploadError("Google OAuth 응답에 refresh token이 없습니다. 동의 화면을 다시 승인해야 합니다.")
        self._persist_tokens()
        return self.tokens

    def access_token(self) -> str:
        if not self.tokens:
            raise UploadError("YouTube OAuth 승인이 필요합니다.")
        if self.tokens.expires_at - self.clock() <= 60:
            payload = self._token_request({
                "refresh_token": self.tokens.refresh_token, "client_id": self.client_id,
                "client_secret": self.client_secret, "grant_type": "refresh_token",
            })
            self.tokens = self._tokens_from_payload(payload, self.tokens.refresh_token)
            self._persist_tokens()
        return self.tokens.access_token

    def _token_request(self, values: dict[str, str]) -> dict:
        body = urlencode(values).encode()
        request = Request(self.token_endpoint, data=body, method="POST", headers={
            "Content-Type": "application/x-www-form-urlencoded", "Content-Length": str(len(body)),
        })
        try:
            response = self.opener(request)
            payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise UploadError(f"Google OAuth 오류 ({error.code}): {detail[-2000:]}") from error
        except (URLError, json.JSONDecodeError, TypeError, ValueError) as error:
            raise UploadError(f"Google OAuth 응답을 처리할 수 없습니다: {error}") from error
        if not payload.get("access_token"):
            raise UploadError("Google OAuth 응답에 access token이 없습니다.")
        return payload

    def _tokens_from_payload(self, payload: dict, refresh_token: str) -> OAuthTokens:
        return OAuthTokens(str(payload["access_token"]), str(refresh_token),
                           self.clock() + float(payload.get("expires_in", 3600)))

    def _persist_tokens(self) -> None:
        if self.token_store and self.tokens:
            self.token_store.save({
                "access_token": self.tokens.access_token, "refresh_token": self.tokens.refresh_token,
                "expires_at": self.tokens.expires_at,
            })


class OAuthYouTubeClient:
    def __init__(self, oauth: YouTubeOAuth, *, uploader_factory: Callable = YouTubeResumableClient):
        self.oauth, self.uploader_factory = oauth, uploader_factory

    def upload(self, file_path: str, metadata: dict, privacy_status: str) -> str:
        return self.uploader_factory(self.oauth.access_token()).upload(file_path, metadata, privacy_status)
