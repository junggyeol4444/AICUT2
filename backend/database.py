from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
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
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(pipeline_steps)")}
            migrations = {
                "input_hash": "ALTER TABLE pipeline_steps ADD COLUMN input_hash TEXT",
                "checkpoint_version": "ALTER TABLE pipeline_steps ADD COLUMN checkpoint_version INTEGER NOT NULL DEFAULT 1",
                "attempt_count": "ALTER TABLE pipeline_steps ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0",
            }
            for column, statement in migrations.items():
                if column not in columns:
                    connection.execute(statement)

            timeline_columns = {row["name"] for row in connection.execute("PRAGMA table_info(edit_timeline)")}
            if "pacing_reason" not in timeline_columns:
                connection.execute("ALTER TABLE edit_timeline ADD COLUMN pacing_reason TEXT NOT NULL DEFAULT ''")
            upload_columns = {row["name"] for row in connection.execute("PRAGMA table_info(upload_jobs)")}
            upload_migrations = {
                "publication_status": "ALTER TABLE upload_jobs ADD COLUMN publication_status TEXT NOT NULL DEFAULT 'PRIVATE'",
                "scheduled_publish_at": "ALTER TABLE upload_jobs ADD COLUMN scheduled_publish_at TEXT",
                "thumbnail_uploaded_at": "ALTER TABLE upload_jobs ADD COLUMN thumbnail_uploaded_at TEXT",
                "upload_session_url": "ALTER TABLE upload_jobs ADD COLUMN upload_session_url TEXT",
                "uploaded_bytes": "ALTER TABLE upload_jobs ADD COLUMN uploaded_bytes INTEGER NOT NULL DEFAULT 0",
                "attempt_count": "ALTER TABLE upload_jobs ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0",
            }
            for column, statement in upload_migrations.items():
                if column not in upload_columns:
                    connection.execute(statement)

    def backup_to(self, destination: str | Path) -> dict[str, Any]:
        target = Path(destination).expanduser().resolve()
        source = Path(self.path).expanduser().resolve()
        if target == source:
            raise ValueError("backup 대상은 현재 SQLite DB와 달라야 합니다.")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            with self.connect() as source_connection, sqlite3.connect(temporary) as backup_connection:
                source_connection.backup(backup_connection)
            with sqlite3.connect(temporary) as verification_connection:
                integrity = verification_connection.execute("PRAGMA quick_check").fetchone()[0]
            if integrity != "ok":
                raise RuntimeError(f"SQLite backup 무결성 검사에 실패했습니다: {integrity}")
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        digest = hashlib.sha256()
        with target.open("rb") as source_file:
            for chunk in iter(lambda: source_file.read(1024 * 1024), b""):
                digest.update(chunk)
        return {
            "path": str(target), "size_bytes": target.stat().st_size,
            "sha256": digest.hexdigest(), "integrity": integrity, "created_at": now(),
        }

    def save_scheduler_run(self, results: dict, completed_at: str | None = None) -> dict[str, Any]:
        timestamp = completed_at or now()
        payload = json.dumps(results, ensure_ascii=False, sort_keys=True)
        with self.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO runtime_scheduler_runs(completed_at,results_json) VALUES(?,?)",
                (timestamp, payload),
            )
            row = connection.execute(
                "SELECT * FROM runtime_scheduler_runs WHERE run_id=?", (cursor.lastrowid,),
            ).fetchone()
        item = dict(row)
        item["results"] = json.loads(item.pop("results_json"))
        return item

    def list_scheduler_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        if limit <= 0 or limit > 500:
            raise ValueError("scheduler run 조회 limit은 1~500이어야 합니다.")
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM runtime_scheduler_runs ORDER BY run_id DESC LIMIT ?", (limit,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["results"] = json.loads(item.pop("results_json"))
            result.append(item)
        return result

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

    def save_pipeline_step(
        self, project_id: str, step: str, status: str, progress: int,
        output: dict[str, Any] | None = None, error_message: str | None = None,
        input_hash: str | None = None, checkpoint_version: int = 1,
    ) -> dict[str, Any]:
        timestamp = now()
        with self.connect() as connection:
            if not connection.execute("SELECT 1 FROM projects WHERE project_id=?", (project_id,)).fetchone():
                raise KeyError(project_id)
            previous = connection.execute(
                "SELECT started_at FROM pipeline_steps WHERE project_id=? AND step=?", (project_id, step),
            ).fetchone()
            started_at = (previous["started_at"] if previous else None) or (timestamp if status == "RUNNING" else None)
            completed_at = timestamp if status in {"COMPLETE", "FAILED", "CANCELLED"} else None
            connection.execute(
                """INSERT INTO pipeline_steps
                (project_id,step,status,progress,output_json,input_hash,checkpoint_version,attempt_count,
                 error_message,started_at,completed_at,updated_at)
                VALUES(?,?,?,?,?,?,?,CASE WHEN ?='RUNNING' THEN 1 ELSE 0 END,?,?,?,?)
                ON CONFLICT(project_id,step) DO UPDATE SET
                  status=excluded.status,progress=excluded.progress,output_json=excluded.output_json,
                  input_hash=COALESCE(excluded.input_hash,pipeline_steps.input_hash),
                  checkpoint_version=excluded.checkpoint_version,
                  attempt_count=pipeline_steps.attempt_count + CASE WHEN excluded.status='RUNNING' THEN 1 ELSE 0 END,
                  error_message=COALESCE(excluded.error_message,pipeline_steps.error_message),
                  started_at=COALESCE(pipeline_steps.started_at,excluded.started_at),
                  completed_at=excluded.completed_at,updated_at=excluded.updated_at""",
                (project_id, step, status, progress, json.dumps(output or {}, ensure_ascii=False),
                 input_hash, checkpoint_version, status, error_message, started_at, completed_at, timestamp),
            )
            row = connection.execute(
                "SELECT * FROM pipeline_steps WHERE project_id=? AND step=?", (project_id, step),
            ).fetchone()
        value = dict(row)
        value["output"] = json.loads(value.pop("output_json"))
        return value

    def pipeline_steps(self, project_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM pipeline_steps WHERE project_id=? ORDER BY updated_at", (project_id,),
            ).fetchall()
        result = []
        for row in rows:
            value = dict(row)
            raw_output = value.pop("output_json")
            try:
                value["output"] = json.loads(raw_output)
                value["corrupt_output"] = not isinstance(value["output"], dict)
                if value["corrupt_output"]:
                    value["output"] = {}
            except (TypeError, json.JSONDecodeError):
                value["output"] = {}
                value["corrupt_output"] = True
            result.append(value)
        return result

    def analysis_input(self, project_id: str) -> dict[str, Any]:
        project = self.get_project(project_id)
        with self.connect() as connection:
            windows = connection.execute(
                "SELECT pass_kind,start_sec,end_sec,reason,status FROM scan_windows WHERE project_id=? ORDER BY start_sec",
                (project_id,),
            ).fetchall()
            segments = connection.execute(
                """SELECT track_index,start_sec,end_sec,speaker_tag,text,confidence,words_json
                FROM transcript_segments WHERE project_id=? ORDER BY start_sec,track_index""", (project_id,),
            ).fetchall()
            artifacts = connection.execute(
                "SELECT kind,path,metadata_json FROM source_artifacts WHERE project_id=? ORDER BY artifact_id", (project_id,),
            ).fetchall()
            observations = connection.execute(
                """SELECT modality,kind,track_index,start_sec,end_sec,confidence,payload_json
                FROM analysis_observations WHERE project_id=? ORDER BY start_sec,modality,track_index""",
                (project_id,),
            ).fetchall()
            summaries = connection.execute(
                """SELECT sequence_order,start_sec,end_sec,summary,memory_json,precision_ranges_json
                FROM understanding_windows WHERE project_id=? ORDER BY sequence_order""", (project_id,),
            ).fetchall()
            event_rows = connection.execute(
                "SELECT event_id,summary,people_json,relations_json FROM events WHERE project_id=? ORDER BY rowid",
                (project_id,),
            ).fetchall()
            candidate_rows = connection.execute(
                """SELECT candidate_id,core_summary,related_event_ids_json,required_context,
                independence_score,decision,decision_reason FROM content_candidates
                WHERE project_id=? ORDER BY independence_score DESC""", (project_id,),
            ).fetchall()
            retrieval_rows = connection.execute(
                """SELECT candidate_id,query,start_sec,end_sec,score,scene_role,reasons_json
                FROM scene_retrieval_results WHERE project_id=? ORDER BY candidate_id,score DESC,start_sec""",
                (project_id,),
            ).fetchall()
            planning_rows = connection.execute(
                """SELECT planning_version_id,version_number,manifest_json,created_at FROM planning_versions
                WHERE project_id=? ORDER BY version_number DESC""", (project_id,),
            ).fetchall()
        transcript = [self._decode(dict(row), "words_json") for row in segments]
        observation_items = [self._decode(dict(row), "payload_json") for row in observations]
        timeline = [{
            "modality": "STT", "kind": "TRANSCRIPT_SEGMENT", "track_index": item["track_index"],
            "start_sec": item["start_sec"], "end_sec": item["end_sec"], "confidence": item["confidence"],
            "payload": {"speaker_tag": item["speaker_tag"], "text": item["text"], "words": item["words"]},
        } for item in transcript] + observation_items
        timeline.sort(key=lambda item: (
            item["start_sec"], item["end_sec"], {"STT": 0, "AUDIO": 1, "VISION": 2}[item["modality"]],
            -1 if item.get("track_index") is None else item["track_index"],
        ))
        events = []
        with self.connect() as connection:
            for row in event_rows:
                event = self._decode(self._decode(dict(row), "people_json"), "relations_json")
                mentions = connection.execute(
                    """SELECT source_start_sec start_sec,source_end_sec end_sec,role
                    FROM event_mentions WHERE event_id=? ORDER BY source_start_sec""", (event["event_id"],),
                ).fetchall()
                event["mentions"] = [dict(item) for item in mentions]
                events.append(event)
        episode_items = self.list_episodes(project_id)
        for episode in episode_items:
            episode["timeline"] = self.get_timeline(episode["episode_id"])
        active_strategy = self.active_strategy(project.get("channel_ref")) if project.get("channel_ref") else None
        return {
            "project": {
                "project_id": project_id, "duration_sec": project["duration_sec"],
                "media_info": json.loads(project.get("media_info_json") or "{}"),
                "target_duration_hint": project.get("target_duration_hint"),
            },
            "scan_windows": [dict(row) for row in windows],
            "transcript": transcript,
            "artifacts": [self._decode(dict(row), "metadata_json") for row in artifacts],
            "observations": observation_items,
            "timeline": timeline,
            "understanding_windows": [self._decode(self._decode(dict(row), "memory_json"), "precision_ranges_json")
                                      for row in summaries],
            "events": events,
            "candidates": [{
                "candidate_id": item["candidate_id"], "summary": item["core_summary"],
                "event_ids": item["related_event_ids"], "required_context": item["required_context"],
                "independence_score": item["independence_score"], "decision": item["decision"],
                "decision_reason": item["decision_reason"],
            } for item in (self._decode(dict(row), "related_event_ids_json") for row in candidate_rows)],
            "retrieved_scenes": [self._decode(dict(row), "reasons_json") for row in retrieval_rows],
            "planning_versions": [self._decode(dict(row), "manifest_json") for row in planning_rows],
            "production_strategy": active_strategy,
            "episodes": episode_items,
        }

    def apply_pacing_decisions(self, project_id: str, decisions: list[dict[str, Any]]) -> int:
        with self.connect() as connection:
            for item in decisions:
                result = connection.execute(
                    """UPDATE edit_timeline SET pacing_mode=?,pacing_reason=?
                    WHERE episode_id IN (SELECT episode_id FROM episodes WHERE project_id=?)
                    AND episode_id=? AND sequence_order=?""",
                    (item["pacing_mode"], item["reason"], project_id,
                     item["episode_id"], item["sequence_order"]),
                )
                if not result.rowcount:
                    raise ValueError("페이싱 결과가 존재하지 않는 프로젝트 컷을 참조합니다.")
            self._log(connection, project_id, "PLANNING", f"스마트 페이싱 컷 {len(decisions)}개 적용")
        return len(decisions)

    def save_planning_version(self, project_id: str, manifest: dict[str, Any]) -> dict[str, Any]:
        with self.connect() as connection:
            if not connection.execute("SELECT 1 FROM projects WHERE project_id=?", (project_id,)).fetchone():
                raise KeyError(project_id)
            version = connection.execute(
                "SELECT COALESCE(MAX(version_number),0)+1 FROM planning_versions WHERE project_id=?", (project_id,),
            ).fetchone()[0]
            connection.execute(
                """INSERT INTO planning_versions(project_id,version_number,manifest_json,created_at)
                VALUES(?,?,?,?)""", (project_id, version, json.dumps(manifest, ensure_ascii=False), now()),
            )
            row = connection.execute(
                "SELECT * FROM planning_versions WHERE project_id=? AND version_number=?", (project_id, version),
            ).fetchone()
        return self._decode(dict(row), "manifest_json")

    def latest_planning_version_for_episode(self, episode_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT v.planning_version_id,v.version_number,v.created_at FROM planning_versions v
                JOIN episodes e ON e.project_id=v.project_id WHERE e.episode_id=?
                ORDER BY v.version_number DESC LIMIT 1""", (episode_id,),
            ).fetchone()
        return dict(row) if row else None

    def replace_retrieved_scenes(self, project_id: str, scenes: list[dict[str, Any]]) -> int:
        duration = float(self.get_project(project_id)["duration_sec"])
        with self.connect() as connection:
            candidate_ids = {row[0] for row in connection.execute(
                "SELECT candidate_id FROM content_candidates WHERE project_id=?", (project_id,),
            )}
            rows = []
            for item in scenes:
                start, end, score = float(item["start_sec"]), float(item["end_sec"]), float(item["score"])
                if item["candidate_id"] not in candidate_ids or start < 0 or end <= start or end > duration:
                    raise ValueError("검색 장면의 후보 또는 원본 시간 범위가 올바르지 않습니다.")
                if not 0 <= score <= 1:
                    raise ValueError("검색 장면 점수는 0과 1 사이여야 합니다.")
                rows.append((project_id, item["candidate_id"], item.get("query", ""), start, end, score,
                             item["scene_role"], json.dumps(item["reasons"], ensure_ascii=False)))
            connection.execute("DELETE FROM scene_retrieval_results WHERE project_id=?", (project_id,))
            connection.executemany("""INSERT INTO scene_retrieval_results
                (project_id,candidate_id,query,start_sec,end_sec,score,scene_role,reasons_json)
                VALUES(?,?,?,?,?,?,?,?)""", rows)
            self._log(connection, project_id, "PLANNING", f"검색 장면 {len(rows)}개 저장")
        return len(rows)

    def replace_understanding_windows(self, project_id: str, windows: list[dict[str, Any]]) -> int:
        duration = float(self.get_project(project_id)["duration_sec"])
        rows = []
        for order, item in enumerate(windows):
            start, end = float(item["start_sec"]), float(item["end_sec"])
            if start < 0 or end <= start or end > duration:
                raise ValueError("장기 이해 창이 원본 시간 범위를 벗어났습니다.")
            if not str(item.get("summary", "")).strip() or not isinstance(item.get("memory"), dict):
                raise ValueError("장기 이해 요약과 메모리가 올바르지 않습니다.")
            rows.append((project_id, order, start, end, item["summary"],
                         json.dumps(item["memory"], ensure_ascii=False),
                         json.dumps(item.get("precision_ranges", []), ensure_ascii=False)))
        with self.connect() as connection:
            connection.execute("DELETE FROM understanding_windows WHERE project_id=?", (project_id,))
            connection.executemany("""INSERT INTO understanding_windows
                (project_id,sequence_order,start_sec,end_sec,summary,memory_json,precision_ranges_json)
                VALUES(?,?,?,?,?,?,?)""", rows)
            self._log(connection, project_id, "UNDERSTANDING", f"누적 장기 이해 창 {len(rows)}개 저장")
        return len(rows)

    def replace_observations(self, project_id: str, modality: str, observations: list[dict[str, Any]]) -> int:
        if modality not in {"AUDIO", "VISION"}:
            raise ValueError("modality must be AUDIO or VISION")
        duration = float(self.get_project(project_id)["duration_sec"])
        rows = []
        for item in observations:
            start, end = float(item["start_sec"]), float(item["end_sec"])
            if start < 0 or end <= start or end > duration + 1e-6:
                raise ValueError(f"관찰 시간 범위가 원본을 벗어났습니다: {start}~{end}")
            rows.append((project_id, modality, item["kind"], item.get("track_index"), start, end,
                         item.get("confidence"), json.dumps(item.get("payload", {}), ensure_ascii=False)))
        with self.connect() as connection:
            connection.execute("DELETE FROM analysis_observations WHERE project_id=? AND modality=?", (project_id, modality))
            connection.executemany(
                """INSERT INTO analysis_observations
                (project_id,modality,kind,track_index,start_sec,end_sec,confidence,payload_json)
                VALUES(?,?,?,?,?,?,?,?)""", rows,
            )
            self._log(connection, project_id, "UNDERSTANDING", f"{modality} 시간축 관찰 {len(rows)}개 저장")
        return len(rows)

    def replace_precision_observations(self, project_id: str, observations: list[dict[str, Any]]) -> int:
        grouped = {"AUDIO": [], "VISION": []}
        for item in observations:
            modality = item.get("modality")
            if modality not in grouped or not str(item.get("kind", "")).startswith("PRECISION_"):
                raise ValueError("정밀 관찰에는 AUDIO/VISION modality와 PRECISION_ kind가 필요합니다.")
            grouped[modality].append(item)
        duration = float(self.get_project(project_id)["duration_sec"])
        with self.connect() as connection:
            connection.execute("DELETE FROM analysis_observations WHERE project_id=? AND kind LIKE 'PRECISION_%'", (project_id,))
            for modality, items in grouped.items():
                for item in items:
                    start, end = float(item["start_sec"]), float(item["end_sec"])
                    if start < 0 or end <= start or end > duration + 1e-6:
                        raise ValueError(f"정밀 관찰 시간 범위가 원본을 벗어났습니다: {start}~{end}")
                    connection.execute(
                        """INSERT INTO analysis_observations
                        (project_id,modality,kind,track_index,start_sec,end_sec,confidence,payload_json)
                        VALUES(?,?,?,?,?,?,?,?)""",
                        (project_id, modality, item["kind"], item.get("track_index"), start, end,
                         item.get("confidence"), json.dumps(item.get("payload", {}), ensure_ascii=False)),
                    )
            self._log(connection, project_id, "UNDERSTANDING", f"2차 정밀 관찰 {len(observations)}개 저장")
        return len(observations)

    def import_analysis(self, project_id: str, manifest: dict[str, Any]) -> dict[str, int]:
        """Atomically replace AI analysis output using the documented interchange contract."""
        from .producer import validate_analysis_manifest

        project = self.get_project(project_id)
        manifest = validate_analysis_manifest(manifest, float(project["duration_sec"]))
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

    def list_episodes(self, project_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM episodes WHERE project_id=? ORDER BY rowid", (project_id,),
            ).fetchall()
        result = []
        for row in rows:
            value = dict(row)
            for key in ("candidate_ids_json", "planned_structure_json", "metadata_json"):
                value = self._decode(value, key)
            result.append(value)
        return result

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

    def set_episode_package(
        self, episode_id: str, metadata: dict[str, Any], thumbnail_path: str | None = None,
    ) -> None:
        with self.connect() as connection:
            result = connection.execute(
                """UPDATE episodes SET metadata_json=?,thumbnail_path=COALESCE(?,thumbnail_path)
                WHERE episode_id=?""",
                (json.dumps(metadata, ensure_ascii=False), thumbnail_path, episode_id),
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

    def list_uploads(
        self, status: str | None = None, *, include_resume_state: bool = False,
    ) -> list[dict[str, Any]]:
        with self.connect() as connection:
            if status:
                rows = connection.execute(
                    """SELECT u.*,e.output_mp4_path,e.thumbnail_path,e.metadata_json FROM upload_jobs u
                    JOIN episodes e USING(episode_id) WHERE u.status=? ORDER BY u.created_at""", (status,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """SELECT u.*,e.output_mp4_path,e.thumbnail_path,e.metadata_json FROM upload_jobs u
                    JOIN episodes e USING(episode_id) ORDER BY u.created_at DESC"""
                ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["metadata"] = json.loads(item.pop("metadata_json"))
            if not include_resume_state:
                item.pop("upload_session_url", None)
                item.pop("uploaded_bytes", None)
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
                retry_at=?,error_message=?,attempt_count=attempt_count+CASE WHEN ?='UPLOADING' THEN 1 ELSE 0 END,
                updated_at=? WHERE upload_id=?""",
                (status, youtube_video_id, retry_at, error_message, status, now(), upload_id),
            )
            if not result.rowcount:
                raise KeyError(upload_id)
            row = connection.execute("SELECT * FROM upload_jobs WHERE upload_id=?", (upload_id,)).fetchone()
        return dict(row)

    def set_upload_progress(self, upload_id: str, session_url: str | None, uploaded_bytes: int) -> dict[str, Any]:
        if uploaded_bytes < 0 or (uploaded_bytes and not session_url):
            raise ValueError("업로드 진행률에는 세션 URL과 0 이상의 바이트가 필요합니다.")
        with self.connect() as connection:
            result = connection.execute(
                """UPDATE upload_jobs SET upload_session_url=?,uploaded_bytes=?,updated_at=?
                WHERE upload_id=?""",
                (session_url, uploaded_bytes, now(), upload_id),
            )
            if not result.rowcount:
                raise KeyError(upload_id)
            row = connection.execute("SELECT * FROM upload_jobs WHERE upload_id=?", (upload_id,)).fetchone()
        return dict(row)

    def recover_interrupted_uploads(self) -> list[str]:
        """Move uploads abandoned by a previous process back to the durable retry queue."""
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT upload_id FROM upload_jobs WHERE status='UPLOADING' ORDER BY created_at"
            ).fetchall()
            upload_ids = [row["upload_id"] for row in rows]
            if upload_ids:
                connection.execute(
                    """UPDATE upload_jobs SET status='RETRY_QUEUED',retry_at=NULL,
                    error_message='이전 프로세스에서 중단된 업로드를 복구했습니다.',updated_at=?
                    WHERE status='UPLOADING'""",
                    (now(),),
                )
        return upload_ids

    def record_upload_publication(
        self, upload_id: str, publication_status: str, scheduled_publish_at: str | None = None,
    ) -> dict[str, Any]:
        if publication_status not in {"PRIVATE", "UNLISTED", "PUBLIC", "SCHEDULED"}:
            raise ValueError("지원하지 않는 YouTube 공개 상태입니다.")
        with self.connect() as connection:
            result = connection.execute(
                """UPDATE upload_jobs SET publication_status=?,scheduled_publish_at=?,updated_at=?
                WHERE upload_id=? AND status='COMPLETE'""",
                (publication_status, scheduled_publish_at, now(), upload_id),
            )
            if not result.rowcount:
                raise ValueError("완료된 업로드만 공개 상태를 변경할 수 있습니다.")
            row = connection.execute("SELECT * FROM upload_jobs WHERE upload_id=?", (upload_id,)).fetchone()
        return dict(row)

    def record_thumbnail_uploaded(self, upload_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            result = connection.execute(
                """UPDATE upload_jobs SET thumbnail_uploaded_at=?,updated_at=?
                WHERE upload_id=? AND status='COMPLETE'""", (now(), now(), upload_id),
            )
            if not result.rowcount:
                raise ValueError("완료된 업로드에만 썸네일을 적용할 수 있습니다.")
            row = connection.execute("SELECT * FROM upload_jobs WHERE upload_id=?", (upload_id,)).fetchone()
        return dict(row)

    def schedule_analytics_snapshots(
        self, episode_id: str, youtube_video_id: str, published_at: datetime | None = None,
    ) -> list[dict[str, Any]]:
        origin = published_at or datetime.now(timezone.utc)
        if origin.tzinfo is None:
            raise ValueError("published_at은 timezone-aware datetime이어야 합니다.")
        schedules = (("24H", timedelta(hours=24)), ("7D", timedelta(days=7)), ("30D", timedelta(days=30)))
        timestamp = now()
        with self.connect() as connection:
            if not connection.execute("SELECT 1 FROM episodes WHERE episode_id=?", (episode_id,)).fetchone():
                raise KeyError(episode_id)
            for label, delay in schedules:
                connection.execute(
                    """INSERT OR IGNORE INTO analytics_collection_jobs
                    (collection_id,episode_id,youtube_video_id,snapshot_label,due_at,status,created_at,updated_at)
                    VALUES(?,?,?,?,?,'QUEUED',?,?)""",
                    (str(uuid.uuid4()), episode_id, youtube_video_id, label,
                     (origin + delay).astimezone(timezone.utc).isoformat(), timestamp, timestamp),
                )
            rows = connection.execute(
                "SELECT * FROM analytics_collection_jobs WHERE episode_id=? ORDER BY due_at", (episode_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def due_analytics_collections(self, at: datetime | None = None) -> list[dict[str, Any]]:
        moment = (at or datetime.now(timezone.utc))
        if moment.tzinfo is None:
            raise ValueError("at은 timezone-aware datetime이어야 합니다.")
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT j.*,e.planned_duration_sec FROM analytics_collection_jobs j
                JOIN episodes e USING(episode_id) WHERE j.status IN ('QUEUED','FAILED') AND j.due_at<=?
                ORDER BY j.due_at""", (moment.astimezone(timezone.utc).isoformat(),),
            ).fetchall()
        return [dict(row) for row in rows]

    def set_analytics_collection_status(
        self, collection_id: str, status: str, error_message: str | None = None,
    ) -> dict[str, Any]:
        if status not in {"QUEUED", "RUNNING", "COMPLETE", "FAILED"}:
            raise ValueError("지원하지 않는 Analytics 수집 상태입니다.")
        with self.connect() as connection:
            result = connection.execute(
                """UPDATE analytics_collection_jobs SET status=?,error_message=?,updated_at=?,
                attempt_count=attempt_count+CASE WHEN ?='RUNNING' THEN 1 ELSE 0 END WHERE collection_id=?""",
                (status, error_message, now(), status, collection_id),
            )
            if not result.rowcount:
                raise KeyError(collection_id)
            row = connection.execute(
                "SELECT * FROM analytics_collection_jobs WHERE collection_id=?", (collection_id,),
            ).fetchone()
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

    def get_calibration(self, profile_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM calibration_profiles WHERE profile_id=?", (profile_id,),
            ).fetchone()
        if not row:
            raise KeyError(profile_id)
        value = dict(row)
        value["params"] = json.loads(value.pop("params_json"))
        return value

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

    def list_channel_performance(self, channel_ref: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT s.* FROM performance_snapshots s JOIN episodes e USING(episode_id)
                JOIN projects p USING(project_id) WHERE p.channel_ref=? ORDER BY s.collected_at""",
                (channel_ref,),
            ).fetchall()
        result = []
        for row in rows:
            value = dict(row)
            value["metrics"] = json.loads(value.pop("metrics_json"))
            result.append(value)
        return result

    def save_strategy_version(self, channel_ref: str, strategy: dict[str, Any]) -> dict[str, Any]:
        with self.connect() as connection:
            version = connection.execute(
                "SELECT COALESCE(MAX(version_number),0)+1 FROM strategy_versions WHERE channel_ref=?",
                (channel_ref,),
            ).fetchone()[0]
            strategy_id = str(uuid.uuid4())
            connection.execute(
                """INSERT INTO strategy_versions
                (strategy_version_id,channel_ref,version_number,status,strategy_json,created_at)
                VALUES(?,?,?,'DRAFT',?,?)""",
                (strategy_id, channel_ref, version, json.dumps(strategy, ensure_ascii=False), now()),
            )
            row = connection.execute(
                "SELECT * FROM strategy_versions WHERE strategy_version_id=?", (strategy_id,),
            ).fetchone()
        return self._decode(dict(row), "strategy_json")

    def activate_strategy_version(self, strategy_version_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            target = connection.execute(
                "SELECT channel_ref FROM strategy_versions WHERE strategy_version_id=?", (strategy_version_id,),
            ).fetchone()
            if not target:
                raise KeyError(strategy_version_id)
            connection.execute(
                "UPDATE strategy_versions SET status='ROLLED_BACK' WHERE channel_ref=? AND status='ACTIVE'",
                (target["channel_ref"],),
            )
            connection.execute(
                "UPDATE strategy_versions SET status='ACTIVE' WHERE strategy_version_id=?", (strategy_version_id,),
            )
            row = connection.execute(
                "SELECT * FROM strategy_versions WHERE strategy_version_id=?", (strategy_version_id,),
            ).fetchone()
        return self._decode(dict(row), "strategy_json")

    def list_strategy_versions(self, channel_ref: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM strategy_versions WHERE channel_ref=? ORDER BY version_number DESC", (channel_ref,),
            ).fetchall()
        return [self._decode(dict(row), "strategy_json") for row in rows]

    def active_strategy(self, channel_ref: str | None) -> dict[str, Any] | None:
        if not channel_ref:
            return None
        with self.connect() as connection:
            row = connection.execute(
                """SELECT * FROM strategy_versions WHERE channel_ref=? AND status='ACTIVE'
                ORDER BY version_number DESC LIMIT 1""", (channel_ref,),
            ).fetchone()
        return self._decode(dict(row), "strategy_json") if row else None

    def replace_scan_windows(self, project_id: str, windows: list[dict[str, Any]]) -> int:
        with self.connect() as connection:
            if not connection.execute("SELECT 1 FROM projects WHERE project_id=?", (project_id,)).fetchone():
                raise KeyError(project_id)
            connection.execute("DELETE FROM scan_windows WHERE project_id=?", (project_id,))
            connection.executemany(
                """INSERT INTO scan_windows(project_id,pass_kind,start_sec,end_sec,reason)
                VALUES(?,?,?,?,?)""",
                [(project_id, item["pass_kind"], item["start_sec"], item["end_sec"], item.get("reason")) for item in windows],
            )
        return len(windows)

    def replace_transcript(self, project_id: str, segments: list[dict[str, Any]]) -> int:
        with self.connect() as connection:
            if not connection.execute("SELECT 1 FROM projects WHERE project_id=?", (project_id,)).fetchone():
                raise KeyError(project_id)
            connection.execute("DELETE FROM transcript_segments WHERE project_id=?", (project_id,))
            connection.executemany(
                """INSERT INTO transcript_segments
                (project_id,track_index,start_sec,end_sec,speaker_tag,text,confidence,words_json)
                VALUES(?,?,?,?,?,?,?,?)""",
                [(project_id, item["track_index"], item["start_sec"], item["end_sec"], item["speaker_tag"],
                  item["text"], item["confidence"], json.dumps(item["words"], ensure_ascii=False)) for item in segments],
            )
            self._log(connection, project_id, "UNDERSTANDING", f"단어 타임스탬프 자막 {len(segments)}개 저장")
        return len(segments)

    def add_artifacts(self, project_id: str, artifacts: list[dict[str, Any]]) -> int:
        with self.connect() as connection:
            connection.executemany(
                """INSERT OR REPLACE INTO source_artifacts(project_id,kind,path,metadata_json,created_at)
                VALUES(?,?,?,?,?)""",
                [(project_id, item["kind"], item["path"], json.dumps(item.get("metadata", {}), ensure_ascii=False), now()) for item in artifacts],
            )
        return len(artifacts)

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
