import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

from backend.oauth import OAuthYouTubeClient, YouTubeOAuth
from backend.upload import UploadError
from backend.token_store import EncryptedTokenStore


class Response:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode()


class OAuthTest(unittest.TestCase):
    def test_authorization_callback_and_refresh_token_flow(self):
        calls, now = [], [1000.0]

        def opener(request):
            values = parse_qs(request.data.decode())
            calls.append(values)
            if values["grant_type"] == ["authorization_code"]:
                return Response({"access_token": "access-1", "refresh_token": "refresh-1", "expires_in": 120})
            return Response({"access_token": "access-2", "expires_in": 3600})

        oauth = YouTubeOAuth("client", "secret", "http://localhost/callback", opener=opener, clock=lambda: now[0])
        authorization = oauth.authorization_url()
        query = parse_qs(urlparse(authorization["authorization_url"]).query)
        self.assertEqual(query["access_type"], ["offline"])
        tokens = oauth.exchange_callback("code-1", authorization["state"])
        self.assertEqual(tokens.refresh_token, "refresh-1")
        self.assertEqual(oauth.access_token(), "access-1")
        now[0] = 1070
        self.assertEqual(oauth.access_token(), "access-2")
        self.assertEqual(calls[-1]["refresh_token"], ["refresh-1"])

    def test_callback_rejects_csrf_state_reuse(self):
        oauth = YouTubeOAuth("client", "secret", "http://localhost/callback")
        with self.assertRaises(UploadError):
            oauth.exchange_callback("code", "unknown-state")

    def test_oauth_upload_client_uses_fresh_access_token(self):
        uploads = []

        class Uploader:
            def __init__(self, token):
                uploads.append(token)

            def upload(self, *_args):
                return "video-1"

        oauth = SimpleNamespace(access_token=lambda: "fresh-token")
        client = OAuthYouTubeClient(oauth, uploader_factory=Uploader)
        self.assertEqual(client.upload("video.mp4", {}, "PRIVATE"), "video-1")
        self.assertEqual(uploads, ["fresh-token"])

    def test_encrypted_refresh_token_restores_after_restart(self):
        def opener(_request):
            return Response({"access_token": "access", "refresh_token": "refresh", "expires_in": 3600})

        with tempfile.TemporaryDirectory() as directory:
            store = EncryptedTokenStore(Path(directory) / "tokens", "master-key")
            oauth = YouTubeOAuth("client", "secret", "http://localhost/callback", opener=opener,
                                 clock=lambda: 1000, token_store=store)
            authorization = oauth.authorization_url()
            oauth.exchange_callback("code", authorization["state"])
            restored = YouTubeOAuth("client", "secret", "http://localhost/callback", opener=opener,
                                    clock=lambda: 1000, token_store=store)
        self.assertEqual(restored.tokens.refresh_token, "refresh")
        self.assertEqual(restored.access_token(), "access")


if __name__ == "__main__":
    unittest.main()
