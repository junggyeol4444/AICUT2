"""Install in DaVinci Resolve's Scripts/Edit folder and invoke from Workspace > Scripts."""

import os
import sys
from pathlib import Path

if os.environ.get("AICUT_ROOT"):
    sys.path.insert(0, os.environ["AICUT_ROOT"])

from backend.plugin_bridge import InEditorBridge, editor_environment


def run(resolve):
    project = resolve.GetProjectManager().GetCurrentProject()
    clip = project.GetMediaPool().GetCurrentFolder().GetClipList()[0]
    media_path = clip.GetClipProperty("File Path")
    workspace = Path(os.environ.get("AICUT_EDITOR_WORKSPACE", Path.home() / ".aicut-editor"))
    options, manifest = editor_environment()
    options.setdefault("output_directory", str(workspace / "analysis"))
    return InEditorBridge(workspace).analyze_selected_media(
        media_path, options=options, manifest_path=manifest,
    )


if "resolve" in globals():
    RESULT = run(resolve)
