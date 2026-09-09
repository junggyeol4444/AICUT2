"""Blender Video Sequence Editor add-on entry point."""

import os
import sys
from pathlib import Path

import bpy

if os.environ.get("AICUT_ROOT"):
    sys.path.insert(0, os.environ["AICUT_ROOT"])

from backend.editor_jobs import launch_editor_job


class AICUT_OT_analyze_selected(bpy.types.Operator):
    bl_idname = "aicut.analyze_selected"
    bl_label = "Analyze and edit selected video with AICUT"

    def execute(self, context):
        strips = [strip for strip in context.selected_sequences if getattr(strip, "filepath", "")]
        if not strips:
            self.report({"ERROR"}, "Select one movie strip")
            return {"CANCELLED"}
        root = os.environ.get("AICUT_ROOT")
        if not root:
            self.report({"ERROR"}, "AICUT_ROOT environment variable is required")
            return {"CANCELLED"}
        workspace = Path(os.environ.get("AICUT_EDITOR_WORKSPACE", Path.home() / ".aicut-editor"))
        result = launch_editor_job(
            root, bpy.path.abspath(strips[0].filepath), workspace,
            fps=round(context.scene.render.fps / context.scene.render.fps_base),
            python_executable=os.environ.get("AICUT_PYTHON", "python3"),
            options_json=os.environ.get("AICUT_EDITOR_OPTIONS"),
            manifest_path=os.environ.get("AICUT_EDITOR_MANIFEST"),
        )
        self.report({"INFO"}, f"AICUT job {result['job_id']} queued (PID {result['pid']})")
        return {"FINISHED"}


def register():
    bpy.utils.register_class(AICUT_OT_analyze_selected)


def unregister():
    bpy.utils.unregister_class(AICUT_OT_analyze_selected)
