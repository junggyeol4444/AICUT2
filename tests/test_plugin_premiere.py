"""The Premiere adapter's arithmetic and its wire, tested without Premiere.

Premiere Pro is not installed in the environment this was written in, so the
plugin is split the same way the Resolve one is (37장): every decision lives in
`plugin/common/aicut_model.js` and `plugin/premiere/aicut_build.js` and is
tested here through node, and only the API calls live in the `.jsx` that needs
the application.

The model reader is a second implementation of one meaning - the Python one is
what Resolve uses - so most of these run the same model through both and
compare. Two copies that agree with each other and not with aicut is exactly
the failure this file exists to catch.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

from aicut.models import Cut, SubtitleLine
from aicut.render.editmodel import from_edit_plan
from aicut.render.editplan import EditPlan

PLUGIN = Path(__file__).resolve().parent.parent / "plugin"
MODEL_JS = PLUGIN / "common" / "aicut_model.js"
ENGINE_JS = PLUGIN / "common" / "aicut_engine.js"
BUILD_JS = PLUGIN / "premiere" / "aicut_build.js"
SHELL = PLUGIN / "premiere" / "aicut_premiere.jsx"
NODE = shutil.which("node")

#: Premiere's own tick rate. Written out rather than read from the module, so a
#: typo in the module is a failure here instead of agreeing with itself.
TICKS_PER_SECOND = 254016000000


def _model(cuts, **kw):
    """A Common Edit Model as the engine serves it, through the real builder."""
    plan = EditPlan(episode_id="ep123456", project_id="p",
                    source_path="/broadcasts/stream.mkv", cuts=cuts, **kw)
    return json.loads(json.dumps(from_edit_plan(plan, source_duration_sec=7200.0).as_dict()))


@unittest.skipIf(NODE is None, "node is needed to run the plugin's own code")
class NodeTests(unittest.TestCase):
    """Run a snippet against the real modules and get its JSON back.

    Reimplementing the logic in Python to test it would only prove the two
    copies agree with each other, which is the failure this is meant to catch.
    """

    def run_js(self, body: str, **names):
        script = (
            "var aicutModel = require({model});\n"
            "var aicutEngine = require({engine});\n"
            "var aicutBuild = require({build});\n"
            "var input = JSON.parse(process.argv[1]);\n"
            "var result;\n"
            "try {{ result = (function () {{ {body} }}()); }}\n"
            "catch (e) {{ result = {{error: e.message || String(e)}}; }}\n"
            "process.stdout.write(JSON.stringify(result));\n"
        ).format(model=json.dumps(str(MODEL_JS)), engine=json.dumps(str(ENGINE_JS)),
                 build=json.dumps(str(BUILD_JS)), body=body)
        done = subprocess.run(
            [NODE, "-e", script, json.dumps(names)],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    # -- the model means one thing, in both languages -------------------------

    def test_the_two_readers_agree_on_what_survives(self):
        """The JS reader and the Python one are the same rule, twice.

        Resolve reads the Python one and Premiere this one. A cut with pacing
        removals inside it is where a second implementation drifts first.
        """
        from plugin.common.aicut_model import kept_spans as python_kept_spans

        model = _model([Cut(sequence_order=1, source_start_sec=0.0, source_end_sec=10.0,
                            remove_spans=[[2.0, 3.0], [7.0, 8.5]])])
        clip = model["sequences"][0]["tracks"][0]["clips"][0]
        spans = self.run_js("return aicutModel.keptSpans(input.clip);", clip=clip)
        self.assertEqual([tuple(s) for s in spans], python_kept_spans(clip))

    def test_the_two_readers_agree_on_the_frames(self):
        """Same spans, each editor's own out-point convention.

        Resolve's endFrame is the last frame of the clip and Premiere's out
        point is the first frame after it, so the numbers differ by one and the
        lengths must not.
        """
        from plugin.common.aicut_model import frame_ranges as python_frame_ranges

        model = _model([
            Cut(sequence_order=1, source_start_sec=100.0, source_end_sec=110.0),
            Cut(sequence_order=2, source_start_sec=300.0, source_end_sec=320.0,
                remove_spans=[[305.0, 307.0]]),
        ])
        sequence = model["sequences"][0]
        premiere = self.run_js(
            "return aicutModel.frameRanges(input.sequence, 30, false).map("
            "function (r) { return [r.startFrame, r.endFrame, r.frames]; });",
            sequence=sequence,
        )
        resolve = python_frame_ranges(sequence, 30)
        self.assertEqual([(a, b) for a, b, _ in resolve],
                         [(a, b - 1) for a, b, _ in premiere])
        self.assertEqual([b - a + 1 for a, b, _ in resolve],
                         [frames for _, _, frames in premiere])

    def test_timeline_order_is_kept_not_source_order(self):
        """2.4: a video may open on the moment that happened last."""
        model = _model([Cut(sequence_order=1, source_start_sec=100.0, source_end_sec=101.0),
                        Cut(sequence_order=2, source_start_sec=10.0, source_end_sec=11.0)])
        entries = self.run_js("return aicutBuild.clipList(input.sequence, 30);",
                              sequence=model["sequences"][0])
        self.assertAlmostEqual(entries[0]["inSeconds"], 100.0)
        self.assertAlmostEqual(entries[1]["inSeconds"], 10.0)

    def test_removals_become_separate_clips(self):
        model = _model([Cut(sequence_order=1, source_start_sec=0.0, source_end_sec=10.0,
                            remove_spans=[[4.0, 6.0]])])
        entries = self.run_js("return aicutBuild.clipList(input.sequence, 30);",
                              sequence=model["sequences"][0])
        self.assertEqual(len(entries), 2)

    # -- where they land ------------------------------------------------------

    def test_clips_are_laid_end_to_end_with_no_gap(self):
        """A gap between clips is black frames in the finished video, and the
        model asked for a cut, not a gap."""
        model = _model([Cut(sequence_order=1, source_start_sec=0.0, source_end_sec=2.0),
                        Cut(sequence_order=2, source_start_sec=50.0, source_end_sec=53.0)])
        entries = self.run_js("return aicutBuild.clipList(input.sequence, 30);",
                              sequence=model["sequences"][0])
        self.assertAlmostEqual(entries[0]["timelineSeconds"], 0.0)
        self.assertAlmostEqual(entries[1]["timelineSeconds"], 2.0)

    def test_a_removal_closes_up_rather_than_leaving_a_hole(self):
        model = _model([Cut(sequence_order=1, source_start_sec=0.0, source_end_sec=10.0,
                            remove_spans=[[4.0, 6.0]])])
        entries = self.run_js("return aicutBuild.clipList(input.sequence, 30);",
                              sequence=model["sequences"][0])
        self.assertAlmostEqual(entries[1]["timelineSeconds"], 4.0)

    def test_the_out_point_makes_the_clip_the_length_the_model_asked_for(self):
        model = _model([Cut(sequence_order=1, source_start_sec=0.0, source_end_sec=1.0)])
        entries = self.run_js("return aicutBuild.clipList(input.sequence, 30);",
                              sequence=model["sequences"][0])
        self.assertEqual(entries[0]["frames"], 30)
        self.assertAlmostEqual(entries[0]["outSeconds"] - entries[0]["inSeconds"], 1.0)

    def test_times_are_snapped_to_the_sequence_grid(self):
        """Inserting at raw seconds leaves sub-frame gaps that accumulate into
        a drift nobody can find by the end of a long timeline."""
        model = _model([Cut(sequence_order=1, source_start_sec=1.017, source_end_sec=2.017)])
        entries = self.run_js("return aicutBuild.clipList(input.sequence, 30);",
                              sequence=model["sequences"][0])
        self.assertAlmostEqual(entries[0]["inSeconds"] * 30,
                               round(entries[0]["inSeconds"] * 30))

    def test_ticks_are_premieres_ticks_not_seconds(self):
        model = _model([Cut(sequence_order=1, source_start_sec=2.0, source_end_sec=3.0)])
        entries = self.run_js("return aicutBuild.clipList(input.sequence, 30);",
                              sequence=model["sequences"][0])
        self.assertEqual(entries[0]["inTicks"], str(2 * TICKS_PER_SECOND))

    def test_ticks_are_a_string_because_the_number_will_not_hold_them(self):
        """Six hours in ticks is 5.5e15 - past where a double counts by ones,
        and Premiere's Time.ticks is a string for the same reason."""
        model = _model([Cut(sequence_order=1, source_start_sec=21600.0,
                            source_end_sec=21601.0)])
        entries = self.run_js("return aicutBuild.clipList(input.sequence, 30);",
                              sequence=model["sequences"][0])
        self.assertIsInstance(entries[0]["inTicks"], str)
        self.assertEqual(entries[0]["inTicks"], str(21600 * TICKS_PER_SECOND))

    def test_a_non_integer_frame_rate_works(self):
        model = _model([Cut(sequence_order=1, source_start_sec=0.0, source_end_sec=1.0)])
        entries = self.run_js("return aicutBuild.clipList(input.sequence, 23.976);",
                              sequence=model["sequences"][0])
        self.assertEqual(entries[0]["frames"], 24)

    # -- refusals -------------------------------------------------------------

    def test_a_span_shorter_than_a_frame_is_dropped_and_reported(self):
        model = _model([Cut(sequence_order=1, source_start_sec=0.0, source_end_sec=1.0),
                        Cut(sequence_order=2, source_start_sec=5.0, source_end_sec=5.001)])
        sequence = model["sequences"][0]
        entries = self.run_js("return aicutBuild.clipList(input.sequence, 30);",
                              sequence=sequence)
        dropped = self.run_js("return aicutModel.droppedSpans(input.sequence, 30);",
                              sequence=sequence)
        self.assertEqual(len(entries), 1)
        self.assertEqual(len(dropped), 1)

    def test_a_sequence_of_nothing_but_slivers_is_an_error_not_an_empty_timeline(self):
        model = _model([Cut(sequence_order=1, source_start_sec=5.0, source_end_sec=5.001)])
        result = self.run_js("return aicutBuild.clipList(input.sequence, 30);",
                             sequence=model["sequences"][0])
        self.assertIn("shorter than one frame", result["error"])

    def test_a_bad_frame_rate_is_refused(self):
        model = _model([Cut(sequence_order=1, source_start_sec=0.0, source_end_sec=1.0)])
        result = self.run_js("return aicutBuild.clipList(input.sequence, 0);",
                             sequence=model["sequences"][0])
        self.assertIn("frame rate", result["error"])

    def test_an_edit_plan_is_named_as_the_wrong_document(self):
        """37장 has the adapter read the model. A plan is the engine's own
        working document and reading it here would be a second implementation
        of what it means."""
        result = self.run_js('return aicutModel.parse("{\\"cuts\\": []}", "plan.json");')
        self.assertIn("not a Common Edit Model", result["error"])
        self.assertIn("/api/episodes/<id>/edit-model", result["error"])

    def test_broken_json_names_the_file(self):
        result = self.run_js('return aicutModel.parse("{not json", "broken.json");')
        self.assertIn("broken.json", result["error"])

    def test_a_model_with_no_sequence_is_refused(self):
        result = self.run_js('return aicutModel.parse("{\\"sequences\\": []}", "empty.json");')
        self.assertIn("no sequence", result["error"])

    def test_a_real_model_round_trips(self):
        model = _model([Cut(sequence_order=1, source_start_sec=1.0, source_end_sec=2.0)])
        count = self.run_js(
            "return aicutModel.sequences("
            "aicutModel.parse(JSON.stringify(input.model))).length;",
            model=model,
        )
        self.assertEqual(count, 1)

    # -- presentation ---------------------------------------------------------

    def test_the_sequence_has_the_name_the_model_gave_it(self):
        model = _model([Cut(sequence_order=1, source_start_sec=0.0, source_end_sec=1.0)])
        name = self.run_js("return aicutModel.timelineName(input.model, input.model.sequences[0]);",
                           model=model)
        self.assertEqual(name, model["sequences"][0]["name"])
        self.assertTrue(name)

    def test_the_summary_states_the_length_the_sequence_will_be(self):
        model = _model([Cut(sequence_order=1, source_start_sec=0.0, source_end_sec=2.0),
                        Cut(sequence_order=2, source_start_sec=10.0, source_end_sec=13.0)])
        text = self.run_js("return aicutBuild.summary(input.model, input.model.sequences[0], 30);",
                           model=model)
        self.assertIn("2 clips", text)
        self.assertIn("5.0s", text)
        self.assertIn("stream.mkv", text)

    def test_the_source_name_survives_either_platforms_separators(self):
        """The model may have been written on the other kind of machine, and the
        name is what the script matches against the project's bins."""
        names = self.run_js(
            "return [aicutModel.baseName(input.win), aicutModel.baseName(input.posix)];",
            win="C:\\\\Users\\\\j\\\\stream.mkv", posix="/broadcasts/stream.mkv")
        self.assertEqual(names, ["stream.mkv", "stream.mkv"])

    def test_the_captions_and_the_placed_audio_are_visible_to_the_adapter(self):
        """24장 gives BGM, 효과음 and 자막 their own tracks. Premiere's scripting
        API places none of them, so the adapter has to be able to say so."""
        model = _model(
            [Cut(sequence_order=1, source_start_sec=0.0, source_end_sec=5.0)],
            subtitles=[SubtitleLine(0.0, 2.0, "first"), SubtitleLine(2.0, 4.0, "second")],
            structure={"bgm": {"path": "/music/bed.mp3"}},
        )
        counts = self.run_js(
            "return [aicutModel.subtitles(input.sequence).length,"
            " aicutModel.audioClips(input.sequence).length];",
            sequence=model["sequences"][0],
        )
        self.assertEqual(counts, [2, 1])

    # -- the wire (36장 9번) --------------------------------------------------

    def test_the_engine_call_is_the_one_the_server_serves(self):
        request = self.run_js(
            "var engine = new aicutEngine.Engine({});"
            "return aicutEngine.buildRequest('POST', '/api/episodes/ep1/edit-model',"
            " {mode: 'edit_current'}, engine);"
        )
        self.assertIn("POST /api/episodes/ep1/edit-model HTTP/1.0", request)
        self.assertIn("Host: 127.0.0.1:8765", request)
        self.assertIn('{"mode":"edit_current"}', request)

    def test_content_length_counts_bytes_not_characters(self):
        """A Korean path is ordinary here, and the socket writes bytes. A length
        in characters makes the server wait for a body that never finishes."""
        request = self.run_js(
            "var engine = new aicutEngine.Engine({});"
            "return aicutEngine.buildRequest('POST', '/api/projects',"
            " {source: '/방송/live.mkv'}, engine);"
        )
        body = request.split("\r\n\r\n", 1)[1]
        stated = [line for line in request.splitlines()
                  if line.startswith("Content-Length:")][0]
        self.assertEqual(int(stated.split(":")[1]), len(body.encode("utf-8")))
        self.assertNotEqual(len(body.encode("utf-8")), len(body))

    def test_a_refusal_from_the_engine_is_read_as_one(self):
        result = self.run_js(
            "return aicutEngine.parseResponse("
            "'HTTP/1.0 404 Not Found\\r\\n\\r\\n{\"error\": \"unknown episode\"}',"
            " 'POST', '/api/episodes/x/edit-model');"
        )
        self.assertIn("HTTP 404", result["error"])
        self.assertIn("unknown episode", result["error"])

    def test_no_reply_at_all_says_the_engine_is_not_running(self):
        result = self.run_js(
            "return aicutEngine.parseResponse('', 'GET', '/api/profiles');"
        )
        self.assertIn("aicut ui", result["error"])

    def test_a_reply_is_parsed_into_the_model_the_adapter_builds_from(self):
        model = _model([Cut(sequence_order=1, source_start_sec=0.0, source_end_sec=1.0)])
        count = self.run_js(
            "var reply = 'HTTP/1.0 200 OK\\r\\nContent-Type: application/json\\r\\n\\r\\n'"
            " + JSON.stringify(input.model);"
            "var engine = new aicutEngine.Engine({transport: function () { return reply; }});"
            "return aicutModel.sequences("
            "aicutModel.validated(engine.editModel('ep1', 'new_sequence'))).length;",
            model=model,
        )
        self.assertEqual(count, 1)

    def test_the_json_writer_works_where_the_host_has_no_JSON_object(self):
        """Older ExtendScript hosts have no JSON. The engine still has to be
        sent a body, and a Korean path still has to arrive intact."""
        script = (
            "var JSONbackup = JSON; JSON = undefined;\n"
            "var aicutEngine = require({engine});\n"
            "var written = aicutEngine.jsonEncode({{source: '/방송/live.mkv', keep: true}});\n"
            "JSON = JSONbackup;\n"
            "process.stdout.write(written);\n"
        ).format(engine=json.dumps(str(ENGINE_JS)))
        done = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout),
                         {"source": "/방송/live.mkv", "keep": True})


class PortabilityTests(unittest.TestCase):
    """ExtendScript is ES3. Anything newer is a syntax error at load, before a
    line of this runs - and there is no console open to say so."""

    BANNED = (
        ("let ", "let"),
        ("const ", "const"),
        ("=>", "arrow functions"),
        ("`", "template literals"),
        ("...", "spread"),
    )

    def _code(self, path: Path) -> str:
        source = path.read_text(encoding="utf-8")
        return "\n".join(line for line in source.splitlines()
                         if not line.strip().startswith(("*", "//", "/*")))

    def test_the_decisions_are_es3(self):
        for path in (MODEL_JS, ENGINE_JS, BUILD_JS):
            code = self._code(path)
            for needle, what in self.BANNED:
                self.assertNotIn(needle, code,
                                 f"{path.name} uses {what}, which ExtendScript has not")

    def test_the_decisions_do_not_need_premiere(self):
        """These are the halves that must run with nothing installed - they are
        what the tests above exercise."""
        for path in (MODEL_JS, ENGINE_JS, BUILD_JS):
            code = path.read_text(encoding="utf-8")
            for name in ("app.project", "new File(", "new Socket("):
                self.assertNotIn(name, code, f"{path.name} reaches for {name}")

    def test_the_adapter_reads_the_model_and_not_the_plan(self):
        """37장: AI Engine -> Common Edit Model -> Editor Adapter -> 편집기."""
        shell = SHELL.read_text(encoding="utf-8")
        self.assertIn("aicutModel", shell)
        self.assertNotIn("aicutPlan", shell)

    def test_both_adapters_state_their_out_point_convention(self):
        """Off by one here is every clip a frame long or a frame short, which
        nobody notices until the export - so each says which way it goes."""
        self.assertIn("OUT_POINT_IS_EXCLUSIVE = true",
                      BUILD_JS.read_text(encoding="utf-8"))
        self.assertIn("END_FRAME_IS_INCLUSIVE = True",
                      (PLUGIN / "common" / "aicut_model.py").read_text(encoding="utf-8"))

    def test_the_shell_says_it_was_never_run(self):
        """Not decoration: it is the difference between a plugin that was
        tested and one that was written from documentation."""
        self.assertIn("NOT VERIFIED HERE", SHELL.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
