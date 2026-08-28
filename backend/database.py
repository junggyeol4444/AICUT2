from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parent


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self.initialize()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript((ROOT / "schema.sql").read_text(encoding="utf-8"))

    def create_project(self, payload: dict[str, Any]) -> dict[str, Any]:
        project_id = str(uuid.uuid4())
        timestamp = now()
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO projects
                (project_id,name,file_path,duration_sec,status,progress,target_duration_hint,
                 channel_ref,calibration_profile_id,media_info_json,error_message,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (project_id, payload.get("name") or Path(payload["file_path"]).stem,
                 payload["file_path"], float(payload.get("duration_sec", 0)), "QUEUED", 0,
                 payload.get("target_duration_hint"), payload.get("channel_ref"),
                 payload.get("calibration_profile_id"), "{}", None, timestamp, timestamp),
            )
            self._log(connection, project_id, "QUEUED", "프로젝트가 분석 큐에 등록되었습니다.")
        return self.get_project(project_id)

    def list_projects(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT p.*,
                (SELECT count(*) FROM content_candidates c WHERE c.project_id=p.project_id) candidate_count,
                (SELECT count(*) FROM episodes e WHERE e.project_id=p.project_id) episode_count
                FROM projects p ORDER BY p.created_at DESC"""
            ).fetchall()
        return [dict(row) for row in rows]

    def get_project(self, project_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM projects WHERE project_id=?", (project_id,)).fetchone()
        if not row:
            raise KeyError(project_id)
        return dict(row)

    def update_status(self, project_id: str, status: str, progress: int, message: str) -> None:
        with self.connect() as connection:
            result = connection.execute(
                "UPDATE projects SET status=?,progress=?,updated_at=? WHERE project_id=?",
                (status, progress, now(), project_id),
            )
            if not result.rowcount:
                raise KeyError(project_id)
            self._log(connection, project_id, status, message)

    def set_media_info(self, project_id: str, media_info: dict[str, Any]) -> None:
        with self.connect() as connection:
            result = connection.execute(
                "UPDATE projects SET duration_sec=?,media_info_json=?,updated_at=? WHERE project_id=?",
                (media_info["duration_sec"], json.dumps(media_info, ensure_ascii=False), now(), project_id),
            )
            if not result.rowcount:
                raise KeyError(project_id)

    def fail_project(self, project_id: str, message: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE projects SET status='FAILED',error_message=?,updated_at=? WHERE project_id=?",
                (message, now(), project_id),
            )
            self._log(connection, project_id, "FAILED", message)

    def import_analysis(self, project_id: str, manifest: dict[str, Any]) -> dict[str, int]:
        """Atomically replace AI analysis output using the documented interchange contract."""
        events = manifest.get("events", [])
        candidates = manifest.get("candidates", [])
        episodes = manifest.get("episodes", [])
        with self.connect() as connection:
            if not connection.execute("SELECT 1 FROM projects WHERE project_id=?", (project_id,)).fetchone():
                raise KeyError(project_id)
            connection.execute("DELETE FROM events WHERE project_id=?", (project_id,))
            connection.execute("DELETE FROM content_candidates WHERE project_id=?", (project_id,))
            connection.execute("DELETE FROM episodes WHERE project_id=?", (project_id,))
            for event in events:
                event_id = event.get("event_id") or str(uuid.uuid4())
                connection.execute(
                    "INSERT INTO events VALUES(?,?,?,?,?)",
                    (event_id, project_id, event["summary"], json.dumps(event.get("people", []), ensure_ascii=False), json.dumps(event.get("relations", []), ensure_ascii=False)),
                )
                for mention in event.get("mentions", []):
                    connection.execute(
                        "INSERT INTO event_mentions(event_id,source_start_sec,source_end_sec,role) VALUES(?,?,?,?)",
                        (event_id, mention["start_sec"], mention["end_sec"], mention["role"]),
                    )
            for candidate in candidates:
                connection.execute(
                    """INSERT INTO content_candidates
                    (candidate_id,project_id,core_summary,related_event_ids_json,required_context,
                     independence_score,decision,decision_reason) VALUES(?,?,?,?,?,?,?,?)""",
                    (candidate.get("candidate_id") or str(uuid.uuid4()), project_id, candidate["summary"],
                     json.dumps(candidate.get("event_ids", [])), candidate.get("required_context", ""),
                     candidate["independence_score"], candidate.get("decision", "HOLD"), candidate.get("decision_reason", "")),
                )
            cut_count = 0
            for episode in episodes:
                episode_id = episode.get("episode_id") or str(uuid.uuid4())
                connection.execute(
                    """INSERT INTO episodes
                    (episode_id,project_id,candidate_ids_json,planned_structure_json,target_type,
                     planned_duration_sec,render_status,review_status,metadata_json)
                    VALUES(?,?,?,?,?,?,?,?,?)""",
                    (episode_id, project_id, json.dumps(episode.get("candidate_ids", [])),
                     json.dumps(episode.get("structure", {}), ensure_ascii=False), episode.get("target_type"),
                     episode.get("planned_duration_sec"), "PENDING", "PENDING", json.dumps(episode.get("metadata", {}), ensure_ascii=False)),
                )
                for order, cut in enumerate(episode.get("timeline", []), 1):
                    connection.execute(
                        """INSERT INTO edit_timeline
                        (episode_id,sequence_order,source_start_sec,source_end_sec,speaker_tag,scene_role,
                         pacing_mode,visual_effect_json,subtitle_ref) VALUES(?,?,?,?,?,?,?,?,?)""",
                        (episode_id, order, cut["source_start_sec"], cut["source_end_sec"], cut.get("speaker_tag", "UNKNOWN"),
                         cut["scene_role"], cut["pacing_mode"], json.dumps(cut.get("visual_effect", {}), ensure_ascii=False), cut.get("subtitle_ref")),
                    )
                    cut_count += 1
            status = "PLANNING" if episodes else ("EVALUATING" if candidates else "NO_CONTENT")
            connection.execute("UPDATE projects SET status=?,progress=?,updated_at=? WHERE project_id=?", (status, 68 if episodes else 100, now(), project_id))
            self._log(connection, project_id, status, f"분석 매니페스트 적용 완료 · 사건 {len(events)}, 후보 {len(candidates)}, 컷 {cut_count}")
        return {"events": len(events), "candidates": len(candidates), "episodes": len(episodes), "cuts": cut_count}

    def list_candidates(self, project_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM content_candidates WHERE project_id=? ORDER BY independence_score DESC",
                (project_id,),
            ).fetchall()
        return [self._decode(dict(row), "related_event_ids_json") for row in rows]

    def review_candidate(self, candidate_id: str, decision: str, feedback: str = "") -> dict[str, Any]:
        allowed = {"MAKE", "COMBINE", "HOLD", "REJECT"}
        if decision not in allowed:
            raise ValueError(f"decision must be one of {sorted(allowed)}")
        with self.connect() as connection:
            result = connection.execute(
                "UPDATE content_candidates SET decision=?,user_feedback=?,reviewed_at=? WHERE candidate_id=?",
                (decision, feedback, now(), candidate_id),
            )
            if not result.rowcount:
                raise KeyError(candidate_id)
            row = connection.execute("SELECT * FROM content_candidates WHERE candidate_id=?", (candidate_id,)).fetchone()
        return self._decode(dict(row), "related_event_ids_json")

    def get_timeline(self, episode_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM edit_timeline WHERE episode_id=? ORDER BY sequence_order", (episode_id,)
            ).fetchall()
        return [self._decode(dict(row), "visual_effect_json") for row in rows]

    def get_episode(self, episode_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT e.*,p.file_path FROM episodes e JOIN projects p USING(project_id)
                WHERE e.episode_id=?""", (episode_id,),
            ).fetchone()
        if not row:
            raise KeyError(episode_id)
        value = dict(row)
        for key in ("candidate_ids_json", "planned_structure_json", "metadata_json"):
            value = self._decode(value, key)
        return value

    def set_render_status(self, episode_id: str, status: str, output_path: str | None = None) -> None:
        with self.connect() as connection:
            result = connection.execute(
                "UPDATE episodes SET render_status=?,output_mp4_path=COALESCE(?,output_mp4_path) WHERE episode_id=?",
                (status, output_path, episode_id),
            )
            if not result.rowcount:
                raise KeyError(episode_id)

    def review_episode(self, episode_id: str, approved: bool) -> dict[str, Any]:
        status = "APPROVED" if approved else "CHANGES_REQUESTED"
        with self.connect() as connection:
            result = connection.execute("UPDATE episodes SET review_status=? WHERE episode_id=?", (status, episode_id))
            if not result.rowcount:
                raise KeyError(episode_id)
            row = connection.execute("SELECT * FROM episodes WHERE episode_id=?", (episode_id,)).fetchone()
        return self._decode(self._decode(self._decode(dict(row), "candidate_ids_json"), "planned_structure_json"), "metadata_json")

    def update_episode_metadata(self, episode_id: str, metadata: dict[str, Any]) -> None:
        with self.connect() as connection:
            result = connection.execute(
                "UPDATE episodes SET metadata_json=? WHERE episode_id=?",
                (json.dumps(metadata, ensure_ascii=False), episode_id),
            )
            if not result.rowcount:
                raise KeyError(episode_id)

    def queue_upload(self, episode_id: str, privacy_status: str = "PRIVATE") -> dict[str, Any]:
        if privacy_status not in {"PRIVATE", "UNLISTED"}:
            raise ValueError("검수 게이트에서는 PRIVATE 또는 UNLISTED 업로드만 허용됩니다.")
        upload_id = str(uuid.uuid4())
        timestamp = now()
        with self.connect() as connection:
            episode = connection.execute(
                "SELECT review_status,render_status,output_mp4_path FROM episodes WHERE episode_id=?", (episode_id,)
            ).fetchone()
            if not episode:
                raise KeyError(episode_id)
            if episode["review_status"] != "APPROVED":
                raise ValueError("사람 검수를 승인한 에피소드만 업로드할 수 있습니다.")
            if episode["render_status"] != "COMPLETE" or not episode["output_mp4_path"]:
                raise ValueError("렌더링이 완료된 에피소드만 업로드할 수 있습니다.")
            connection.execute(
                """INSERT INTO upload_jobs
                (upload_id,episode_id,privacy_status,status,created_at,updated_at)
                VALUES(?,?,?,?,?,?)""",
                (upload_id, episode_id, privacy_status, "QUEUED", timestamp, timestamp),
            )
            row = connection.execute("SELECT * FROM upload_jobs WHERE upload_id=?", (upload_id,)).fetchone()
        return dict(row)

    def list_uploads(self, status: str | None = None) -> list[dict[str, Any]]:
        with self.connect() as connection:
            if status:
                rows = connection.execute(
                    """SELECT u.*,e.output_mp4_path,e.metadata_json FROM upload_jobs u
                    JOIN episodes e USING(episode_id) WHERE u.status=? ORDER BY u.created_at""", (status,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """SELECT u.*,e.output_mp4_path,e.metadata_json FROM upload_jobs u
                    JOIN episodes e USING(episode_id) ORDER BY u.created_at DESC"""
                ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["metadata"] = json.loads(item.pop("metadata_json"))
            result.append(item)
        return result

    def set_upload_status(
        self, upload_id: str, status: str, *, youtube_video_id: str | None = None,
        retry_at: str | None = None, error_message: str | None = None,
    ) -> dict[str, Any]:
        allowed = {"QUEUED", "UPLOADING", "RETRY_QUEUED", "COMPLETE", "FAILED"}
        if status not in allowed:
            raise ValueError(f"지원하지 않는 업로드 상태입니다: {status}")
        with self.connect() as connection:
            result = connection.execute(
                """UPDATE upload_jobs SET status=?,youtube_video_id=COALESCE(?,youtube_video_id),
                retry_at=?,error_message=?,updated_at=? WHERE upload_id=?""",
                (status, youtube_video_id, retry_at, error_message, now(), upload_id),
            )
            if not result.rowcount:
                raise KeyError(upload_id)
            row = connection.execute("SELECT * FROM upload_jobs WHERE upload_id=?", (upload_id,)).fetchone()
        return dict(row)

    def save_calibration(
        self, channel_ref: str, name: str, params: dict[str, Any], eval_score: float
    ) -> dict[str, Any]:
        profile_id = str(uuid.uuid4())
        timestamp = now()
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO calibration_profiles VALUES(?,?,?,?,?,?)",
                (profile_id, channel_ref, name, json.dumps(params, ensure_ascii=False), timestamp, eval_score),
            )
            row = connection.execute("SELECT * FROM calibration_profiles WHERE profile_id=?", (profile_id,)).fetchone()
        value = dict(row)
        value["params"] = json.loads(value.pop("params_json"))
        return value

    def list_calibrations(self, channel_ref: str | None = None) -> list[dict[str, Any]]:
        with self.connect() as connection:
            if channel_ref:
                rows = connection.execute(
                    "SELECT * FROM calibration_profiles WHERE channel_ref=? ORDER BY measured_at DESC", (channel_ref,),
                ).fetchall()
            else:
                rows = connection.execute("SELECT * FROM calibration_profiles ORDER BY measured_at DESC").fetchall()
        result = []
        for row in rows:
            value = dict(row)
            value["params"] = json.loads(value.pop("params_json"))
            result.append(value)
        return result

    def save_source_output_pair(
        self, source_ref: str, output_ref: str, analysis: dict[str, Any], project_id: str | None = None
    ) -> dict[str, Any]:
        pair_id = str(uuid.uuid4())
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO source_output_pairs VALUES(?,?,?,?,?,?)",
                (pair_id, project_id, source_ref, output_ref, json.dumps(analysis, ensure_ascii=False), now()),
            )
            row = connection.execute("SELECT * FROM source_output_pairs WHERE pair_id=?", (pair_id,)).fetchone()
        value = dict(row)
        value["selection_analysis"] = json.loads(value.pop("selection_analysis_json"))
        return value

    def list_source_output_pairs(self, project_id: str | None = None) -> list[dict[str, Any]]:
        with self.connect() as connection:
            if project_id:
                rows = connection.execute(
                    "SELECT * FROM source_output_pairs WHERE project_id=? ORDER BY created_at DESC", (project_id,),
                ).fetchall()
            else:
                rows = connection.execute("SELECT * FROM source_output_pairs ORDER BY created_at DESC").fetchall()
        result = []
        for row in rows:
            value = dict(row)
            value["selection_analysis"] = json.loads(value.pop("selection_analysis_json"))
            result.append(value)
        return result

    def save_performance(self, episode_id: str, metrics: dict[str, Any]) -> dict[str, Any]:
        performance_id = str(uuid.uuid4())
        with self.connect() as connection:
            if not connection.execute("SELECT 1 FROM episodes WHERE episode_id=?", (episode_id,)).fetchone():
                raise KeyError(episode_id)
            connection.execute(
                "INSERT INTO performance_snapshots VALUES(?,?,?,?)",
                (performance_id, episode_id, json.dumps(metrics, ensure_ascii=False), now()),
            )
            row = connection.execute(
                "SELECT * FROM performance_snapshots WHERE performance_id=?", (performance_id,),
            ).fetchone()
        value = dict(row)
        value["metrics"] = json.loads(value.pop("metrics_json"))
        return value

    def list_performance(self, episode_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM performance_snapshots WHERE episode_id=? ORDER BY collected_at DESC", (episode_id,),
            ).fetchall()
        result = []
        for row in rows:
            value = dict(row)
            value["metrics"] = json.loads(value.pop("metrics_json"))
            result.append(value)
        return result

    def logs(self, project_id: str | None = None) -> list[dict[str, Any]]:
        with self.connect() as connection:
            if project_id:
                rows = connection.execute("SELECT * FROM job_logs WHERE project_id=? ORDER BY log_id DESC", (project_id,)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM job_logs ORDER BY log_id DESC LIMIT 200").fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _decode(data: dict[str, Any], key: str) -> dict[str, Any]:
        data[key.removesuffix("_json")] = json.loads(data.pop(key))
        return data

    @staticmethod
    def _log(connection: sqlite3.Connection, project_id: str, stage: str, message: str) -> None:
        connection.execute(
            "INSERT INTO job_logs(project_id,stage,level,message,created_at) VALUES(?,?,?,?,?)",
            (project_id, stage, "INFO", message, now()),
        )
