import unittest

from aicut.db.store import Store
from aicut.intelligence.knowledge import ProductionKnowledge, consolidate
from aicut.intelligence.source_output import AlignedSpan, Alignment, align_by_transcript, learn
from aicut.llm.mock import MockProducer
from aicut.models import Utterance


class SourceOutputTests(unittest.TestCase):
    """12.3 B: what a human kept, dropped and reordered is the core signal."""

    def setUp(self):
        self.source = [
            Utterance(0, 4, "hello everyone welcome to the stream"),
            Utterance(60, 64, "this boss keeps killing me"),
            Utterance(600, 604, "i finally beat the boss"),
            Utterance(900, 904, "anyway lets talk about lunch"),
        ]
        self.output = [
            Utterance(0, 4, "i finally beat the boss"),
            Utterance(5, 9, "this boss keeps killing me"),
        ]

    def test_alignment_finds_what_was_dropped(self):
        alignment = align_by_transcript(self.source, self.output)
        dropped = [s.text for s in alignment.spans if not s.kept]
        self.assertIn("hello everyone welcome to the stream", dropped)
        self.assertIn("anyway lets talk about lunch", dropped)

    def test_alignment_detects_reordering(self):
        self.assertTrue(align_by_transcript(self.source, self.output).reordered())

    def test_keep_ratio_is_measured(self):
        alignment = align_by_transcript(self.source, self.output)
        self.assertAlmostEqual(alignment.keep_ratio, 0.5)

    def test_learning_stores_the_pair_and_the_measurements(self):
        store = Store()
        alignment = align_by_transcript(self.source, self.output)
        analysis = learn(MockProducer(), store, alignment, source_ref="src.mkv", output_ref="out.mp4")
        self.assertEqual(analysis["measured"]["kept_spans"], 2)
        self.assertEqual(analysis["measured"]["dropped_spans"], 2)
        pairs = store.source_output_pairs()
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0]["source_ref"], "src.mkv")
        store.close()


class KnowledgeTests(unittest.TestCase):
    def test_patterns_are_counted_not_copied(self):
        """4.6: the value is what several references share."""
        knowledge = consolidate([
            {"structure": {"opening": "show the result first"}},
            {"structure": {"opening": "show the result first"}},
            {"structure": {"opening": "chronological"}},
        ])
        top = knowledge.structure_patterns[0]
        self.assertEqual(top["support"], 2)
        self.assertAlmostEqual(top["share"], 0.667, places=2)

    def test_planner_view_carries_the_caveat(self):
        summary = ProductionKnowledge(sample_size=3).summary_for_planner()
        self.assertIn("not rules", summary["caveat"])

    def test_reference_rows_store_patterns_only(self):
        """4.6: there is nowhere in the schema to keep reference media."""
        store = Store()
        store.save_reference("vid1", "chan1", {"views": 10}, {"structure": {}})
        columns = {row[1] for row in store.conn.execute("PRAGMA table_info(tb_yt_reference)")}
        self.assertFalse({"media_path", "video_file", "download_path"} & columns)
        self.assertEqual(store.references()[0]["public_metrics"], {"views": 10})
        store.close()


class CalibrationMetricTests(unittest.TestCase):
    """17.3, including 9.4's requirement that pacing be scored against a human edit."""

    def test_pacing_recall_and_precision(self):
        from aicut.calibration.metrics import score_pacing

        # human kept 1 and 3; system kept 1 and 2
        score = score_pacing([True, True, False], [True, False, True])
        self.assertEqual(score.keep_recall, 0.5)
        self.assertEqual(score.cut_precision, 0.0)

    def test_perfect_agreement_scores_one(self):
        from aicut.calibration.metrics import score_pacing

        score = score_pacing([True, False, True], [True, False, True])
        self.assertEqual(score.accuracy, 1.0)
        self.assertEqual(score.f1, 1.0)

    def test_false_positive_rate_counts_promoted_junk(self):
        from aicut.calibration.metrics import score_content_discovery

        score = score_content_discovery([(0, 100), (500, 600)], [(0, 90)])
        self.assertEqual(score.matched, 1)
        self.assertEqual(score.false_positive_rate, 0.5)
        self.assertEqual(score.recall, 1.0)

    def test_sweep_marks_the_parameters_it_measured(self):
        from aicut.calibration import sweep
        from aicut.config import CalibrationProfile

        base = CalibrationProfile.load()
        self.assertTrue(base.is_provisional("pacing.keep_score_threshold"))
        result = sweep(
            base,
            {"pacing.keep_score_threshold": [0.3, 0.5, 0.7]},
            lambda p: 1.0 - abs(p.get_float("pacing.keep_score_threshold") - 0.5),
        )
        self.assertEqual(result.best_params["pacing.keep_score_threshold"], 0.5)
        self.assertFalse(result.profile.is_provisional("pacing.keep_score_threshold"))
        self.assertTrue(result.profile.is_provisional("pacing.trim_target_sec"))
        self.assertIsNotNone(result.profile.measured_at)


if __name__ == "__main__":
    unittest.main()


class RepetitionCountingTests(unittest.TestCase):
    """12.3 B learns repetition decisions from this field, so it has to mean it."""

    def test_a_moment_used_twice_is_counted_twice(self):
        source = [Utterance(600, 604, "i finally beat the boss")]
        output = [
            Utterance(0, 4, "i finally beat the boss"),      # cold open
            Utterance(300, 304, "i finally beat the boss"),  # again, in place
        ]
        spans = align_by_transcript(source, output).spans
        self.assertEqual(len(spans), 1)
        self.assertTrue(spans[0].kept)
        self.assertEqual(spans[0].repeated, 2)

    def test_two_similar_source_lines_are_not_a_repeat(self):
        """They merely collide on one output line; nothing was repeated."""
        source = [
            Utterance(60, 64, "this boss keeps killing me"),
            Utterance(120, 124, "this boss keeps killing me"),
        ]
        output = [Utterance(0, 4, "this boss keeps killing me")]
        spans = align_by_transcript(source, output).spans
        self.assertEqual([s.repeated for s in spans], [1, 1])

    def test_a_single_use_stays_one(self):
        source = [Utterance(0, 4, "hello everyone welcome to the stream")]
        output = [Utterance(0, 4, "hello everyone welcome to the stream")]
        self.assertEqual(align_by_transcript(source, output).spans[0].repeated, 1)

    def test_a_dropped_line_is_still_dropped(self):
        source = [Utterance(0, 4, "farming for twenty minutes now")]
        output = [Utterance(0, 4, "completely different words here")]
        span = align_by_transcript(source, output).spans[0]
        self.assertFalse(span.kept)
        self.assertEqual(span.repeated, 1)

    def test_the_strongest_match_represents_the_span(self):
        """Reordering is measured from where the editor actually put it.

        Overlap is containment (the smaller set is the denominator), so a
        subset of the source scores 1.0 while a partial paraphrase scores less.
        The weaker match is placed first here on purpose: the representative
        must be chosen by score, not by position.
        """
        source = [Utterance(600, 604, "i finally beat the boss")]
        output = [
            Utterance(0, 4, "i finally beat boss zzz"),   # 4/5 = 0.8
            Utterance(300, 304, "i finally beat the"),    # 4/4 = 1.0
        ]
        span = align_by_transcript(source, output).spans[0]
        self.assertEqual(span.repeated, 2)
        self.assertEqual(span.output_start_sec, 300)


class WholeBroadcastRemovalTests(unittest.TestCase):
    """12.3 B asks what the editor removed, and most of that has no speech in it."""

    def setUp(self):
        # A six-hour broadcast. Somebody talks three times; the rest is farming,
        # walking and away-from-desk — exactly what an editor cuts and exactly
        # what an alignment built from speech alone cannot see.
        self.source = [
            Utterance(600, 640, "this boss keeps killing me"),
            Utterance(9000, 9040, "i finally beat the boss"),
            Utterance(20000, 20040, "thanks for watching everyone"),
        ]
        self.output = [
            Utterance(0, 40, "this boss keeps killing me"),
            Utterance(40, 80, "i finally beat the boss"),
        ]
        self.duration = 21600.0

    def _aligned(self):
        return align_by_transcript(self.source, self.output, source_duration_sec=self.duration)

    def test_speech_keep_ratio_and_whole_broadcast_ratio_are_different_numbers(self):
        alignment = self._aligned()
        self.assertAlmostEqual(alignment.keep_ratio, 2 / 3, places=3)
        # 80 seconds of 21,600 actually reached the video.
        self.assertAlmostEqual(alignment.selection_ratio, 80 / 21600, places=6)

    def test_the_silent_hours_show_up_as_removed(self):
        removed = self._aligned().removed_segments()
        total = sum(i.duration for i in removed)
        self.assertAlmostEqual(total, self.duration - 80, places=3)
        self.assertEqual(removed[0].start_sec, 0.0)
        self.assertEqual(removed[-1].end_sec, self.duration)

    def test_selected_segments_are_the_kept_source_ranges(self):
        selected = self._aligned().selected_segments()
        self.assertEqual([(i.start_sec, i.end_sec) for i in selected],
                         [(600.0, 640.0), (9000.0, 9040.0)])

    def test_adjacent_and_overlapping_uses_fold_into_one_stretch(self):
        alignment = Alignment(
            source_ref="", output_ref="", source_duration_sec=100.0,
            spans=[
                AlignedSpan(10, 20, 0, 10, kept=True),
                AlignedSpan(18, 30, 10, 22, kept=True),
                AlignedSpan(30, 40, 22, 32, kept=True),
            ],
        )
        self.assertEqual([(i.start_sec, i.end_sec) for i in alignment.selected_segments()],
                         [(10.0, 40.0)])
        self.assertEqual([(i.start_sec, i.end_sec) for i in alignment.removed_segments()],
                         [(0.0, 10.0), (40.0, 100.0)])

    def test_without_a_duration_the_figures_are_absent_not_guessed(self):
        """17.5: a number nobody measured is not reported as one."""
        alignment = align_by_transcript(self.source, self.output)
        self.assertEqual(alignment.source_duration_sec, 0.0)
        self.assertEqual(alignment.removed_segments(), [])
        self.assertEqual(alignment.selection_ratio, 0.0)

    def test_learn_carries_the_whole_broadcast_figures(self):
        from aicut.db.store import Store

        store = Store(":memory:")
        try:
            analysis = learn(MockProducer(), store, self._aligned(),
                             source_ref="src.mkv", output_ref="out.mp4")
        finally:
            store.close()
        measured = analysis["measured"]
        self.assertEqual(measured["source_duration_sec"], self.duration)
        self.assertAlmostEqual(measured["removed_sec"], self.duration - 80, places=1)
        self.assertGreater(measured["removed_segments"], 0)


class EditingDecisionDetailTests(unittest.TestCase):
    """12.3 B asks *how* the order changed and *what* was emphasised."""

    def test_the_moment_that_jumped_backwards_is_the_one_marked(self):
        """05:12 placed before 01:42 is the decision; a whole-video flag is not."""
        source = [
            Utterance(100, 140, "보스한테 계속 죽네"),
            Utterance(9000, 9040, "드디어 잡았다"),
            Utterance(9100, 9140, "끝나고 얘기하자"),
        ]
        output = [
            Utterance(0, 40, "드디어 잡았다"),        # 결과 먼저
            Utterance(40, 80, "보스한테 계속 죽네"),   # 과거로 점프
            Utterance(80, 120, "끝나고 얘기하자"),
        ]
        spans = {s.text: s for s in align_by_transcript(source, output).kept_spans}
        self.assertFalse(spans["드디어 잡았다"].order_changed)
        self.assertTrue(spans["보스한테 계속 죽네"].order_changed,
                        "the span that went backwards is not marked")
        self.assertFalse(spans["끝나고 얘기하자"].order_changed)

    def test_a_straight_edit_moves_nothing(self):
        source = [Utterance(0, 10, "첫번째"), Utterance(100, 110, "두번째")]
        output = [Utterance(0, 10, "첫번째"), Utterance(10, 20, "두번째")]
        alignment = align_by_transcript(source, output)
        self.assertFalse(any(s.order_changed for s in alignment.kept_spans))
        self.assertFalse(alignment.reordered())

    def test_a_moment_given_more_room_is_measured_not_labelled(self):
        """18장 gives 편집 의도 to the AI. Code reports the ratio, nothing more."""
        span = AlignedSpan(100, 110, output_start_sec=0, output_end_sec=14, kept=True)
        self.assertAlmostEqual(span.compression, 1.4, places=3)
        self.assertFalse(hasattr(span, "emphasis"),
                         "code is deciding 강조 again; 12.3 B asks the analysis")

    def test_a_trimmed_moment_reports_a_ratio_below_one(self):
        span = AlignedSpan(100, 110, output_start_sec=0, output_end_sec=6, kept=True)
        self.assertAlmostEqual(span.compression, 0.6, places=3)

    def test_a_dropped_span_has_no_output_length(self):
        self.assertEqual(AlignedSpan(100, 110, kept=False, repeated=3).compression, 0.0)

    def test_the_repeat_count_reaches_the_payload_uninterpreted(self):
        source = [Utterance(0, 10, "같은 말")]
        output = [Utterance(0, 10, "같은 말"), Utterance(20, 30, "같은 말")]
        span = align_by_transcript(source, output).kept_spans[0]
        self.assertEqual(span.repeated, 2)

    def test_the_counts_reach_the_measured_block(self):
        from aicut.db.store import Store

        source = [Utterance(100, 140, "나중 발언"), Utterance(9000, 9040, "먼저 보여줄 것")]
        output = [Utterance(0, 40, "먼저 보여줄 것"), Utterance(40, 80, "나중 발언")]
        alignment = align_by_transcript(source, output, source_duration_sec=10000.0)
        store = Store(":memory:")
        try:
            measured = learn(MockProducer(), store, alignment,
                             source_ref="s", output_ref="o")["measured"]
        finally:
            store.close()
        self.assertEqual(measured["moved_spans"], 1)
        self.assertNotIn("emphasised_spans", measured,
                         "강조 is the analysis's answer (12.3 B), not a count code made")
