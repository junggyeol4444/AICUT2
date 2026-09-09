"""Local operator UI (15장).

The four screens of 15.1, served from the stdlib so the app stays a single
runnable program with no JS build step:

1. 입력 (15.2)      POST /api/projects
2. 진행 모니터 (15.3) GET  /api/jobs/<id>
3. 후보 검토 (15.4)   GET/POST /api/projects/<id>/candidates
4. 결과 (15.5)       GET  /api/projects/<id>/episodes, /plan, /report
                    POST /api/episodes/<id>/review   ← 11.3 gate

Deviation from 20.1, stated plainly: the plan names PyQt6 or Electron for the
desktop wrapper. This is an HTTP server plus one static page, because that runs
and is testable headless; a PyQt6 QWebEngineView (or Electron shell) can wrap
this same server later without the UI logic changing.

The server binds to localhost only and holds a single-user workspace. It carries
no authentication and must not be exposed to a network.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import re
import subprocess
import sys
import threading
import uuid
from dataclasses import dataclass
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from aicut.config import CalibrationProfile
from aicut.db.backup import DatabaseBackup
from aicut.db.store import Store
from aicut.errors import AicutError
from aicut.intelligence.knowledge import ProductionKnowledge
from aicut.llm import get_producer
from aicut.media.stt import TranscriptFileTranscriber
from aicut.models import to_dict
from aicut.pipeline import review as review_mod
from aicut.pipeline.context import RunContext
from aicut.pipeline.runner import Pipeline
from aicut.pipeline.states import State
from aicut.render.editplan import EditPlan, describe
from aicut.scheduler import Periodic, Scheduler
from aicut.ui.auth import ApiKeyGuard, guard_from_environment
from aicut.ui.jobs import Job, JobRunner

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"

# The UI's requests are small - a path, a verdict, a reviewer's name. A body
# larger than this is a mistake or a stray client, and reading it into memory
# on the strength of a header the sender chose is how a local tool becomes a
# way to exhaust the machine.
MAX_BODY_BYTES = 4 * 1024 * 1024

#: Media the result panel (15.5) is allowed to show. The extension decides the
#: Content-Type; anything not listed is not served, so a stray path in the
#: database cannot turn a preview route into a general file reader.
MEDIA_TYPES = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".mp4": "video/mp4",
}


@dataclass(frozen=True)
class FileResponse:
    """A route's answer that is bytes on disk rather than JSON.

    Returned by the thumbnail and video routes of 15.5 and streamed by the
    handler; keeping it a value means routing still lives in one table.
    """

    path: Path
    content_type: str


class UiServer:
    """Holds the workspace and dispatches API calls. Transport-agnostic."""

    def __init__(
        self,
        workspace: str | Path = "workspace",
        *,
        profile_path: str | None = None,
        producer_name: str = "mock",
        guard: ApiKeyGuard | None = None,
        backup_interval_sec: float | None = None,
        backup_retention: int = 7,
        client_secrets: str | Path = "client_secrets.json",
        token_path: str | Path | None = None,
        stt: dict[str, Any] | None = None,
    ):
        self.workspace = Path(workspace)
        try:
            self.workspace.mkdir(parents=True, exist_ok=True)
        except (NotADirectoryError, FileExistsError) as exc:
            raise AicutError(f"{self.workspace} is not a usable workspace directory") from exc
        except PermissionError as exc:
            raise AicutError(f"cannot write to the workspace {self.workspace}") from exc
        self.profile_path = profile_path
        self.producer_name = producer_name
        # 15.5's upload button needs the same OAuth material the CLI uses.
        self.client_secrets = Path(client_secrets)
        self.token_path = Path(token_path) if token_path else None
        # 18장 puts STT 처리 on the program's side. 15.2 lets the operator drop
        # a file in and nothing else, so the server has to own a recogniser -
        # without one, a submission with no transcript ran on no speech at all
        # and usually ended NO_CONTENT, which reads as "nothing was worth
        # making" rather than "nothing was heard".
        self.stt = dict(stt or {})
        self.jobs = JobRunner()
        self.db_path = self.workspace / "aicut.db"
        self.guard = guard if guard is not None else guard_from_environment()
        self.backup = DatabaseBackup(
            self.db_path, self.workspace / "backups", retention=backup_retention,
        )
        self.scheduler = self._build_scheduler(backup_interval_sec)
        self._local = threading.local()
        self._stores: list[Store] = []
        self._stores_lock = threading.Lock()

    # ---- helpers -----------------------------------------------------------
    @property
    def store(self) -> Store:
        """A Store belonging to the calling thread.

        SQLite connections cannot cross threads, and this server has several:
        the pipeline worker plus one per in-flight request. Each gets its own
        connection to the same WAL database.
        """
        store = getattr(self._local, "store", None)
        if store is None:
            store = Store(self.db_path)
            self._local.store = store
            with self._stores_lock:
                self._stores.append(store)
        return store

    def release_thread_store(self) -> None:
        """Close this thread's connection, if it opened one.

        ThreadingHTTPServer runs one thread per request, so without this every
        request leaks a connection for the life of the server. On Windows the
        leak is louder than a leak: the open handles keep the database file
        locked, and the workspace cannot be removed.
        """
        store = getattr(self._local, "store", None)
        if store is None:
            return
        self._local.store = None
        with self._stores_lock:
            if store in self._stores:
                self._stores.remove(store)
        try:
            store.close()
        except Exception:                     # a connection already gone
            pass

    def profile(self, profile_id: str | None = None) -> CalibrationProfile:
        """The server default, or one measured profile from the database.

        17장 makes a profile channel-scoped: a mic or game change means the
        numbers no longer describe the broadcast being analysed. So the choice
        belongs per submission (15.2), not only to the process.
        """
        if not profile_id:
            return CalibrationProfile.load(self.profile_path)
        for row in self.store.profiles():
            if row["profile_id"] == profile_id:
                return CalibrationProfile.from_mapping(row["params"])
        raise KeyError(f"no calibration profile {profile_id}")

    def profiles(self) -> dict[str, Any]:
        """What 15.2's profile picker offers, and what is still a guess (17.5)."""
        default = self.profile()
        return {
            "default": {
                "name": default.name,
                "source": str(default.source_path),
                "measured_at": default.measured_at,
                "provisional": sorted(default.provisional),
            },
            "measured": [
                {"profile_id": row["profile_id"], "name": row["name"],
                 "channel_ref": row["channel_ref"], "measured_at": row["measured_at"],
                 "eval_score": row["eval_score"]}
                for row in self.store.profiles()
            ],
        }

    def workspace_file(self, raw: str | None, *, what: str) -> Path:
        """Resolve a stored path, refusing anything outside the workspace.

        The paths come from the database, written by this pipeline into
        `workspace/<project>/…`. Confining anyway is what keeps a hand-edited
        row from turning a preview route into an arbitrary file read.
        """
        if not raw:
            raise KeyError(f"no {what} for this episode yet")
        target = Path(raw).expanduser().resolve()
        root = self.workspace.resolve()
        if root not in target.parents:
            raise PermissionError(f"{what} is outside the workspace")
        if not target.is_file():
            raise KeyError(f"{what} is recorded but missing: {target}")
        content_type = MEDIA_TYPES.get(target.suffix.lower())
        if not content_type:
            raise PermissionError(f"{target.suffix} is not a previewable type")
        return target

    def thumbnail(self, episode_id: str, index: str) -> FileResponse:
        """One of 11.1's candidate frames. The operator picks; no template."""
        episode = self.store.get_episode(episode_id)
        if episode is None:
            raise KeyError(f"unknown episode {episode_id}")
        candidates = episode.thumbnail_candidates
        position = int(index)
        if not 0 <= position < len(candidates):
            raise KeyError(f"episode {episode_id} has no thumbnail {position}")
        target = self.workspace_file(candidates[position], what="thumbnail")
        return FileResponse(target, MEDIA_TYPES[target.suffix.lower()])

    def video(self, episode_id: str) -> FileResponse:
        """The rendered episode, for 15.5's preview."""
        episode = self.store.get_episode(episode_id)
        if episode is None:
            raise KeyError(f"unknown episode {episode_id}")
        target = self.workspace_file(episode.output_mp4_path, what="rendered video")
        return FileResponse(target, MEDIA_TYPES[target.suffix.lower()])

    def reveal(self, episode_id: str, body: dict[str, Any], *, remote: str) -> dict[str, Any]:
        """Open the output folder in the OS file manager (15.5 "폴더 열기").

        Only for a request from this machine. The route runs a program, and a
        desktop affordance is not one a remote caller should reach even when
        the API key is off — which is exactly when an exposed port is at risk.
        """
        if remote not in ("127.0.0.1", "::1", "localhost"):
            raise PermissionError("the folder can only be opened from this machine")
        episode = self.store.get_episode(episode_id)
        if episode is None:
            raise KeyError(f"unknown episode {episode_id}")
        target = self.workspace_file(episode.output_mp4_path, what="rendered video")
        directory = target.parent
        # A fixed argv per platform, never a shell: the path is data.
        if sys.platform == "darwin":
            command = ["open", "-R", str(target)]
        elif os.name == "nt":
            command = ["explorer", f"/select,{target}"]
        else:
            command = ["xdg-open", str(directory)]
        try:
            subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as exc:
            raise AicutError(
                f"could not open a file manager ({command[0]}: {exc}). The folder is {directory}"
            ) from exc
        return {"opened": str(directory)}

    def pipeline(self, profile_id: str | None = None) -> Pipeline:
        knowledge = ProductionKnowledge.load(self.workspace / "knowledge.json").summary_for_planner()
        return Pipeline(
            self.store,
            self.profile(profile_id),
            get_producer(self.producer_name),
            workspace=self.workspace,
            knowledge=knowledge,
        )

    def profile_for_project(self, project) -> CalibrationProfile:
        """The profile this project was analysed with, not whatever is current.

        `Pipeline.submit` records the profile's id on the project. Reading a
        finished project back under a different profile would report thresholds
        that never produced it — the silent mismatch 17장 is written to prevent.

        The id, not the name: `tb_calibration_profile.name` is not unique, and
        recalibrating a channel naturally writes another row under the same
        `<channel>-calibrated`. Resolving by name returned whichever row came
        first, so after a restart a project analysed under the newer profile was
        reviewed, reported and uploaded under the older one's thresholds.

        Projects submitted before the id was recorded fall back to the name, and
        then to the profile on disk - an old workspace still opens.
        """
        rows = self.store.profiles()
        if project.profile_id:
            for row in rows:
                if row["profile_id"] == project.profile_id:
                    return CalibrationProfile.from_mapping(row["params"])
            log.warning(
                "project %s names profile %s, which is no longer stored; falling back",
                project.project_id, project.profile_id,
            )
        for row in rows:
            if row["name"] == project.profile_name:
                return CalibrationProfile.from_mapping(row["params"])
        return CalibrationProfile.load(self.profile_path)

    def context(self, project_id: str) -> RunContext:
        project = self.store.get_project(project_id)
        if project is None:
            raise KeyError(f"unknown project {project_id}")
        return RunContext(
            project=project,
            store=self.store,
            profile=self.profile_for_project(project),
            producer=get_producer(self.producer_name),
            workspace=self.workspace,
        )

    # ---- 15.2 input --------------------------------------------------------
    def submit(self, body: dict[str, Any]) -> dict[str, Any]:
        source = (body.get("source") or "").strip()
        if not source:
            raise ValueError("source file path is required")
        if not Path(source).exists():
            raise ValueError(f"file not found: {source}")

        # 15.2's profile picker. Unset means the server default, which is what
        # a first run has before anything has been measured (17.4).
        chosen_profile = body.get("profile_id") or ""
        pipeline = self.pipeline(chosen_profile or None)
        project = pipeline.submit(
            source,
            length_hint_sec=_optional_float(body.get("length_hint_sec")),
            channel_ref=body.get("channel_ref", "") or "",
            # The row that was picked, so reading this project back later finds
            # the same thresholds even after the channel is recalibrated (17장).
            profile_id=chosen_profile,
        )
        transcript = body.get("transcript") or None
        render = bool(body.get("render", True))
        # 5.2 reads 화면과 소리를 분리하지 않고 같이 본다, and the browser sends
        # no such field - so this default meant every ordinary run understood the
        # broadcast from speech and a motion number alone: no faces (5.3, 11.1),
        # no frames for the model to look at. Looking is the normal path; the
        # caller can still turn it off.
        frames = bool(body.get("sample_frames", True))
        stop_after = body.get("stop_after") or None

        # Built here rather than inside the worker: a missing dependency should
        # refuse the submission with a message the operator can act on, not
        # surface later as a job that quietly found nothing.
        if not transcript:
            try:
                self._speech_recogniser()
            except Exception as exc:
                raise ValueError(
                    f"no transcript given and the speech recogniser cannot start: {exc}. "
                    "Install it (pip install 'aicut[stt]'), point --stt-backend at one that "
                    "runs here, or run `aicut transcribe` first and pass the transcript."
                ) from exc

        def work(job: Job):
            job.append("info", f"submitted {source}")
            # The worker owns its own connection; the pipeline built above holds
            # the submitting thread's, so rebind it before running.
            pipeline.store = self.store
            if transcript:
                transcriber = TranscriptFileTranscriber(transcript)
                job.append("info", f"using the supplied transcript {transcript}")
            else:
                transcriber = self._speech_recogniser()
                job.append("info", f"transcribing with {self.stt.get('backend', 'whisperx')}")
            try:
                return pipeline.run(
                    project,
                    transcriber=transcriber,
                    stop_after=State(stop_after) if stop_after else None,
                    sample_frames=frames,
                    render=render,
                )
            finally:
                # The request threads release theirs after every call; this one
                # never did, and `_stores` keeps the object alive after the
                # worker exits, so a desktop session left running accumulated
                # one SQLite connection and file descriptor per project. On
                # Windows those handles also keep the database file locked -
                # the same shape that once made the workspace undeletable.
                self.release_thread_store()

        job = self.jobs.start(str(uuid.uuid4()), project.project_id, source, work)
        return {"job_id": job.job_id, "project_id": project.project_id}

    # ---- 15.3 monitor ------------------------------------------------------
    def _speech_recogniser(self):
        """The recogniser this server transcribes with, from its own settings."""
        from aicut.media.stt import build_transcriber

        settings = dict(self.stt)
        return build_transcriber(settings.pop("backend", "whisperx"), **settings)

    def job(self, job_id: str) -> dict[str, Any]:
        job = self.jobs.get(job_id)
        if job is None:
            raise KeyError(f"unknown job {job_id}")
        return job.to_dict()

    def projects(self) -> list[dict[str, Any]]:
        rows = []
        for project in self.store.list_projects():
            episodes = self.store.episodes(project.project_id)
            rows.append({
                "project_id": project.project_id,
                "file_path": project.file_path,
                "status": project.status,
                "duration_sec": project.duration_sec,
                "created_at": project.created_at,
                "episodes": len(episodes),
            })
        return rows

    # ---- 15.4 candidate review --------------------------------------------
    def candidates(self, project_id: str) -> dict[str, Any]:
        ctx = self.context(project_id)
        return {
            "candidates": review_mod.candidate_review(ctx),
            "agreement": review_mod.agreement_rate(ctx),
            # 19장 MVP 3 is scored on the four items of 원본 32장, so the screen
            # that collects them also shows where they stand.
            "assessment_items": list(review_mod.ASSESSMENT_ITEMS),
            "assessment_verdicts": list(review_mod.ASSESSMENT_VERDICTS),
            "assessment": review_mod.assessment_rates(ctx),
            "events": [
                {"event_id": e.event_id, "summary": e.summary, "span": list(e.span()),
                 "mentions": len(e.mentions), "people": e.people}
                for e in self.store.events(project_id)
            ],
        }

    def verdict(self, project_id: str, body: dict[str, Any]) -> dict[str, Any]:
        ctx = self.context(project_id)
        # A request may carry the 15.4 agree/disagree, the four 원본 32장 answers,
        # or both - the reviewer works through one candidate, not two screens.
        if body.get("verdict"):
            review_mod.record_candidate_verdict(
                ctx, body["candidate_id"], body["verdict"], body.get("note", "") or ""
            )
        assessment = body.get("assessment") or {}
        if assessment:
            review_mod.record_candidate_assessment(ctx, body["candidate_id"], assessment)
        if not body.get("verdict") and not assessment:
            raise ValueError("a candidate review needs a verdict or an assessment")
        return {
            "agreement": review_mod.agreement_rate(ctx),
            "assessment": review_mod.assessment_rates(ctx),
        }

    # ---- 15.5 results ------------------------------------------------------
    def episodes(self, project_id: str) -> list[dict[str, Any]]:
        rows = []
        for episode in self.store.episodes(project_id):
            plan_path = self.workspace / project_id / "plans" / f"{episode.episode_id}.json"
            rows.append({
                "episode_id": episode.episode_id,
                "target_type": episode.target_type,
                "structure": episode.planned_structure.get("structure_name", ""),
                "rationale": episode.planned_structure.get("rationale", ""),
                "duration_sec": round(episode.planned_duration_sec, 1),
                "cuts": len(episode.timeline),
                "titles": episode.title_candidates,
                "thumbnails": episode.thumbnail_candidates,
                "thumbnail_chosen": (
                    episode.thumbnail_candidates.index(episode.thumbnail_path)
                    if episode.thumbnail_path in episode.thumbnail_candidates else None
                ),
                "output": episode.output_mp4_path,
                "render_status": episode.render_status,
                "review_status": episode.review_status,
                # The fact that settles whether uploading again would duplicate
                # the video. review_status does not: a successful upload leaves
                # it at pending or approved.
                "youtube_video_id": (episode.metadata.get("youtube") or {}).get("video_id", ""),
                "notes": episode.notes,
                "plan_path": str(plan_path) if plan_path.exists() else None,
                "metadata": episode.metadata,
            })
        return rows

    def plan(self, episode_id: str) -> dict[str, Any]:
        episode = self.store.get_episode(episode_id)
        if episode is None:
            raise KeyError(f"unknown episode {episode_id}")
        path = self.workspace / episode.project_id / "plans" / f"{episode_id}.json"
        if not path.exists():
            raise KeyError(f"no edit plan written for {episode_id}")
        plan = EditPlan.load(path)
        return {"path": str(path), "readable": describe(plan), "plan": plan.to_dict()}

    def edit_model(self, episode_id: str, mode: str = "new_sequence") -> dict[str, Any]:
        """The Common Edit Model for one episode (플러그인 기획안 37장).

        This is the 9번 AI Engine Connector's half on the engine side: what an
        editor plugin asks for after the analysis is done. 37장 puts this
        structure between the engine and any editor API, so an adapter reads
        this and never the edit plan - a plugin that re-read the plan would be
        a second implementation of its meaning, and two of those disagree.

        ``mode`` is 25장's choice and it comes from the person at the plugin:
        `new_sequence` leaves what they built alone, `edit_current` does not.
        """
        from aicut.render.editmodel import from_edit_plan

        episode = self.store.get_episode(episode_id)
        if episode is None:
            raise KeyError(f"unknown episode {episode_id}")
        path = self.workspace / episode.project_id / "plans" / f"{episode_id}.json"
        if not path.exists():
            raise KeyError(f"no edit plan written for {episode_id}")
        project = self.store.get_project(episode.project_id)
        model = from_edit_plan(
            EditPlan.load(path),
            name=(episode.title_candidates or [f"AI_{episode_id[:8]}"])[0],
            mode=mode,
            source_duration_sec=project.duration_sec if project else 0.0,
        )
        return model.as_dict()

    def review(self, episode_id: str, body: dict[str, Any]) -> dict[str, Any]:
        episode = self.store.get_episode(episode_id)
        if episode is None:
            raise KeyError(f"unknown episode {episode_id}")
        ctx = self.context(episode.project_id)
        action = body.get("action")
        if action == "thumbnail":
            # 11.1's candidates are offered so a person picks one; without a way
            # to say which, the offer was decorative.
            updated = review_mod.choose_thumbnail(ctx, episode_id, int(body.get("index", 0)))
            return {"episode_id": episode_id, "thumbnail_path": updated.thumbnail_path}
        reviewer = (body.get("reviewer") or "").strip()
        if not reviewer:
            raise ValueError("a reviewer name is required; the gate records who released the video (11.3)")
        if action == "approve":
            updated = review_mod.approve(ctx, episode_id, reviewer=reviewer, note=body.get("note", "") or "")
        elif action == "reject":
            updated = review_mod.reject(ctx, episode_id, reviewer=reviewer, reason=body.get("note", "") or "")
        else:
            raise ValueError("action must be 'approve' or 'reject'")
        return {"episode_id": episode_id, "review_status": updated.review_status}

    def upload(self, episode_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """15.5's 업로드 button. 11.3 decides what it is allowed to do.

        This uploads private and queues on a spent quota (11.4); it never makes
        anything public. Releasing an approved episode is a separate action, and
        `publishing.publish_approved` is the one that refuses without a review.
        """
        episode = self.store.get_episode(episode_id)
        if episode is None:
            raise KeyError(f"unknown episode {episode_id}")
        ctx = self.context(episode.project_id)
        client = self._youtube(ctx)
        from aicut.pipeline import publishing

        if body.get("action") == "publish":
            updated = publishing.publish_approved(ctx, episode, client)
            return {"episode_id": episode_id, "review_status": updated.review_status,
                    "youtube": updated.metadata.get("youtube", {})}
        return publishing.upload_episode(ctx, episode, client)

    def _youtube(self, ctx: RunContext):
        """The API client, built the same way the CLI builds it."""
        from aicut.intelligence.youtube import YouTubeClient, load_credentials
        from aicut.intelligence.quota import QuotaLedger

        # The same workspace-local default the CLI uses. `aicut ui` takes no
        # --token, so this was None, and `load_credentials` does `Path(None)`:
        # every upload and publish button raised a TypeError before OAuth could
        # even start.
        token = self.token_path or (self.workspace / "youtube_token.json")
        credentials = load_credentials(str(self.client_secrets), str(token))
        return YouTubeClient(credentials, QuotaLedger(ctx.store, ctx.profile))

    def report(self, project_id: str) -> dict[str, Any]:
        path = self.workspace / project_id / "report.json"
        if not path.exists():
            raise KeyError(f"no report for {project_id} yet")
        return json.loads(path.read_text(encoding="utf-8"))

    def profile_info(self) -> dict[str, Any]:
        profile = self.profile()
        return {
            "name": profile.name,
            "source": str(profile.source_path),
            "measured_at": profile.measured_at,
            "provisional": sorted(profile.provisional),
            "measured": sorted(profile.measured),
            "producer": self.producer_name,
            "warning": (
                "unmeasured parameters are in use; run the 17.4 sweep before trusting output"
                if profile.provisional else ""
            ),
        }

    # ---- runtime (18장: 작업 큐 / 서버) ------------------------------------
    def _build_scheduler(self, backup_interval_sec: float | None) -> Scheduler | None:
        """Nothing to tick unless a backup cadence is set.

        The upload retry queue of 11.4 is aimed at PT midnight and is drained by
        `aicut upload --retry`, which needs OAuth credentials this process may
        not hold; it is registered by the caller that has them, not here.
        """
        if not backup_interval_sec:
            return None
        return Scheduler(
            {"backup": Periodic(self.backup.create, backup_interval_sec)},
            interval_sec=min(60.0, backup_interval_sec),
        )

    def runtime(self) -> dict[str, Any]:
        """What the background loop has done, and what snapshots exist."""
        return {
            "scheduler": self.scheduler.status() if self.scheduler else {"running": False},
            "backups": self.backup.list(),
            "auth": {"api_key_required": self.guard.enabled},
        }

    def close(self) -> None:
        if self.scheduler:
            self.scheduler.stop()
        with self._stores_lock:
            for store in self._stores:
                try:
                    store.close()
                except Exception:                    # a worker thread may still hold one
                    pass
            self._stores.clear()


# ---------------------------------------------------------------------------
# HTTP transport
# ---------------------------------------------------------------------------
Route = tuple[re.Pattern[str], str, Callable[..., Any]]


class _Handler(BaseHTTPRequestHandler):
    server_version = "aicut"

    def __init__(self, ui: UiServer, *args, **kwargs):
        self.ui = ui
        self.routes: list[Route] = [
            (re.compile(r"^/api/health$"), "GET", lambda: {"status": "ok", "service": "aicut"}),
            (re.compile(r"^/api/runtime$"), "GET", lambda: ui.runtime()),
            (re.compile(r"^/api/profile$"), "GET", lambda: ui.profile_info()),
            (re.compile(r"^/api/profiles$"), "GET", lambda: ui.profiles()),
            (re.compile(r"^/api/episodes/([\w-]+)/thumbnail/(\d+)$"), "GET",
             lambda eid, index: ui.thumbnail(eid, index)),
            (re.compile(r"^/api/episodes/([\w-]+)/video$"), "GET", lambda eid: ui.video(eid)),
            (re.compile(r"^/api/episodes/([\w-]+)/reveal$"), "POST",
             lambda eid, body: ui.reveal(eid, body, remote=self.client_address[0])),
            (re.compile(r"^/api/projects$"), "GET", lambda: ui.projects()),
            (re.compile(r"^/api/projects$"), "POST", lambda body: ui.submit(body)),
            (re.compile(r"^/api/jobs$"), "GET", lambda: ui.jobs.list()),
            (re.compile(r"^/api/jobs/([\w-]+)$"), "GET", lambda job_id: ui.job(job_id)),
            (re.compile(r"^/api/projects/([\w-]+)/candidates$"), "GET", lambda pid: ui.candidates(pid)),
            (re.compile(r"^/api/projects/([\w-]+)/candidates$"), "POST", lambda pid, body: ui.verdict(pid, body)),
            (re.compile(r"^/api/projects/([\w-]+)/episodes$"), "GET", lambda pid: ui.episodes(pid)),
            (re.compile(r"^/api/projects/([\w-]+)/report$"), "GET", lambda pid: ui.report(pid)),
            (re.compile(r"^/api/episodes/([\w-]+)/plan$"), "GET", lambda eid: ui.plan(eid)),
            # 37장: what an editor adapter reads. The mode is 25장's and the
            # plugin passes the person's choice through.
            (re.compile(r"^/api/episodes/([\w-]+)/edit-model$"), "GET",
             lambda eid: ui.edit_model(eid)),
            (re.compile(r"^/api/episodes/([\w-]+)/edit-model$"), "POST",
             lambda eid, body: ui.edit_model(eid, body.get("mode", "new_sequence"))),
            (re.compile(r"^/api/episodes/([\w-]+)/review$"), "POST", lambda eid, body: ui.review(eid, body)),
            (re.compile(r"^/api/episodes/([\w-]+)/upload$"), "POST", lambda eid, body: ui.upload(eid, body)),
        ]
        super().__init__(*args, **kwargs)

    # -- plumbing ------------------------------------------------------------
    def log_message(self, fmt: str, *args) -> None:      # quieter than the default
        log.debug("ui %s", fmt % args)

    def _send(self, status: int, payload: Any) -> None:
        body = json.dumps(to_dict(payload), ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, response: FileResponse) -> None:
        """Stream a file, honouring a byte range.

        Without ranges a browser will still play an mp4, but it cannot seek —
        it has to refetch from zero for every scrub. 15.5 calls for a preview,
        and a preview you cannot scrub is not one.
        """
        size = response.path.stat().st_size
        start, end = 0, size - 1
        partial = False
        header = self.headers.get("Range", "")
        if header.startswith("bytes="):
            first, _, last = header[len("bytes="):].partition("-")
            try:
                if first:
                    start = int(first)
                    end = int(last) if last else size - 1
                elif last:                       # bytes=-N: the final N bytes
                    start = max(0, size - int(last))
            except ValueError:
                start, end = 0, size - 1
            else:
                partial = True
            end = min(end, size - 1)
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

        length = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", response.content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        # The rendered episode is the operator's own footage; nothing about it
        # should be embedded by another page.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; sandbox")
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with response.path.open("rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining > 0:
                chunk = handle.read(min(64 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def _dispatch(self, method: str, body: dict[str, Any] | None = None) -> None:
        path = urlparse(self.path).path
        if not self.ui.guard.authorized(path, self.headers):
            self.send_response(401)
            payload = json.dumps({"error": "a valid Bearer API key is required"}).encode("utf-8")
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("WWW-Authenticate", 'Bearer realm="aicut"')
            self.end_headers()
            self.wfile.write(payload)
            return
        for pattern, verb, handler in self.routes:
            if verb != method:
                continue
            match = pattern.match(path)
            if not match:
                continue
            args = list(match.groups())
            if body is not None:
                args.append(body)
            try:
                result = handler(*args)
                if isinstance(result, FileResponse):
                    self._send_file(result)
                else:
                    self._send(200, result)
            except KeyError as exc:
                self._send(404, {"error": str(exc)})
            except (ValueError, PermissionError, AicutError) as exc:
                self._send(400, {"error": str(exc)})
            except Exception as exc:                      # never take the server down
                log.exception("ui request failed: %s %s", method, path)
                self._send(500, {"error": f"{type(exc).__name__}: {exc}"})
            return
        if method == "GET":
            self._serve_static(path)
        else:
            self._send(404, {"error": f"no route for {method} {path}"})

    def _serve_static(self, path: str) -> None:
        name = "index.html" if path in ("/", "") else path.lstrip("/")
        target = (STATIC_DIR / name).resolve()
        if not str(target).startswith(str(STATIC_DIR.resolve())) or not target.is_file():
            self._send(404, {"error": f"not found: {path}"})
            return
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(target.name)[0] or "text/plain")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # -- verbs ---------------------------------------------------------------
    def do_GET(self) -> None:       # noqa: N802
        try:
            self._dispatch("GET")
        finally:
            self.ui.release_thread_store()

    def do_POST(self) -> None:      # noqa: N802
        header = self.headers.get("Content-Length") or "0"
        try:
            length = int(header)
        except ValueError:
            self._send(400, {"error": f"Content-Length is not a number: {header!r}"})
            return
        if length < 0:
            self._send(400, {"error": "Content-Length is negative"})
            return
        if length > MAX_BODY_BYTES:
            self._send(413, {"error": f"request body of {length} bytes exceeds the {MAX_BODY_BYTES} byte limit"})
            return

        raw = self.rfile.read(length) if length else b"{}"
        if len(raw) < length:
            self._send(400, {"error": "the request body ended before Content-Length said it would"})
            return
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            self._send(400, {"error": "request body is not valid JSON"})
            return
        if not isinstance(body, dict):
            self._send(400, {"error": "request body must be a JSON object"})
            return
        try:
            self._dispatch("POST", body)
        finally:
            self.ui.release_thread_store()


def serve(
    workspace: str | Path = "workspace",
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    profile_path: str | None = None,
    producer_name: str = "mock",
    guard: ApiKeyGuard | None = None,
    backup_interval_sec: float | None = None,
    backup_retention: int = 7,
    client_secrets: str | Path = "client_secrets.json",
    token_path: str | Path | None = None,
    stt: dict[str, Any] | None = None,
) -> tuple[ThreadingHTTPServer, UiServer]:
    """Start the UI.

    Localhost by default. Set AICUT_UI_API_KEY to require a Bearer key on
    /api/*; without it these endpoints are open to anything that can reach the
    port, so do not bind a routable address without one.
    """
    ui = UiServer(
        workspace,
        profile_path=profile_path,
        producer_name=producer_name,
        guard=guard,
        backup_interval_sec=backup_interval_sec,
        backup_retention=backup_retention,
        client_secrets=client_secrets,
        token_path=token_path,
        stt=stt,
    )
    if ui.scheduler:
        ui.scheduler.start()
    httpd = ThreadingHTTPServer((host, port), partial(_Handler, ui))
    return httpd, ui


def _optional_float(value: Any) -> float | None:
    if value in (None, "", "null"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
