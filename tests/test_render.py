import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from backend.render import (
    LoudnessMeasurement, LoudnessTarget, RenderError, RenderPlan,
    build_filter_graph, build_measurement_command, build_render_command,
    export_plan, parse_loudness_measurement, render,
)


class RenderPlanTest(unittest.TestCase):
    def setUp(self):
        self.plan = RenderPlan(
            input_path="/media/source.mkv", output_path="/output/episode.mp4",
            cuts=(
                {"sequence_order": 1, "source_start_sec": 300, "source_end_sec": 310, "pacing_mode": "KEEP", "visual_effect": {"type": "zoom", "ratio": 1.1}},
                {"sequence_order": 2, "source_start_sec": 20, "source_end_sec": 29, "pacing_mode": "TRIM", "visual_effect": {}},
                {"sequence_order": 3, "source_start_sec": 100, "source_end_sec": 105, "pacing_mode": "CUT", "visual_effect": {}},
            ),
        )

    def test_builds_non_linear_concat_with_per_cut_fades(self):
        graph, video, audio = build_filter_graph(self.plan)
        self.assertIn("trim=start=300.000:end=310.000", graph)
        self.assertIn("trim=start=20.000:end=29.000", graph)
        self.assertNotIn("trim=start=100.000:end=105.000", graph)
        self.assertIn("concat=n=2:v=1:a=1", graph)
        self.assertIn("afade=t=in", graph)
        self.assertNotIn("acrossfade", graph)
        self.assertEqual((video, audio), ("[vout]", "[aout]"))

    def test_command_uses_argument_list_and_youtube_compatible_codecs(self):
        measurement = LoudnessMeasurement(-20.1, -3.2, 5.4, -30.0, 0.1)
        command = build_render_command(self.plan, target=LoudnessTarget(), measurement=measurement)
        self.assertEqual(command[0], "ffmpeg")
        self.assertIn("libx264", command)
        self.assertIn("aac", command)
        self.assertEqual(command[-1], "/output/episode.mp4")
        self.assertNotIn("shell=True", command)
        self.assertIn("measured_I=-20.1", command[command.index("-filter_complex") + 1])

    def test_ass_subtitles_are_burned_after_non_linear_concat(self):
        with tempfile.TemporaryDirectory() as directory:
            subtitle = f"{directory}/episode.ass"
            with open(subtitle, "w", encoding="utf-8") as target:
                target.write("[Script Info]\n")
            plan = RenderPlan(self.plan.input_path, self.plan.output_path, self.plan.cuts,
                              subtitle_path=subtitle)
            graph, video, _audio = build_filter_graph(plan)
        self.assertIn("concat=n=2:v=1:a=1[vconcat][aout]", graph)
        self.assertIn("[vconcat]subtitles=filename=", graph)
        self.assertEqual(video, "[vout]")

    def test_two_pass_loudness_measurement_is_configurable(self):
        target = LoudnessTarget(integrated_lufs=-16, true_peak_db=-1.5, loudness_range=9)
        command = build_measurement_command(self.plan, target)
        graph = command[command.index("-filter_complex") + 1]
        self.assertIn("loudnorm=I=-16:TP=-1.5:LRA=9", graph)
        self.assertEqual(command[-3:], ["-f", "null", "-"])

    def test_parses_ffmpeg_loudness_json(self):
        stderr = '''frame=10\n{\n "input_i" : "-20.10", "input_tp" : "-3.20",
          "input_lra" : "5.40", "input_thresh" : "-30.00", "target_offset" : "0.10"\n}'''
        measured = parse_loudness_measurement(stderr)
        self.assertEqual(measured, LoudnessMeasurement(-20.1, -3.2, 5.4, -30.0, 0.1))

    def test_export_is_human_reviewable_json(self):
        exported = json.loads(export_plan(self.plan))
        self.assertEqual(exported["input_path"], "/media/source.mkv")
        self.assertIn("filter_graph", exported)
        self.assertTrue(exported["final_command_requires_measurement"])

    def test_rejects_empty_and_reversed_cuts(self):
        with self.assertRaises(RenderError):
            build_filter_graph(RenderPlan("in", "out", ()))
        with self.assertRaises(RenderError):
            build_filter_graph(RenderPlan("in", "out", ({"source_start_sec": 5, "source_end_sec": 2, "pacing_mode": "KEEP"},)))

    def test_render_executes_measurement_before_final_encode(self):
        measurement_json = json.dumps({
            "input_i": "-20.1", "input_tp": "-3.2", "input_lra": "5.4",
            "input_thresh": "-30.0", "target_offset": "0.1",
        })
        calls = []
        def runner(command, **kwargs):
            calls.append(command)
            return SimpleNamespace(returncode=0, stderr=measurement_json if len(calls) == 1 else "")
        with tempfile.TemporaryDirectory() as directory, patch("backend.render.shutil.which", return_value="/usr/bin/ffmpeg"):
            plan = RenderPlan(self.plan.input_path, f"{directory}/episode.mp4", self.plan.cuts)
            result = render(plan, runner=runner)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][-3:], ["-f", "null", "-"])
        self.assertIn("measured_I=-20.1", calls[1][calls[1].index("-filter_complex") + 1])
        self.assertEqual(result["cut_count"], 2)
