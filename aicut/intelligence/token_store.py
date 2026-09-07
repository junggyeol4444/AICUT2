"""Authenticated, password-derived encryption for the cached OAuth token.

`load_credentials` used to cache Google's token as plain JSON next to the
workspace (`creds.to_json()`). That file carries a refresh token, which is a
standing grant to the operator's channel — upload, thumbnail, privacy, and the
Analytics scopes of 12.1 — and it does not expire on its own. Anything that can
read the workspace directory (a backup, a sync client, a second account on the
machine) inherits that grant.

Ported from the Codex build's `legacy/backend/token_store.py`. The construction
is theirs and it is sound: scrypt derives two independent keys from the
passphrase, the payload is encrypted then MAC'd (never the reverse), the tag is
compared in constant time, and salt and nonce are fresh on every save.

Two things are changed here:

- The temporary file is opened at 0o600 instead of being chmod'ed after the
  write. The original wrote first and tightened after, so between those two
  calls the token sat on disk at whatever the umask allowed.
- Failure to read is not silent. A wrong passphrase and a corrupted file are
  told apart, because the operator's next move differs.

The cipher is HMAC-SHA256 in counter mode rather than AES-GCM: 20.1 puts the
core on the standard library, which ships no AEAD. If `cryptography` is ever
accepted as a dependency, `_keystream` is the only thing that has to change —
the envelope carries a `version` for exactly that.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
from typing import Any

from aicut.errors import AicutError

#: Names the key-derivation and cipher construction below. Bump on any change
#: to either; :meth:`EncryptedTokenStore.load` refuses versions it cannot read.
VERSION = 1

#: scrypt cost. n=2^14 keeps an interactive `aicut upload` responsive while
#: making an offline guess against a weak passphrase expensive.
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**14, 8, 1
_BLOCK = hashlib.sha256().digest_size


class TokenStoreError(AicutError):
    """The token store exists but could not be read."""


class EncryptedTokenStore:
    """Reads and writes one encrypted JSON document."""

    def __init__(self, path: str | Path, secret: str):
        if not secret:
            raise TokenStoreError(
                "an encryption passphrase is required to store the OAuth token; "
                f"set {ENV_SECRET} or pass --token-key"
            )
        self.path = Path(path).expanduser().resolve()
        self._secret = secret.encode("utf-8")

    # -- api -----------------------------------------------------------------
    def save(self, document: dict[str, Any]) -> None:
        plaintext = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        salt, nonce = os.urandom(16), os.urandom(16)
        cipher_key, mac_key = self._keys(salt)
        ciphertext = self._xor(plaintext, cipher_key, nonce)
        envelope = {
            "version": VERSION,
            "salt": _b64(salt),
            "nonce": _b64(nonce),
            "ciphertext": _b64(ciphertext),
            "tag": _b64(hmac.new(mac_key, nonce + ciphertext, hashlib.sha256).digest()),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        # Opened at 0o600 rather than chmod'ed afterwards: the original left the
        # token readable at the umask's discretion for the length of the write.
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(envelope, handle, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        temporary.replace(self.path)

    def load(self) -> dict[str, Any] | None:
        """Return the stored document, or None when nothing has been saved yet."""
        if not self.path.is_file():
            return None
        try:
            envelope = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise TokenStoreError(f"the OAuth token store at {self.path} is unreadable: {exc}") from exc
        if not isinstance(envelope, dict):
            raise TokenStoreError(f"the OAuth token store at {self.path} is not an object")
        if envelope.get("version") != VERSION:
            raise TokenStoreError(
                f"the OAuth token store at {self.path} is version {envelope.get('version')!r}, "
                f"and this build reads version {VERSION}"
            )
        try:
            salt, nonce = _unb64(envelope["salt"]), _unb64(envelope["nonce"])
            ciphertext, supplied = _unb64(envelope["ciphertext"]), _unb64(envelope["tag"])
        except (KeyError, ValueError, TypeError) as exc:
            raise TokenStoreError(f"the OAuth token store at {self.path} is malformed: {exc}") from exc

        cipher_key, mac_key = self._keys(salt)
        expected = hmac.new(mac_key, nonce + ciphertext, hashlib.sha256).digest()
        if not hmac.compare_digest(supplied, expected):
            # Authentication covers both cases, so this cannot say which — but
            # naming both is what tells the operator where to look.
            raise TokenStoreError(
                f"the OAuth token store at {self.path} did not authenticate: "
                "the passphrase is wrong, or the file was modified. "
                "Delete it and re-authorise to start over."
            )
        try:
            document = json.loads(self._xor(ciphertext, cipher_key, nonce).decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:  # pragma: no cover - MAC makes this unreachable
            raise TokenStoreError(f"the decrypted OAuth token is not JSON: {exc}") from exc
        if not isinstance(document, dict):
            raise TokenStoreError("the decrypted OAuth token is not an object")
        return document

    # -- construction --------------------------------------------------------
    def _keys(self, salt: bytes) -> tuple[bytes, bytes]:
        """One scrypt pass, split into independent cipher and MAC keys."""
        derived = hashlib.scrypt(
            self._secret, salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=2 * _BLOCK
        )
        return derived[:_BLOCK], derived[_BLOCK:]

    @staticmethod
    def _xor(data: bytes, key: bytes, nonce: bytes) -> bytes:
        out = bytearray(len(data))
        for counter in range((len(data) + _BLOCK - 1) // _BLOCK):
            block = hmac.new(key, nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest()
            start = counter * _BLOCK
            chunk = data[start:start + _BLOCK]
            out[start:start + len(chunk)] = bytes(a ^ b for a, b in zip(chunk, block))
        return bytes(out)


#: Passphrase for the store. Unset means tokens stay in plain JSON, as before.
ENV_SECRET = "AICUT_TOKEN_KEY"


def store_from_environment(path: str | Path, env: dict[str, str] | None = None) -> EncryptedTokenStore | None:
    """Return a store when a passphrase is configured, else None (plain caching)."""
    secret = (env if env is not None else os.environ).get(ENV_SECRET)
    return EncryptedTokenStore(path, secret) if secret else None


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text.encode("ascii"))
