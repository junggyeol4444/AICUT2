"""The fifteen findings from the third Codex review, each with the failure it caused.

Every one was checked against running code before it was fixed. Where the fix
changes what a number means, the test states the number the old code produced.
"""

import json
import tempfile
import unittest
from pathlib import Path

from aicut.models import Cut, SubtitleLine, Utterance


class SilenceAcrossTracksTests(unittest.TestCase):
    """A track with nothing to say was read as a track with nothing on it.

    `intersect_silences` dropped empty per-track lists before intersecting, so a
    guest who talks continuously - no silences at all - was skipped, and the
    host's silences became the answer. Measured before the fix: mic silences
    [(0,10),(20,40)] with an empty call track intersected to [(0,10),(20,40)],
    which is 30 seconds of the guest talking, marked as dead air for 9장 to cut.
    """

    def test_a_track_that_never_goes_quiet_leaves_no_common_silence(self):
        from aicut.media.audio import Silence, intersect_silences

        mic = [Silence(0, 10), Silence(20, 40)]
        self.assertEqual(intersect_silences([mic, []], min_duration_sec=1.0), [])

    def test_two_tracks_still_intersect(self):
        from aicut.media.audio import Silence, intersect_silences

        got = intersect_silences(
            [[Silence(0, 10), Silence(20, 40)], [Silence(5, 30)]], min_duration_sec=1.0,
        )
        self.assertEqual([(s.start_sec, s.end_sec) for s in got], [(5, 10), (20, 30)])

    def test_no_tracks_at_all_is_still_empty(self):
        from aicut.media.audio import intersect_silences

        self.assertEqual(intersect_silences([], min_duration_sec=1.0), [])


class TranscriberThatCannotReadTracksTests(unittest.TestCase):
    """One transcript, asked for once per track, became several transcripts.

    `_transcribe_tracks` calls the recogniser once per speech track, but
    `TranscriptFileTranscriber` returns the whole file whatever track is asked
    for. On the repository's own three-track fixture that turned 16 utterances
    into 32 - every line duplicated, every speaker doubled.
    """

    def test_a_file_transcriber_says_it_cannot_select_a_track(self):
        from aicut.media.stt import TranscriptFileTranscriber

        self.assertFalse(TranscriptFileTranscriber("x.json").separates_tracks)

    def test_a_recogniser_that_does_read_tracks_keeps_the_per_track_pass(self):
        from aicut.media.stt import Transcriber

        self.assertTrue(Transcriber.separates_tracks)

    def test_the_transcript_is_loaded_once_for_a_multitrack_source(self):
        from types import SimpleNamespace

        from aicut.media.stt import TranscriptFileTranscriber
        from aicut.pipeline.parsing import _transcribe_tracks

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.json"
            path.write_text(json.dumps({"segments": [
                {"start": 0.0, "end": 1.0, "text": "host line"},
                {"start": 2.0, "end": 3.0, "text": "guest line"},
            ]}), encoding="utf-8")
            ctx = SimpleNamespace(
                project=SimpleNamespace(file_path="live.mkv"), media=None, report={},
            )
            tracks = [
                SimpleNamespace(index=1, role="mic", title="mic", needs_diarization=False),
                SimpleNamespace(index=3, role="call", title="call", needs_diarization=False),
            ]
            got = _transcribe_tracks(ctx, TranscriptFileTranscriber(path), tracks)
        self.assertEqual([u.text for u in got], ["host line", "guest line"])


class SignalCacheProfileTests(unittest.TestCase):
    """`resume --profile` reported the new profile and used the old measurements.

    Silences are found at the profile's noise floor and motion at its scan
    interval. The cache was keyed on nothing but the file existing, so a
    re-tuned profile changed the run's report and not its numbers (17.1).
    """

    def test_the_cache_records_what_it_was_measured_under(self):
        from aicut.config import CalibrationProfile
        from aicut.pipeline.parsing import MEASUREMENT_KEYS, measurement_fingerprint

        got = measurement_fingerprint(CalibrationProfile.load())
        self.assertEqual(sorted(got), sorted(MEASUREMENT_KEYS))

    def test_a_changed_noise_floor_gives_a_different_fingerprint(self):
        from aicut.config import CalibrationProfile
        from aicut.pipeline.parsing import measurement_fingerprint

        before = measurement_fingerprint(CalibrationProfile.load())
        louder = CalibrationProfile.load()
        louder.params["silence"]["level_db"] = -20.0
        self.assertNotEqual(before, measurement_fingerprint(louder))

    def test_the_fingerprint_survives_a_save_and_load(self):
        from aicut.pipeline.context import SignalBundle

        with tempfile.TemporaryDirectory() as tmp:
            path = SignalBundle(measured_with={"silence.level_db": -35.0}).save(
                Path(tmp) / "signals.json"
            )
            self.assertEqual(
                SignalBundle.load(path).measured_with, {"silence.level_db": -35.0}
            )


class TimedEffectsOnTheEditModelTests(unittest.TestCase):
    """A sound effect kept the time it had before pacing removed anything.

    On a cut running 0-20s with 10-12s removed, the plan's `at: 14` is 12s into
    the clip on the timeline. The model wrote 14.0, which is two seconds late -
    and the same arithmetic the renderer had already been corrected for.
    """

    def _clip(self):
        from aicut.render.editmodel import Clip

        return Clip(clip_id="c", source_media_id="m", in_point_sec=0.0,
                    out_point_sec=20.0, timeline_position_sec=0.0,
                    remove_spans=[[10.0, 12.0]])

    def test_a_time_after_a_removal_moves_up_by_it(self):
        self.assertEqual(self._clip().timeline_offset_of(14.0), 12.0)

    def test_a_time_inside_a_removal_is_nowhere(self):
        self.assertIsNone(self._clip().timeline_offset_of(11.0))

    def test_a_time_before_the_removal_is_unchanged(self):
        self.assertEqual(self._clip().timeline_offset_of(4.0), 4.0)

    def test_a_graphic_is_clamped_to_what_survives(self):
        clip = self._clip()
        self.assertEqual(clip.timeline_offset_at_least(11.0), 10.0)
        self.assertEqual(clip.timeline_offset_at_most(20.0), 18.0)

    def test_the_model_places_the_sound_effect_where_the_renderer_does(self):
        from aicut.render.editmodel import TRACK_SFX, from_edit_plan
        from aicut.render.editplan import EditPlan

        cut = Cut(sequence_order=1, source_start_sec=0.0, source_end_sec=20.0,
                  remove_spans=[[10.0, 12.0]],
                  audio_effect={"sfx": {"path": "/x.wav", "at": 14.0}})
        plan = EditPlan(episode_id="ep", project_id="p", source_path="/live.mkv", cuts=[cut])
        model = from_edit_plan(plan, source_duration_sec=60.0)
        sfx = [t for t in model.sequences[0].tracks if t.name == TRACK_SFX][0]
        self.assertEqual([a.timeline_position_sec for a in sfx.audio], [12.0])


class PluginRecoveryTextTests(unittest.TestCase):
    """The reader told the editor to run a command that does not exist.

    Handed an edit plan instead of a Common Edit Model, it advised
    `aicut export --format edit-model`; the exporter takes edl, fcpxml and srt.
    """

    def test_it_names_a_way_that_works(self):
        import plugin.common.aicut_model as model_mod

        with self.assertRaises(model_mod.ModelError) as raised:
            model_mod.validated({"cuts": []})
        message = str(raised.exception)
        self.assertIn("/api/episodes/<id>/edit-model", message)
        self.assertNotIn("--format edit-model", message)

    def test_the_exporter_really_does_not_have_that_format(self):
        from aicut.cli import build_parser

        action = [a for a in build_parser()._subparsers._group_actions[0].choices["export"]._actions
                  if a.dest == "format"][0]
        self.assertNotIn("edit-model", action.choices)


class ReferenceFrameCapTests(unittest.TestCase):
    """A whole video's frames went into one analysis call.

    `watch` sampled at the scan interval across the entire duration: 36 minutes
    at 5s is 432 images in a single request, and a six-hour stream is 4,320.
    """

    def test_the_interval_widens_so_the_count_stays_under_the_cap(self):
        from unittest import mock

        from aicut.config import CalibrationProfile
        from aicut.intelligence import reference as reference_mod

        profile = CalibrationProfile.load()
        cap = profile.get_int("scan.reference_frame_cap")
        media = type("M", (), {"duration_sec": 3600.0})()
        with mock.patch("aicut.media.probe.probe", return_value=media), \
             mock.patch("aicut.media.vision.sample_frames", return_value=[]) as sampler:
            seen = reference_mod.watch("/ref.mp4", profile, frames_dir="/tmp/frames")
        interval = sampler.call_args.kwargs["interval_sec"]
        self.assertGreaterEqual(interval, 3600.0 / cap)
        self.assertEqual(seen["frame_interval_sec"], interval)

    def test_a_short_video_keeps_the_scan_interval(self):
        from unittest import mock

        from aicut.config import CalibrationProfile
        from aicut.intelligence import reference as reference_mod

        profile = CalibrationProfile.load()
        media = type("M", (), {"duration_sec": 60.0})()
        with mock.patch("aicut.media.probe.probe", return_value=media), \
             mock.patch("aicut.media.vision.sample_frames", return_value=[]) as sampler:
            reference_mod.watch("/ref.mp4", profile, frames_dir="/tmp/frames")
        self.assertEqual(sampler.call_args.kwargs["interval_sec"],
                         profile.get_float("scan.pass1_frame_interval_sec"))


class KnowledgeFileTests(unittest.TestCase):
    """Loop A rebuilt the knowledge file and dropped loops B and C.

    12.3 runs three loops into one file. `build_knowledge` returns a fresh
    object built from references alone, and saving it over knowledge.json wiped
    every rule the operator's own pairs had produced.
    """

    def test_the_other_loops_survive_a_reference_run(self):
        from aicut.intelligence.knowledge import ProductionKnowledge

        previous = ProductionKnowledge(
            source_output_rules=["the editor cuts every pause over 2s"],
            performance_learning=[{"observation": "openings under 8s hold"}],
        )
        rebuilt = ProductionKnowledge(sample_size=12).carry_over_learning(previous)
        self.assertEqual(rebuilt.source_output_rules, previous.source_output_rules)
        self.assertEqual(rebuilt.performance_learning, previous.performance_learning)
        self.assertEqual(rebuilt.sample_size, 12)

    def test_the_newest_learning_is_what_the_planner_sees(self):
        from aicut.intelligence.knowledge import ProductionKnowledge

        knowledge = ProductionKnowledge(
            source_output_rules=[f"rule {i}" for i in range(20)],
        )
        shown = knowledge.summary_for_planner(limit=3)["learned_from_human_edits"]
        self.assertEqual(shown, ["rule 17", "rule 18", "rule 19"])


class RepeatedPhraseAlignmentTests(unittest.TestCase):
    """A phrase the host repeats matched the one time it survived, every time.

    Each source line was compared to every output line independently, so twenty
    "네 네 네" in the broadcast all matched the single one in the finished video
    and all twenty were marked kept. 12.3 B then learned its keep ratio from
    material the editor had thrown away.
    """

    def test_one_output_line_is_claimed_once(self):
        from aicut.intelligence.source_output import align_by_transcript

        source = [
            Utterance(0, 2, "okay lets go"),
            Utterance(60, 62, "okay lets go"),
            Utterance(120, 122, "okay lets go"),
        ]
        output = [Utterance(0, 2, "okay lets go")]
        spans = align_by_transcript(source, output).spans
        self.assertEqual([s.kept for s in spans], [True, False, False])

    def test_a_moment_used_twice_is_still_counted_twice(self):
        from aicut.intelligence.source_output import align_by_transcript

        source = [Utterance(0, 4, "i finally beat the boss")]
        output = [Utterance(0, 4, "i finally beat the boss"),
                  Utterance(300, 304, "i finally beat the boss")]
        spans = align_by_transcript(source, output).spans
        self.assertEqual(spans[0].repeated, 2)


class TokenEncryptionMigrationTests(unittest.TestCase):
    """Setting the key on a workspace that already had a token did nothing.

    The cached token was valid, so the branch that writes the encrypted copy
    never ran and the plaintext file stayed in the workspace - until it expired,
    which for a refresh token is not a schedule anyone can rely on.
    """

    def test_a_valid_plaintext_token_is_migrated(self):
        import inspect

        from aicut.intelligence.youtube import load_credentials

        source = inspect.getsource(load_credentials)
        self.assertIn("from_plaintext = True", source)
        self.assertIn("elif secure and from_plaintext:", source)


class LoudnessTargetTests(unittest.TestCase):
    """The two passes of 10.4-3 aimed at different numbers.

    loudnorm's measurement carries an offset computed against the target it was
    told. The measuring pass read the profile and the correcting pass reads the
    render settings, which a stored plan can carry (8.2) - so a plan rendered
    with its own targets landed at neither.
    """

    def test_the_measurement_is_told_what_the_correction_will_ask_for(self):
        import inspect

        from aicut.render.ffmpeg import measure_loudness_with_bed

        source = inspect.getsource(measure_loudness_with_bed)
        self.assertIn(
            "targets = (settings.loudness_i, settings.loudness_tp, settings.loudness_lra)",
            source,
        )

    def test_the_targets_reach_the_filter(self):
        from unittest import mock

        from aicut.config import CalibrationProfile
        from aicut.render.ffmpeg import RenderSettings, measure_loudness_with_bed

        settings = RenderSettings.from_profile(CalibrationProfile.load())
        settings.loudness_i = -18.0
        with mock.patch("aicut.media.audio.require_ffmpeg"), \
             mock.patch("aicut.media.audio.run", return_value="") as runner, \
             mock.patch("aicut.media.audio.parse_loudnorm_json", return_value=None):
            measure_loudness_with_bed("/joined.mp4", settings, CalibrationProfile.load(), None)
        self.assertIn("loudnorm=I=-18.0", " ".join(runner.call_args[0][0]))


class ZoomPieceEffectTests(unittest.TestCase):
    """A graphic timed inside a zoom step never appeared.

    10.4-1 (a) splits a cut at its keyframes. The graphic and the sound effect
    stayed on the first piece with the times they had on the whole cut, so a
    graphic at 5s was staged inside a piece that ends at 2s.
    """

    def test_an_effect_moves_onto_the_piece_that_contains_it(self):
        from aicut.render.ffmpeg import scope_timed_effects

        visual = {"graphic": {"path": "/g.png", "start": 5.0, "end": 7.0}}
        audio = {"sfx": {"path": "/s.wav", "at": 6.0}}
        scope_timed_effects(visual, audio, offset=4.0, duration=4.0)
        self.assertEqual(visual["graphic"]["start"], 1.0)
        self.assertEqual(visual["graphic"]["end"], 3.0)
        self.assertEqual(audio["sfx"]["at"], 2.0)

    def test_a_piece_that_does_not_contain_it_does_not_get_it(self):
        from aicut.render.ffmpeg import scope_timed_effects

        visual = {"graphic": {"path": "/g.png", "start": 5.0, "end": 7.0}}
        audio = {"sfx": {"path": "/s.wav", "at": 6.0}}
        scope_timed_effects(visual, audio, offset=0.0, duration=2.0)
        self.assertNotIn("graphic", visual)
        self.assertNotIn("sfx", audio)

    def test_the_renderer_scopes_every_zoom_piece(self):
        import inspect

        from aicut.render.ffmpeg import Renderer

        source = inspect.getsource(Renderer.render)
        self.assertIn("scope_timed_effects(", source)
        self.assertIn("piece.source_start_sec - segment.source_start_sec", source)


class ChapterAlignmentTests(unittest.TestCase):
    """A cut removed whole handed its chapter mark to the next cut.

    `cut_boundaries()` only listed cuts that produced a segment, and packaging
    paired that list with the cuts in order. One fully removed cut shifted every
    later chapter to the previous cut's time (11.2).
    """

    def test_a_removed_cut_has_no_start(self):
        from aicut.render.timeline import Timeline

        cuts = [
            Cut(sequence_order=1, source_start_sec=0.0, source_end_sec=10.0),
            Cut(sequence_order=2, source_start_sec=20.0, source_end_sec=30.0,
                remove_spans=[[20.0, 30.0]]),
            Cut(sequence_order=3, source_start_sec=40.0, source_end_sec=50.0),
        ]
        starts = Timeline.from_cuts(cuts).cut_starts()
        self.assertEqual(starts, {1: 0.0, 3: 10.0})

    def test_the_markers_on_the_edit_model_skip_it_too(self):
        from aicut.render.editmodel import from_edit_plan
        from aicut.render.editplan import EditPlan

        cuts = [
            Cut(sequence_order=1, source_start_sec=0.0, source_end_sec=10.0, scene_role="core"),
            Cut(sequence_order=2, source_start_sec=20.0, source_end_sec=30.0,
                remove_spans=[[20.0, 30.0]], scene_role="dropped"),
            Cut(sequence_order=3, source_start_sec=40.0, source_end_sec=50.0, scene_role="result"),
        ]
        model = from_edit_plan(
            EditPlan(episode_id="ep", project_id="p", source_path="/live.mkv", cuts=cuts),
            source_duration_sec=60.0,
        )
        markers = model.sequences[0].markers
        self.assertEqual([(m.at_sec, m.name) for m in markers],
                         [(0.0, "core"), (10.0, "result")])


class SubtitleFieldTests(unittest.TestCase):
    """A comma in a speaker's name shifted every column after it.

    libass splits the nine fields before the caption text on commas, so
    `Dialogue: 0,start,end,default,Kim, Minsu,0,0,0,,text` made "Minsu" the
    left margin and the caption came out mispositioned or not at all.
    """

    def _dialogue(self, line):
        from aicut.render.subtitles import SubtitleStyleProfile, build_ass

        text = build_ass([line], SubtitleStyleProfile.load("default"))
        return [row for row in text.splitlines() if row.startswith("Dialogue:")][0]

    def test_the_name_column_carries_no_comma(self):
        row = self._dialogue(SubtitleLine(0.0, 2.0, "hi", speaker="Kim, Minsu"))
        fields = row[len("Dialogue: "):].split(",", 9)
        self.assertEqual(len(fields), 10)
        self.assertEqual(fields[4], "Kim; Minsu")
        self.assertEqual(fields[9], "{\\fad(60,60)}hi")

    def test_the_caption_itself_may_still_contain_commas(self):
        row = self._dialogue(SubtitleLine(0.0, 2.0, "wait, what?", speaker="host"))
        self.assertTrue(row.endswith("wait, what?"))


class QuotaReservationTests(unittest.TestCase):
    """Two processes both saw room for the last upload.

    The check and the spend were separate statements with an API call between
    them, so a second `aicut upload` - or the UI's worker beside the CLI - got
    the same yes and 11.4's daily allowance was overspent by a whole video.
    """

    def setUp(self):
        from aicut.db.store import Store
        from aicut.intelligence.quota import QuotaLedger

        self.store = Store(":memory:")
        self.ledger = QuotaLedger(self.store, daily_limit=1000)

    def test_a_reservation_that_fits_is_recorded_at_once(self):
        self.assertTrue(self.ledger.reserve(600, "videos.insert"))
        self.assertEqual(self.ledger.state().used, 600)

    def test_the_second_one_is_refused(self):
        self.assertTrue(self.ledger.reserve(600, "videos.insert"))
        self.assertFalse(self.ledger.reserve(600, "videos.insert"))
        self.assertEqual(self.ledger.state().used, 600)

    def test_the_client_books_before_it_calls(self):
        import inspect

        from aicut.intelligence.youtube import YouTubeClient

        source = inspect.getsource(YouTubeClient._require)
        self.assertIn("self.ledger.reserve(units, what)", source)
        self.assertNotIn("can_afford", source)


if __name__ == "__main__":
    unittest.main()
