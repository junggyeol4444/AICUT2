from __future__ import annotations

import json
from typing import BinaryIO


def read_json_object(
    stream: BinaryIO, content_length: str | None, content_type: str | None,
    *, max_bytes: int,
) -> dict:
    """Read one bounded JSON object without accepting truncated or trailing payloads."""
    if max_bytes <= 0:
        raise ValueError("max_bytes는 0보다 커야 합니다.")
    try:
        length = int(content_length or 0)
    except (TypeError, ValueError) as error:
        raise ValueError("Content-Length가 올바른 정수가 아닙니다.") from error
    if length < 0:
        raise ValueError("Content-Length는 음수일 수 없습니다.")
    if length > max_bytes:
        raise ValueError(f"요청 본문이 허용 크기 {max_bytes} bytes를 초과합니다.")
    if length and (content_type or "").partition(";")[0].strip().lower() != "application/json":
        raise ValueError("POST 요청 본문은 application/json이어야 합니다.")
    payload = stream.read(length)
    if len(payload) != length:
        raise ValueError("요청 본문이 Content-Length보다 짧습니다.")
    if not payload:
        return {}
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("요청 본문이 올바른 JSON이 아닙니다.") from error
    if not isinstance(value, dict):
        raise ValueError("요청 JSON의 최상위 값은 object여야 합니다.")
    return value
