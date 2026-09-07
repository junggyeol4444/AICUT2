from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Callable

from .database import Database
from .editor_export import export_editor_bundle
from .pipeline import PipelineManager


def editor_environment() -> tuple[dict, str | None]:
    """Load the same optional model configuration for every in-editor adapter."""
    options_path = os.environ.get("AICUT_EDITOR_OPTIONS")
    options = json.loads(Path(options_path).expanduser().read_text(encoding="utf-8")) if options_path else {}
    if not isinstance(options, dict):
        raise ValueError("AICUT_EDITOR_OPTIONS JSON의 최상위 값은 객체여야 합니다.")
    return options, os.environ.get("AICUT_EDITOR_MANIFEST")


class InEditorBridge:
    """Embed AICUT inside editor Python hosts; no separately launched API server is required."""

    def __init__(
        self, workspace: str | Path, *,
        pipeline_factory: Callable[[Database], PipelineManager] = PipelineManager,
    ):
        self.workspace = Path(workspace).expanduser().resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.database = Database(self.workspace / "aicut-editor.sqlite3")
        self.pipeline_factory = pipeline_factory

    def analyze_selected_media(
        self, media_path: str, *, options: dict | None = None,
        manifest_path: str | None = None, fps: int = 30,
    ) -> dict:
        source = Path(media_path).expanduser().resolve()
        if not source.is_file():
            raise ValueError(f"선택한 편집기 미디어를 찾을 수 없습니다: {source}")
        project = self.database.create_project({"file_path": str(source), "name": source.stem})
        manager = self.pipeline_factory(self.database)
        try:
            state = manager.run_sync(
                project["project_id"], manifest_path, options=options, resume=True,
            )
        finally:
            manager.shutdown()
        exports = []
        for episode in self.database.list_episodes(project["project_id"]):
            exports.append({
                "episode_id": episode["episode_id"],
                **export_editor_bundle(
                    str(source), self.database.get_timeline(episode["episode_id"]),
                    self.workspace / "exports" / episode["episode_id"], fps=fps,
                    title=episode["episode_id"],
                ),
            })
        return {
            "project_id": project["project_id"], "project": self.database.get_project(project["project_id"]),
            "pipeline": state, "editor_exports": exports,
        }
