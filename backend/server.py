from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import uuid
from datetime import date, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .database import Database
from .pipeline import PipelineManager
from .render import RenderError, RenderPlan, export_plan, render
from .package import MetadataPackage, build_thumbnail_commands, extract_thumbnails, write_metadata_package
from .upload import UnconfiguredYouTubeClient, UploadManager, client_from_environment
from .oauth import OAuthYouTubeClient, YouTubeOAuth
from .token_store import EncryptedTokenStore
from .analytics import AnalyticsCollectionManager, YouTubeAnalyticsClient
from .strategy import aggregate_edit_strategies
from .calibration import calibrate_pacing
from .learning import analyze_source_output
from .performance import attribute_retention_to_cuts, performance_insights, validate_metrics
from .producer import run_producer
from .understanding import (
    PreprocessPlan, build_preprocess_commands, build_scan_plan, execute_preprocess,
    validate_transcript_segments,
)
from .stt import build_stt_command, SttJob, transcribe_tracks
from .scheduler import PeriodicTask, RuntimeScheduler
from .auth import ApiKeyGuard
from .http_utils import read_json_object
from .backup import DatabaseBackupManager
from .health import runtime_readiness
from .editor_export import export_editor_bundle
from .intelligence import run_reference_analyzer, validate_reference_analysis
from dataclasses import asdict

ROOT = Path(__file__).resolve().parents[1]
DB = PIPELINE = UPLOADS = None
YOUTUBE_TOKEN_STORE = YOUTUBE_OAUTH = None
SCHEDULER: RuntimeScheduler | None = None
API_AUTH = ApiKeyGuard(None)
MAX_REQUEST_BYTES = 1024 * 1024
MAX_MEDIA_BYTES = 50 * 1024 * 1024 * 1024
BACKUPS = None
READINESS_STORAGE = str(ROOT / "outputs")
READINESS_MIN_FREE_BYTES = 0
READINESS_TOOLS: tuple[str, ...] = ()


def initialize_runtime(environment=None) -> None:
    """Build runtime services explicitly; importing this module performs no I/O."""
    global DB, PIPELINE, UPLOADS, YOUTUBE_TOKEN_STORE, YOUTUBE_OAUTH
    global API_AUTH, MAX_REQUEST_BYTES, MAX_MEDIA_BYTES, BACKUPS, READINESS_STORAGE
    global READINESS_MIN_FREE_BYTES, READINESS_TOOLS
    environment = os.environ if environment is None else environment
    maximum = int(environment.get("AICUT_MAX_REQUEST_BYTES", str(1024 * 1024)))
    if maximum <= 0:
        raise ValueError("AICUT_MAX_REQUEST_BYTES는 0보다 커야 합니다.")
    database = Database(environment.get("AICUT_DB", str(ROOT / "aicut.db")))
    token_store = EncryptedTokenStore(
        environment["YOUTUBE_TOKEN_STORE"], environment["YOUTUBE_TOKEN_KEY"],
    ) if environment.get("YOUTUBE_TOKEN_STORE") and environment.get("YOUTUBE_TOKEN_KEY") else None
    oauth = YouTubeOAuth(
        environment["YOUTUBE_CLIENT_ID"], environment["YOUTUBE_CLIENT_SECRET"], environment["YOUTUBE_REDIRECT_URI"],
        token_store=token_store,
    ) if all(environment.get(key) for key in (
        "YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REDIRECT_URI",
    )) else None
    uploads = UploadManager(database, client_from_environment())
    if oauth and oauth.tokens:
        uploads.client = OAuthYouTubeClient(oauth)
    DB, PIPELINE, UPLOADS = database, PipelineManager(database), uploads
    YOUTUBE_TOKEN_STORE, YOUTUBE_OAUTH = token_store, oauth
    API_AUTH, MAX_REQUEST_BYTES = ApiKeyGuard(environment.get("AICUT_API_KEY")), maximum
    MAX_MEDIA_BYTES = int(environment.get("AICUT_MAX_MEDIA_BYTES", str(50 * 1024 * 1024 * 1024)))
    if MAX_MEDIA_BYTES <= 0:
        raise ValueError("AICUT_MAX_MEDIA_BYTES는 0보다 커야 합니다.")
    BACKUPS = DatabaseBackupManager(
        database, environment.get("AICUT_BACKUP_DIR", str(ROOT / "backups")),
        retention_count=int(environment.get("AICUT_BACKUP_RETENTION", "7")),
    )
    READINESS_STORAGE = environment.get("AICUT_OUTPUT_DIR", str(ROOT / "outputs"))
    READINESS_MIN_FREE_BYTES = int(environment.get("AICUT_MIN_FREE_BYTES", "0"))
    READINESS_TOOLS = tuple(filter(None, (
        item.strip() for item in environment.get("AICUT_REQUIRED_TOOLS", "").split(",")
    )))


def scheduled_uploads() -> object:
    if isinstance(UPLOADS.client, UnconfiguredYouTubeClient):
        return {"disabled": "YouTube OAuth 또는 access token이 필요합니다."}
    return UPLOADS.submit_due()


def scheduled_analytics() -> object:
    if not YOUTUBE_OAUTH or not YOUTUBE_OAUTH.tokens:
        return {"disabled": "YouTube Analytics OAuth가 필요합니다."}
    client = YouTubeAnalyticsClient(YOUTUBE_OAUTH.access_token)
    return AnalyticsCollectionManager(DB, client).run_due()


class ApiHandler(BaseHTTPRequestHandler):
    server_version = "AICUT/1.0"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if not API_AUTH.authorized(path, self.headers):
            return self.unauthorized()
        try:
            if path == "/api/health":
                self.json({"status": "ok", "service": "aicut-local-runtime"})
            elif path == "/api/ready":
                readiness = runtime_readiness(
                    DB, READINESS_STORAGE, SCHEDULER.status() if SCHEDULER else {},
                    min_free_bytes=READINESS_MIN_FREE_BYTES, required_tools=READINESS_TOOLS,
                )
                status = HTTPStatus.OK if readiness["status"] == "READY" else HTTPStatus.SERVICE_UNAVAILABLE
                self.json(readiness, status)
            elif path == "/api/projects":
                self.json(DB.list_projects())
            elif path.startswith("/api/projects/") and path.endswith("/candidates"):
                self.json(DB.list_candidates(path.split("/")[3]))
            elif path.startswith("/api/projects/") and path.endswith("/episodes"):
                self.json(DB.list_episodes(path.split("/")[3]))
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
            elif path == "/api/runtime/scheduler":
                self.json(SCHEDULER.status() if SCHEDULER else {"running": False})
            elif path == "/api/runtime/backups":
                self.json(BACKUPS.list())
            elif path == "/api/runtime/scheduler/runs":
                query = parse_qs(parsed.query)
                self.json(DB.list_scheduler_runs(int(query.get("limit", ["50"])[0])))
            elif path == "/api/youtube/oauth/start":
                if not YOUTUBE_OAUTH:
                    raise ValueError("YouTube OAuth 환경변수가 설정되지 않았습니다.")
                self.json(YOUTUBE_OAUTH.authorization_url())
            elif path == "/api/youtube/oauth/callback":
                if not YOUTUBE_OAUTH:
                    raise ValueError("YouTube OAuth 환경변수가 설정되지 않았습니다.")
                query = parse_qs(parsed.query)
                tokens = YOUTUBE_OAUTH.exchange_callback(
                    query.get("code", [""])[0], query.get("state", [""])[0],
                )
                UPLOADS.client = OAuthYouTubeClient(YOUTUBE_OAUTH)
                self.json({"authorized": True, "expires_at": tokens.expires_at})
            elif path == "/api/calibrations":
                self.json(DB.list_calibrations())
            elif path == "/api/strategies":
                channel_ref = parse_qs(parsed.query).get("channel_ref", [""])[0]
                self.json(DB.list_strategy_versions(channel_ref))
            elif path == "/api/learning/source-output":
                self.json(DB.list_source_output_pairs())
            elif path == "/api/youtube/references":
                self.json(DB.list_youtube_references())
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
        if not API_AUTH.authorized(path, self.headers):
            return self.unauthorized()
        try:
            if path == "/api/media":
                return self.receive_media()
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
            elif path == "/api/youtube/references":
                self.json(DB.save_youtube_reference(validate_reference_analysis(payload)), HTTPStatus.CREATED)
            elif path == "/api/youtube/references/analyze":
                executable = payload.get("executable")
                if not isinstance(executable, list):
                    raise ValueError("executable은 셸 문자열이 아닌 인자 배열이어야 합니다.")
                metadata = payload.get("metadata")
                if not isinstance(metadata, dict):
                    raise ValueError("metadata 객체가 필요합니다.")
                output = payload.get("output_directory") or str(ROOT / "artifacts" / "references" / uuid.uuid4().hex)
                result = run_reference_analyzer(executable, metadata, output)
                saved = DB.save_youtube_reference(result["analysis"])
                self.json({"reference": saved, "command": result["command"]}, HTTPStatus.CREATED)
            elif path.startswith("/api/episodes/") and path.endswith("/performance"):
                episode_id = path.split("/")[3]
                metrics = validate_metrics(payload["metrics"])
                version = DB.latest_planning_version_for_episode(episode_id)
                if version:
                    metrics["planning_version_id"] = version["planning_version_id"]
                    metrics["planning_version_number"] = version["version_number"]
                if payload.get("attribution_profile"):
                    metrics["cut_attribution"] = attribute_retention_to_cuts(
                        metrics, DB.get_timeline(episode_id), payload["attribution_profile"],
                    )
                snapshot = DB.save_performance(episode_id, metrics)
                snapshot["insights"] = performance_insights(metrics, payload["profile"])
                self.json(snapshot, HTTPStatus.CREATED)
            elif path.startswith("/api/episodes/") and path.endswith("/analytics/collect"):
                episode_id = path.split("/")[3]
                episode = DB.get_episode(episode_id)
                if not YOUTUBE_OAUTH:
                    raise ValueError("YouTube Analytics OAuth가 설정되지 않았습니다.")
                video_id = payload.get("video_id") or next((
                    item["youtube_video_id"] for item in DB.list_uploads()
                    if item["episode_id"] == episode_id and item["status"] == "COMPLETE"
                ), None)
                duration = episode.get("planned_duration_sec") or sum(
                    item["source_end_sec"] - item["source_start_sec"]
                    for item in DB.get_timeline(episode_id) if item["pacing_mode"] != "CUT"
                )
                metrics = YouTubeAnalyticsClient(YOUTUBE_OAUTH.access_token).collect_video_metrics(
                    video_id, date.fromisoformat(payload["start_date"]),
                    date.fromisoformat(payload["end_date"]), duration,
                )
                self.json(DB.save_performance(episode_id, metrics), HTTPStatus.CREATED)
            elif path == "/api/analytics/run-due":
                if not YOUTUBE_OAUTH:
                    raise ValueError("YouTube Analytics OAuth가 설정되지 않았습니다.")
                manager = AnalyticsCollectionManager(DB, YouTubeAnalyticsClient(YOUTUBE_OAUTH.access_token))
                self.json(manager.run_due(), HTTPStatus.OK)
            elif path == "/api/uploads/run-due":
                self.json(UPLOADS.submit_due(), HTTPStatus.ACCEPTED)
            elif path == "/api/runtime/backup":
                self.json(BACKUPS.create(), HTTPStatus.CREATED)
            elif path == "/api/strategies/analyze":
                channel_ref = str(payload.get("channel_ref", "")).strip()
                if not channel_ref:
                    raise ValueError("channel_ref가 필요합니다.")
                strategy = aggregate_edit_strategies(
                    DB.list_channel_performance(channel_ref), payload["profile"],
                )
                self.json(DB.save_strategy_version(channel_ref, strategy), HTTPStatus.CREATED)
            elif path.startswith("/api/strategies/") and path.endswith("/activate"):
                self.json(DB.activate_strategy_version(path.split("/")[3]))
            elif path.startswith("/api/projects/") and path.endswith("/run"):
                project_id = path.split("/")[3]
                DB.get_project(project_id)
                accepted = PIPELINE.submit(
                    project_id, payload.get("manifest_path"), options=payload.get("options"),
                    resume=bool(payload.get("resume", True)),
                )
                self.json({"accepted": accepted, "project_id": project_id}, HTTPStatus.ACCEPTED if accepted else HTTPStatus.CONFLICT)
            elif path.startswith("/api/projects/") and path.endswith("/cancel"):
                project_id = path.split("/")[3]
                DB.get_project(project_id)
                accepted = PIPELINE.cancel(project_id)
                self.json({"accepted": accepted, "project_id": project_id}, HTTPStatus.ACCEPTED if accepted else HTTPStatus.CONFLICT)
            elif path.startswith("/api/projects/") and path.endswith("/analysis"):
                project_id = path.split("/")[3]
                self.json(DB.import_analysis(project_id, payload))
            elif path.startswith("/api/projects/") and path.endswith("/produce"):
                project_id = path.split("/")[3]
                project = DB.get_project(project_id)
                executable = payload.get("executable")
                if not isinstance(executable, list):
                    raise ValueError("executable은 셸 문자열이 아닌 인자 배열이어야 합니다.")
                output_directory = payload.get("output_directory") or str(ROOT / "artifacts" / project_id / "producer")
                result = run_producer(executable, DB.analysis_input(project_id), output_directory, project["duration_sec"])
                counts = DB.import_analysis(project_id, result["manifest"])
                self.json({"project_id": project_id, "counts": counts, "command": result["command"]}, HTTPStatus.CREATED)
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
            elif path.startswith("/api/episodes/") and path.endswith("/editor-export"):
                episode_id = path.split("/")[3]
                episode = DB.get_episode(episode_id)
                output_directory = payload.get("output_directory") or str(ROOT / "exports" / episode_id)
                result = export_editor_bundle(
                    episode["file_path"], DB.get_timeline(episode_id), output_directory,
                    title=payload.get("title") or episode.get("title") or episode_id,
                    fps=int(payload.get("fps", 30)),
                )
                self.json({"episode_id": episode_id, **result}, HTTPStatus.CREATED)
            elif path.startswith("/api/episodes/") and path.endswith("/publish"):
                episode_id = path.split("/")[3]
                self.json(DB.queue_upload(episode_id, payload.get("privacy_status", "PRIVATE")), HTTPStatus.CREATED)
            elif path.startswith("/api/uploads/") and path.endswith("/run"):
                upload_id = path.split("/")[3]
                accepted = UPLOADS.submit(upload_id)
                self.json({"upload_id": upload_id, "accepted": accepted}, HTTPStatus.ACCEPTED if accepted else HTTPStatus.CONFLICT)
            elif path.startswith("/api/uploads/") and path.endswith("/cancel"):
                upload_id = path.split("/")[3]
                accepted = UPLOADS.cancel(upload_id)
                self.json(
                    {"upload_id": upload_id, "accepted": accepted},
                    HTTPStatus.ACCEPTED if accepted else HTTPStatus.CONFLICT,
                )
            elif path.startswith("/api/uploads/") and path.endswith("/thumbnail"):
                upload_id = path.split("/")[3]
                job = next((item for item in DB.list_uploads() if item["upload_id"] == upload_id), None)
                if not job or job["status"] != "COMPLETE" or not job.get("youtube_video_id"):
                    raise ValueError("완료된 YouTube 업로드가 필요합니다.")
                thumbnail_path = payload.get("thumbnail_path") or job.get("thumbnail_path")
                method = getattr(UPLOADS.client, "upload_thumbnail", None)
                if not method:
                    raise ValueError("현재 YouTube 클라이언트가 썸네일 업로드를 지원하지 않습니다.")
                result = method(job["youtube_video_id"], thumbnail_path)
                self.json({"upload": DB.record_thumbnail_uploaded(upload_id), "youtube": result})
            elif path.startswith("/api/uploads/") and path.endswith("/publication"):
                upload_id = path.split("/")[3]
                job = next((item for item in DB.list_uploads() if item["upload_id"] == upload_id), None)
                if not job or job["status"] != "COMPLETE" or not job.get("youtube_video_id"):
                    raise ValueError("완료된 YouTube 업로드가 필요합니다.")
                privacy = str(payload.get("privacy_status", "")).upper()
                publish_at = datetime.fromisoformat(payload["publish_at"]) if payload.get("publish_at") else None
                method = getattr(UPLOADS.client, "update_video_status", None)
                if not method:
                    raise ValueError("현재 YouTube 클라이언트가 공개 상태 변경을 지원하지 않습니다.")
                result = method(job["youtube_video_id"], privacy, publish_at)
                recorded = DB.record_upload_publication(
                    upload_id, "SCHEDULED" if publish_at else privacy,
                    publish_at.isoformat() if publish_at else None,
                )
                self.json({"upload": recorded, "youtube": result})
            else:
                self.json({"error": "not_found"}, HTTPStatus.NOT_FOUND)
        except ValueError as error:
            self.json({"error": "invalid_request", "message": str(error)}, HTTPStatus.BAD_REQUEST)
        except RenderError as error:
            self.json({"error": "render_error", "message": str(error)}, HTTPStatus.UNPROCESSABLE_ENTITY)
        except KeyError as error:
            self.json({"error": "not_found", "id": str(error.args[0])}, HTTPStatus.NOT_FOUND)
        except Exception as error:
            self.json({"error": "internal_error", "message": str(error)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def body(self) -> dict:
        return read_json_object(
            self.rfile, self.headers.get("Content-Length"), self.headers.get("Content-Type"),
            max_bytes=MAX_REQUEST_BYTES,
        )

    def receive_media(self) -> None:
        """Stream a browser-selected video to private runtime storage."""
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError as error:
            raise ValueError("올바른 Content-Length가 필요합니다.") from error
        if length <= 0 or length > MAX_MEDIA_BYTES:
            raise ValueError("미디어 크기가 허용 범위를 벗어났습니다.")
        supplied = self.headers.get("X-Filename", "upload.mp4")
        safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", Path(supplied).name) or "upload.mp4"
        destination = (ROOT / "uploads" / f"{uuid.uuid4().hex}-{safe_name}").resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".part")
        remaining = length
        try:
            with temporary.open("xb") as target:
                while remaining:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise ValueError("미디어 요청 본문이 중간에 종료되었습니다.")
                    target.write(chunk)
                    remaining -= len(chunk)
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
        self.json({"file_path": str(destination), "size_bytes": length}, HTTPStatus.CREATED)

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    def unauthorized(self) -> None:
        encoded = json.dumps({
            "error": "unauthorized", "message": "유효한 Bearer API key가 필요합니다.",
        }, ensure_ascii=False).encode()
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("WWW-Authenticate", 'Bearer realm="aicut"')
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(encoded)

    def json(self, value: object, status: HTTPStatus = HTTPStatus.OK) -> None:
        encoded = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(encoded)

    def serve_static(self, request_path: str) -> None:
        root = ROOT / "dist"
        if not root.is_dir():
            return self.json(
                {"error": "frontend_not_built", "message": "npm run build를 먼저 실행하세요."},
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
        relative = request_path.lstrip("/") or "index.html"
        target = (root / relative).resolve()
        if root.resolve() not in target.parents and target != root.resolve():
            return self.json({"error": "invalid_path"}, HTTPStatus.BAD_REQUEST)
        if not target.is_file():
            if Path(relative).suffix:
                return self.json({"error": "not_found"}, HTTPStatus.NOT_FOUND)
            target = root / "index.html"
        content = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mimetypes.guess_type(target.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, format: str, *args: object) -> None:
        print(f"[AICUT] {self.address_string()} {format % args}")


def create_app(environment=None) -> type[ApiHandler]:
    """Configure runtime dependencies and return the request-handler application."""
    initialize_runtime(environment)
    return ApiHandler


def main() -> None:
    global SCHEDULER
    parser = argparse.ArgumentParser(description="AICUT local API and web server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()
    create_app()
    interval = float(os.environ.get("AICUT_SCHEDULER_INTERVAL_SEC", "60"))
    backup_interval = float(os.environ.get("AICUT_BACKUP_INTERVAL_SEC", "86400"))
    SCHEDULER = RuntimeScheduler({
        "uploads": scheduled_uploads,
        "analytics": scheduled_analytics,
        "backups": PeriodicTask(BACKUPS.create, backup_interval),
    }, interval, on_run=lambda results, completed_at: DB.save_scheduler_run(results, completed_at))
    SCHEDULER.start()
    print(f"AICUT local runtime: http://{args.host}:{args.port}")
    server = ThreadingHTTPServer((args.host, args.port), ApiHandler)
    try:
        server.serve_forever()
    finally:
        SCHEDULER.stop(5)
        PIPELINE.cancel_all()
        UPLOADS.cancel_all()
        PIPELINE.shutdown(cancel_running=False)
        UPLOADS.shutdown(cancel_running=False)
        server.server_close()


if __name__ == "__main__":
    main()
