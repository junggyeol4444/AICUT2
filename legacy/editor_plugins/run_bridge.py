#!/usr/bin/env python3
import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.plugin_bridge import InEditorBridge, editor_environment


def main() -> None:
    parser = argparse.ArgumentParser(description="Run AICUT inside an editor plugin process")
    parser.add_argument("--media", required=True)
    parser.add_argument("--workspace", default=os.environ.get("AICUT_EDITOR_WORKSPACE", "~/.aicut-editor"))
    parser.add_argument("--options-json", default=os.environ.get("AICUT_EDITOR_OPTIONS"))
    parser.add_argument("--manifest", default=os.environ.get("AICUT_EDITOR_MANIFEST"))
    parser.add_argument("--fps", type=int, default=30)
    args = parser.parse_args()
    options, environment_manifest = editor_environment()
    if args.options_json:
        options = json.loads(Path(args.options_json).read_text(encoding="utf-8"))
    result = InEditorBridge(args.workspace).analyze_selected_media(
        args.media, options=options, manifest_path=args.manifest or environment_manifest, fps=args.fps,
    )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
