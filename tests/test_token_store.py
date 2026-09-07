import json
import os
import tempfile
import unittest
from pathlib import Path

from backend.token_store import EncryptedTokenStore
from backend.upload import UploadError


class TokenStoreTest(unittest.TestCase):
    def test_tokens_are_authenticated_encrypted_and_owner_only(self):
        tokens = {"access_token": "access-secret", "refresh_token": "refresh-secret", "expires_at": 1234.5}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "youtube.tokens"
            store = EncryptedTokenStore(path, "local-master-key")
            store.save(tokens)
            raw = path.read_text()
            loaded = store.load()
            mode = os.stat(path).st_mode & 0o777
        self.assertNotIn("access-secret", raw)
        self.assertNotIn("refresh-secret", raw)
        self.assertEqual(loaded, tokens)
        self.assertEqual(mode, 0o600)

    def test_wrong_key_and_tampering_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "youtube.tokens"
            EncryptedTokenStore(path, "correct-key").save({"access_token": "a"})
            with self.assertRaises(UploadError):
                EncryptedTokenStore(path, "wrong-key").load()
            payload = json.loads(path.read_text())
            payload["ciphertext"] = payload["ciphertext"][::-1]
            path.write_text(json.dumps(payload))
            with self.assertRaises(UploadError):
                EncryptedTokenStore(path, "correct-key").load()


if __name__ == "__main__":
    unittest.main()
