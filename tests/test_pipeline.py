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

    def test_step_retry_policy_recovers_and_records_each_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "pipeline.db")
            project = database.create_project({"file_path": "/media/live.mkv"})
            calls = []

            def probe(_path):
                calls.append("probe")
                if len(calls) == 1:
                    raise RuntimeError("temporary probe failure")
                return SimpleNamespace(to_dict=lambda: {
                    "duration_sec": 100, "width": 1920, "height": 1080, "audio_tracks": 0,
                })

            manager = PipelineManager(database, probe=probe)
            manager._run(project["project_id"], {
                "retry_policy": {"PROBE": {"max_attempts": 2, "backoff_sec": 0}},
            }, True, threading.Event())
            probe_step = database.pipeline_steps(project["project_id"])[0]
            self.assertEqual(calls, ["probe", "probe"])
            self.assertEqual(probe_step["status"], "COMPLETE")
            self.assertEqual(probe_step["attempt_count"], 2)
            self.assertEqual(probe_step["error_message"], "temporary probe failure")
            manager.shutdown()

    def test_invalid_retry_policy_fails_before_analysis(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "pipeline.db")
            project = database.create_project({"file_path": "/media/live.mkv"})
            manager = PipelineManager(database, probe=lambda _path: self.fail("probe must not run"))
            manager._run(project["project_id"], {
                "retry_policy": {"PROBE": {"max_attempts": 0}},
            }, True, threading.Event())
            self.assertEqual(database.get_project(project["project_id"])["status"], "FAILED")
            self.assertEqual(database.pipeline_steps(project["project_id"]), [])
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

    def test_corrupt_checkpoint_output_is_recomputed(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "pipeline.db")
            project = database.create_project({"file_path": "/media/live.mkv"})
            calls = []
            manager = PipelineManager(database, probe=lambda _path: (
                calls.append("probe") or SimpleNamespace(to_dict=lambda: {
                    "duration_sec": 100, "width": 1920, "height": 1080, "audio_tracks": 0,
                })
            ))
            manager._run(project["project_id"], {}, True, threading.Event())
            with database.connect() as connection:
                connection.execute(
                    "UPDATE pipeline_steps SET output_json='not-json' WHERE project_id=? AND step='PROBE'",
                    (project["project_id"],),
                )
            self.assertTrue(database.pipeline_steps(project["project_id"])[0]["corrupt_output"])
            manager._run(project["project_id"], {}, True, threading.Event())
            self.assertEqual(calls, ["probe", "probe"])
            self.assertFalse(database.pipeline_steps(project["project_id"])[0]["corrupt_output"])
            manager.shutdown()

    def test_missing_preprocess_artifact_invalidates_its_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "pipeline.db")
            project = database.create_project({"file_path": "/media/live.mkv"})
            calls = []

            def preprocess(_plan):
                calls.append("preprocess")
                return {"artifacts": [{"kind": "FRAMES", "path": str(Path(directory) / "missing.jpg"),
                                        "command": ["ffmpeg"]}]}

            manager = PipelineManager(
                database, preprocess=preprocess,
                probe=lambda _path: SimpleNamespace(to_dict=lambda: {
                    "duration_sec": 100, "width": 1920, "height": 1080, "audio_tracks": 0,
                }),
            )
            options = {"preprocess": True, "frame_interval_sec": 10}
            manager._run(project["project_id"], options, True, threading.Event())
            manager._run(project["project_id"], options, True, threading.Event())
            self.assertEqual(calls, ["preprocess", "preprocess"])
            manager.shutdown()

    def test_disk_check_runs_before_analysis(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "pipeline.db")
            project = database.create_project({"file_path": "/media/live.mkv"})
            calls = []
            manager = PipelineManager(
                database,
                probe=lambda _path: SimpleNamespace(to_dict=lambda: {
                    "duration_sec": 100, "width": 1920, "height": 1080, "audio_tracks": 0,
                }),
                check_disk=lambda _path, required, reserve: (
                    calls.append((required, reserve)) or {"available_bytes": required}
                ),
            )
            manager._run(project["project_id"], {
                "disk_check": True, "disk_required_bytes": 1000, "disk_reserve_bytes": 200,
            }, True, threading.Event())
            self.assertEqual(calls, [(1000, 200)])
            self.assertEqual([step["step"] for step in database.pipeline_steps(project["project_id"])],
                             ["PROBE", "DISK_CHECK", "SCAN_PLAN"])
            manager.shutdown()

    def test_channel_calibration_supplies_pipeline_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "pipeline.db")
            profile = database.save_calibration("channel-1", "profile", {
                "pipeline_options": {"coarse_window_sec": 4},
            }, 90)
            project = database.create_project({
                "file_path": "/media/live.mkv", "calibration_profile_id": profile["profile_id"],
            })
            manager = PipelineManager(database, probe=lambda _path: SimpleNamespace(to_dict=lambda: {
                "duration_sec": 10, "width": 1920, "height": 1080, "audio_tracks": 0,
            }))
            manager._run(project["project_id"], {}, True, threading.Event())
            coarse = [item for item in database.analysis_input(project["project_id"])["scan_windows"]
                      if item["pass_kind"] == "COARSE"]
            self.assertEqual([(item["start_sec"], item["end_sec"]) for item in coarse], [(0, 4), (4, 8), (8, 10)])
            self.assertEqual(database.get_calibration(profile["profile_id"])["name"], "profile")
            manager.shutdown()

    def test_long_term_understanding_carries_memory_across_windows(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "pipeline.db")
            project = database.create_project({"file_path": "/media/live.mkv"})
            received = []

            def understand(_executable, window, _timeline, memory, _output):
                received.append(dict(memory))
                count = memory.get("count", 0) + 1
                return {"summary": f"window {count}", "memory": {"count": count},
                        "precision_ranges": []}

            manager = PipelineManager(
                database, understand_window=understand,
                probe=lambda _path: SimpleNamespace(to_dict=lambda: {
                    "duration_sec": 10, "width": 1920, "height": 1080, "audio_tracks": 0,
                }),
            )
            manager._run(project["project_id"], {
                "coarse_window_sec": 5, "understanding_executable": ["model"],
            }, True, threading.Event())
            self.assertEqual(received, [{}, {"count": 1}])
            windows = database.analysis_input(project["project_id"])["understanding_windows"]
            self.assertEqual([item["summary"] for item in windows], ["window 1", "window 2"])
            self.assertEqual(windows[-1]["memory"]["count"], 2)
            manager.shutdown()

    def test_content_discovery_persists_event_graph_without_forcing_episodes(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "pipeline.db")
            project = database.create_project({"file_path": "/media/live.mkv"})
            manager = PipelineManager(
                database,
                probe=lambda _path: SimpleNamespace(to_dict=lambda: {
                    "duration_sec": 10, "width": 1920, "height": 1080, "audio_tracks": 0,
                }),
                discover=lambda *_args: {"manifest": {
                    "events": [{"event_id": "event-1", "summary": "event",
                                "mentions": [{"start_sec": 1, "end_sec": 2, "role": "origin"}]}],
                    "candidates": [{"candidate_id": "candidate-1", "summary": "candidate",
                                    "event_ids": ["event-1"], "independence_score": .8,
                    "decision": "MAKE", "decision_reason": "complete"}], "episodes": [],
                }},
                retrieve=lambda *_args: {"scenes": [{
                    "candidate_id": "candidate-1", "query": "origin", "start_sec": 1, "end_sec": 2,
                    "score": .9, "scene_role": "origin", "reasons": ["event_mention"],
                }]},
            )
            manager._run(project["project_id"], {
                "discovery_executable": ["model"], "retrieval_executable": ["retriever"],
            }, True, threading.Event())
            analysis = database.analysis_input(project["project_id"])
            self.assertEqual(analysis["events"][0]["mentions"][0]["role"], "origin")
            self.assertEqual(analysis["candidates"][0]["event_ids"], ["event-1"])
            self.assertEqual(analysis["retrieved_scenes"][0]["reasons"], ["event_mention"])
            self.assertEqual(database.get_project(project["project_id"])["status"], "PLANNING")
            manager.shutdown()

    def test_dynamic_planning_versions_non_linear_episode(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "pipeline.db")
            project = database.create_project({"file_path": "/media/live.mkv"})
            subtitle = Path(directory) / "episode-1.ass"
            subtitle.write_text("[Script Info]\n")
            discovery = {"events": [{"event_id": "event-1", "summary": "event", "mentions": []}],
                         "candidates": [{"candidate_id": "candidate-1", "summary": "candidate",
                                         "event_ids": ["event-1"], "independence_score": .8,
                                         "decision": "MAKE", "decision_reason": "complete"}], "episodes": []}

            def render_episode(plan, loudness_target):
                self.assertEqual(plan.subtitle_path, str(subtitle))
                self.assertEqual(plan.audio_mix[1]["role"], "GAME")
                self.assertEqual(plan.ducking["foreground_track_index"], 0)
                output = Path(plan.output_path)
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(b"rendered")
                return {"output_path": str(output), "target": loudness_target.integrated_lufs}

            def generate_packages(_executable, analysis, _output):
                self.assertEqual(analysis["episodes"][0]["render_status"], "COMPLETE")
                return {"packages": [{
                    "episode_id": "episode-1", "metadata": {
                        "title_options": ["제목 A", "제목 B", "제목 C"],
                        "description": "설명", "tags": ["게임"], "chapters": [],
                    }, "thumbnail_timestamps": [1],
                }]}

            def package_episode(metadata, _video, _timestamps, output):
                output.mkdir(parents=True, exist_ok=True)
                json_path, text_path = output / "metadata.json", output / "metadata.txt"
                thumbnail = output / "thumbnail-01.jpg"
                json_path.write_text(json.dumps(metadata))
                text_path.write_text("metadata")
                thumbnail.write_bytes(b"image")
                return {"json_path": str(json_path), "text_path": str(text_path),
                        "thumbnails": [str(thumbnail)], "metadata": metadata}

            manager = PipelineManager(
                database,
                probe=lambda _path: SimpleNamespace(to_dict=lambda: {
                    "duration_sec": 100, "width": 1920, "height": 1080, "audio_tracks": 2,
                }),
                discover=lambda *_args: {"manifest": discovery},
                plan=lambda *_args: {"episodes": [{
                    "episode_id": "episode-1", "candidate_ids": ["candidate-1"], "target_type": "LONG",
                    "timeline": [{"source_start_sec": 50, "source_end_sec": 55, "scene_role": "result",
                                  "pacing_mode": "KEEP"},
                                 {"source_start_sec": 10, "source_end_sec": 20, "scene_role": "context",
                                  "pacing_mode": "TRIM"}],
                }], "manifest": {**discovery, "episodes": [{
                    "episode_id": "episode-1", "candidate_ids": ["candidate-1"], "target_type": "LONG",
                    "timeline": [{"source_start_sec": 50, "source_end_sec": 55, "scene_role": "result",
                                  "pacing_mode": "KEEP"},
                                 {"source_start_sec": 10, "source_end_sec": 20, "scene_role": "context",
                                  "pacing_mode": "TRIM"}],
                }]}},
                pace=lambda *_args: {"decisions": [
                    {"episode_id": "episode-1", "sequence_order": 1, "pacing_mode": "KEEP",
                     "reason": "preserve result reaction"},
                    {"episode_id": "episode-1", "sequence_order": 2, "pacing_mode": "CUT",
                     "reason": "remove repeated context"},
                ]},
                render_episode=render_episode,
                generate_packages=generate_packages,
                package_episode=package_episode,
            )
            manager._run(project["project_id"], {
                "discovery_executable": ["discovery"], "planner_executable": ["planner"],
                "pacing_executable": ["pacing"], "render": True,
                "render_output_directory": str(Path(directory) / "renders"),
                "subtitle_paths": {"episode-1": str(subtitle)},
                "render_audio_mix": [{"track_index": 0, "volume": 1, "role": "MIC"},
                                     {"track_index": 1, "volume": 0.4, "role": "GAME"}],
                "render_ducking": {"foreground_track_index": 0, "threshold": 0.08, "ratio": 6,
                                   "attack_ms": 20, "release_ms": 350},
                "packaging_executable": ["packager"],
                "package_output_directory": str(Path(directory) / "packages"),
            }, True, threading.Event())
            self.assertEqual([item["source_start_sec"] for item in database.get_timeline("episode-1")], [50, 10])
            self.assertEqual([item["pacing_mode"] for item in database.get_timeline("episode-1")], ["KEEP", "CUT"])
            self.assertEqual(database.get_timeline("episode-1")[1]["pacing_reason"], "remove repeated context")
            versions = database.analysis_input(project["project_id"])["planning_versions"]
            self.assertEqual(versions[0]["version_number"], 1)
            self.assertEqual(database.get_episode("episode-1")["render_status"], "COMPLETE")
            self.assertEqual(database.get_episode("episode-1")["metadata"]["title_options"][0], "제목 A")
            self.assertTrue(Path(database.get_episode("episode-1")["thumbnail_path"]).is_file())
            self.assertEqual(database.get_project(project["project_id"])["status"], "REVIEW_PENDING")
            manager.shutdown()

    def test_chunked_analysis_resumes_from_the_failed_chunk(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "pipeline.db")
            project = database.create_project({"file_path": "/media/live.mkv"})
            calls, fail_once = [], {4.0}

            def analyze_audio(_paths, _duration, *, window_sec, ranges):
                start = ranges[0]["start_sec"]
                calls.append(start)
                if start in fail_once:
                    fail_once.remove(start)
                    raise RuntimeError("temporary chunk failure")
                return {"observations": [{
                    "kind": "SIGNAL_WINDOW", "track_index": 0, "start_sec": start,
                    "end_sec": ranges[0]["end_sec"], "confidence": None,
                    "payload": {"rms_dbfs": -10, "window_sec": window_sec},
                }]}

            manager = PipelineManager(
                database,
                probe=lambda _path: SimpleNamespace(to_dict=lambda: {
                    "duration_sec": 10, "width": 1920, "height": 1080, "audio_tracks": 1,
                }),
                analyze_audio=analyze_audio,
            )
            manager._default_audio = True
            options = {"audio_analysis": True, "audio_paths": ["track.wav"], "analysis_chunk_sec": 4}
            manager._run(project["project_id"], options, True, threading.Event())
            self.assertEqual(database.get_project(project["project_id"])["status"], "FAILED")
            manager._run(project["project_id"], options, True, threading.Event())
            self.assertEqual(calls, [0.0, 4.0, 4.0, 8.0])
            observations = database.analysis_input(project["project_id"])["observations"]
            self.assertEqual([(item["start_sec"], item["end_sec"]) for item in observations],
                             [(0, 4), (4, 8), (8, 10)])
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
            self.assertEqual([item["modality"] for item in observed["timeline"]], ["AUDIO", "VISION"])
            self.assertEqual([step["step"] for step in database.pipeline_steps(project["project_id"])],
                             ["PROBE", "SCAN_PLAN", "AUDIO_ANALYSIS", "VISION_ANALYSIS", "AI_PRODUCER"])
            manager.shutdown()

    def test_pipeline_executes_selected_precision_ranges(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "pipeline.db")
            project = database.create_project({"file_path": "/media/live.mkv"})
            selected = []
            manager = PipelineManager(
                database,
                probe=lambda _path: SimpleNamespace(to_dict=lambda: {
                    "duration_sec": 10, "width": 1920, "height": 1080, "audio_tracks": 0,
                }),
                analyze_vision=lambda *_args, **_kwargs: {"observations": [{
                    "kind": "FRAME_SIGNAL", "track_index": None, "start_sec": 4, "end_sec": 5,
                    "confidence": None, "payload": {"scd.score": 0.9},
                }]},
                analyze_precision=lambda _source, _audio, _duration, ranges, **_kwargs: (
                    selected.extend(ranges) or {"observations": [{
                        "modality": "VISION", "kind": "PRECISION_FRAME_SIGNAL", "track_index": None,
                        "start_sec": 3, "end_sec": 6, "confidence": None,
                        "payload": {"selection_reason": "vision_scene_change"},
                    }]}
                ),
            )
            manager._run(project["project_id"], {
                "vision_analysis": True, "precision_analysis": True,
                "precision_audio_window_sec": 0.2, "precision_vision_interval_sec": 0.4,
                "precision_policy": {"context_before_sec": 1, "context_after_sec": 1,
                                     "vision_scene_score_above": 0.8},
            }, True, threading.Event())
            self.assertEqual(selected, [{"start_sec": 3, "end_sec": 6, "reason": "vision_scene_change"}])
            self.assertIn("PRECISION_ANALYSIS", [step["step"] for step in database.pipeline_steps(project["project_id"])])
            self.assertIn("PRECISION_FRAME_SIGNAL", {
                item["kind"] for item in database.analysis_input(project["project_id"])["observations"]
            })
            manager.shutdown()


if __name__ == "__main__":
    unittest.main()
