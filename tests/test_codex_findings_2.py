"""The nine findings from the second Codex review, each with the failure it caused.

Every one was reproduced against running code before it was fixed; these hold
the fixes in place. Where a finding was a spec clause not being met rather than
a defect, the clause is named.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


class ConcatPathEscapingTests(unittest.TestCase):
    """A workspace path containing an apostrophe lost the whole render.

    The concat demuxer parses quotes as syntax, so `/home/O'Brien/aicut` ended
    the quoted string early. Reproduced: ffmpeg reported
    `Impossible to open '/tmp/OBrien/seg.mp4'` - the apostrophe gone from the
    path it went looking for - and it failed only after every segment had
    already been encoded.
    """

    def test_an_apostrophe_is_escaped_the_way_the_demuxer_expects(self):
        from aicut.render.ffmpeg import concat_entry

        self.assertEqual(concat_entry("/tmp/O'Brien/seg.mp4"),
                         "file '/tmp/O'\\''Brien/seg.mp4'")

    def test_an_ordinary_path_is_untouched(self):
        from aicut.render.ffmpeg import concat_entry

        self.assertEqual(concat_entry("/w/seg.mp4"), "file '/w/seg.mp4'")

    def test_ffmpeg_itself_accepts_the_escaped_manifest(self):
        """The claim is about ffmpeg's parser, so ffmpeg is what decides it."""
        import subprocess

        from aicut.media.ffmpeg_util import have_ffmpeg
        from aicut.render.ffmpeg import concat_entry

        if not have_ffmpeg():
            self.skipTest("ffmpeg is not installed")
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            odd = Path(tmp) / "O'Brien"
            odd.mkdir()
            clip = odd / "clip.mp4"
            subprocess.run(
                ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=d=0.4",
                 "-f", "lavfi", "-i", "color=c=black:s=64x64:d=0.4",
                 "-shortest", "-y", str(clip)],
                check=True,
            )
            manifest = Path(tmp) / "list.txt"
            manifest.write_text(concat_entry(clip) + "\n", encoding="utf-8")
            out = Path(tmp) / "joined.mp4"
            done = subprocess.run(
                ["ffmpeg", "-v", "error", "-f", "concat", "-safe", "0",
                 "-i", str(manifest), "-c", "copy", "-y", str(out)],
                capture_output=True, text=True,
            )
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertTrue(out.exists())


class EffectRebasingTests(unittest.TestCase):
    """A timed effect was rebased from output durations, not source offsets.

    The plan states a graphic's start and a sound effect's `at` in seconds
    within the cut, and it is written before pacing removes anything - so the
    piece's offset is its position in the source.
    """

    def _placed(self, remove, at):
        from aicut.models import Cut
        from aicut.render.ffmpeg import _effects_for_piece, _pieces_per_cut
        from aicut.render.timeline import Timeline

        cut = Cut(sequence_order=0, source_start_sec=0, source_end_sec=30,
                  audio_effect={"sfx": {"path": "/d.wav", "at": at}},
                  remove_spans=remove)
        timeline = Timeline.from_cuts([cut])
        pieces = _pieces_per_cut(timeline.segments)
        return [_effects_for_piece(cut, s, pieces) for s in timeline.segments]

    def test_the_offset_is_the_pieces_position_in_the_source(self):
        placed = self._placed([[10, 12]], 14.0)

        self.assertNotIn("sfx", placed[0][1])
        self.assertEqual(placed[1][1]["sfx"]["at"], 2.0)

    def test_a_large_removal_does_not_push_the_effect_off_its_piece(self):
        placed = self._placed([[5, 20]], 25.0)

        self.assertEqual(placed[1][1]["sfx"]["at"], 5.0)

    def test_an_effect_inside_the_removed_span_belongs_to_no_piece(self):
        """The moment it referred to is gone; putting it anywhere would move it."""
        placed = self._placed([[10, 12]], 11.0)

        for _, audio in placed:
            self.assertNotIn("sfx", audio)


class DuplicateUploadTests(unittest.TestCase):
    """A second click uploaded the same video again for another 1,600 units.

    A successful upload leaves review_status at pending or approved, and the UI
    hid the button only for uploaded and published - so a reload put it back.
    """

    def _episode(self, **metadata):
        from aicut.models import Episode

        episode = Episode(episode_id="e1", project_id="p1")
        episode.output_mp4_path = "/tmp/out.mp4"
        episode.metadata = dict(metadata)
        return episode

    def test_an_episode_with_a_video_id_is_refused(self):
        from aicut.errors import AlreadyUploaded
        from aicut.pipeline import publishing

        episode = self._episode(youtube={"video_id": "abc123"})
        with self.assertRaises(AlreadyUploaded) as caught:
            publishing.upload_episode(mock.Mock(), episode, mock.Mock())

        self.assertIn("abc123", str(caught.exception))
        self.assertIn("1,600", str(caught.exception))

    def test_the_client_is_never_called_for_a_duplicate(self):
        from aicut.errors import AlreadyUploaded
        from aicut.pipeline import publishing

        client = mock.Mock()
        with self.assertRaises(AlreadyUploaded):
            publishing.upload_episode(
                mock.Mock(), self._episode(youtube={"video_id": "abc"}), client,
            )
        client.upload.assert_not_called()

    def test_the_ui_hides_the_button_on_the_video_id_not_the_status(self):
        page = Path("aicut/ui/static/index.html").read_text(encoding="utf-8")

        self.assertIn("!e.youtube_video_id", page)
        self.assertNotIn('e.review_status !== "uploaded"', page)


class PerformanceSnapshotTests(unittest.TestCase):
    """Two runs over the same rolling window counted the same views twice."""

    def _context(self, tmp):
        from aicut.config import CalibrationProfile
        from aicut.db.store import Store
        from aicut.llm import get_producer
        from aicut.models import Episode, Project
        from aicut.pipeline.context import RunContext

        store = Store(str(Path(tmp) / "aicut.db"))
        project = store.create_project(Project(project_id="p1", file_path="/x.mkv"))
        episode = Episode(episode_id="e1", project_id="p1")
        store.save_episode(episode)
        return RunContext(project=project, store=store, profile=CalibrationProfile.load(),
                          producer=get_producer("mock"), workspace=Path(tmp))

    def test_only_the_newest_snapshot_of_an_episode_is_learned_from(self):
        from aicut.pipeline import performance

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            ctx = self._context(tmp)
            ctx.store.save_performance("e1", {"views": 100})
            ctx.store.save_performance("e1", {"views": 140})
            seen = {}

            def capture(_self, payload):
                seen.update(payload)
                return {"observations": [], "strategy_updates": []}

            with mock.patch.object(type(ctx.producer), "learn_from_performance", capture):
                performance.learn(ctx)
            ctx.store.close()

        self.assertEqual(len(seen["episodes"]), 1)
        self.assertEqual(seen["episodes"][0]["metrics"]["views"], 140)
        self.assertEqual(seen["episodes"][0]["snapshots"], 2)

    def test_the_history_is_still_kept_in_the_table(self):
        """Deduplicating the evidence must not throw the trend away."""
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            ctx = self._context(tmp)
            ctx.store.save_performance("e1", {"views": 100})
            ctx.store.save_performance("e1", {"views": 140})
            rows = ctx.store.performance("e1")
            ctx.store.close()

        self.assertEqual(len(rows), 2)


class ProfileIdentityTests(unittest.TestCase):
    """Profiles were resolved by name, and names repeat across recalibrations."""

    def test_the_project_is_read_back_under_the_row_it_was_submitted_with(self):
        from aicut.ui.server import UiServer

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            workspace = UiServer(Path(tmp))
            try:
                older = workspace.store.save_profile(
                    "chan-calibrated", "chan", {"pacing": {"keep_max_sec": 1.0}}, None, {},
                )
                newer = workspace.store.save_profile(
                    "chan-calibrated", "chan", {"pacing": {"keep_max_sec": 9.0}}, None, {},
                )
                self.assertNotEqual(older, newer)

                from aicut.models import Project

                project = workspace.store.create_project(Project(
                    project_id="p1", file_path="/x.mkv",
                    profile_name="chan-calibrated", profile_id=newer,
                ))
                profile = workspace.profile_for_project(project)
            finally:
                workspace.store.close()

        self.assertEqual(profile.get_float("pacing.keep_max_sec"), 9.0)

    def test_a_project_from_before_the_id_existed_still_opens(self):
        from aicut.models import Project
        from aicut.ui.server import UiServer

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            workspace = UiServer(Path(tmp))
            try:
                workspace.store.save_profile(
                    "old", "chan", {"pacing": {"keep_max_sec": 4.0}}, None, {},
                )
                project = workspace.store.create_project(Project(
                    project_id="p1", file_path="/x.mkv", profile_name="old",
                ))
                profile = workspace.profile_for_project(project)
            finally:
                workspace.store.close()

        self.assertEqual(profile.get_float("pacing.keep_max_sec"), 4.0)


class SpeechTrackTests(unittest.TestCase):
    """5.2 separates 내 마이크 / 통화 / 게임 / BGM and people talk on two of them.

    Reading the mic alone left a guest out of the transcript, out of
    understanding and discovery - and, because the mic is quiet while the guest
    talks, 9장 read those stretches as dead air and cut them from a video whose
    audio does contain the guest.
    """

    def _media(self, *roles):
        from aicut.media.probe import AudioTrack, MediaInfo

        return MediaInfo(path="/x.mkv", duration_sec=600.0, audio_tracks=[
            AudioTrack(index=i, channels=2, title=r.title(), role=r)
            for i, r in enumerate(roles)
        ])

    def test_the_call_track_is_read_as_well_as_the_mic(self):
        tracks = self._media("mic", "call", "game", "bgm").speech_tracks()

        self.assertEqual([t.role for t in tracks], ["mic", "call"])

    def test_game_and_bgm_are_not_transcribed(self):
        """A game character's line is not somebody in the room saying it."""
        tracks = self._media("game", "bgm").speech_tracks()

        self.assertEqual([t.role for t in tracks], [])

    def test_an_unlabelled_recording_is_not_narrowed_arbitrarily(self):
        tracks = self._media("unknown", "unknown").speech_tracks()

        self.assertEqual(len(tracks), 2)

    def test_a_silence_needs_every_speech_track_to_be_quiet(self):
        from aicut.media.audio import Silence, intersect_silences

        mic = [Silence(0, 10), Silence(20, 40)]
        call = [Silence(5, 15), Silence(25, 30), Silence(35, 50)]

        common = intersect_silences([mic, call], min_duration_sec=1.0)

        self.assertEqual([(s.start_sec, s.end_sec) for s in common],
                         [(5, 10), (25, 30), (35, 40)])

    def test_an_intersection_below_the_minimum_is_not_a_silence(self):
        from aicut.media.audio import Silence, intersect_silences

        self.assertEqual(
            intersect_silences([[Silence(0, 10)], [Silence(9, 20)]], min_duration_sec=5.0),
            [],
        )

    def test_tension_takes_the_loudest_track_at_each_moment(self):
        """5.2 lists 웃음 / 비명 / 환호 without saying which track they arrive on."""
        from aicut.media.audio import loudest_envelope

        mic = [(0.0, -40.0), (1.0, -30.0)]
        call = [(0.0, -20.0), (1.0, -35.0)]

        self.assertEqual(loudest_envelope([mic, call]), [(0.0, -20.0), (1.0, -30.0)])


class PlaylistTests(unittest.TestCase):
    """Packaging stored the playlist and the upload never acted on it."""

    def test_the_client_can_add_a_video_to_a_playlist(self):
        from aicut.intelligence.youtube import YouTubeClient

        self.assertTrue(hasattr(YouTubeClient, "add_to_playlist"))

    def test_it_is_budgeted_rather_than_spent_unnoticed(self):
        from aicut.intelligence.quota import COST_PLAYLIST_ITEM_INSERT

        self.assertEqual(COST_PLAYLIST_ITEM_INSERT, 50)

    def test_the_upload_path_reads_the_packaged_playlist(self):
        import inspect

        from aicut.pipeline import publishing

        source = inspect.getsource(publishing.upload_episode)
        self.assertIn('upload_info.get("playlist")', source)
        self.assertIn("add_to_playlist", source)


class ThumbnailChoiceTests(unittest.TestCase):
    """11.1 offers the frames 사용자에게 제시한다 and 15.5 says 사람이 고른다.

    Nothing ever passed `thumbnail_path`, so candidate 0 was always uploaded and
    looking at the others changed nothing.
    """

    def _context(self, tmp, candidates):
        from aicut.config import CalibrationProfile
        from aicut.db.store import Store
        from aicut.llm import get_producer
        from aicut.models import Episode, Project
        from aicut.pipeline.context import RunContext

        store = Store(str(Path(tmp) / "aicut.db"))
        project = store.create_project(Project(project_id="p1", file_path="/x.mkv"))
        episode = Episode(episode_id="e1", project_id="p1")
        episode.thumbnail_candidates = list(candidates)
        store.save_episode(episode)
        return RunContext(project=project, store=store, profile=CalibrationProfile.load(),
                          producer=get_producer("mock"), workspace=Path(tmp))

    def test_a_choice_is_recorded_on_the_episode(self):
        from aicut.pipeline import review

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            ctx = self._context(tmp, ["/a.png", "/b.png", "/c.png"])
            try:
                updated = review.choose_thumbnail(ctx, "e1", 2)
                stored = ctx.store.get_episode("e1")
            finally:
                ctx.store.close()

        self.assertEqual(updated.thumbnail_path, "/c.png")
        self.assertEqual(stored.thumbnail_path, "/c.png")

    def test_an_index_outside_the_candidates_is_refused(self):
        from aicut.pipeline import review

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            ctx = self._context(tmp, ["/a.png"])
            try:
                with self.assertRaises(ValueError):
                    review.choose_thumbnail(ctx, "e1", 5)
            finally:
                ctx.store.close()

    def test_the_upload_prefers_the_recorded_choice_over_the_first_frame(self):
        import inspect

        from aicut.pipeline import publishing

        source = inspect.getsource(publishing.upload_episode)
        self.assertIn("episode.thumbnail_path", source)

    def test_the_ui_sends_the_choice_when_a_candidate_is_clicked(self):
        page = Path("aicut/ui/static/index.html").read_text(encoding="utf-8")

        self.assertIn('action: "thumbnail"', page)


class FfmpegFetchAdviceTests(unittest.TestCase):
    """Every platform build has an empty checksum, and `fetch()` refuses those.

    The refusal is right - an unverified ffmpeg runs whatever the network
    returned - but doctor and the desktop launcher offered `aicut fetch-ffmpeg`
    as the way out of a missing ffmpeg, which could not succeed.
    """

    def test_no_build_here_can_be_verified_yet(self):
        from aicut.media.ffmpeg_fetch import has_recorded_checksum

        self.assertFalse(has_recorded_checksum())

    def test_doctor_does_not_offer_a_command_that_would_refuse(self):
        from aicut.cli import _ffmpeg_remedy

        remedy = _ffmpeg_remedy()
        self.assertIn("--sha256", remedy)
        self.assertIn("install ffmpeg", remedy)

    def test_an_operator_supplied_digest_is_what_the_download_is_checked_against(self):
        from aicut.media.ffmpeg_fetch import Build, FetchRefused, fetch

        build = Build(url="http://127.0.0.1:1/x.tar.gz", sha256="", members=("ffmpeg",))
        with self.assertRaises(FetchRefused) as refused:
            fetch(build=build)
        self.assertIn("--sha256", str(refused.exception))

        # With a digest the refusal is gone and the fetch proceeds to the
        # network, which is where it should fail in a test with no server.
        with self.assertRaises(Exception) as reached:
            fetch(build=build, sha256="0" * 64, timeout=1)
        self.assertNotIn("no checksum is recorded", str(reached.exception))
