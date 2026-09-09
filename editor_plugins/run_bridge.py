#!/usr/bin/env python3
import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.editor_jobs import write_job_state
from backend.plugin_bridge import InEditorBridge, editor_environment


def main() -> None:
    parser = argparse.ArgumentParser(description="Run AICUT inside an editor plugin process")
    parser.add_argument("--media", required=True)
    parser.add_argument("--workspace", default=os.environ.get("AICUT_EDITOR_WORKSPACE", "~/.aicut-editor"))
    parser.add_argument("--options-json", default=os.environ.get("AICUT_EDITOR_OPTIONS"))
    parser.add_argument("--manifest", default=os.environ.get("AICUT_EDITOR_MANIFEST"))
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--result-file")
    parser.add_argument("--job-id")
    args = parser.parse_args()
    state_path = Path(args.result_file).expanduser().resolve() if args.result_file else None
    previous_state = json.loads(state_path.read_text(encoding="utf-8")) if state_path and state_path.is_file() else {}
    base_state = {
        **previous_state, "job_id": args.job_id,
        "media_path": str(Path(args.media).expanduser().resolve()),
    }
    if state_path:
        base_state["started_at"] = datetime.now(timezone.utc).isoformat()
        write_job_state(state_path, {**base_state, "status": "RUNNING"})
    try:
        options, environment_manifest = editor_environment()
        if args.options_json:
            options = json.loads(Path(args.options_json).read_text(encoding="utf-8"))
        result = InEditorBridge(args.workspace).analyze_selected_media(
            args.media, options=options, manifest_path=args.manifest or environment_manifest, fps=args.fps,
        )
    except Exception as error:
        if state_path:
            write_job_state(state_path, {
                **base_state, "status": "FAILED", "error": str(error),
                "completed_at": datetime.now(timezone.utc).isoformat(),
            })
        raise
    if state_path:
        write_job_state(state_path, {
            **base_state, "status": "COMPLETE", "result": result,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        })
        print(json.dumps({"job_id": args.job_id, "status": "COMPLETE", "state_path": str(state_path)}, ensure_ascii=False))
    else:
        print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
