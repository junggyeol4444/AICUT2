from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from pathlib import Path

from .upload import UploadError


class EncryptedTokenStore:
    """Authenticated, password-derived encrypted storage for local OAuth tokens."""

    version = 1

    def __init__(self, path: str | Path, secret: str):
        if not secret:
            raise UploadError("OAuth token 저장소 암호화 키가 필요합니다.")
        self.path = Path(path).expanduser().resolve()
        self.secret = secret.encode("utf-8")

    def save(self, tokens: dict) -> None:
        plaintext = json.dumps(tokens, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        salt, nonce = os.urandom(16), os.urandom(16)
        encryption_key, mac_key = self._keys(salt)
        ciphertext = self._xor(plaintext, encryption_key, nonce)
        tag = hmac.new(mac_key, nonce + ciphertext, hashlib.sha256).digest()
        payload = {
            "version": self.version, "salt": self._encode(salt), "nonce": self._encode(nonce),
            "ciphertext": self._encode(ciphertext), "tag": self._encode(tag),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(self.path)

    def load(self) -> dict | None:
        if not self.path.is_file():
            return None
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if payload.get("version") != self.version:
                raise UploadError("지원하지 않는 OAuth token 저장소 버전입니다.")
            salt, nonce = self._decode(payload["salt"]), self._decode(payload["nonce"])
            ciphertext, supplied_tag = self._decode(payload["ciphertext"]), self._decode(payload["tag"])
            encryption_key, mac_key = self._keys(salt)
            expected_tag = hmac.new(mac_key, nonce + ciphertext, hashlib.sha256).digest()
            if not hmac.compare_digest(supplied_tag, expected_tag):
                raise UploadError("OAuth token 저장소 인증에 실패했습니다.")
            return json.loads(self._xor(ciphertext, encryption_key, nonce).decode("utf-8"))
        except UploadError:
            raise
        except (KeyError, ValueError, OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise UploadError(f"OAuth token 저장소를 읽을 수 없습니다: {error}") from error

    def _keys(self, salt: bytes) -> tuple[bytes, bytes]:
        derived = hashlib.scrypt(self.secret, salt=salt, n=2**14, r=8, p=1, dklen=64)
        return derived[:32], derived[32:]

    @staticmethod
    def _xor(value: bytes, key: bytes, nonce: bytes) -> bytes:
        output = bytearray()
        for counter in range((len(value) + 31) // 32):
            block = hmac.new(key, nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest()
            start = counter * 32
            output.extend(left ^ right for left, right in zip(value[start:start + 32], block))
        return bytes(output)

    @staticmethod
    def _encode(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).decode("ascii")

    @staticmethod
    def _decode(value: str) -> bytes:
        return base64.urlsafe_b64decode(value.encode("ascii"))
