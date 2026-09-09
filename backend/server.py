from __future__ import annotations

import argparse
import json
import mimetypes
import os
from dataclasses import asdict, dataclass
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
from .static_files import StaticFileError, resolve_static_file

ROOT = Path(__file__).resolve().parents[1]


@dataclass
class Runtime:
    database: Database
    pipeline: PipelineManager
    uploads: UploadManager
    youtube_oauth: YouTubeOAuth | None
    auth: ApiKeyGuard
    max_request_bytes: int
    backups: DatabaseBackupManager
    readiness_storage: str | Path
    readiness_min_free_bytes: int
    readiness_tools: tuple[str, ...]
    scheduler: RuntimeScheduler | None = None

    def scheduled_uploads(self) -> object:
        if isinstance(self.uploads.client, UnconfiguredYouTubeClient):
            return {"disabled": "YouTube OAuth 또는 access token이 필요합니다."}
        return self.uploads.submit_due()

    def scheduled_analytics(self) -> object:
        if not self.youtube_oauth or not self.youtube_oauth.tokens:
            return {"disabled": "YouTube Analytics OAuth가 필요합니다."}
        client = YouTubeAnalyticsClient(self.youtube_oauth.access_token)
        return AnalyticsCollectionManager(self.database, client).run_due()

    def start_scheduler(self, interval: float, backup_interval: float) -> None:
        self.scheduler = RuntimeScheduler({
            "uploads": self.scheduled_uploads,
            "analytics": self.scheduled_analytics,
            "backups": PeriodicTask(self.backups.create, backup_interval),
        }, interval, on_run=lambda results, completed_at: self.database.save_scheduler_run(results, completed_at))
        self.scheduler.start()

    def shutdown(self) -> None:
        if self.scheduler:
            self.scheduler.stop(5)
        self.pipeline.cancel_all()
        self.uploads.cancel_all()
        self.pipeline.shutdown(cancel_running=False)
        self.uploads.shutdown(cancel_running=False)


def create_runtime(environment: dict[str, str] | os._Environ[str] | None = None) -> Runtime:
    env = os.environ if environment is None else environment
    max_request_bytes = int(env.get("AICUT_MAX_REQUEST_BYTES", str(1024 * 1024)))
    if max_request_bytes <= 0:
        raise ValueError("AICUT_MAX_REQUEST_BYTES는 0보다 커야 합니다.")
    database = Database(env.get("AICUT_DB", str(ROOT / "aicut.db")))
    token_store = EncryptedTokenStore(
        env["YOUTUBE_TOKEN_STORE"], env["YOUTUBE_TOKEN_KEY"],
    ) if env.get("YOUTUBE_TOKEN_STORE") and env.get("YOUTUBE_TOKEN_KEY") else None
    youtube_oauth = YouTubeOAuth(
        env["YOUTUBE_CLIENT_ID"], env["YOUTUBE_CLIENT_SECRET"], env["YOUTUBE_REDIRECT_URI"],
        token_store=token_store,
    ) if all(env.get(key) for key in (
        "YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REDIRECT_URI",
    )) else None
    uploads = UploadManager(database, client_from_environment(env))
    if youtube_oauth and youtube_oauth.tokens:
        uploads.client = OAuthYouTubeClient(youtube_oauth)
    return Runtime(
        database=database, pipeline=PipelineManager(database), uploads=uploads,
        youtube_oauth=youtube_oauth, auth=ApiKeyGuard(env.get("AICUT_API_KEY")),
        max_request_bytes=max_request_bytes,
        backups=DatabaseBackupManager(
            database, env.get("AICUT_BACKUP_DIR", str(ROOT / "backups")),
            retention_count=int(env.get("AICUT_BACKUP_RETENTION", "7")),
        ),
        readiness_storage=env.get("AICUT_OUTPUT_DIR", str(ROOT / "outputs")),
        readiness_min_free_bytes=int(env.get("AICUT_MIN_FREE_BYTES", "0")),
        readiness_tools=tuple(filter(None, (
            item.strip() for item in env.get("AICUT_REQUIRED_TOOLS", "").split(",")
        ))),
    )


class ApiHandler(BaseHTTPRequestHandler):
    server_version = "AICUT/1.0"

    @property
    def runtime(self) -> Runtime:
        return self.server.runtime

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if not self.runtime.auth.authorized(path, self.headers):
            return self.unauthorized()
        try:
            if path == "/api/health":
                self.json({"status": "ok", "service": "aicut-local-runtime"})
            elif path == "/api/ready":
                readiness = runtime_readiness(
                    self.runtime.database, self.runtime.readiness_storage, self.runtime.scheduler.status() if self.runtime.scheduler else {},
                    min_free_bytes=self.runtime.readiness_min_free_bytes, required_tools=self.runtime.readiness_tools,
                )
                status = HTTPStatus.OK if readiness["status"] == "READY" else HTTPStatus.SERVICE_UNAVAILABLE
                self.json(readiness, status)
            elif path == "/api/projects":
                self.json(self.runtime.database.list_projects())
            elif path.startswith("/api/projects/") and path.endswith("/candidates"):
                self.json(self.runtime.database.list_candidates(path.split("/")[3]))
            elif path.startswith("/api/projects/") and path.endswith("/episodes"):
                self.json(self.runtime.database.list_episodes(path.split("/")[3]))
            elif path.startswith("/api/projects/") and path.endswith("/logs"):
                self.json(self.runtime.database.logs(path.split("/")[3]))
            elif path.startswith("/api/projects/") and path.endswith("/job"):
                self.json(self.runtime.pipeline.state(path.split("/")[3]))
            elif path.startswith("/api/projects/"):
                self.json(self.runtime.database.get_project(path.split("/")[3]))
            elif path.startswith("/api/episodes/") and path.endswith("/timeline"):
                self.json(self.runtime.database.get_timeline(path.split("/")[3]))
            elif path == "/api/logs":
                self.json(self.runtime.database.logs())
            elif path == "/api/uploads":
                self.json(self.runtime.database.list_uploads())
            elif path == "/api/runtime/scheduler":
                self.json(self.runtime.scheduler.status() if self.runtime.scheduler else {"running": False})
            elif path == "/api/runtime/backups":
                self.json(self.runtime.backups.list())
            elif path == "/api/runtime/scheduler/runs":
                query = parse_qs(parsed.query)
                self.json(self.runtime.database.list_scheduler_runs(int(query.get("limit", ["50"])[0])))
            elif path == "/api/youtube/oauth/start":
                if not self.runtime.youtube_oauth:
                    raise ValueError("YouTube OAuth 환경변수가 설정되지 않았습니다.")
                self.json(self.runtime.youtube_oauth.authorization_url())
            elif path == "/api/youtube/oauth/callback":
                if not self.runtime.youtube_oauth:
                    raise ValueError("YouTube OAuth 환경변수가 설정되지 않았습니다.")
                query = parse_qs(parsed.query)
                tokens = self.runtime.youtube_oauth.exchange_callback(
                    query.get("code", [""])[0], query.get("state", [""])[0],
                )
                self.runtime.uploads.client = OAuthYouTubeClient(self.runtime.youtube_oauth)
                self.json({"authorized": True, "expires_at": tokens.expires_at})
            elif path == "/api/calibrations":
                self.json(self.runtime.database.list_calibrations())
            elif path == "/api/strategies":
                channel_ref = parse_qs(parsed.query).get("channel_ref", [""])[0]
                self.json(self.runtime.database.list_strategy_versions(channel_ref))
            elif path == "/api/learning/source-output":
                self.json(self.runtime.database.list_source_output_pairs())
            elif path.startswith("/api/episodes/") and path.endswith("/performance"):
                self.json(self.runtime.database.list_performance(path.split("/")[3]))
            else:
                self.serve_static(path)
        except ValueError as error:
            self.json({"error": "invalid_request", "message": str(error)}, HTTPStatus.BAD_REQUEST)
        except KeyError as error:
            self.json({"error": "not_found", "id": str(error.args[0])}, HTTPStatus.NOT_FOUND)
        except Exception as error:
            self.log_error("Unhandled GET error: %s", error)
            self.json(
                {"error": "internal_error", "message": "요청을 처리하는 중 내부 오류가 발생했습니다."},
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if not self.runtime.auth.authorized(path, self.headers):
            return self.unauthorized()
        try:
            payload = self.body()
            if path == "/api/projects":
                if not payload.get("file_path"):
                    return self.json({"error": "file_path_required"}, HTTPStatus.BAD_REQUEST)
                self.json(self.runtime.database.create_project(payload), HTTPStatus.CREATED)
            elif path == "/api/calibrations":
                result = calibrate_pacing(payload.get("samples", []))
                profile = self.runtime.database.save_calibration(
                    payload.get("channel_ref", "default"), payload.get("name", "Pacing profile"),
                    {"pacing": result.params, "evaluation": result.to_dict()}, result.f1 * 100,
                )
                self.json(profile, HTTPStatus.CREATED)
            elif path == "/api/learning/source-output":
                cuts = payload.get("cuts")
                if cuts is None and payload.get("episode_id"):
                    cuts = self.runtime.database.get_timeline(payload["episode_id"])
                analysis = analyze_source_output(payload["source_duration_sec"], cuts or [])
                pair = self.runtime.database.save_source_output_pair(
                    payload["source_ref"], payload["output_ref"], analysis, payload.get("project_id"),
                )
                self.json(pair, HTTPStatus.CREATED)
            elif path.startswith("/api/episodes/") and path.endswith("/performance"):
                episode_id = path.split("/")[3]
                metrics = validate_metrics(payload["metrics"])
                version = self.runtime.database.latest_planning_version_for_episode(episode_id)
                if version:
                    metrics["planning_version_id"] = version["planning_version_id"]
                    metrics["planning_version_number"] = version["version_number"]
                if payload.get("attribution_profile"):
                    metrics["cut_attribution"] = attribute_retention_to_cuts(
                        metrics, self.runtime.database.get_timeline(episode_id), payload["attribution_profile"],
                    )
                snapshot = self.runtime.database.save_performance(episode_id, metrics)
                snapshot["insights"] = performance_insights(metrics, payload["profile"])
                self.json(snapshot, HTTPStatus.CREATED)
            elif path.startswith("/api/episodes/") and path.endswith("/analytics/collect"):
                episode_id = path.split("/")[3]
                episode = self.runtime.database.get_episode(episode_id)
                if not self.runtime.youtube_oauth:
                    raise ValueError("YouTube Analytics OAuth가 설정되지 않았습니다.")
                video_id = payload.get("video_id") or next((
                    item["youtube_video_id"] for item in self.runtime.database.list_uploads()
                    if item["episode_id"] == episode_id and item["status"] == "COMPLETE"
                ), None)
                duration = episode.get("planned_duration_sec") or sum(
                    item["source_end_sec"] - item["source_start_sec"]
                    for item in self.runtime.database.get_timeline(episode_id) if item["pacing_mode"] != "CUT"
                )
                metrics = YouTubeAnalyticsClient(self.runtime.youtube_oauth.access_token).collect_video_metrics(
                    video_id, date.fromisoformat(payload["start_date"]),
                    date.fromisoformat(payload["end_date"]), duration,
                )
                self.json(self.runtime.database.save_performance(episode_id, metrics), HTTPStatus.CREATED)
            elif path == "/api/analytics/run-due":
                if not self.runtime.youtube_oauth:
                    raise ValueError("YouTube Analytics OAuth가 설정되지 않았습니다.")
                manager = AnalyticsCollectionManager(self.runtime.database, YouTubeAnalyticsClient(self.runtime.youtube_oauth.access_token))
                self.json(manager.run_due(), HTTPStatus.OK)
            elif path == "/api/uploads/run-due":
                self.json(self.runtime.uploads.submit_due(), HTTPStatus.ACCEPTED)
            elif path == "/api/runtime/backup":
                self.json(self.runtime.backups.create(), HTTPStatus.CREATED)
            elif path == "/api/strategies/analyze":
                channel_ref = str(payload.get("channel_ref", "")).strip()
                if not channel_ref:
                    raise ValueError("channel_ref가 필요합니다.")
                strategy = aggregate_edit_strategies(
                    self.runtime.database.list_channel_performance(channel_ref), payload["profile"],
                )
                self.json(self.runtime.database.save_strategy_version(channel_ref, strategy), HTTPStatus.CREATED)
            elif path.startswith("/api/strategies/") and path.endswith("/activate"):
                self.json(self.runtime.database.activate_strategy_version(path.split("/")[3]))
            elif path.startswith("/api/projects/") and path.endswith("/run"):
                project_id = path.split("/")[3]
                self.runtime.database.get_project(project_id)
                accepted = self.runtime.pipeline.submit(
                    project_id, payload.get("manifest_path"), options=payload.get("options"),
                    resume=bool(payload.get("resume", True)),
                )
                self.json({"accepted": accepted, "project_id": project_id}, HTTPStatus.ACCEPTED if accepted else HTTPStatus.CONFLICT)
            elif path.startswith("/api/projects/") and path.endswith("/cancel"):
                project_id = path.split("/")[3]
                self.runtime.database.get_project(project_id)
                accepted = self.runtime.pipeline.cancel(project_id)
                self.json({"accepted": accepted, "project_id": project_id}, HTTPStatus.ACCEPTED if accepted else HTTPStatus.CONFLICT)
            elif path.startswith("/api/projects/") and path.endswith("/analysis"):
                project_id = path.split("/")[3]
                self.json(self.runtime.database.import_analysis(project_id, payload))
            elif path.startswith("/api/projects/") and path.endswith("/produce"):
                project_id = path.split("/")[3]
                project = self.runtime.database.get_project(project_id)
                executable = payload.get("executable")
                if not isinstance(executable, list):
                    raise ValueError("executable은 셸 문자열이 아닌 인자 배열이어야 합니다.")
                output_directory = payload.get("output_directory") or str(ROOT / "artifacts" / project_id / "producer")
                result = run_producer(executable, self.runtime.database.analysis_input(project_id), output_directory, project["duration_sec"])
                counts = self.runtime.database.import_analysis(project_id, result["manifest"])
                self.json({"project_id": project_id, "counts": counts, "command": result["command"]}, HTTPStatus.CREATED)
            elif path.startswith("/api/projects/") and path.endswith("/preprocess"):
                project_id = path.split("/")[3]
                project = self.runtime.database.get_project(project_id)
                media = json.loads(project.get("media_info_json") or "{}")
                plan = PreprocessPlan(
                    source_path=project["file_path"],
                    output_directory=payload.get("output_directory") or str(ROOT / "artifacts" / project_id),
                    audio_tracks=int(payload.get("audio_tracks", media.get("audio_tracks", 0))),
                    frame_interval_sec=float(payload["frame_interval_sec"]),
                )
                if payload.get("execute", False):
                    result = execute_preprocess(plan)
                    self.runtime.database.add_artifacts(project_id, [
                        {"kind": item["kind"], "path": item["path"], "metadata": {"command": item["command"]}}
                        for item in result["artifacts"]
                    ])
                    self.json({"project_id": project_id, **result})
                else:
                    self.json({"project_id": project_id, "dry_run": True, "commands": build_preprocess_commands(plan)})
            elif path.startswith("/api/projects/") and path.endswith("/scan-plan"):
                project_id = path.split("/")[3]
                project = self.runtime.database.get_project(project_id)
                windows = build_scan_plan(
                    project["duration_sec"], payload["coarse_window_sec"], payload.get("precision_ranges"),
                )
                encoded = [asdict(window) for window in windows]
                self.runtime.database.replace_scan_windows(project_id, encoded)
                self.json({"project_id": project_id, "windows": encoded}, HTTPStatus.CREATED)
            elif path.startswith("/api/projects/") and path.endswith("/transcript"):
                project_id = path.split("/")[3]
                project = self.runtime.database.get_project(project_id)
                segments = validate_transcript_segments(payload.get("segments", []), project["duration_sec"])
                self.runtime.database.replace_transcript(project_id, segments)
                self.json({"project_id": project_id, "segment_count": len(segments)}, HTTPStatus.CREATED)
            elif path.startswith("/api/projects/") and path.endswith("/transcribe"):
                project_id = path.split("/")[3]
                project = self.runtime.database.get_project(project_id)
                executable = payload.get("executable")
                audio_paths = payload.get("audio_paths", [])
                if not isinstance(executable, list) or not all(isinstance(value, str) for value in executable):
                    raise ValueError("executable은 셸 문자열이 아닌 인자 배열이어야 합니다.")
                output_directory = payload.get("output_directory") or str(ROOT / "artifacts" / project_id / "stt")
                if payload.get("execute", False):
                    result = transcribe_tracks(
                        executable, audio_paths, project["duration_sec"], output_directory, payload.get("language"),
                    )
                    self.runtime.database.replace_transcript(project_id, result["segments"])
                    self.json({"project_id": project_id, **result}, HTTPStatus.CREATED)
                else:
                    commands = [build_stt_command(executable, SttJob(
                        audio_path, index, str(Path(output_directory) / f"audio-track-{index:02d}.json"), payload.get("language"),
                    )) for index, audio_path in enumerate(audio_paths)]
                    self.json({"project_id": project_id, "dry_run": True, "commands": commands})
            elif path.startswith("/api/candidates/") and path.endswith("/review"):
                self.json(self.runtime.database.review_candidate(path.split("/")[3], payload.get("decision", ""), payload.get("feedback", "")))
            elif path.startswith("/api/episodes/") and path.endswith("/review"):
                self.json(self.runtime.database.review_episode(path.split("/")[3], bool(payload.get("approved"))))
            elif path.startswith("/api/episodes/") and path.endswith("/render"):
                episode_id = path.split("/")[3]
                episode = self.runtime.database.get_episode(episode_id)
                output_path = payload.get("output_path") or str(ROOT / "outputs" / f"{episode_id}.mp4")
                plan = RenderPlan(
                    input_path=episode["file_path"], output_path=output_path,
                    cuts=tuple(self.runtime.database.get_timeline(episode_id)),
                    width=int(payload.get("width", 1920)), height=int(payload.get("height", 1080)),
                )
                if not payload.get("execute", False):
                    self.json({"episode_id": episode_id, "dry_run": True, "plan": json.loads(export_plan(plan))})
                else:
                    self.runtime.database.set_render_status(episode_id, "RENDERING")
                    try:
                        result = render(plan)
                    except Exception:
                        self.runtime.database.set_render_status(episode_id, "FAILED")
                        raise
                    self.runtime.database.set_render_status(episode_id, "COMPLETE", result["output_path"])
                    self.json({"episode_id": episode_id, **result})
            elif path.startswith("/api/episodes/") and path.endswith("/package"):
                episode_id = path.split("/")[3]
                episode = self.runtime.database.get_episode(episode_id)
                package = MetadataPackage.from_dict(payload["metadata"])
                output_directory = payload.get("output_directory") or str(ROOT / "outputs" / episode_id)
                timestamps = [float(value) for value in payload.get("thumbnail_timestamps", [])]
                video_path = episode.get("output_mp4_path") or str(ROOT / "outputs" / f"{episode_id}.mp4")
                commands = build_thumbnail_commands("ffmpeg", video_path, timestamps, output_directory) if timestamps else []
                if payload.get("execute", False):
                    paths = write_metadata_package(package, output_directory)
                    thumbnails = extract_thumbnails(video_path, timestamps, output_directory) if timestamps else []
                    self.runtime.database.update_episode_metadata(episode_id, payload["metadata"])
                    self.json({"episode_id": episode_id, **paths, "thumbnails": thumbnails})
                else:
                    self.json({"episode_id": episode_id, "dry_run": True, "thumbnail_commands": commands})
            elif path.startswith("/api/episodes/") and path.endswith("/editor-export"):
                episode_id = path.split("/")[3]
                episode = self.runtime.database.get_episode(episode_id)
                output_directory = payload.get("output_directory") or str(ROOT / "exports" / episode_id)
                result = export_editor_bundle(
                    episode["file_path"], self.runtime.database.get_timeline(episode_id), output_directory,
                    title=payload.get("title") or episode.get("title") or episode_id,
                    fps=int(payload.get("fps", 30)),
                )
                self.json({"episode_id": episode_id, **result}, HTTPStatus.CREATED)
            elif path.startswith("/api/episodes/") and path.endswith("/publish"):
                episode_id = path.split("/")[3]
                self.json(self.runtime.database.queue_upload(episode_id, payload.get("privacy_status", "PRIVATE")), HTTPStatus.CREATED)
            elif path.startswith("/api/uploads/") and path.endswith("/run"):
                upload_id = path.split("/")[3]
                accepted = self.runtime.uploads.submit(upload_id)
                self.json({"upload_id": upload_id, "accepted": accepted}, HTTPStatus.ACCEPTED if accepted else HTTPStatus.CONFLICT)
            elif path.startswith("/api/uploads/") and path.endswith("/cancel"):
                upload_id = path.split("/")[3]
                accepted = self.runtime.uploads.cancel(upload_id)
                self.json(
                    {"upload_id": upload_id, "accepted": accepted},
                    HTTPStatus.ACCEPTED if accepted else HTTPStatus.CONFLICT,
                )
            elif path.startswith("/api/uploads/") and path.endswith("/thumbnail"):
                upload_id = path.split("/")[3]
                job = next((item for item in self.runtime.database.list_uploads() if item["upload_id"] == upload_id), None)
                if not job or job["status"] != "COMPLETE" or not job.get("youtube_video_id"):
                    raise ValueError("완료된 YouTube 업로드가 필요합니다.")
                thumbnail_path = payload.get("thumbnail_path") or job.get("thumbnail_path")
                method = getattr(self.runtime.uploads.client, "upload_thumbnail", None)
                if not method:
                    raise ValueError("현재 YouTube 클라이언트가 썸네일 업로드를 지원하지 않습니다.")
                result = method(job["youtube_video_id"], thumbnail_path)
                self.json({"upload": self.runtime.database.record_thumbnail_uploaded(upload_id), "youtube": result})
            elif path.startswith("/api/uploads/") and path.endswith("/publication"):
                upload_id = path.split("/")[3]
                job = next((item for item in self.runtime.database.list_uploads() if item["upload_id"] == upload_id), None)
                if not job or job["status"] != "COMPLETE" or not job.get("youtube_video_id"):
                    raise ValueError("완료된 YouTube 업로드가 필요합니다.")
                privacy = str(payload.get("privacy_status", "")).upper()
                publish_at = datetime.fromisoformat(payload["publish_at"]) if payload.get("publish_at") else None
                method = getattr(self.runtime.uploads.client, "update_video_status", None)
                if not method:
                    raise ValueError("현재 YouTube 클라이언트가 공개 상태 변경을 지원하지 않습니다.")
                result = method(job["youtube_video_id"], privacy, publish_at)
                recorded = self.runtime.database.record_upload_publication(
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
            self.log_error("Unhandled POST error: %s", error)
            self.json(
                {"error": "internal_error", "message": "요청을 처리하는 중 내부 오류가 발생했습니다."},
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    def body(self) -> dict:
        return read_json_object(
            self.rfile, self.headers.get("Content-Length"), self.headers.get("Content-Type"),
            max_bytes=self.runtime.max_request_bytes,
        )

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
        try:
            target = resolve_static_file(ROOT / "dist", request_path)
        except StaticFileError:
            return self.json({"error": "invalid_path"}, HTTPStatus.BAD_REQUEST)
        except FileNotFoundError:
            return self.json({"error": "frontend_not_built"}, HTTPStatus.NOT_FOUND)
        content = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mimetypes.guess_type(target.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, format: str, *args: object) -> None:
        print(f"[AICUT] {self.address_string()} {format % args}")


class ApiServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], runtime: Runtime):
        self.runtime = runtime
        super().__init__(address, ApiHandler)


def create_server(host: str, port: int, runtime: Runtime) -> ApiServer:
    return ApiServer((host, port), runtime)


def main() -> None:
    parser = argparse.ArgumentParser(description="AICUT local API and web server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()
    interval = float(os.environ.get("AICUT_SCHEDULER_INTERVAL_SEC", "60"))
    backup_interval = float(os.environ.get("AICUT_BACKUP_INTERVAL_SEC", "86400"))
    runtime = create_runtime()
    runtime.start_scheduler(interval, backup_interval)
    print(f"AICUT local runtime: http://{args.host}:{args.port}")
    server = create_server(args.host, args.port, runtime)
    try:
        server.serve_forever()
    finally:
        runtime.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
