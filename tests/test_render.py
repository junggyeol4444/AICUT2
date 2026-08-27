import json
import unittest

from backend.render import RenderError, RenderPlan, build_filter_graph, build_render_command, export_plan


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
        command = build_render_command(self.plan)
        self.assertEqual(command[0], "ffmpeg")
        self.assertIn("libx264", command)
        self.assertIn("aac", command)
        self.assertEqual(command[-1], "/output/episode.mp4")
        self.assertNotIn("shell=True", command)

    def test_export_is_human_reviewable_json(self):
        exported = json.loads(export_plan(self.plan))
        self.assertEqual(exported["input_path"], "/media/source.mkv")
        self.assertIn("filter_graph", exported)

    def test_rejects_empty_and_reversed_cuts(self):
        with self.assertRaises(RenderError):
            build_filter_graph(RenderPlan("in", "out", ()))
        with self.assertRaises(RenderError):
            build_filter_graph(RenderPlan("in", "out", ({"source_start_sec": 5, "source_end_sec": 2, "pacing_mode": "KEEP"},)))
