import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from backend.database import Database
from backend.pipeline import PipelineManager


class PipelineTest(unittest.TestCase):
    def test_pipeline_persists_steps_and_reuses_checkpoints(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "pipeline.db")
            project = database.create_project({"file_path": "/media/live.mkv"})
            calls = []

            def probe(_path):
                calls.append("probe")
                return SimpleNamespace(to_dict=lambda: {
                    "duration_sec": 100, "width": 1920, "height": 1080, "audio_tracks": 0,
                })

            manager = PipelineManager(database, probe=probe)
            manager._run(project["project_id"], {"coarse_window_sec": 30}, True, threading.Event())
            self.assertEqual(
                [step["step"] for step in database.pipeline_steps(project["project_id"])],
                ["PROBE", "SCAN_PLAN"],
            )
            self.assertEqual(database.get_project(project["project_id"])["status"], "UNDERSTANDING")
            manager._run(project["project_id"], {"coarse_window_sec": 30}, True, threading.Event())
            self.assertEqual(calls, ["probe"])
            manager.shutdown()

    def test_failed_step_is_durable_and_project_can_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "pipeline.db")
            project = database.create_project({"file_path": "/missing.mkv"})
            manager = PipelineManager(database, probe=lambda _path: (_ for _ in ()).throw(RuntimeError("probe failed")))
            manager._run(project["project_id"], {}, True, threading.Event())
            self.assertEqual(database.get_project(project["project_id"])["status"], "FAILED")
            self.assertEqual(database.pipeline_steps(project["project_id"])[0]["status"], "FAILED")
            manager.shutdown()

    def test_changed_options_invalidate_old_checkpoints(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "pipeline.db")
            project = database.create_project({"file_path": "/media/live.mkv"})
            calls = []
            manager = PipelineManager(database, probe=lambda _path: (
                calls.append("probe") or SimpleNamespace(to_dict=lambda: {
                    "duration_sec": 100, "width": 1920, "height": 1080, "audio_tracks": 0,
                })
            ))
            manager._run(project["project_id"], {"coarse_window_sec": 30}, True, threading.Event())
            first_hash = database.pipeline_steps(project["project_id"])[0]["input_hash"]
            manager._run(project["project_id"], {"coarse_window_sec": 20}, True, threading.Event())
            step = database.pipeline_steps(project["project_id"])[0]
            self.assertEqual(calls, ["probe", "probe"])
            self.assertNotEqual(first_hash, step["input_hash"])
            self.assertEqual(step["attempt_count"], 2)
            manager.shutdown()

    def test_pipeline_can_run_external_producer_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "pipeline.db")
            project = database.create_project({"file_path": "/media/live.mkv"})
            manifest = json.loads(
                (Path(__file__).parent / "fixtures" / "analysis-manifest.json").read_text(encoding="utf-8")
            )
            observed = {}

            def produce(_command, analysis_input, _output, _duration):
                observed.update(analysis_input)
                return {"manifest": manifest, "command": ["producer"]}

            manager = PipelineManager(
                database,
                probe=lambda _path: SimpleNamespace(to_dict=lambda: {
                    "duration_sec": 5000, "width": 1920, "height": 1080, "audio_tracks": 0,
                }),
                produce=produce,
            )
            manager._run(project["project_id"], {"producer_executable": ["producer"]}, True, threading.Event())
            self.assertEqual(observed["project"]["project_id"], project["project_id"])
            self.assertEqual(database.get_project(project["project_id"])["status"], "PLANNING")
            self.assertEqual(database.pipeline_steps(project["project_id"])[-1]["step"], "AI_PRODUCER")
            manager.shutdown()

    def test_pipeline_aligns_audio_and_vision_before_producer(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "pipeline.db")
            project = database.create_project({"file_path": "/media/live.mkv"})
            observed = {}
            manager = PipelineManager(
                database,
                probe=lambda _path: SimpleNamespace(to_dict=lambda: {
                    "duration_sec": 10, "width": 1920, "height": 1080, "audio_tracks": 1,
                }),
                analyze_audio=lambda *_args, **_kwargs: {"observations": [{
                    "kind": "SIGNAL_WINDOW", "track_index": 0, "start_sec": 0, "end_sec": 1,
                    "confidence": None, "payload": {"rms_dbfs": -12},
                }]},
                analyze_vision=lambda *_args, **_kwargs: {"observations": [{
                    "kind": "FRAME_SIGNAL", "track_index": None, "start_sec": 0, "end_sec": 5,
                    "confidence": None, "payload": {"signalstats.YAVG": 80},
                }]},
                produce=lambda _command, analysis, *_args: observed.update(analysis) or {
                    "manifest": {"schema_version": 1, "events": [], "candidates": [], "episodes": []}
                },
            )
            manager._run(project["project_id"], {
                "audio_analysis": True, "vision_analysis": True, "audio_paths": ["ignored.wav"],
                "producer_executable": ["producer"],
            }, True, threading.Event())
            self.assertEqual([item["modality"] for item in observed["observations"]], ["AUDIO", "VISION"])
            self.assertEqual([step["step"] for step in database.pipeline_steps(project["project_id"])],
                             ["PROBE", "SCAN_PLAN", "AUDIO_ANALYSIS", "VISION_ANALYSIS", "AI_PRODUCER"])
            manager.shutdown()


if __name__ == "__main__":
    unittest.main()
