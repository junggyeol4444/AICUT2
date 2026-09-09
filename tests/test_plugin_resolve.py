"""The DaVinci Resolve Adapter (플러그인 기획안 37장).

    AI Engine -> Common Edit Model -> Resolve Adapter -> DaVinci Resolve

Resolve is not installed here, so the adapter is driven against a stand-in that
records the calls it receives. That does not prove Resolve accepts them - only
a machine with Resolve can - but it does prove the adapter asks for what the
model says, in the order and shape the API documents, and that is where the
mistakes this file can make actually live.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "plugin" / "common"))
sys.path.insert(0, str(_ROOT / "plugin" / "resolve"))

import aicut_model  # noqa: E402
import aicut_resolve  # noqa: E402


# --------------------------------------------------------------------------
# a Resolve that records rather than edits
# --------------------------------------------------------------------------
class FakePoolItem:
    def __init__(self, name, path):
        self._name = name
        self._path = path

    def GetName(self):
        return self._name

    def GetClipProperty(self, key):
        return self._path if key == "File Path" else ""


class FakeTimelineItem:
    def __init__(self, pool_item):
        self._pool_item = pool_item

    def GetMediaPoolItem(self):
        return self._pool_item


class FakeTimeline:
    def __init__(self, name="Live_01", items=()):
        self.name = name
        self._items = list(items)

    def GetTrackCount(self, kind):
        return 1 if kind == "video" else 0

    def GetItemListInTrack(self, kind, index):
        return list(self._items) if kind == "video" and index == 1 else []


class FakeMediaPool:
    def __init__(self, root_items=()):
        self._root = list(root_items)
        self.created = []
        self.appended = []

    def GetRootFolder(self):
        pool = self

        class Root:
            def GetClipList(self_inner):
                return list(pool._root)

        return Root()

    def CreateTimelineFromClips(self, name, entries):
        self.created.append((name, entries))
        return FakeTimeline(name=name)

    def AppendToTimeline(self, entries):
        self.appended.append(entries)
        return True


class FakeProject:
    def __init__(self, fps="30", timeline=None, pool=None):
        self._fps = fps
        self._timeline = timeline
        self._pool = pool or FakeMediaPool()

    def GetSetting(self, key):
        return self._fps if key == "timelineFrameRate" else ""

    def GetMediaPool(self):
        return self._pool

    def GetCurrentTimeline(self):
        return self._timeline


class FakeResolve:
    def __init__(self, project, storage_paths=()):
        self._project = project
        self.imported = []
        self._storage_paths = list(storage_paths)

    def GetProjectManager(self):
        resolve = self

        class Manager:
            def GetCurrentProject(self_inner):
                return resolve._project

        return Manager()

    def GetMediaStorage(self):
        resolve = self

        class Storage:
            def AddItemListToMediaPool(self_inner, paths):
                resolve.imported.extend(paths)
                return [FakePoolItem(Path(p).name, p) for p in paths]

        return Storage()


def _model(**overrides):
    """A model the engine actually produced, not one written for the test."""
    from aicut.render.editmodel import from_edit_plan
    from aicut.render.editplan import EditPlan

    data = {
        "episode_id": "ep-abcdefgh", "project_id": "p1",
        "source_path": "/media/live.mkv", "structure": {}, "render": {},
        "cuts": [
            {"sequence_order": 0, "source_start_sec": 4000.0, "source_end_sec": 4020.0,
             "remove_spans": [[4010.0, 4012.0]]},
            {"sequence_order": 1, "source_start_sec": 100.0, "source_end_sec": 130.0},
        ],
        "subtitles": [],
    }
    data.update(overrides)
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        path = Path(tmp) / "plan.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return from_edit_plan(EditPlan.load(path)).as_dict()


class BuildsWhatTheModelSaysTests(unittest.TestCase):
    def setUp(self):
        # The broadcast is already in the media pool: 4장 has the operator
        # import it before pressing anything.
        self.pool = FakeMediaPool([FakePoolItem("live.mkv", "/media/live.mkv")])
        self.project = FakeProject(pool=self.pool)
        self.resolve = FakeResolve(self.project)
        self.model = _model()

    def _build(self, mode="new_sequence"):
        return aicut_resolve.build_sequence(
            self.resolve, self.project, self.model,
            self.model["sequences"][0], mode=mode,
        )

    def test_the_clips_are_the_spans_that_survive_pacing(self):
        """The 2s removal splits the first cut; the second is whole."""
        self._build()
        _, entries = self.pool.created[0]

        self.assertEqual([(e["startFrame"], e["endFrame"]) for e in entries],
                         [(120000, 120299), (120360, 120599), (3000, 3899)])

    def test_the_order_is_the_timeline_not_the_source(self):
        """2.4: the video opens on the moment that happened last."""
        self._build()
        _, entries = self.pool.created[0]

        self.assertGreater(entries[0]["startFrame"], entries[-1]["startFrame"])

    def test_every_entry_names_the_media_pool_item(self):
        self._build()
        _, entries = self.pool.created[0]

        for entry in entries:
            self.assertIn("mediaPoolItem", entry)

    def test_the_frame_rate_is_the_projects_not_the_models(self):
        """A timeline built at the model's rate slides every cut."""
        self.project._fps = "24"
        self._build()
        _, entries = self.pool.created[0]

        self.assertEqual(entries[0]["startFrame"], int(round(4000.0 * 24)))

    def test_an_unreadable_frame_rate_says_where_to_set_it(self):
        self.project._fps = "not a number"

        with self.assertRaises(aicut_model.ModelError) as refused:
            self._build()

        self.assertIn("Project Settings", str(refused.exception))


class TimelineModeTests(unittest.TestCase):
    """25장: the default leaves what the operator built alone."""

    def setUp(self):
        self.pool = FakeMediaPool([FakePoolItem("live.mkv", "/media/live.mkv")])
        self.timeline = FakeTimeline()
        self.project = FakeProject(timeline=self.timeline, pool=self.pool)
        self.resolve = FakeResolve(self.project)
        self.model = _model()

    def test_the_default_makes_a_new_timeline(self):
        aicut_resolve.build_sequence(self.resolve, self.project, self.model,
                                     self.model["sequences"][0])

        self.assertEqual(len(self.pool.created), 1)
        self.assertEqual(self.pool.appended, [])

    def test_edit_current_appends_to_the_open_one_instead(self):
        aicut_resolve.build_sequence(self.resolve, self.project, self.model,
                                     self.model["sequences"][0], mode="edit_current")

        self.assertEqual(self.pool.created, [])
        self.assertEqual(len(self.pool.appended), 1)

    def test_the_model_carries_the_mode_when_building_from_a_file(self):
        from unittest import mock

        model = _model()
        model["mode"] = "edit_current"
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = Path(tmp) / "model.json"
            path.write_text(json.dumps(model), encoding="utf-8")
            with mock.patch.object(aicut_resolve, "get_resolve", return_value=self.resolve):
                aicut_resolve.build_from_file(str(path))

        self.assertEqual(self.pool.created, [])
        self.assertEqual(len(self.pool.appended), 1)


class ReadingWhatTheOperatorPutOnTheTimelineTests(unittest.TestCase):
    """4장 steps 2-3: they import the broadcast and drop it on a timeline, and
    that clip is what the button analyses."""

    def _project(self, *paths):
        items = [FakeTimelineItem(FakePoolItem(Path(p).name, p)) for p in paths]
        return FakeProject(timeline=FakeTimeline(items=items))

    def test_the_one_file_on_the_timeline_is_the_broadcast(self):
        project = self._project("/media/Live_01.mkv")

        self.assertEqual(aicut_resolve.source_in_timeline(project), "/media/Live_01.mkv")

    def test_several_different_files_is_refused_rather_than_guessed(self):
        """Picking one would analyse something they did not ask about."""
        project = self._project("/media/a.mkv", "/media/b.mkv")

        with self.assertRaises(aicut_model.ModelError) as refused:
            aicut_resolve.source_in_timeline(project)

        self.assertIn("2 different files", str(refused.exception))

    def test_the_same_file_twice_on_the_timeline_is_still_one_broadcast(self):
        project = self._project("/media/a.mkv", "/media/a.mkv")

        self.assertEqual(aicut_resolve.source_in_timeline(project), "/media/a.mkv")

    def test_no_timeline_open_says_what_to_do(self):
        project = FakeProject(timeline=None)

        with self.assertRaises(aicut_model.ModelError) as refused:
            aicut_resolve.source_in_timeline(project)

        self.assertIn("4장", str(refused.exception))


class TheButtonTests(unittest.TestCase):
    """4장: the person presses it and the engine does 5장's whole list."""

    class FakeEngine:
        def __init__(self, episodes, states=None, model=None):
            self._episodes = episodes
            self._states = list(states or [{"running": False, "project_id": "p1"}])
            self._model = model
            self.submitted = []
            self.asked_modes = []

        def submit(self, source, **options):
            self.submitted.append(source)
            return {"job_id": "j1", "project_id": "p1"}

        def job(self, job_id):
            return self._states.pop(0) if len(self._states) > 1 else self._states[0]

        def episodes(self, project_id):
            return list(self._episodes)

        def edit_model(self, episode_id, mode="new_sequence"):
            self.asked_modes.append(mode)
            return self._model

    def setUp(self):
        self.pool = FakeMediaPool([FakePoolItem("live.mkv", "/media/live.mkv")])
        item = FakeTimelineItem(FakePoolItem("Live_01.mkv", "/media/Live_01.mkv"))
        self.project = FakeProject(timeline=FakeTimeline(items=[item]), pool=self.pool)
        self.resolve = FakeResolve(self.project)

    def _run(self, engine, **kwargs):
        from unittest import mock

        with mock.patch.object(aicut_resolve, "get_resolve", return_value=self.resolve):
            return aicut_resolve.build_from_engine(engine=engine, poll_sec=0, **kwargs)

    def test_it_hands_the_engine_the_file_from_the_timeline(self):
        engine = self.FakeEngine([{"episode_id": "e1"}], model=_model())

        self._run(engine, on_progress=lambda line: None)

        self.assertEqual(engine.submitted, ["/media/Live_01.mkv"])

    def test_it_builds_a_timeline_for_every_episode_the_engine_made(self):
        engine = self.FakeEngine(
            [{"episode_id": "e1"}, {"episode_id": "e2"}], model=_model(),
        )

        built = self._run(engine, on_progress=lambda line: None)

        self.assertEqual(len(built), 2)
        self.assertEqual(len(self.pool.created), 2)

    def test_finding_nothing_is_a_normal_ending(self):
        """16장: 제작 가치 있는 콘텐츠 없음 is not a failure."""
        engine = self.FakeEngine([])
        said = []

        built = self._run(engine, on_progress=said.append)

        self.assertEqual(built, [])
        self.assertEqual(self.pool.created, [])
        self.assertTrue(any("16장" in line for line in said))

    def test_a_failed_analysis_is_raised_not_built_around(self):
        engine = self.FakeEngine([], states=[{"running": False, "error": "ffmpeg died"}])

        with self.assertRaises(aicut_resolve.EngineError) as raised:
            self._run(engine, on_progress=lambda line: None)

        self.assertIn("ffmpeg died", str(raised.exception))

    def test_the_persons_mode_reaches_the_engine(self):
        """25장's choice is theirs; the adapter passes it through."""
        engine = self.FakeEngine([{"episode_id": "e1"}], model=_model())

        self._run(engine, mode="edit_current", on_progress=lambda line: None)

        self.assertEqual(engine.asked_modes, ["edit_current"])

    def test_each_stage_the_engine_names_is_reported_once(self):
        """26장's progress panel is this. The engine names its own stages;
        inventing names here would describe a pipeline that is not running."""
        engine = self.FakeEngine(
            [{"episode_id": "e1"}],
            states=[{"running": True, "state": "PARSING"},
                    {"running": True, "state": "PARSING"},
                    {"running": True, "state": "UNDERSTANDING"},
                    {"running": False, "state": "REVIEW_PENDING", "project_id": "p1"}],
            model=_model(),
        )
        said = []

        self._run(engine, on_progress=said.append)

        self.assertEqual([line for line in said if line.isupper()],
                         ["PARSING", "UNDERSTANDING", "REVIEW_PENDING"])


class ItRefusesAnEditPlanTests(unittest.TestCase):
    def test_a_plan_handed_to_the_adapter_is_named_as_such(self):
        """37장 has the adapter read the model; the plan is the other thing."""
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = Path(tmp) / "plan.json"
            path.write_text(json.dumps({"episode_id": "x", "cuts": [{"sequence_order": 0}]}),
                            encoding="utf-8")
            with self.assertRaises(aicut_model.ModelError) as refused:
                aicut_model.load(str(path))

        self.assertIn("edit-model", str(refused.exception))


class ImportingTheSourceTests(unittest.TestCase):
    """36장 3번 Media Reader: put the broadcast in the pool, or find it."""

    def test_a_file_already_in_the_pool_is_not_imported_again(self):
        pool = FakeMediaPool([FakePoolItem("live.mkv", "/media/live.mkv")])
        project = FakeProject(pool=pool)
        resolve = FakeResolve(project)

        aicut_resolve.import_source(resolve, project, "/media/live.mkv")

        self.assertEqual(resolve.imported, [])

    def test_a_file_not_in_the_pool_is_imported(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            source = Path(tmp) / "live.mkv"
            source.write_bytes(b"not really a video")
            pool = FakeMediaPool()
            project = FakeProject(pool=pool)
            resolve = FakeResolve(project)

            aicut_resolve.import_source(resolve, project, str(source))

            self.assertEqual(resolve.imported, [str(source)])

    def test_a_source_that_moved_says_so_rather_than_failing_inside_resolve(self):
        pool = FakeMediaPool()
        project = FakeProject(pool=pool)
        resolve = FakeResolve(project)

        with self.assertRaises(aicut_model.ModelError) as refused:
            aicut_resolve.import_source(resolve, project, "/gone/live.mkv")

        self.assertIn("/gone/live.mkv", str(refused.exception))
