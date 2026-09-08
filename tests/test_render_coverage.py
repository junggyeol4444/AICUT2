"""10장: the renderer executes the plan and decides nothing.

10.2 lists twelve things it supports. Five of them — 크롭 as its own operation,
그래픽, 효과음, BGM, 전환 — had no path from the plan to a filter, so a plan that
asked for them rendered plain and said nothing about it.

10.1 is the rule that makes each of these narrow: the renderer never chooses.
An intent the plan did not state does not happen, and one it stated in a form
this cannot execute is dropped loudly rather than approximated.
"""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from aicut.config import CalibrationProfile
from aicut.render.ffmpeg import (
    RenderSettings, build_final_command, build_segment_command, crop_filter,
    transition_filters,
)
from aicut.render.timeline import Segment


def settings():
    return RenderSettings.from_profile(CalibrationProfile.load())


def segment(duration=10.0):
    return Segment(cut_index=0, sequence_order=0, source_start_sec=100.0,
                   source_end_sec=100.0 + duration, out_start_sec=0.0)


class CropTests(unittest.TestCase):
    """10.2 크롭, separate from the 확대 of 10.4-1."""

    def test_an_aspect_crops_the_middle(self):
        chain = crop_filter({"crop": "9:16"})
        self.assertIn("crop=", chain)
        self.assertIn("(iw-ow)/2", chain)

    def test_a_box_is_taken_as_normalised_coordinates(self):
        self.assertEqual(
            crop_filter({"crop": {"x": 0.25, "y": 0.1, "w": 0.5, "h": 0.5}}),
            "crop=w=iw*0.5000:h=ih*0.5000:x=iw*0.2500:y=ih*0.1000",
        )

    def test_a_box_can_never_leave_the_frame(self):
        chain = crop_filter({"crop": {"x": 9.0, "y": -3.0, "w": 0.5, "h": 0.5}})
        self.assertEqual(chain, "crop=w=iw*0.5000:h=ih*0.5000:x=iw*0.5000:y=ih*0.0000")

    def test_no_crop_stated_is_no_crop_applied(self):
        self.assertEqual(crop_filter({}), "")
        self.assertEqual(crop_filter({"crop": None}), "")

    def test_an_unusable_value_is_dropped_not_guessed(self):
        """10.1: the renderer does not decide what was meant."""
        self.assertEqual(crop_filter({"crop": "square-ish"}), "")
        self.assertEqual(crop_filter({"crop": "0:16"}), "")

    def test_the_crop_reaches_the_segment_command(self):
        cmd = build_segment_command(
            "/x/source.mp4", segment(), "/x/out.mp4", settings(),
            visual_effect={"crop": "9:16"},
        )
        self.assertTrue(any("crop=" in part for part in cmd))


class TransitionTests(unittest.TestCase):
    """10.2 전환, expressed on the segment — see 10.4-2 for why not per join."""

    def test_a_named_transition_fades_both_edges(self):
        chains = transition_filters({"transition": "fade"}, 10.0, settings())
        self.assertEqual(len(chains), 2)
        self.assertIn("fade=t=in:st=0", chains[0])
        self.assertIn("fade=t=out", chains[1])

    def test_the_out_fade_ends_at_the_end_of_the_cut(self):
        chains = transition_filters({"transition": {"out": "fade", "sec": 2.0}}, 10.0, settings())
        self.assertEqual(chains, ["fade=t=out:st=8.000:d=2.000"])

    def test_a_transition_never_eats_more_than_half_the_cut(self):
        chains = transition_filters({"transition": {"in": "fade", "sec": 99.0}}, 4.0, settings())
        self.assertEqual(chains, ["fade=t=in:st=0:d=2.000"])

    def test_a_hard_cut_is_a_transition_and_adds_nothing(self):
        self.assertEqual(transition_filters({"transition": "cut"}, 10.0, settings()), [])

    def test_an_unimplemented_transition_is_a_hard_cut_not_a_substitute(self):
        """10.1: rendering a wipe as a dissolve would be the renderer deciding."""
        self.assertEqual(
            transition_filters({"transition": "star-wipe"}, 10.0, settings()), [],
        )

    def test_the_default_length_comes_from_the_profile(self):
        chains = transition_filters({"transition": "fade"}, 10.0, settings())
        self.assertIn(f"{settings().transition_sec:.3f}", chains[0])


class GraphicTests(unittest.TestCase):
    """10.2 그래픽."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.png = Path(self.tmp.name) / "meme.png"
        self.png.write_bytes(b"\x89PNG\r\n\x1a\n")

    def test_an_overlay_becomes_a_second_input(self):
        cmd = build_segment_command(
            "/x/source.mp4", segment(), "/x/out.mp4", settings(),
            visual_effect={"graphic": str(self.png)},
        )
        self.assertEqual(cmd.count("-i"), 2)
        graph = cmd[cmd.index("-filter_complex") + 1]
        self.assertIn("overlay=", graph)

    def test_it_is_shown_only_for_the_span_the_plan_gave(self):
        cmd = build_segment_command(
            "/x/source.mp4", segment(), "/x/out.mp4", settings(),
            visual_effect={"graphic": {"path": str(self.png), "start": 2.0, "end": 5.0}},
        )
        graph = cmd[cmd.index("-filter_complex") + 1]
        self.assertIn("between(t,2.000,5.000)", graph)

    def test_a_missing_file_costs_the_overlay_not_the_cut(self):
        cmd = build_segment_command(
            "/x/source.mp4", segment(), "/x/out.mp4", settings(),
            visual_effect={"graphic": "/nope/missing.png"},
        )
        self.assertEqual(cmd.count("-i"), 1)
        self.assertIn("-vf", cmd)


class SoundEffectTests(unittest.TestCase):
    """10.2 효과음."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.wav = Path(self.tmp.name) / "ding.wav"
        self.wav.write_bytes(b"RIFF")

    def test_it_is_mixed_in_at_the_second_the_plan_named(self):
        cmd = build_segment_command(
            "/x/source.mp4", segment(), "/x/out.mp4", settings(),
            audio_effect={"sfx": {"path": str(self.wav), "at": 1.5}},
        )
        graph = cmd[cmd.index("-filter_complex") + 1]
        self.assertIn("adelay=1500:all=1", graph)
        self.assertIn("amix=inputs=2", graph)

    def test_it_cannot_lengthen_the_cut_it_decorates(self):
        cmd = build_segment_command(
            "/x/source.mp4", segment(), "/x/out.mp4", settings(),
            audio_effect={"sfx": str(self.wav)},
        )
        self.assertIn("duration=first", cmd[cmd.index("-filter_complex") + 1])

    def test_a_missing_file_is_skipped(self):
        cmd = build_segment_command(
            "/x/source.mp4", segment(), "/x/out.mp4", settings(),
            audio_effect={"sfx": "/nope/ding.wav"},
        )
        self.assertEqual(cmd.count("-i"), 1)

    def test_a_graphic_and_an_effect_together_get_one_input_each(self):
        png = Path(self.tmp.name) / "g.png"
        png.write_bytes(b"\x89PNG")
        cmd = build_segment_command(
            "/x/source.mp4", segment(), "/x/out.mp4", settings(),
            visual_effect={"graphic": str(png)}, audio_effect={"sfx": str(self.wav)},
        )
        self.assertEqual(cmd.count("-i"), 3)
        graph = cmd[cmd.index("-filter_complex") + 1]
        self.assertIn("overlay=", graph)
        self.assertIn("adelay=", graph)


class BgmTests(unittest.TestCase):
    """10.2 BGM, laid under the whole timeline rather than per cut."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.track = Path(self.tmp.name) / "bed.mp3"
        self.track.write_bytes(b"ID3")

    def test_the_bed_becomes_a_second_input_and_is_mixed_under(self):
        cmd = build_final_command(
            "/x/joined.mp4", "/x/out.mp4", settings(), bgm=str(self.track),
        )
        self.assertEqual(cmd.count("-i"), 2)
        graph = cmd[cmd.index("-filter_complex") + 1]
        self.assertIn("amix=inputs=2", graph)
        self.assertIn("volume=-18.0dB", graph)

    def test_the_loudness_correction_is_applied_after_the_mix(self):
        """10.4-3 measures the timeline a viewer hears, bed included."""
        graph = build_final_command(
            "/x/joined.mp4", "/x/out.mp4", settings(), bgm=str(self.track),
        )[build_final_command(
            "/x/joined.mp4", "/x/out.mp4", settings(), bgm=str(self.track),
        ).index("-filter_complex") + 1]
        mix = graph.index("amix=inputs=2")
        self.assertGreater(graph.index("loudnorm"), mix)

    def test_it_loops_under_a_longer_timeline_but_stops_with_the_video(self):
        cmd = build_final_command(
            "/x/joined.mp4", "/x/out.mp4", settings(), bgm=str(self.track),
        )
        self.assertIn("-stream_loop", cmd)
        self.assertIn("duration=first", cmd[cmd.index("-filter_complex") + 1])

    def test_no_bgm_leaves_the_audio_chain_exactly_as_it_was(self):
        cmd = build_final_command("/x/joined.mp4", "/x/out.mp4", settings())
        self.assertNotIn("-filter_complex", cmd)
        self.assertIn("-af", cmd)

    def test_a_missing_track_renders_without_a_bed(self):
        cmd = build_final_command(
            "/x/joined.mp4", "/x/out.mp4", settings(), bgm="/nope/bed.mp3",
        )
        self.assertNotIn("-filter_complex", cmd)


# These run the filter strings through ffmpeg itself. They need no ffprobe,
# so they are gated on the encoder alone rather than on have_ffmpeg().
@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is not installed")
class FiltersActuallyRunTests(unittest.TestCase):
    """A filter string that ffmpeg rejects is a render that dies at the end."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.source = self.dir / "src.mp4"
        subprocess.run([
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=10:duration=6",
            "-f", "lavfi", "-i", "sine=frequency=300:duration=6",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
            str(self.source),
        ], check=True, capture_output=True)

    def _run(self, **effects):
        out = self.dir / "seg.mp4"
        cmd = build_segment_command(
            str(self.source), Segment(cut_index=0, sequence_order=0, source_start_sec=1.0,
                    source_end_sec=4.0, out_start_sec=0.0),
            str(out), settings(), **effects,
        )
        result = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr[-700:])
        self.assertTrue(out.exists() and out.stat().st_size > 0)

    def test_a_cropped_segment_encodes(self):
        self._run(visual_effect={"crop": "9:16"})

    def test_a_faded_segment_encodes(self):
        self._run(visual_effect={"transition": "fade"})

    def test_a_crop_and_a_transition_together_encode(self):
        self._run(visual_effect={"crop": "1:1", "transition": {"in": "fade", "sec": 0.5}})

    def test_a_sound_effect_encodes(self):
        sfx = self.dir / "ding.wav"
        subprocess.run([
            "ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
            "-i", "sine=frequency=900:duration=0.4", str(sfx),
        ], check=True, capture_output=True)
        self._run(audio_effect={"sfx": {"path": str(sfx), "at": 1.0, "gain_db": -6}})


if __name__ == "__main__":
    unittest.main()


class ZoomSegmentPiecesTests(unittest.TestCase):
    """10.4-1 (a): 줌 구간을 세그먼트로 분리하고 세그먼트별 고정 crop 적용 후 concat.

    Before this, keyframes were read only by the sendcmd path. Under
    ``segment_crop`` a plan carrying a moving zoom rendered as one static crop
    and nothing said the movement had been dropped.
    """

    def _segment(self, start=10.0, end=16.0):
        from aicut.render.timeline import Segment

        return Segment(cut_index=0, sequence_order=0, source_start_sec=start,
                       source_end_sec=end, out_start_sec=0.0)

    def test_each_keyframe_becomes_a_piece_holding_its_own_framing(self):
        from aicut.render.ffmpeg import zoom_pieces

        pieces = zoom_pieces(self._segment(), [
            {"at_sec": 0.0, "scale": 1.0, "center": [0.2, 0.5]},
            {"at_sec": 2.0, "scale": 0.75, "center": [0.5, 0.5]},
            {"at_sec": 4.0, "scale": 0.5, "center": [0.8, 0.5]},
        ])

        self.assertEqual(
            [(p.source_start_sec, p.source_end_sec) for p, _ in pieces],
            [(10.0, 12.0), (12.0, 14.0), (14.0, 16.0)],
        )
        self.assertEqual([f["scale"] for _, f in pieces], [1.0, 0.75, 0.5])

    def test_the_pieces_cover_the_segment_with_no_gap_and_nothing_added(self):
        """A dropped head or an overrun would change the cut the plan asked for."""
        from aicut.render.ffmpeg import zoom_pieces

        segment = self._segment()
        pieces = [p for p, _ in zoom_pieces(segment, [
            {"at_sec": 1.5, "scale": 1.0}, {"at_sec": 3.0, "scale": 0.8},
        ])]

        self.assertEqual(pieces[0].source_start_sec, segment.source_start_sec)
        self.assertEqual(pieces[-1].source_end_sec, segment.source_end_sec)
        for earlier, later in zip(pieces, pieces[1:]):
            self.assertEqual(earlier.source_end_sec, later.source_start_sec)
        self.assertAlmostEqual(sum(p.duration for p in pieces), segment.duration)

    def test_one_keyframe_is_not_a_camera_move(self):
        from aicut.render.ffmpeg import zoom_pieces

        self.assertEqual(zoom_pieces(self._segment(), [{"at_sec": 0.0, "scale": 0.8}]), [])
        self.assertEqual(zoom_pieces(self._segment(), []), [])

    def test_keyframes_closer_than_the_minimum_do_not_become_pieces(self):
        """A quarter-second piece costs a seek and a join and reads as a glitch."""
        from aicut.config import CalibrationProfile
        from aicut.render.ffmpeg import zoom_pieces

        # 17.1: how short a framing step may be is a judgement about camera
        # work, so it comes from the profile, not from a constant in the file.
        minimum = CalibrationProfile.load().get_float("render.zoom.min_piece_sec")
        pieces = zoom_pieces(self._segment(), [
            {"at_sec": 0.0, "scale": 1.0},
            {"at_sec": 0.05, "scale": 0.9},
            {"at_sec": 3.0, "scale": 0.5},
        ], min_piece_sec=minimum)

        self.assertEqual(len(pieces), 2)
        for piece, _ in pieces:
            self.assertGreaterEqual(piece.duration, minimum)

    def test_a_keyframe_past_the_segment_end_cannot_lengthen_it(self):
        from aicut.render.ffmpeg import zoom_pieces

        segment = self._segment(10.0, 16.0)
        pieces = [p for p, _ in zoom_pieces(segment, [
            {"at_sec": 0.0, "scale": 1.0}, {"at_sec": 2.0, "scale": 0.7},
            {"at_sec": 99.0, "scale": 0.4},
        ])]

        self.assertEqual(pieces[-1].source_end_sec, 16.0)
        self.assertTrue(all(p.source_end_sec <= 16.0 for p in pieces))


class SubtitleFontChecksTests(unittest.TestCase):
    """20.2 makes 자막 폰트의 임베딩·상업 사용 허용 여부 확인 a pre-start item.

    Two separate things could go wrong and neither was visible: a style profile
    that records no licence for the font it names, and a font the machine does
    not have. libass substitutes a missing font with no warning and exit code 0,
    so the second is only discoverable by watching the finished video.
    """

    def _profile(self, data):
        from aicut.render.subtitles import SubtitleStyleProfile

        return SubtitleStyleProfile(data)

    def test_libass_substitutes_a_missing_font_without_saying_so(self):
        """The claim behind the check, made by ffmpeg rather than asserted."""
        import subprocess

        from aicut.media.ffmpeg_util import have_ffmpeg, has_filter

        if not have_ffmpeg() or not has_filter("subtitles"):
            self.skipTest("this ffmpeg cannot burn subtitles")
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            frames = []
            for font in ("Nonexistent Font AAA", "Nonexistent Font ZZZ"):
                ass = Path(tmp) / f"{font}.ass"
                ass.write_text(
                    "[Script Info]\nScriptType: v4.00+\nPlayResX: 320\nPlayResY: 180\n\n"
                    "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour,"
                    " Bold, Alignment, MarginV, Encoding\n"
                    f"Style: Default,{font},36,&H00FFFFFF,0,2,20,1\n\n"
                    "[Events]\nFormat: Layer, Start, End, Style, Text\n"
                    "Dialogue: 0,0:00:00.00,0:00:01.00,Default,,test\n",
                    encoding="utf-8",
                )
                out = Path(tmp) / f"{font}.png"
                done = subprocess.run(
                    ["ffmpeg", "-v", "error", "-f", "lavfi",
                     "-i", "color=c=black:s=320x180:d=1",
                     "-vf", f"subtitles={ass}", "-frames:v", "1", "-y", str(out)],
                    capture_output=True, text=True,
                )
                self.assertEqual(done.returncode, 0, done.stderr)
                # Not a word about the font it could not find.
                self.assertNotIn("not found", done.stderr.lower())
                frames.append(out.read_bytes())

        self.assertEqual(frames[0], frames[1],
                         "two different missing fonts rendered differently")

    def test_a_profile_with_no_recorded_licence_is_reported(self):
        problems = self._profile({
            "styles": {"default": {"fontname": "Some Display Font"}},
        }).licence_problems()

        self.assertTrue(any("font_licence" in p for p in problems), problems)

    def test_the_shipped_profile_records_its_licence(self):
        from aicut.render.subtitles import SubtitleStyleProfile

        profile = SubtitleStyleProfile.load("default")

        self.assertIn("Open Font License", profile.data["_meta"]["font_licence"])
        self.assertNotIn(
            "font_licence",
            " ".join(p for p in profile.licence_problems() if "font_licence" in p),
        )

    def test_every_font_the_styles_name_is_checked_not_only_the_default(self):
        profile = self._profile({
            "_meta": {"font_licence": "SIL OFL 1.1"},
            "styles": {
                "default": {"fontname": "Font A"},
                "emphasis": {"fontname": "Font B"},
                "quiet": {"inherits": "default"},
            },
        })

        self.assertEqual(profile.fonts, ["Font A", "Font B"])

    def test_a_machine_without_fontconfig_is_not_told_a_font_is_missing(self):
        """None means the question could not be asked. Reporting it as an
        answer would send the operator installing what they may already have."""
        from unittest import mock

        from aicut.render import subtitles

        with mock.patch.object(subtitles, "font_installed", return_value=None):
            problems = self._profile({
                "_meta": {"font_licence": "SIL OFL 1.1"},
                "styles": {"default": {"fontname": "Font A"}},
            }).licence_problems()

        self.assertEqual(problems, [])

    def test_fc_match_substituting_is_read_as_absent(self):
        from aicut.render.subtitles import font_installed

        import shutil

        if not shutil.which("fc-match"):
            self.skipTest("fontconfig is not installed here")
        self.assertIs(font_installed("Definitely Not A Font 12345"), False)
