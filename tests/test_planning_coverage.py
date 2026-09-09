"""7·8·9장: what decides the structure, the scenes, and the breathing.

7장 says the shape is not hardcoded and shows four different ones to prove it.
8.1 gives the queries, 8.2 gives what rides along with each scene. 9.1 names two
kinds of silence, 9.2 names the five signals that tell them apart, and 9.4 says
this is the most subjective judgement in the system.
"""

import unittest

from aicut.analysis.pacing import PacingJudge, SilenceContext, build_silence_contexts
from aicut.config import CalibrationProfile
from aicut.llm.prompts import _TASKS
from aicut.media.audio import Silence
from aicut.media.faces import FaceReading
from aicut.media.vision import MotionSample
from aicut.models import PacingMode


class StructurePromptTests(unittest.TestCase):
    def setUp(self):
        self.prompt = _TASKS["plan_structure"]

    def test_the_question_of_7_is_asked(self):
        self.assertIn("어떤 방식으로 보여주는 것이 가장 좋은가", self.prompt)

    def test_all_four_example_structures_are_shown(self):
        """One example reads as the house style; four read as "pick your own"."""
        for shape in ("결과 장면 -> 과거 장면", "평범한 대화 -> 이상한 발언",
                      "게임 시작 -> 실패", "짧은 사건 -> 반응"):
            self.assertIn(shape, self.prompt, f"7장: the {shape} example is missing")

    def test_they_are_marked_as_examples_not_a_menu(self):
        self.assertIn("not a menu", self.prompt)
        self.assertIn("콘텐츠마다 다른 구조를 사용할 수 있다", self.prompt)

    def test_7_1_says_knowledge_is_not_applied_unconditionally(self):
        self.assertIn("무조건 적용하지 않는다", self.prompt)
        self.assertIn("evidence, not an instruction", self.prompt)

    def test_the_length_hint_stays_a_hint(self):
        self.assertIn("length_note", self.prompt)

    def test_all_four_query_examples_of_8_1(self):
        for query in ("사건이 처음 언급된 장면", "상대방이 처음 반응한 장면",
                      "상황이 바뀐 장면", "결과가 발생한 장면"):
            self.assertIn(query, self.prompt, f"8.1: the {query} example is missing")


class EditIntentTests(unittest.TestCase):
    """8.2: 각 장면에 편집 의도를 함께 지정한다."""

    def test_all_eight_scene_level_intents_are_asked_for(self):
        prompt = _TASKS["select_scene"]
        for intent in ("자막", "확대", "크롭", "BGM", "효과음", "그래픽",
                       "전환", "오디오 조정"):
            self.assertIn(intent, prompt, f"8.2: {intent} is never asked for")

    def test_the_ninth_is_left_to_9(self):
        """호흡 처리 방식 is 8.2's ninth item and 9장's whole subject."""
        self.assertIn("judged separately", _TASKS["select_scene"])

    def test_the_renderer_decides_nothing(self):
        self.assertIn("renderer decides nothing", _TASKS["select_scene"])

    def test_an_unstated_intent_does_not_reach_the_cut(self):
        from aicut.pipeline.planning import _intent

        merged = _intent({"zoom": 1.2}, {"zoom": None, "crop": "9:16", "graphic": None})
        self.assertEqual(merged, {"zoom": 1.2, "crop": "9:16"})

    def test_what_the_selection_saw_wins_over_the_beat_s_guess(self):
        from aicut.pipeline.planning import _intent

        self.assertEqual(_intent({"zoom": 1.2}, {"zoom": 1.8}), {"zoom": 1.8})


class PacingPromptTests(unittest.TestCase):
    def setUp(self):
        self.prompt = _TASKS["judge_pacing"]

    def test_all_four_boring_silences_of_9_1(self):
        for kind in ("의미 없는 마우스 클릭", "숨소리만 있는 구간",
                     "파밍·이동 등 반복 작업", "자리비움"):
            self.assertIn(kind, self.prompt, f"9.1 지루한 정적: {kind} is missing")

    def test_all_four_kept_silences_of_9_1(self):
        for kind in ("황당한 상황에 직면해 말을 잇지 못하는 구간",
                     "반박 직전 숨을 고르는 구간",
                     "화자 전환 대기 구간",
                     "직전에 고텐션 발화(소리지름/폭소)가 있었던 직후"):
            self.assertIn(kind, self.prompt, f"9.1 예능적 정적: {kind} is missing")

    def test_all_five_signals_of_9_2(self):
        for signal in ("무음 지속 시간", "직전 구간의 오디오 텐션", "화자 전환",
                       "표정", "부여받은 역할"):
            self.assertIn(signal, self.prompt, f"9.2: {signal} is not offered")

    def test_the_three_modes_of_9_3_carry_their_definitions(self):
        self.assertIn("정적을 그대로 보존", self.prompt)
        self.assertIn("정적을 일부만 남기고 압축", self.prompt)
        self.assertIn("구간 자체를 제거", self.prompt)

    def test_9_4_asks_for_a_checkable_reason(self):
        self.assertIn("checked against a human", self.prompt)

    def test_no_threshold_is_written_into_the_prompt(self):
        """9.2: 임계값을 여기에 적지 않는다."""
        self.assertIn("every threshold is in the profile", self.prompt)


class ExpressionSignalTests(unittest.TestCase):
    """9.2 asks for 표정·움직임 정지 여부 — two signals, not one."""

    def setUp(self):
        self.profile = CalibrationProfile.load()

    def _faces(self, moving: bool):
        # A face box that grows and slides = a reaction; one that does not = still.
        if moving:
            return [
                FaceReading(at_sec=9.0, face_ratio=0.10, box=(100, 100, 200, 200)),
                FaceReading(at_sec=10.0, face_ratio=0.30, box=(400, 300, 500, 500)),
            ]
        return [
            FaceReading(at_sec=9.0, face_ratio=0.20, box=(100, 100, 200, 200)),
            FaceReading(at_sec=10.0, face_ratio=0.20, box=(100, 100, 200, 200)),
        ]

    def _contexts(self, faces):
        return build_silence_contexts(
            [Silence(9.0, 11.0)], [], _flat_tension(), [MotionSample(at_sec=10.0, score=0.0)],
            self.profile, faces=faces,
        )

    def test_a_moving_face_on_a_still_frame_is_measured(self):
        context = self._contexts(self._faces(moving=True))[0]
        self.assertIsNotNone(context.expression_change)
        self.assertGreater(context.expression_change, 0.0)

    def test_a_still_face_measures_near_zero(self):
        context = self._contexts(self._faces(moving=False))[0]
        self.assertAlmostEqual(context.expression_change, 0.0, places=3)

    def test_no_face_signal_is_none_not_zero(self):
        """No detector is not the same claim as "the face did not move"."""
        self.assertIsNone(self._contexts([])[0].expression_change)

    def test_a_reacting_face_on_a_still_frame_counts_towards_keeping(self):
        judge = PacingJudge(self.profile)
        still = SilenceContext(start_sec=9.0, end_sec=11.0, motion=0.0, expression_change=0.9)
        blank = SilenceContext(start_sec=9.0, end_sec=11.0, motion=0.0, expression_change=0.0)
        self.assertGreater(judge.judge(still).score, judge.judge(blank).score)

    def test_the_expression_thresholds_live_in_the_profile(self):
        for key in ("pacing.expression_window_sec", "pacing.expression_reaction_min"):
            self.assertIsInstance(self.profile.get_float(key), float)
        self.assertIn(
            "reacting_face_on_still_frame", self.profile.get("pacing.keep_signal_weights"),
        )

    def test_the_signal_is_reported_so_9_4_can_check_it(self):
        judge = PacingJudge(self.profile)
        decision = judge.judge(
            SilenceContext(start_sec=9.0, end_sec=11.0, motion=0.0, expression_change=0.9)
        )
        self.assertIn("expression_change", decision.signals)
        self.assertEqual(decision.mode.__class__, PacingMode)


def _flat_tension():
    from aicut.analysis.tension import TensionCurve

    return TensionCurve(times=[9.0, 10.0, 11.0], values=[0.1, 0.1, 0.1])


if __name__ == "__main__":
    unittest.main()


class DroppedBeatsAreReportedTests(unittest.TestCase):
    """8.2: a person must be able to read the plan and know what the result is.

    A structure of five beats that renders as three is a different video from
    the one the plan describes. Both ways a beat can vanish used to leave only a
    log line, and a candidate decided PRODUCE could disappear entirely with the
    report showing nothing but a smaller episode count.
    """

    def _context(self, tmp):
        from pathlib import Path

        from aicut.config import CalibrationProfile
        from aicut.db.store import Store
        from aicut.llm import get_producer
        from aicut.models import Project
        from aicut.pipeline.context import RunContext

        store = Store(str(Path(tmp) / "aicut.db"))
        project = store.create_project(Project(
            project_id="p1", file_path="/nowhere.mkv", duration_sec=600.0,
        ))
        return RunContext(project=project, store=store, profile=CalibrationProfile.load(),
                          producer=get_producer("mock"), workspace=Path(tmp))

    def test_a_beat_that_retrieved_nothing_says_so_with_its_query(self):
        import tempfile

        from aicut.pipeline.planning import _lay_out_cuts
        from aicut.pipeline.retrieval import SceneIndex

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            ctx = self._context(tmp)
            try:
                empty = SceneIndex([])
                cuts = _lay_out_cuts(
                    ctx,
                    {"beats": [{"role": "결과", "query": "결과가 발생한 장면"}]},
                    empty, episode_id="ep1",
                )
            finally:
                ctx.store.close()

        self.assertEqual(cuts, [])
        dropped = ctx.report["beats_unfilled"]
        self.assertEqual(len(dropped), 1)
        self.assertEqual(dropped[0]["role"], "결과")
        self.assertEqual(dropped[0]["query"], "결과가 발생한 장면")
        self.assertIn("retrieved nothing", dropped[0]["why"])

    def test_the_two_reasons_a_beat_vanishes_are_told_apart(self):
        """Nothing retrieved means the broadcast has no such scene; every scene
        rejected means retrieval found the wrong ones. Different problems."""
        import tempfile
        from unittest import mock

        from aicut.pipeline.planning import _lay_out_cuts
        from aicut.pipeline.retrieval import Scene, SceneIndex, ScoredScene

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            ctx = self._context(tmp)
            scene = Scene(start_sec=10.0, end_sec=20.0, text="말", speaker="A")
            index = SceneIndex([scene])
            with mock.patch.object(
                SceneIndex, "search", return_value=[ScoredScene(scene=scene, score=1.0)]
            ), mock.patch.object(
                type(ctx.producer), "select_scene", return_value={"chosen_index": None}
            ):
                try:
                    cuts = _lay_out_cuts(
                        ctx, {"beats": [{"role": "배경", "query": "사건의 배경"}]},
                        index, episode_id="ep1",
                    )
                finally:
                    ctx.store.close()

        self.assertEqual(cuts, [])
        self.assertIn("rejected all 1", ctx.report["beats_unfilled"][0]["why"])

    def test_an_episode_that_found_no_scene_at_all_is_named_in_the_report(self):
        import tempfile
        from unittest import mock

        from aicut.models import ContentCandidate, Event, EventMention
        from aicut.pipeline import planning
        from aicut.pipeline.retrieval import SceneIndex

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            ctx = self._context(tmp)
            candidate = ContentCandidate(candidate_id="c1", project_id="p1",
                                         core_summary="싸움", related_event_ids=["e1"])
            event = Event(event_id="e1", project_id="p1", summary="싸움",
                          mentions=[EventMention(event_id="e1", source_start_sec=10.0,
                                                 source_end_sec=20.0)])
            with mock.patch.object(
                type(ctx.producer), "plan_structure",
                return_value={"structure_name": "결과 먼저", "beats": [
                    {"role": "결과", "query": "결과"}, {"role": "원인", "query": "원인"}]},
            ):
                try:
                    episode = planning.plan_episode(
                        ctx, [candidate], {"e1": event}, SceneIndex([]), [],
                    )
                finally:
                    ctx.store.close()

        self.assertIsNone(episode)
        entry = ctx.report["episodes_not_produced"][0]
        self.assertEqual(entry["candidate_ids"], ["c1"])
        self.assertEqual(entry["beats"], 2)
        self.assertIn("does not contain", entry["detail"])

    def test_a_candidate_whose_events_do_not_resolve_is_named_too(self):
        """The other way a PRODUCE candidate vanishes without a video."""
        import tempfile

        from aicut.models import ContentCandidate
        from aicut.pipeline import planning
        from aicut.pipeline.retrieval import SceneIndex

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            ctx = self._context(tmp)
            candidate = ContentCandidate(candidate_id="c9", project_id="p1",
                                         core_summary="사건", related_event_ids=["gone"])
            try:
                episode = planning.plan_episode(ctx, [candidate], {}, SceneIndex([]), [])
            finally:
                ctx.store.close()

        self.assertIsNone(episode)
        entry = ctx.report["episodes_not_produced"][0]
        self.assertEqual(entry["candidate_ids"], ["c9"])
        self.assertIn("resolve", entry["detail"])


class StitchedEpisodeIsReportedTests(unittest.TestCase):
    """6.2: 하나의 영상은 하나의 사건으로 완결되어야 한다.

    Retrieval scores the episode's own events higher but does not require them,
    so a scene from an unrelated hour can win on wording alone and become a cut.
    That is the 짜깁기 1.2 names, and nothing measured it on the result.
    """

    def _scene(self, start, end, events, text="말"):
        from aicut.pipeline.retrieval import Scene

        return Scene(start_sec=start, end_sec=end, text=text, speaker="A",
                     event_ids=list(events))

    def _episode(self, spans):
        from aicut.models import Cut, Episode

        episode = Episode(episode_id="ep1", project_id="p1")
        episode.timeline = [
            Cut(sequence_order=i, source_start_sec=a, source_end_sec=b,
                scene_role="core")
            for i, (a, b) in enumerate(spans)
        ]
        return episode

    def _report(self, episode, scenes, event_ids):
        import tempfile
        from pathlib import Path

        from aicut.config import CalibrationProfile
        from aicut.db.store import Store
        from aicut.llm import get_producer
        from aicut.models import Event, Project
        from aicut.pipeline.context import RunContext
        from aicut.pipeline.planning import _note_stitching
        from aicut.pipeline.retrieval import SceneIndex

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            store = Store(str(Path(tmp) / "aicut.db"))
            project = store.create_project(Project(
                project_id="p1", file_path="/nowhere.mkv", duration_sec=9000.0))
            ctx = RunContext(project=project, store=store,
                             profile=CalibrationProfile.load(),
                             producer=get_producer("mock"), workspace=Path(tmp))
            try:
                _note_stitching(ctx, episode, SceneIndex(scenes),
                                [Event(event_id=e, project_id="p1") for e in event_ids])
            finally:
                store.close()
        return ctx.report

    def test_an_episode_whose_cuts_all_carry_its_events_reports_nothing(self):
        episode = self._episode([(10.0, 20.0), (400.0, 410.0)])
        scenes = [self._scene(10.0, 20.0, ["e1"]), self._scene(400.0, 410.0, ["e1"])]

        self.assertNotIn("cuts_off_event", self._report(episode, scenes, ["e1"]))

    def test_a_cut_from_an_unrelated_event_is_named_with_its_span(self):
        episode = self._episode([(10.0, 20.0), (5000.0, 5010.0)])
        scenes = [self._scene(10.0, 20.0, ["e1"]),
                  self._scene(5000.0, 5010.0, ["e9"])]

        entry = self._report(episode, scenes, ["e1"])["cuts_off_event"][0]

        self.assertEqual(entry["cuts"], 2)
        self.assertEqual(len(entry["off_event"]), 1)
        self.assertEqual(entry["off_event"][0]["span"], [5000.0, 5010.0])
        self.assertIn("6.2", entry["detail"])

    def test_a_screen_change_inside_one_event_is_not_stitching(self):
        """6.2 allows the situation to change inside one content, explicitly.
        What it forbids is 맥락 없이 화면만 오가는 구성 - a different thing."""
        episode = self._episode([(10.0, 20.0), (3000.0, 3010.0), (30.0, 40.0)])
        scenes = [self._scene(10.0, 20.0, ["e1"]),
                  self._scene(3000.0, 3010.0, ["e1"]),
                  self._scene(30.0, 40.0, ["e1"])]

        self.assertNotIn("cuts_off_event", self._report(episode, scenes, ["e1"]))

    def test_a_cut_no_scene_covers_is_not_counted_as_off_event(self):
        """Absence of a scene is not evidence the cut is off-event; a gap in
        the index would otherwise read as 짜깁기."""
        episode = self._episode([(10.0, 20.0), (8000.0, 8010.0)])
        scenes = [self._scene(10.0, 20.0, ["e1"])]

        self.assertNotIn("cuts_off_event", self._report(episode, scenes, ["e1"]))

    def test_it_reports_and_never_removes_the_cut(self):
        """8.1 chose the scene. This says the result stopped being one event."""
        episode = self._episode([(10.0, 20.0), (5000.0, 5010.0)])
        scenes = [self._scene(10.0, 20.0, ["e1"]), self._scene(5000.0, 5010.0, ["e9"])]

        self._report(episode, scenes, ["e1"])

        self.assertEqual(len(episode.timeline), 2)


class SilentSecondPassBeatsAreRetrievableTests(unittest.TestCase):
    """16장 row 1: 음성 미감지 구간 — STT 우회, 화면 기반으로 구성.

    The mention branch already gives a silent event mention its own scene, with
    the comment "a hole in one path is a hole". The second pass had the same
    hole: a beat from 5.1's precise sweep landing on a stretch with no speech
    and no mention had no scene to attach to and was dropped, so the moments
    the system looked hardest at were the ones retrieval could not reach.
    """

    def _index(self, beats, utterances=(), notes=""):
        from aicut.models import DetailSpan
        from aicut.pipeline.retrieval import SceneIndex

        return SceneIndex.build(
            list(utterances), [],
            [DetailSpan(start_sec=100.0, end_sec=160.0, beats=list(beats), notes=notes)],
            source_sec=3600.0,
        )

    def test_a_beat_over_silence_becomes_a_retrievable_scene(self):
        index = self._index([{"at_sec": 110.0, "what": "캐릭터가 절벽에서 떨어진다"}])

        self.assertEqual(len(index.scenes), 1)
        scene = index.scenes[0]
        self.assertEqual(scene.start_sec, 110.0)
        self.assertIn("절벽에서", scene.text)

    def test_it_is_findable_by_what_the_pass_saw(self):
        from aicut.config import CalibrationProfile

        index = self._index([{"at_sec": 110.0, "what": "캐릭터가 절벽에서 떨어진다"}])
        found = index.search("절벽에서 떨어진 장면", CalibrationProfile.load())

        self.assertTrue(found)
        self.assertEqual(found[0].scene.start_sec, 110.0)

    def test_a_beat_inside_a_spoken_scene_still_only_enriches_it(self):
        """Where speech already covers the moment, the beat is context for that
        scene - not a second scene competing with it."""
        from aicut.models import Utterance

        index = self._index(
            [{"at_sec": 105.0, "what": "표정이 굳는다"}],
            utterances=[Utterance(start_sec=104.0, end_sec=108.0, text="어", speaker="A")],
        )

        self.assertEqual(len(index.scenes), 1)
        self.assertIn("굳는다", " ".join(index.scenes[0].tokens))

    def test_a_beat_with_no_timestamp_makes_no_scene(self):
        self.assertEqual(self._index([{"what": "무언가"}]).scenes, [])

    def test_the_new_scene_obeys_the_same_length_cap_as_the_others(self):
        """A hole in one path is a hole: the mention branch was capped after an
        uncapped scene reached a plan, and this path must not reopen it."""
        from aicut.models import DetailSpan
        from aicut.pipeline.retrieval import SceneIndex

        index = SceneIndex.build(
            [], [],
            [DetailSpan(start_sec=0.0, end_sec=5000.0,
                        beats=[{"at_sec": 10.0, "what": "일이 벌어진다"}])],
            source_sec=100.0,
        )

        self.assertTrue(index.scenes)
        for scene in index.scenes:
            self.assertLessEqual(scene.end_sec - scene.start_sec, 100.0 * 0.2)
