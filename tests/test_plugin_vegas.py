"""The VEGAS adapter, tested without VEGAS.

VEGAS Pro is Windows-only and is not in the environment this was written in, so
the API calls cannot be run here. What can: the arithmetic, through node against
the real module, cross-checked against the Python reader that Resolve uses - and
the bundle, because VEGAS compiles one file at a time and a script assembled
wrongly does not run at all.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from aicut.models import Cut
from aicut.render.editmodel import from_edit_plan
from aicut.render.editplan import EditPlan

PLUGIN = Path(__file__).resolve().parent.parent / "plugin"
VEGAS = PLUGIN / "vegas"
MODEL_JS = PLUGIN / "common" / "aicut_model.js"
TIME_JS = VEGAS / "aicut_vegas_time.js"
BODY_JS = VEGAS / "aicut_vegas_body.js"
BUNDLE_PY = VEGAS / "bundle.py"
BUNDLED = VEGAS / "aicut_vegas.js"
NODE = shutil.which("node")


def _model(cuts, fps=30, duration=7200.0, **kw):
    plan = EditPlan(episode_id="ep123456", project_id="p",
                    source_path="/broadcasts/stream.mkv", cuts=cuts,
                    render_settings={"fps": fps}, **kw)
    return json.loads(json.dumps(
        from_edit_plan(plan, source_duration_sec=duration, fps=fps).as_dict()
    ))


@unittest.skipIf(NODE is None, "node is needed to run the plugin's own code")
class TheArithmeticTests(unittest.TestCase):
    """VEGAS states an event as start, length and the take's offset into the
    source. There is no end frame, so the off-by-one Resolve and Premiere have
    to declare does not arise - but the frame grid still does."""

    def run_js(self, body: str, **names):
        script = (
            "var aicutModel = require({model});\n"
            "var aicutVegasTime = require({time});\n"
            "var input = JSON.parse(process.argv[1]);\n"
            "var result;\n"
            "try {{ result = (function () {{ {body} }}()); }}\n"
            "catch (e) {{ result = {{error: e.message || String(e)}}; }}\n"
            "process.stdout.write(JSON.stringify(result));\n"
        ).format(model=json.dumps(str(MODEL_JS)), time=json.dumps(str(TIME_JS)),
                 body=body)
        done = subprocess.run([NODE, "-e", script, json.dumps(names)],
                              capture_output=True, text=True, encoding="utf-8",
                              timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def test_the_events_match_what_the_python_reader_says_survives(self):
        """Resolve reads the Python one and VEGAS this one; they are one rule."""
        from plugin.common.aicut_model import frame_ranges as python_frame_ranges

        model = _model([
            Cut(sequence_order=1, source_start_sec=100.0, source_end_sec=110.0),
            Cut(sequence_order=2, source_start_sec=300.0, source_end_sec=320.0,
                remove_spans=[[305.0, 307.0]]),
        ])
        sequence = model["sequences"][0]
        events = self.run_js("return aicutVegasTime.eventList(input.sequence, 30);",
                             sequence=sequence)
        resolve = python_frame_ranges(sequence, 30)

        self.assertEqual([e["offsetFrames"] for e in events],
                         [start for start, _end, _clip in resolve])
        self.assertEqual([e["lengthFrames"] for e in events],
                         [end - start + 1 for start, end, _clip in resolve])

    def test_the_events_are_laid_end_to_end_in_frames(self):
        """A gap between events is black frames, and the model asked for a cut."""
        model = _model([Cut(sequence_order=1, source_start_sec=0.0, source_end_sec=2.0),
                        Cut(sequence_order=2, source_start_sec=50.0, source_end_sec=53.0)])
        events = self.run_js("return aicutVegasTime.eventList(input.sequence, 30);",
                             sequence=model["sequences"][0])
        self.assertEqual([e["startFrames"] for e in events], [0, 60])
        self.assertEqual([e["lengthFrames"] for e in events], [60, 90])

    def test_timeline_order_is_kept_not_source_order(self):
        """2.4: a video may open on the moment that happened last."""
        model = _model([Cut(sequence_order=1, source_start_sec=500.0, source_end_sec=505.0),
                        Cut(sequence_order=2, source_start_sec=10.0, source_end_sec=15.0)])
        events = self.run_js("return aicutVegasTime.eventList(input.sequence, 30);",
                             sequence=model["sequences"][0])
        self.assertEqual([e["offsetFrames"] for e in events], [15000, 300])

    def test_a_removal_inside_a_cut_becomes_two_events(self):
        model = _model([Cut(sequence_order=1, source_start_sec=0.0, source_end_sec=10.0,
                            remove_spans=[[4.0, 6.0]])])
        events = self.run_js("return aicutVegasTime.eventList(input.sequence, 30);",
                             sequence=model["sequences"][0])
        self.assertEqual(len(events), 2)
        self.assertEqual(events[1]["startFrames"], events[0]["lengthFrames"])

    def test_the_whole_timeline_is_a_whole_number_of_frames(self):
        """VEGAS will take seconds and put an event between two frames; a
        timeline full of sub-frame offsets is a drift nobody can find."""
        model = _model([Cut(sequence_order=1, source_start_sec=1.017, source_end_sec=2.017),
                        Cut(sequence_order=2, source_start_sec=9.983, source_end_sec=11.5)],
                       fps=23.976)
        total = self.run_js("return aicutVegasTime.totalFrames(input.sequence, 23.976);",
                            sequence=model["sequences"][0])
        self.assertEqual(total, int(total))

    def test_a_span_shorter_than_a_frame_is_dropped_and_reported(self):
        model = _model([Cut(sequence_order=1, source_start_sec=0.0, source_end_sec=1.0),
                        Cut(sequence_order=2, source_start_sec=5.0, source_end_sec=5.001)])
        sequence = model["sequences"][0]
        events = self.run_js("return aicutVegasTime.eventList(input.sequence, 30);",
                             sequence=sequence)
        dropped = self.run_js("return aicutModel.droppedSpans(input.sequence, 30);",
                              sequence=sequence)
        self.assertEqual(len(events), 1)
        self.assertEqual(len(dropped), 1)

    def test_a_sequence_of_nothing_but_slivers_is_an_error_not_an_empty_project(self):
        model = _model([Cut(sequence_order=1, source_start_sec=5.0, source_end_sec=5.001)])
        result = self.run_js("return aicutVegasTime.eventList(input.sequence, 30);",
                             sequence=model["sequences"][0])
        self.assertIn("shorter than one frame", result["error"])


class TheBundleTests(unittest.TestCase):
    """VEGAS compiles one file at a time, so the script is built, not copied.

    A bundle assembled wrongly does not run at all: the failure is a dialog with
    a line number that means nothing to the person reading it.
    """

    def _built(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "aicut_vegas.js"
            done = subprocess.run(
                ["python", str(BUNDLE_PY), "--out", str(target)],
                capture_output=True, text=True, encoding="utf-8", timeout=60,
            )
            self.assertEqual(done.returncode, 0, done.stderr)
            return target.read_text(encoding="utf-8")

    def test_it_carries_every_part_the_script_needs(self):
        text = self._built()
        for needle in ("var aicutModel =", "var aicutEngine =", "var aicutVegasTime =",
                       "class EntryPoint"):
            self.assertIn(needle, text)

    def test_node_only_lines_do_not_reach_vegas(self):
        """`module` and `require` are node's. VEGAS would stop at the first one
        before a line of the script ran."""
        text = self._built()
        self.assertNotIn("module.exports", text)
        self.assertNotIn("require(", text)

    def test_the_imports_come_before_anything_else(self):
        """JScript.NET wants them first, and the body they belong to is
        concatenated last."""
        text = self._built()
        lines = [line for line in text.splitlines()
                 if line.strip() and not line.strip().startswith(("*", "/*", "//"))]
        imports = [i for i, line in enumerate(lines) if line.startswith("import ")]
        self.assertTrue(imports)
        self.assertEqual(imports, list(range(imports[0], imports[0] + len(imports))))
        self.assertEqual(imports[0], 0)

    def test_the_committed_script_is_the_one_the_parts_build(self):
        """A generated file in the repository goes stale silently; this is what
        stops it being installed after the modules moved on."""
        self.assertEqual(BUNDLED.read_text(encoding="utf-8"), self._built())

    def test_it_is_the_same_file_wherever_it_was_built(self):
        """windows-latest built `plugin\\common\\aicut_model.js` into the header
        and CRLF into every line, so the file the builder wrote depended on the
        machine that ran it.

        The line endings are checked on what the builder writes, not on the
        checked-out copy: git converts those on Windows by itself, and that is
        the checkout's business rather than the builder's.
        """
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "aicut_vegas.js"
            done = subprocess.run(
                ["python", str(BUNDLE_PY), "--out", str(target)],
                capture_output=True, text=True, encoding="utf-8", timeout=60,
            )
            self.assertEqual(done.returncode, 0, done.stderr)
            with open(target, encoding="utf-8", newline="") as handle:
                text = handle.read()
        self.assertNotIn("\r\n", text)
        header = text[:text.index("*/")]
        self.assertIn("plugin/common/aicut_model.js", header)
        self.assertNotIn("\\", header)

    def test_it_says_it_is_generated(self):
        self.assertIn("GENERATED - do not edit", BUNDLED.read_text(encoding="utf-8"))

    def test_the_shared_modules_still_work_under_node_after_stripping(self):
        """The strip removes node's own lines; it must not touch anything else."""
        if NODE is None:
            self.skipTest("node is needed for this")
        import sys

        sys.path.insert(0, str(VEGAS))
        from bundle import strip_node                       # noqa: E402

        stripped = strip_node(MODEL_JS.read_text(encoding="utf-8"), str(MODEL_JS))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "m.js"
            path.write_text(stripped + "\nmodule.exports = aicutModel;\n",
                            encoding="utf-8")
            done = subprocess.run(
                [NODE, "-e",
                 "var m = require({});"
                 "process.stdout.write(String(typeof m.frameRanges));".format(
                     json.dumps(str(path)))],
                capture_output=True, text=True, encoding="utf-8", timeout=60,
            )
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout, "function")


class HonestyTests(unittest.TestCase):
    def test_the_body_reads_the_model_rather_than_reimplementing_it(self):
        """37장: one meaning of the model, however many editors read it."""
        body = BODY_JS.read_text(encoding="utf-8")
        self.assertIn("aicutModel.", body)
        self.assertIn("aicutVegasTime.eventList", body)
        self.assertNotIn("remove_spans", body)

    def test_a_failed_run_is_not_read_as_finding_nothing(self):
        """The same rule the other three adapters use."""
        body = BODY_JS.read_text(encoding="utf-8")
        self.assertIn("aicutEngine.failureReason(state)", body)

    def test_it_says_it_was_never_run(self):
        self.assertIn("NOT VERIFIED IN VEGAS", BODY_JS.read_text(encoding="utf-8"))

    def test_the_arithmetic_does_not_need_vegas(self):
        """This is the half the tests above exercise."""
        code = TIME_JS.read_text(encoding="utf-8")
        for name in ("ScriptPortal", "new VideoEvent(", "vegas.Project"):
            self.assertNotIn(name, code)


if __name__ == "__main__":
    unittest.main()
