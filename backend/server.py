from __future__ import annotations

import argparse
import json
import mimetypes
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .database import Database
from .pipeline import PipelineManager
from .render import RenderError, RenderPlan, export_plan, render
from .package import MetadataPackage, build_thumbnail_commands, extract_thumbnails, write_metadata_package
from .upload import UnconfiguredYouTubeClient, UploadManager
from .calibration import calibrate_pacing
from .learning import analyze_source_output
from .performance import performance_insights, validate_metrics
from .understanding import (
    PreprocessPlan, build_preprocess_commands, build_scan_plan, execute_preprocess,
    validate_transcript_segments,
)
from .stt import build_stt_command, SttJob, transcribe_tracks
from dataclasses import asdict

ROOT = Path(__file__).resolve().parents[1]
DB = Database(os.environ.get("AICUT_DB", ROOT / "aicut.db"))
PIPELINE = PipelineManager(DB)
UPLOADS = UploadManager(DB, UnconfiguredYouTubeClient())


class ApiHandler(BaseHTTPRequestHandler):
    server_version = "AICUT/1.0"

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        try:
            if path == "/api/health":
                self.json({"status": "ok", "service": "aicut-local-runtime"})
            elif path == "/api/projects":
                self.json(DB.list_projects())
            elif path.startswith("/api/projects/") and path.endswith("/candidates"):
                self.json(DB.list_candidates(path.split("/")[3]))
            elif path.startswith("/api/projects/") and path.endswith("/logs"):
                self.json(DB.logs(path.split("/")[3]))
            elif path.startswith("/api/projects/") and path.endswith("/job"):
                self.json(PIPELINE.state(path.split("/")[3]))
            elif path.startswith("/api/projects/"):
                self.json(DB.get_project(path.split("/")[3]))
            elif path.startswith("/api/episodes/") and path.endswith("/timeline"):
                self.json(DB.get_timeline(path.split("/")[3]))
            elif path == "/api/logs":
                self.json(DB.logs())
            elif path == "/api/uploads":
                self.json(DB.list_uploads())
            elif path == "/api/calibrations":
                self.json(DB.list_calibrations())
            elif path == "/api/learning/source-output":
                self.json(DB.list_source_output_pairs())
            elif path.startswith("/api/episodes/") and path.endswith("/performance"):
                self.json(DB.list_performance(path.split("/")[3]))
            else:
                self.serve_static(path)
        except KeyError as error:
            self.json({"error": "not_found", "id": str(error.args[0])}, HTTPStatus.NOT_FOUND)
        except Exception as error:
            self.json({"error": "internal_error", "message": str(error)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            payload = self.body()
            if path == "/api/projects":
                if not payload.get("file_path"):
                    return self.json({"error": "file_path_required"}, HTTPStatus.BAD_REQUEST)
                self.json(DB.create_project(payload), HTTPStatus.CREATED)
            elif path == "/api/calibrations":
                result = calibrate_pacing(payload.get("samples", []))
                profile = DB.save_calibration(
                    payload.get("channel_ref", "default"), payload.get("name", "Pacing profile"),
                    {"pacing": result.params, "evaluation": result.to_dict()}, result.f1 * 100,
                )
                self.json(profile, HTTPStatus.CREATED)
            elif path == "/api/learning/source-output":
                cuts = payload.get("cuts")
                if cuts is None and payload.get("episode_id"):
                    cuts = DB.get_timeline(payload["episode_id"])
                analysis = analyze_source_output(payload["source_duration_sec"], cuts or [])
                pair = DB.save_source_output_pair(
                    payload["source_ref"], payload["output_ref"], analysis, payload.get("project_id"),
                )
                self.json(pair, HTTPStatus.CREATED)
            elif path.startswith("/api/episodes/") and path.endswith("/performance"):
                episode_id = path.split("/")[3]
                metrics = validate_metrics(payload["metrics"])
                snapshot = DB.save_performance(episode_id, metrics)
                snapshot["insights"] = performance_insights(metrics, payload["profile"])
                self.json(snapshot, HTTPStatus.CREATED)
            elif path.startswith("/api/projects/") and path.endswith("/run"):
                project_id = path.split("/")[3]
                DB.get_project(project_id)
                accepted = PIPELINE.submit(project_id, payload.get("manifest_path"))
                self.json({"accepted": accepted, "project_id": project_id}, HTTPStatus.ACCEPTED if accepted else HTTPStatus.CONFLICT)
            elif path.startswith("/api/projects/") and path.endswith("/analysis"):
                project_id = path.split("/")[3]
                self.json(DB.import_analysis(project_id, payload))
            elif path.startswith("/api/projects/") and path.endswith("/preprocess"):
                project_id = path.split("/")[3]
                project = DB.get_project(project_id)
                media = json.loads(project.get("media_info_json") or "{}")
                plan = PreprocessPlan(
                    source_path=project["file_path"],
                    output_directory=payload.get("output_directory") or str(ROOT / "artifacts" / project_id),
                    audio_tracks=int(payload.get("audio_tracks", media.get("audio_tracks", 0))),
                    frame_interval_sec=float(payload["frame_interval_sec"]),
                )
                if payload.get("execute", False):
                    result = execute_preprocess(plan)
                    DB.add_artifacts(project_id, [
                        {"kind": item["kind"], "path": item["path"], "metadata": {"command": item["command"]}}
                        for item in result["artifacts"]
                    ])
                    self.json({"project_id": project_id, **result})
                else:
                    self.json({"project_id": project_id, "dry_run": True, "commands": build_preprocess_commands(plan)})
            elif path.startswith("/api/projects/") and path.endswith("/scan-plan"):
                project_id = path.split("/")[3]
                project = DB.get_project(project_id)
                windows = build_scan_plan(
                    project["duration_sec"], payload["coarse_window_sec"], payload.get("precision_ranges"),
                )
                encoded = [asdict(window) for window in windows]
                DB.replace_scan_windows(project_id, encoded)
                self.json({"project_id": project_id, "windows": encoded}, HTTPStatus.CREATED)
            elif path.startswith("/api/projects/") and path.endswith("/transcript"):
                project_id = path.split("/")[3]
                project = DB.get_project(project_id)
                segments = validate_transcript_segments(payload.get("segments", []), project["duration_sec"])
                DB.replace_transcript(project_id, segments)
                self.json({"project_id": project_id, "segment_count": len(segments)}, HTTPStatus.CREATED)
            elif path.startswith("/api/projects/") and path.endswith("/transcribe"):
                project_id = path.split("/")[3]
                project = DB.get_project(project_id)
                executable = payload.get("executable")
                audio_paths = payload.get("audio_paths", [])
                if not isinstance(executable, list) or not all(isinstance(value, str) for value in executable):
                    raise ValueError("executable은 셸 문자열이 아닌 인자 배열이어야 합니다.")
                output_directory = payload.get("output_directory") or str(ROOT / "artifacts" / project_id / "stt")
                if payload.get("execute", False):
                    result = transcribe_tracks(
                        executable, audio_paths, project["duration_sec"], output_directory, payload.get("language"),
                    )
                    DB.replace_transcript(project_id, result["segments"])
                    self.json({"project_id": project_id, **result}, HTTPStatus.CREATED)
                else:
                    commands = [build_stt_command(executable, SttJob(
                        audio_path, index, str(Path(output_directory) / f"audio-track-{index:02d}.json"), payload.get("language"),
                    )) for index, audio_path in enumerate(audio_paths)]
                    self.json({"project_id": project_id, "dry_run": True, "commands": commands})
            elif path.startswith("/api/candidates/") and path.endswith("/review"):
                self.json(DB.review_candidate(path.split("/")[3], payload.get("decision", ""), payload.get("feedback", "")))
            elif path.startswith("/api/episodes/") and path.endswith("/review"):
                self.json(DB.review_episode(path.split("/")[3], bool(payload.get("approved"))))
            elif path.startswith("/api/episodes/") and path.endswith("/render"):
                episode_id = path.split("/")[3]
                episode = DB.get_episode(episode_id)
                output_path = payload.get("output_path") or str(ROOT / "outputs" / f"{episode_id}.mp4")
                plan = RenderPlan(
                    input_path=episode["file_path"], output_path=output_path,
                    cuts=tuple(DB.get_timeline(episode_id)),
                    width=int(payload.get("width", 1920)), height=int(payload.get("height", 1080)),
                )
                if not payload.get("execute", False):
                    self.json({"episode_id": episode_id, "dry_run": True, "plan": json.loads(export_plan(plan))})
                else:
                    DB.set_render_status(episode_id, "RENDERING")
                    try:
                        result = render(plan)
                    except Exception:
                        DB.set_render_status(episode_id, "FAILED")
                        raise
                    DB.set_render_status(episode_id, "COMPLETE", result["output_path"])
                    self.json({"episode_id": episode_id, **result})
            elif path.startswith("/api/episodes/") and path.endswith("/package"):
                episode_id = path.split("/")[3]
                episode = DB.get_episode(episode_id)
                package = MetadataPackage.from_dict(payload["metadata"])
                output_directory = payload.get("output_directory") or str(ROOT / "outputs" / episode_id)
                timestamps = [float(value) for value in payload.get("thumbnail_timestamps", [])]
                video_path = episode.get("output_mp4_path") or str(ROOT / "outputs" / f"{episode_id}.mp4")
                commands = build_thumbnail_commands("ffmpeg", video_path, timestamps, output_directory) if timestamps else []
                if payload.get("execute", False):
                    paths = write_metadata_package(package, output_directory)
                    thumbnails = extract_thumbnails(video_path, timestamps, output_directory) if timestamps else []
                    DB.update_episode_metadata(episode_id, payload["metadata"])
                    self.json({"episode_id": episode_id, **paths, "thumbnails": thumbnails})
                else:
                    self.json({"episode_id": episode_id, "dry_run": True, "thumbnail_commands": commands})
            elif path.startswith("/api/episodes/") and path.endswith("/publish"):
                episode_id = path.split("/")[3]
                self.json(DB.queue_upload(episode_id, payload.get("privacy_status", "PRIVATE")), HTTPStatus.CREATED)
            elif path.startswith("/api/uploads/") and path.endswith("/run"):
                upload_id = path.split("/")[3]
                accepted = UPLOADS.submit(upload_id)
                self.json({"upload_id": upload_id, "accepted": accepted}, HTTPStatus.ACCEPTED if accepted else HTTPStatus.CONFLICT)
            else:
                self.json({"error": "not_found"}, HTTPStatus.NOT_FOUND)
        except ValueError as error:
            self.json({"error": "invalid_request", "message": str(error)}, HTTPStatus.BAD_REQUEST)
        except RenderError as error:
            self.json({"error": "render_error", "message": str(error)}, HTTPStatus.UNPROCESSABLE_ENTITY)
        except KeyError as error:
            self.json({"error": "not_found", "id": str(error.args[0])}, HTTPStatus.NOT_FOUND)

    def body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length) or b"{}")

    def json(self, value: object, status: HTTPStatus = HTTPStatus.OK) -> None:
        encoded = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(encoded)

    def serve_static(self, request_path: str) -> None:
        root = ROOT / ("dist" if (ROOT / "dist").exists() else "")
        relative = request_path.lstrip("/") or "index.html"
        target = (root / relative).resolve()
        if root.resolve() not in target.parents and target != root.resolve():
            return self.json({"error": "invalid_path"}, HTTPStatus.BAD_REQUEST)
        if not target.is_file():
            target = root / "index.html"
        content = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mimetypes.guess_type(target.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, format: str, *args: object) -> None:
        print(f"[AICUT] {self.address_string()} {format % args}")


def main() -> None:
    parser = argparse.ArgumentParser(description="AICUT local API and web server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()
    print(f"AICUT local runtime: http://{args.host}:{args.port}")
    ThreadingHTTPServer((args.host, args.port), ApiHandler).serve_forever()


if __name__ == "__main__":
    main()
