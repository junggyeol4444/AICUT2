from __future__ import annotations

from pathlib import Path


class StaticFileError(ValueError):
    pass


def resolve_static_file(distribution_root: str | Path, request_path: str) -> Path:
    """Resolve a public build asset without ever falling back to repository files."""
    root = Path(distribution_root).resolve()
    if not root.is_dir():
        raise FileNotFoundError("프런트엔드 빌드가 없습니다. 먼저 npm run build를 실행하세요.")
    relative = request_path.lstrip("/") or "index.html"
    target = (root / relative).resolve()
    if root not in target.parents and target != root:
        raise StaticFileError("정적 파일 경로가 올바르지 않습니다.")
    if target.is_file():
        return target
    if Path(relative).suffix:
        raise FileNotFoundError("요청한 정적 파일이 없습니다.")
    index = root / "index.html"
    if not index.is_file():
        raise FileNotFoundError("프런트엔드 진입 파일이 없습니다.")
    return index
