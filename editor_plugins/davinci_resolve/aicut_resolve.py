"""Install in DaVinci Resolve's Scripts/Edit folder and invoke from Workspace > Scripts."""

import os
import sys
from pathlib import Path

if os.environ.get("AICUT_ROOT"):
    sys.path.insert(0, os.environ["AICUT_ROOT"])

from backend.editor_jobs import launch_editor_job


def run(resolve):
    root = os.environ.get("AICUT_ROOT")
    if not root:
        raise RuntimeError("AICUT_ROOT 환경변수가 필요합니다.")
    project = resolve.GetProjectManager().GetCurrentProject()
    clips = project.GetMediaPool().GetCurrentFolder().GetClipList()
    if not clips:
        raise RuntimeError("현재 Media Pool 폴더에 분석할 클립이 없습니다.")
    clip = clips[0]
    media_path = clip.GetClipProperty("File Path")
    workspace = Path(os.environ.get("AICUT_EDITOR_WORKSPACE", Path.home() / ".aicut-editor"))
    return launch_editor_job(
        root, media_path, workspace, python_executable=os.environ.get("AICUT_PYTHON", "python3"),
        options_json=os.environ.get("AICUT_EDITOR_OPTIONS"), manifest_path=os.environ.get("AICUT_EDITOR_MANIFEST"),
    )


if "resolve" in globals():
    RESULT = run(resolve)
