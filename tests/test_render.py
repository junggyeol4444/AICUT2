import unittest

from aicut.config import CalibrationProfile
from aicut.media.audio import LoudnessStats, _parse_silences, parse_loudnorm_json
from aicut.models import SubtitleLine
from aicut.render.ffmpeg import (
    RenderSettings,
    audio_edge_filter,
    build_concat_command,
    build_final_command,
    build_segment_command,
    zoom_filter,
)
from aicut.render.subtitles import SubtitleStyleProfile, build_ass
from aicut.render.timeline import Segment


class ZoomTests(unittest.TestCase):
    """10.4-1: the documented crop expression cannot work; this is the replacement."""

    def test_crop_uses_real_ffmpeg_variables_only(self):
        expression = zoom_filter({"scale": 0.83, "center": [0.6, 0.4]}, RenderSettings())
        self.assertNotIn("face_center_x", expression)
        self.assertNotIn("face_center_y", expression)
        self.assertTrue(expression.startswith("crop=w=iw*"))

    def test_crop_window_is_clamped_inside_the_frame(self):
        expression = zoom_filter({"scale": 0.5, "center": [0.0, 1.0]}, RenderSettings())
        self.assertIn("max(", expression)      # cannot go negative
        self.assertIn("min(", expression)      # cannot pass the right/bottom edge


class SegmentCommandTests(unittest.TestCase):
    def setUp(self):
        self.settings = RenderSettings.from_profile(CalibrationProfile.load())
        self.segment = Segment(cut_index=0, sequence_order=0, source_start_sec=1800.0,
                               source_end_sec=1840.0, out_start_sec=0.0)

    def test_segment_seeks_accurately_to_the_planned_moment(self):
        cmd = build_segment_command("/src.mkv", self.segment, "/out.mp4", self.settings)
        self.assertIn("-accurate_seek", cmd)
        self.assertEqual(cmd[cmd.index("-ss") + 1], "1800.000")
        self.assertEqual(cmd[cmd.index("-t") + 1], "40.000")

    def test_joins_use_short_fades_not_crossfades(self):
        """10.4-2: acrossfade per join would explode the filter graph."""
        cmd = build_segment_command("/src.mkv", self.segment, "/out.mp4", self.settings)
        audio = cmd[cmd.index("-af") + 1]
        self.assertIn("afade=t=in", audio)
        self.assertIn("afade=t=out", audio)
        self.assertNotIn("acrossfade", " ".join(cmd))
        self.assertNotIn("acrossfade", " ".join(build_concat_command("/list.txt", "/j.mp4")))

    def test_a_segment_shorter_than_two_fades_gets_none(self):
        self.assertEqual(audio_edge_filter(0.005, self.settings), "anull")

    def test_final_pass_applies_the_measured_loudness(self):
        """10.4-3: measure first, then apply, so the level does not drift."""
        stats = LoudnessStats(input_i=-21.0, input_tp=-3.0, input_lra=8.0, input_thresh=-31.0, target_offset=0.4)
        cmd = build_final_command("/j.mp4", "/f.mp4", self.settings, ass_path="/s.ass", loudness=stats)
        audio = cmd[cmd.index("-af") + 1]
        self.assertIn("measured_I=-21.0", audio)
        self.assertIn("linear=true", audio)
        video_filter = cmd[cmd.index("-vf") + 1]
        self.assertIn("subtitles=filename='/s.ass'", video_filter)
        self.assertNotIn("subtitles='", video_filter,
                         "ffmpeg 7.2 rejects the positional form with 'No option name near'")

    def test_shorts_get_a_vertical_frame(self):
        settings = RenderSettings.from_profile(CalibrationProfile.load(), target_type="shorts")
        cmd = build_segment_command("/src.mkv", self.segment, "/out.mp4", settings)
        self.assertIn("crop=1080:1920", cmd[cmd.index("-vf") + 1])


class SubtitleTests(unittest.TestCase):
    def test_style_is_taken_from_the_profile_not_the_code(self):
        """10.3: font, size and colour are updatable data, not constants."""
        profile = SubtitleStyleProfile({
            "styles": {"default": {"fontname": "Custom Sans", "fontsize": 40}},
            "effects": {},
        })
        text = build_ass([SubtitleLine(0, 1, "hi")], profile)
        self.assertIn("Custom Sans,40", text)

    def test_emphasis_inherits_and_overrides(self):
        profile = SubtitleStyleProfile.load("default")
        emphasis = profile.resolved("emphasis")
        self.assertEqual(emphasis["fontname"], profile.resolved("default")["fontname"])
        self.assertNotEqual(emphasis["fontsize"], profile.resolved("default")["fontsize"])

    def test_timestamps_and_escaping(self):
        profile = SubtitleStyleProfile.load("default")
        text = build_ass([SubtitleLine(3661.5, 3662.005, "a {tag} line\nsecond")], profile)
        self.assertIn("1:01:01.50", text)
        self.assertIn("(tag)", text)          # braces would be read as ASS override tags
        self.assertIn("\\N", text)


class AudioParsingTests(unittest.TestCase):
    def test_silence_pairs_are_merged_across_tiny_gaps(self):
        output = "silence_start: 10.0\nsilence_end: 11.0\nsilence_start: 11.1\nsilence_end: 12.0\n"
        merged = _parse_silences(output, merge_gap=0.2)
        self.assertEqual(len(merged), 1)
        self.assertEqual((merged[0].start_sec, merged[0].end_sec), (10.0, 12.0))

    def test_loudnorm_block_is_read_from_the_tail_of_stderr(self):
        stats = parse_loudnorm_json(
            'noise {"input_i":"-23.5","input_tp":"-2.0","input_lra":"7.0",'
            '"input_thresh":"-33.0","target_offset":"0.5"} trailing'
        )
        self.assertEqual(stats.input_i, -23.5)
        self.assertEqual(stats.target_offset, 0.5)


if __name__ == "__main__":
    unittest.main()


class MultitrackAudioTests(unittest.TestCase):
    """5.2: mic / call / game / BGM arrive as separate streams."""

    def setUp(self):
        self.segment = Segment(cut_index=0, sequence_order=0, source_start_sec=10.0,
                               source_end_sec=12.0, out_start_sec=0.0)
        self.settings = RenderSettings.from_profile(CalibrationProfile.load())

    def test_one_track_keeps_the_simple_mapping(self):
        cmd = build_segment_command("/src.mkv", self.segment, "/out.mp4", self.settings)
        self.assertIn("-af", cmd)
        self.assertNotIn("-filter_complex", cmd)
        self.assertIn("0:a:0?", cmd)

    def test_every_source_track_reaches_the_mix(self):
        """Mapping 0:a:0 alone drops the rest of the broadcast, permanently."""
        cmd = build_segment_command("/src.mkv", self.segment, "/out.mp4", self.settings,
                                    audio_streams=4)
        graph = cmd[cmd.index("-filter_complex") + 1]
        for index in range(4):
            self.assertIn(f"[0:a:{index}]", graph, f"track {index} is not in the mix")
        self.assertIn("amix=inputs=4", graph)
        self.assertEqual(cmd[-5:], ["-map", "[v]", "-map", "[a]", "/out.mp4"])

    def test_the_mix_does_not_divide_the_level_by_the_track_count(self):
        """amix normalises by default; a four-track broadcast would come out quiet."""
        cmd = build_segment_command("/src.mkv", self.segment, "/out.mp4", self.settings,
                                    audio_streams=4)
        self.assertIn("normalize=0", cmd[cmd.index("-filter_complex") + 1])

    def test_video_filters_survive_the_move_into_the_complex_graph(self):
        cmd = build_segment_command("/src.mkv", self.segment, "/out.mp4", self.settings,
                                    audio_streams=2)
        graph = cmd[cmd.index("-filter_complex") + 1]
        self.assertIn("[0:v:0]", graph)
        self.assertIn("setsar=1", graph)
        self.assertIn("[v]", graph)
        self.assertNotIn("-vf", cmd, "-vf and -filter_complex cannot both be given")

    def test_the_audio_chain_still_applies_after_the_mix(self):
        cmd = build_segment_command("/src.mkv", self.segment, "/out.mp4", self.settings,
                                    audio_streams=3)
        graph = cmd[cmd.index("-filter_complex") + 1]
        self.assertIn("aresample=", graph)
        self.assertIn("afade", graph)


class PlanRenderSettingsTests(unittest.TestCase):
    """10.1 + 8.2: the renderer executes the plan, including how it renders."""

    def setUp(self):
        self.profile_settings = RenderSettings.from_profile(CalibrationProfile.load())

    def test_no_stored_settings_leaves_the_profile_alone(self):
        self.assertEqual(self.profile_settings.with_plan({}), self.profile_settings)
        self.assertEqual(self.profile_settings.with_plan(None), self.profile_settings)

    def test_the_plan_wins_over_the_current_profile(self):
        """Re-rendering after a re-calibration must not change the reviewed file."""
        applied = self.profile_settings.with_plan({"crf": 12, "height": 720, "video_codec": "libx265"})
        self.assertEqual(applied.crf, 12)
        self.assertEqual(applied.height, 720)
        self.assertEqual(applied.video_codec, "libx265")

    def test_keys_the_plan_omits_keep_the_measured_value(self):
        applied = self.profile_settings.with_plan({"crf": 12})
        self.assertEqual(applied.preset, self.profile_settings.preset)
        self.assertEqual(applied.loudness_i, self.profile_settings.loudness_i)

    def test_a_hand_edited_string_is_coerced_to_the_field_type(self):
        """8.2 says a person may amend the plan; they will not type JSON types."""
        applied = self.profile_settings.with_plan({"crf": "12", "height": "720"})
        self.assertEqual(applied.crf, 12)
        self.assertEqual(applied.height, 720)

    def test_a_string_false_does_not_read_as_true(self):
        applied = self.profile_settings.with_plan({"two_pass_loudness": "false"})
        self.assertFalse(applied.two_pass_loudness)

    def test_unknown_and_unusable_keys_are_ignored_not_fatal(self):
        applied = self.profile_settings.with_plan(
            {"from_a_future_version": "x", "crf": "not a number"})
        self.assertEqual(applied.crf, self.profile_settings.crf)

    def test_a_round_trip_through_as_dict_is_identity(self):
        self.assertEqual(
            self.profile_settings.with_plan(self.profile_settings.as_dict()),
            self.profile_settings,
        )
