"""End-to-end pipeline tests, run offline against the synthetic broadcast."""

import tempfile
import unittest
from pathlib import Path

from aicut.config import CalibrationProfile
from aicut.db.store import Store
from aicut.llm import get_producer
from aicut.llm.mock import MockProducer
from aicut.models import Decision, PacingMode
from aicut.pipeline.context import RunContext, SignalBundle
from aicut.pipeline.runner import Pipeline
from aicut.pipeline.states import State, can_transition
from aicut.render.editplan import EditPlan
from aicut.render.timeline import Timeline
from tests import fixtures


class PipelineHarness(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.workspace = Path(self._tmp.name)
        self.store = Store(self.workspace / "aicut.db")
        self.profile = CalibrationProfile.load()
        self.pipeline = Pipeline(
            self.store, self.profile, get_producer("mock"), workspace=self.workspace
        )

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def seed(self, *, producer=None, utterances=None, length_hint=None) -> RunContext:
        """A project whose measurement stage is already done (no ffmpeg needed)."""
        project = self.pipeline.submit("/fixture/stream.mkv", length_hint_sec=length_hint)
        self.store.replace_utterances(
            project.project_id, fixtures.utterances() if utterances is None else utterances
        )
        ctx = RunContext(
            project=project,
            store=self.store,
            profile=self.profile,
            producer=producer or self.pipeline.producer,
            workspace=self.workspace,
            media=fixtures.media(),
            signals=SignalBundle(
                tension=fixtures.tension(),
                motion=fixtures.motion(),
                silences=fixtures.silences(),
                speaker_reliability=1.0,
            ),
        )
        ctx.signals.save(ctx.signal_cache_path)
        return ctx


class StateMachineTests(unittest.TestCase):
    def test_review_is_the_only_route_to_published(self):
        """11.3: the gate is structural, not a convention."""
        for state in State:
            if state is State.REVIEW_PENDING or state is State.RETRY_QUEUED:
                continue
            self.assertFalse(can_transition(state, State.PUBLISHED), f"{state} reached PUBLISHED directly")

    def test_no_content_is_terminal_and_not_a_failure(self):
        self.assertFalse(can_transition(State.NO_CONTENT, State.FAILED))
        self.assertTrue(can_transition(State.DISCOVERING, State.NO_CONTENT))
        self.assertTrue(can_transition(State.EVALUATING, State.NO_CONTENT))

    def test_a_failure_can_resume_at_the_render_stage(self):
        """16장: a failed render must not cost the edit plan."""
        self.assertTrue(can_transition(State.FAILED, State.RENDERING))


class RunTests(PipelineHarness):
    def test_run_to_planning_produces_readable_edit_plans(self):
        ctx = self.seed()
        result = self.pipeline.run(ctx.project, context=ctx, render=False)

        self.assertIs(result.final_state, State.PLANNING)
        self.assertTrue(result.episodes)
        plans = list((self.workspace / ctx.project.project_id / "plans").glob("*.json"))
        self.assertEqual(len(plans), len(result.episodes))

        plan = EditPlan.load(plans[0])
        # submit() resolves the source, so the recorded path is absolute and
        # platform-shaped - D:\fixture\stream.mkv on Windows.
        self.assertTrue(Path(plan.source_path).is_absolute())
        self.assertEqual(Path(plan.source_path).name, "stream.mkv")
        self.assertTrue(plan.cuts)
        self.assertEqual(
            [c.sequence_order for c in plan.cuts], list(range(len(plan.cuts))),
            "cut order must be dense and explicit",
        )

    def test_the_whole_broadcast_is_covered_by_the_first_pass(self):
        """5.1: no second of the source may be skipped."""
        ctx = self.seed()
        self.pipeline.run(ctx.project, context=ctx, render=False)
        windows = self.store.windows(ctx.project.project_id)
        self.assertTrue(windows)
        self.assertAlmostEqual(windows[0].start_sec, 0.0)
        self.assertAlmostEqual(windows[-1].end_sec, fixtures.DURATION)
        for earlier, later in zip(windows, windows[1:]):
            self.assertAlmostEqual(earlier.end_sec, later.start_sec, msg="a gap opened between windows")

    def test_events_link_moments_that_sit_far_apart(self):
        """5.4: the boss story is mentioned at 00:30 and paid off at 30:00."""
        ctx = self.seed()
        self.pipeline.run(ctx.project, context=ctx, render=False)
        events = self.store.events(ctx.project.project_id)
        self.assertTrue(events)
        spans = [event.span() for event in events]
        self.assertTrue(
            any(end - start > 1500 for start, end in spans),
            f"no event spans a long stretch of the broadcast: {spans}",
        )

    def test_the_report_records_refusals_and_provisional_parameters(self):
        ctx = self.seed()
        result = self.pipeline.run(ctx.project, context=ctx, render=False)
        report = result.report
        self.assertIn("decisions", report)
        self.assertIn("provisional_parameters_used", report)
        self.assertTrue(report["provisional_parameters_used"], "unmeasured values must be declared (17.5)")
        self.assertIn("17.5", report["warning"])
        self.assertTrue((self.workspace / ctx.project.project_id / "report.json").exists())

    def test_an_empty_broadcast_ends_in_no_content_not_failure(self):
        """1.3 / 16장: producing nothing is a correct answer."""
        ctx = self.seed(utterances=[])
        result = self.pipeline.run(ctx.project, context=ctx, render=False)
        self.assertIs(result.final_state, State.NO_CONTENT)
        self.assertTrue(result.produced_nothing)
        self.assertTrue(result.report["no_content_reason"])
        self.assertEqual(self.store.get_project(ctx.project.project_id).status, "NO_CONTENT")

    def test_a_producer_that_rejects_everything_yields_no_episodes(self):
        class Refuser(MockProducer):
            name = "refuser"

            def _task_evaluate_candidates(self, payload):
                return [
                    {"candidate_id": c["candidate_id"], "decision": "reject",
                     "reason": "not enough happens here to carry a video", "combine_with": []}
                    for c in payload.get("candidates", [])
                ]

        ctx = self.seed(producer=Refuser())
        result = self.pipeline.run(ctx.project, context=ctx, render=False)
        self.assertIs(result.final_state, State.NO_CONTENT)
        self.assertTrue(result.report["rejections"])
        self.assertIn("not enough happens", result.report["rejections"][0]["reason"])

    def test_pacing_decisions_reach_the_plan_with_their_reasons(self):
        ctx = self.seed()
        result = self.pipeline.run(ctx.project, context=ctx, render=False)
        cuts = [c for e in result.episodes for c in e.timeline]
        self.assertTrue(cuts)
        for cut in cuts:
            self.assertIsInstance(cut.pacing_mode, PacingMode)
            self.assertTrue(cut.pacing_reason)
            for start, end in cut.remove_spans:
                self.assertGreaterEqual(start, cut.source_start_sec)
                self.assertLessEqual(end, cut.source_end_sec)

    def test_subtitles_land_on_the_output_clock(self):
        ctx = self.seed()
        result = self.pipeline.run(ctx.project, context=ctx, render=False)
        episode = next(e for e in result.episodes if e.subtitles)
        timeline = Timeline.from_cuts(episode.timeline)
        for line in episode.subtitles:
            self.assertGreaterEqual(line.start_sec, 0.0)
            self.assertLessEqual(line.end_sec, timeline.duration + 0.01)

    def test_length_hint_is_a_hint_and_deviation_is_explained(self):
        """2.6: the slider never constrains the edit, but the report says so."""
        ctx = self.seed(length_hint=45.0)
        result = self.pipeline.run(ctx.project, context=ctx, render=False)
        deviations = result.report.get("length_deviations", [])
        if deviations:
            self.assertTrue(deviations[0]["reason"])
        else:
            for episode in result.episodes:
                self.assertLess(abs(episode.planned_duration_sec - 45.0) / 45.0, 0.25)

    def test_state_log_records_every_transition(self):
        ctx = self.seed()
        self.pipeline.run(ctx.project, context=ctx, render=False)
        states = [row["state"] for row in self.store.state_log(ctx.project.project_id)]
        self.assertEqual(
            states[:5],
            [State.PARSING.value, State.UNDERSTANDING.value, State.DISCOVERING.value,
             State.EVALUATING.value, State.PLANNING.value],
        )


class ReviewGateTests(PipelineHarness):
    def test_publishing_refuses_an_unapproved_episode(self):
        from aicut.pipeline import publishing, review

        ctx = self.seed()
        result = self.pipeline.run(ctx.project, context=ctx, render=False)
        episode = result.episodes[0]
        episode.output_mp4_path = "/fake/out.mp4"
        episode.metadata = {"youtube": {"video_id": "abc123"}}
        self.store.save_episode(episode)

        with self.assertRaises(PermissionError):
            publishing.publish_approved(ctx, episode, client=None)

        review.approve(ctx, episode.episode_id, reviewer="tester", note="fine")
        approved = self.store.get_episode(episode.episode_id)
        self.assertEqual(approved.review_status, "approved")
        self.assertFalse(approved.metadata["review"]["auto"])

    def test_human_verdicts_on_candidates_are_kept_for_learning(self):
        from aicut.pipeline import review

        ctx = self.seed()
        self.pipeline.run(ctx.project, context=ctx, render=False)
        candidates = self.store.candidates(ctx.project.project_id)
        produced = [c for c in candidates if c.decision is Decision.PRODUCE]
        self.assertTrue(produced)

        review.record_candidate_verdict(ctx, produced[0].candidate_id, "disagree", "I'd never cut this")
        rate = review.agreement_rate(ctx)
        self.assertEqual(rate["reviewed"], 1)
        self.assertEqual(rate["agreement"], 0.0)
        self.assertEqual(rate["false_positive_rate"], 1.0)


if __name__ == "__main__":
    unittest.main()


class ImplausiblePlanTests(unittest.TestCase):
    """An episode cannot run longer than the broadcast it was cut from.

    The length check that existed compared against the operator's hint, so a
    run given no hint had nothing checking it at all - which is how a six-hour
    source produced a 3,830,063 second episode and reached the render stage
    without a word about it.
    """

    def test_a_plan_longer_than_its_source_is_reported(self):
        from aicut.config import CalibrationProfile
        from aicut.db.store import Store
        from aicut.llm import get_producer
        from aicut.models import Cut, Episode, Project
        from aicut.pipeline import planning
        from aicut.pipeline.context import RunContext

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            store = Store(Path(tmp) / "db.sqlite")
            try:
                project = store.create_project(Project(file_path="broadcast.mkv", duration_sec=3600.0))
                ctx = RunContext(
                    project=project, store=store, profile=CalibrationProfile.load(),
                    producer=get_producer("mock"), workspace=Path(tmp) / "ws",
                )
                episode = Episode(project_id=project.project_id, timeline=[Cut(0, 0.0, 3000.0)])
                episode.planned_duration_sec = 90000.0        # 25 hours from a 1 hour source

                planning._note_implausible_plan(ctx, episode)

                reported = ctx.report.get("implausible_plans", [])
                self.assertEqual(len(reported), 1, "an impossible plan went unreported")
                self.assertEqual(reported[0]["source_sec"], 3600.0)
                self.assertIn("cannot outrun its own broadcast", episode.notes)
            finally:
                store.close()

    def test_a_plan_that_fits_its_source_is_not_flagged(self):
        from aicut.config import CalibrationProfile
        from aicut.db.store import Store
        from aicut.llm import get_producer
        from aicut.models import Cut, Episode, Project
        from aicut.pipeline import planning
        from aicut.pipeline.context import RunContext

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            store = Store(Path(tmp) / "db.sqlite")
            try:
                project = store.create_project(Project(file_path="b.mkv", duration_sec=3600.0))
                ctx = RunContext(
                    project=project, store=store, profile=CalibrationProfile.load(),
                    producer=get_producer("mock"), workspace=Path(tmp) / "ws",
                )
                episode = Episode(project_id=project.project_id, timeline=[Cut(0, 0.0, 600.0)])
                episode.planned_duration_sec = 600.0
                planning._note_implausible_plan(ctx, episode)
                self.assertNotIn("implausible_plans", ctx.report)
            finally:
                store.close()


class SubtitleConfidenceTests(unittest.TestCase):
    """A recogniser handed music does not return nothing - it returns words.

    Measured on a real film: a passage with no dialogue produced "whom moon
    when he first thing and that", burned into the video. Both production
    backends report per-word confidence and nothing read it, so every guess
    went on screen.
    """

    def _lines(self, words, *, floor=0.35, ratio=0.5):
        from aicut.models import Cut, Episode, Utterance
        from aicut.pipeline import planning
        from aicut.render.timeline import Timeline

        class _Ctx:
            profile = CalibrationProfile.load().with_overrides(
                {"subtitle.min_word_confidence": floor,
                 "subtitle.min_kept_word_ratio": ratio}, measured=[])

            def __init__(self):
                self.report = {}

        ctx = _Ctx()
        episode = Episode(project_id="p", timeline=[Cut(0, 0.0, 10.0)])
        utterance = Utterance(
            start_sec=0.5, end_sec=4.0, text=" ".join(w["word"] for w in words),
            speaker="HOST", words=words,
        )
        lines = planning._subtitles(
            episode, Timeline.from_cuts(episode.timeline), [utterance], 1.0, ctx=ctx,
        )
        return lines, ctx.report

    def _word(self, text, start, score):
        return {"word": text, "start": start, "end": start + 0.3, "score": score}

    def test_a_line_the_recogniser_doubted_is_not_burned_in(self):
        lines, report = self._lines([
            self._word("whom", 1.0, 0.04), self._word("moon", 1.4, 0.02),
            self._word("when", 1.8, 0.05), self._word("he", 2.2, 0.09),
        ])
        self.assertEqual(lines, [], "invented words went on screen")
        self.assertTrue(report.get("subtitles_dropped"), "the drop was not reported")

    def test_a_line_the_recogniser_stood_behind_is_kept(self):
        lines, _ = self._lines([
            self._word("he", 1.0, 0.95), self._word("wins", 1.4, 0.91),
            self._word("the", 1.8, 0.88), self._word("tournament", 2.2, 0.93),
        ])
        self.assertEqual(len(lines), 1)
        self.assertIn("tournament", lines[0].text)

    def test_a_mostly_confident_line_keeps_only_its_confident_words(self):
        lines, _ = self._lines([
            self._word("he", 1.0, 0.95), self._word("wins", 1.4, 0.91),
            self._word("xyzzy", 1.8, 0.03), self._word("tournament", 2.2, 0.93),
        ])
        self.assertEqual(len(lines), 1)
        self.assertNotIn("xyzzy", lines[0].text)
        self.assertIn("wins", lines[0].text)

    def test_a_backend_with_no_scores_loses_nothing(self):
        """PocketSphinx reports no confidence. Not knowing a word is wrong is
        not the same as knowing it is - so nothing is dropped, and the run says
        the captions could not be checked."""
        lines, report = self._lines([
            {"word": "hey", "start": 1.0, "end": 1.3},
            {"word": "look", "start": 1.4, "end": 1.7},
        ])
        self.assertEqual(len(lines), 1)
        self.assertIn("hey", lines[0].text)
        self.assertNotIn("subtitles_dropped", report)
        self.assertIn("backend reports no per-word confidence",
                      report.get("subtitle_confidence_note", ""))


class HallucinatedSceneBoundsTests(unittest.TestCase):
    """8.1: the provider refines the scene it picked. It does not get a free hand."""

    def setUp(self):
        from aicut.db.store import Store
        from aicut.models import Project

        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.store = Store(":memory:")
        self.project = Project(file_path="/src.mkv", duration_sec=21600.0)
        self.store.create_project(self.project)

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def _cuts(self, returned: dict) -> tuple[list, dict]:
        from aicut.config import CalibrationProfile
        from aicut.pipeline import planning
        from aicut.pipeline.context import RunContext
        from aicut.pipeline.retrieval import Scene, SceneIndex

        class StubProducer:
            name = "stub"

            def select_scene(self, payload):
                return {"chosen_index": 0, **returned}

        # Tokens must match the beat's query exactly: retrieval is BM25, so a
        # near-miss returns nothing and select_scene is never reached.
        scene = Scene(start_sec=1000.0, end_sec=1030.0, text="사건 발생",
                      tokens=["사건", "발생"])
        ctx = RunContext(
            project=self.project, store=self.store,
            profile=CalibrationProfile.load(), producer=StubProducer(),
            workspace=Path(self._tmp.name),
        )
        structure = {"beats": [{"role": "핵심", "query": "사건"}]}
        cuts = planning._lay_out_cuts(ctx, structure, SceneIndex([scene]))
        return cuts, ctx.report

    def test_bounds_outside_the_chosen_scene_are_pulled_back_into_it(self):
        """0-21600 on a 30-second scene is a six-hour cut nothing vouches for."""
        cuts, _ = self._cuts({"start_sec": 0.0, "end_sec": 21600.0})
        self.assertEqual(len(cuts), 1)
        cut = cuts[0]
        self.assertGreaterEqual(cut.source_end_sec - cut.source_start_sec, 0.0)
        self.assertLess(cut.source_end_sec - cut.source_start_sec, 120.0,
                        "the cut escaped the 30-second scene it was refined from")

    def test_the_correction_is_reported_rather_than_hidden(self):
        _, report = self._cuts({"start_sec": 0.0, "end_sec": 21600.0})
        entries = report.get("out_of_scene_bounds", [])
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["scene"], [1000.0, 1030.0])
        self.assertEqual(entries[0]["returned"], [0.0, 21600.0])

    def test_bounds_inside_the_scene_pass_through_and_say_nothing(self):
        cuts, report = self._cuts({"start_sec": 1005.0, "end_sec": 1020.0})
        self.assertEqual(len(cuts), 1)
        self.assertNotIn("out_of_scene_bounds", report)


class FailureReasonInTheReportTests(unittest.TestCase):
    """22.6: a work report that says FAILED and not why is not a report."""

    def test_the_exception_text_reaches_the_built_report(self):
        from aicut.config import CalibrationProfile
        from aicut.db.store import Store
        from aicut.models import Project
        from aicut.pipeline.context import RunContext
        from aicut.pipeline.runner import build_report
        from aicut.pipeline.states import State

        store = Store(":memory:")
        try:
            project = Project(file_path="/src.mkv", duration_sec=10.0)
            store.create_project(project)
            ctx = RunContext(
                project=project, store=store, profile=CalibrationProfile.load(),
                producer=None, workspace=Path("."),
            )
            ctx.note("error", "ffprobe found no audio stream")
            report = build_report(ctx, State.FAILED, [])
            self.assertEqual(report["error"], "ffprobe found no audio stream")
        finally:
            store.close()


class FramesReachTheJudgementTests(unittest.TestCase):
    """5.2: frames must arrive at the judgement as pictures, not as path strings.

    They used to be put in the payload as a list of file paths and serialised
    to JSON, so the model was handed filenames it could not open. Nothing about
    that failed - the run produced summaries, from sound alone, which is the
    tool 1.2 criticises.
    """

    def _windowed(self, frame_count: int, limit: int):
        from aicut.config import CalibrationProfile
        from aicut.pipeline import understanding

        class Ctx:
            profile = CalibrationProfile.load().with_overrides(
                {"scan": {"frames_per_window": limit}}, measured=["scan.frames_per_window"],
            )

        return understanding._frames_to_show(Ctx(), [f"f{i}.jpg" for i in range(frame_count)])

    def test_a_window_under_the_limit_shows_everything_it_has(self):
        self.assertEqual(self._windowed(3, 6), ["f0.jpg", "f1.jpg", "f2.jpg"])

    def test_a_dense_window_is_thinned_to_the_profile_count(self):
        shown = self._windowed(300, 6)
        self.assertEqual(len(shown), 6)

    def test_the_thinning_spans_the_window_rather_than_its_opening(self):
        """Taking the first six would describe the first six seconds."""
        shown = self._windowed(300, 6)
        self.assertEqual(shown[0], "f0.jpg")
        self.assertGreater(int(shown[-1][1:-4]), 200, "the tail of the window is never seen")

    def test_zero_means_send_none(self):
        self.assertEqual(self._windowed(300, 0), [])

    def test_the_mock_records_what_it_was_shown(self):
        """The offline suite can only pin this wiring if the mock reports it."""
        from aicut.llm.mock import MockProducer

        producer = MockProducer()
        producer.summarize_window({"window": {"start_sec": 0, "end_sec": 5}, "utterances": []},
                                  images=["a.jpg", "b.jpg"])
        self.assertEqual(producer.seen_images["summarize_window"], ["a.jpg", "b.jpg"])
