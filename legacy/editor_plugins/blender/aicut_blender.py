"""Blender Video Sequence Editor add-on entry point."""

import os
import sys
from pathlib import Path

import bpy

if os.environ.get("AICUT_ROOT"):
    sys.path.insert(0, os.environ["AICUT_ROOT"])

from backend.plugin_bridge import InEditorBridge, editor_environment


class AICUT_OT_analyze_selected(bpy.types.Operator):
    bl_idname = "aicut.analyze_selected"
    bl_label = "Analyze and edit selected video with AICUT"

    def execute(self, context):
        strips = [strip for strip in context.selected_sequences if getattr(strip, "filepath", "")]
        if not strips:
            self.report({"ERROR"}, "Select one movie strip")
            return {"CANCELLED"}
        workspace = Path(os.environ.get("AICUT_EDITOR_WORKSPACE", Path.home() / ".aicut-editor"))
        options, manifest = editor_environment()
        options.setdefault("output_directory", str(workspace / "analysis"))
        result = InEditorBridge(workspace).analyze_selected_media(
            bpy.path.abspath(strips[0].filepath),
            options=options, manifest_path=manifest,
            fps=round(context.scene.render.fps / context.scene.render.fps_base),
        )
        self.report({"INFO"}, f"AICUT project {result['project_id']} complete")
        return {"FINISHED"}


def register():
    bpy.utils.register_class(AICUT_OT_analyze_selected)


def unregister():
    bpy.utils.unregister_class(AICUT_OT_analyze_selected)
