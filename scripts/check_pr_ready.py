#!/usr/bin/env python3
"""Fail fast on repository states that commonly block pull-request creation."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

BINARY_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".mp4", ".mkv",
    ".mov", ".mp3", ".wav", ".zip", ".gz", ".7z", ".pdf", ".db",
}
MARKERS = (b"<<<<<<< ", b"=======\n", b">>>>>>> ")


def git(*args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", *args], capture_output=True, check=check)


def tracked_files() -> list[Path]:
    output = git("ls-files", "--cached", "--others", "--exclude-standard", "-z").stdout
    return [Path(value.decode()) for value in output.split(b"\0") if value]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--base", default="origin/main",
        help="base revision used for PR diff checks (defaults to the fetched GitHub main branch)",
    )
    args = parser.parse_args()
    errors: list[str] = []

    unmerged = git("diff", "--name-only", "--diff-filter=U").stdout.decode().splitlines()
    if unmerged:
        errors.append("unresolved Git index conflicts: " + ", ".join(unmerged))

    for path in tracked_files():
        if path.suffix.lower() in BINARY_SUFFIXES:
            errors.append(f"tracked binary-like file: {path}")
            continue
        try:
            content = path.read_bytes()
        except FileNotFoundError:
            continue
        if b"\0" in content:
            errors.append(f"tracked file contains NUL bytes: {path}")
        if all(marker in content for marker in MARKERS):
            errors.append(f"conflict markers remain in: {path}")

    base_exists = git("cat-file", "-e", f"{args.base}^{{commit}}", check=False).returncode == 0
    if not base_exists:
        errors.append(f"base revision is not available locally: {args.base}")
    else:
        numstat = git("diff", "--numstat", args.base).stdout.decode().splitlines()
        binary_diff = [line for line in numstat if line.startswith("-\t-\t")]
        if binary_diff:
            errors.extend(f"binary PR diff: {line.split(chr(9), 2)[-1]}" for line in binary_diff)

    if errors:
        print("PR readiness check failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print(f"PR-ready: no unmerged paths, conflict markers, or binary changes against {args.base}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
