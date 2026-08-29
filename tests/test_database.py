import json
import tempfile
import unittest
from pathlib import Path

from backend.database import Database


class DatabaseTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "test.db")

    def tearDown(self):
        self.temp.cleanup()

    def test_project_creation_and_status_log_are_atomic(self):
        project = self.db.create_project({"file_path": "/media/live.mkv", "channel_ref": "JUNE"})
        self.assertEqual(project["status"], "QUEUED")
        self.assertEqual(project["name"], "live")
        self.db.update_status(project["project_id"], "PARSING", 12, "트랙 분석 중")
        updated = self.db.get_project(project["project_id"])
        self.assertEqual((updated["status"], updated["progress"]), ("PARSING", 12))
        self.assertEqual([log["stage"] for log in self.db.logs(project["project_id"])], ["PARSING", "QUEUED"])

    def test_candidate_review_and_non_linear_timeline(self):
        project = self.db.create_project({"file_path": "/media/live.mkv"})
        with self.db.connect() as connection:
            connection.execute(
                "INSERT INTO content_candidates VALUES(?,?,?,?,?,?,?,?,?,?)",
                ("candidate-1", project["project_id"], "사건", "[]", "맥락", .91, "HOLD", "판단 근거", None, None),
            )
            connection.execute(
                "INSERT INTO episodes VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                ("episode-1", project["project_id"], '["candidate-1"]', '{}', "LONG", 300, None, None, "PENDING", "PENDING", '{}'),
            )
            for order, start in enumerate((300.0, 20.0, 180.0), 1):
                connection.execute(
                    """INSERT INTO edit_timeline
                    (episode_id,sequence_order,source_start_sec,source_end_sec,speaker_tag,scene_role,pacing_mode,visual_effect_json)
                    VALUES(?,?,?,?,?,?,?,?)""",
                    ("episode-1", order, start, start + 10, "JUNE", "핵심", "KEEP", json.dumps({"type": "zoom"})),
                )
        reviewed = self.db.review_candidate("candidate-1", "MAKE", "동의")
        self.assertEqual(reviewed["decision"], "MAKE")
        timeline = self.db.get_timeline("episode-1")
        self.assertEqual([cut["source_start_sec"] for cut in timeline], [300, 20, 180])
        self.assertEqual(timeline[0]["visual_effect"], {"type": "zoom"})

    def test_constraints_reject_invalid_pacing(self):
        project = self.db.create_project({"file_path": "/media/live.mkv"})
        with self.db.connect() as connection:
            connection.execute(
                "INSERT INTO episodes VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                ("episode-1", project["project_id"], '[]', '{}', "LONG", 300, None, None, "PENDING", "PENDING", '{}'),
            )
            with self.assertRaises(Exception):
                connection.execute(
                    """INSERT INTO edit_timeline
                    (episode_id,sequence_order,source_start_sec,source_end_sec,scene_role,pacing_mode)
                    VALUES(?,?,?,?,?,?)""",
                    ("episode-1", 1, 10, 20, "핵심", "INVALID"),
                )

    def test_analysis_manifest_is_imported_atomically(self):
        project = self.db.create_project({"file_path": "/media/live.mkv"})
        manifest = json.loads((Path(__file__).parent / "fixtures" / "analysis-manifest.json").read_text())
        counts = self.db.import_analysis(project["project_id"], manifest)
        self.assertEqual(counts, {"events": 1, "candidates": 1, "episodes": 1, "cuts": 3})
        self.assertEqual(self.db.get_project(project["project_id"])["status"], "PLANNING")
        self.assertEqual([cut["source_start_sec"] for cut in self.db.get_timeline("episode-operation")], [2581, 1022, 3827])
        episodes = self.db.list_episodes(project["project_id"])
        self.assertEqual(episodes[0]["episode_id"], "episode-operation")
        self.assertEqual(episodes[0]["candidate_ids"], ["candidate-operation"])

    def test_upload_queue_enforces_render_and_human_review_gates(self):
        project = self.db.create_project({"file_path": "/media/live.mkv"})
        manifest = json.loads((Path(__file__).parent / "fixtures" / "analysis-manifest.json").read_text())
        self.db.import_analysis(project["project_id"], manifest)
        with self.assertRaisesRegex(ValueError, "사람 검수"):
            self.db.queue_upload("episode-operation")
        self.db.review_episode("episode-operation", True)
        with self.assertRaisesRegex(ValueError, "렌더링"):
            self.db.queue_upload("episode-operation")
        self.db.set_render_status("episode-operation", "COMPLETE", "/output/episode.mp4")
        upload = self.db.queue_upload("episode-operation", "PRIVATE")
        self.assertEqual((upload["status"], upload["privacy_status"]), ("QUEUED", "PRIVATE"))
        with self.assertRaises(ValueError):
            self.db.queue_upload("episode-operation", "PUBLIC")


if __name__ == "__main__":
    unittest.main()
