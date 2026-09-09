"""DaVinci Resolve Adapter (플러그인 기획안 37장).

    AI Engine -> Common Edit Model -> Resolve Adapter -> DaVinci Resolve

This file is the last arrow. It reads the Common Edit Model and calls Resolve;
it decides nothing. Every decision - which spans survive, what order they go
in, how seconds become frames - is in `plugin/common/aicut_model.py`, which has
tests that run without Resolve.

Install by copying `plugin/common` and this file into Resolve's script folder:

    Windows  %APPDATA%\\Blackmagic Design\\DaVinci Resolve\\Support\\Fusion\\Scripts\\Utility
    macOS    ~/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility
    Linux    ~/.local/share/DaVinciResolve/Fusion/Scripts/Utility

Then, with a project open: Workspace > Scripts > aicut_resolve.

Two ways in, and the second is 4장's button:

    aicut_resolve <model.json>   build from a model already on disk
    aicut_resolve                ask the engine about the clip in this timeline,
                                 have it analyse, then build

NOT VERIFIED HERE. Resolve is not present in the environment this was written
in, so these API calls are written against Blackmagic's scripting
documentation and have not been executed. The arithmetic they depend on has
been, and so has the engine call. Treat the first run as the test.
"""

import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
for _path in (_HERE, os.path.join(os.path.dirname(_HERE), "common")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from aicut_engine import Engine, EngineError, failure_reason  # noqa: E402
from aicut_model import (                               # noqa: E402
    ModelError,
    audio_clips,
    dropped_spans,
    frame_ranges,
    load,
    media_path,
    sequences,
    subtitles,
    summary,
    timeline_name,
    validated,
)


def get_resolve():
    """Resolve injects `resolve` into scripts it runs; fall back to the module."""
    injected = globals().get("resolve")
    if injected is not None:
        return injected
    try:
        import DaVinciResolveScript as bmd
    except ImportError:
        raise ModelError(
            "this script must be run from inside DaVinci Resolve "
            "(Workspace > Scripts), where its scripting module is on the path"
        )
    found = bmd.scriptapp("Resolve")
    if found is None:
        raise ModelError("Resolve is not running, or scripting is disabled in its preferences")
    return found


# -- 36장 2번 Project Reader / 3번 Media Reader -----------------------------
def current_project(resolve):
    project = resolve.GetProjectManager().GetCurrentProject()
    if project is None:
        raise ModelError("open a project in Resolve first")
    return project


def timeline_fps(project):
    """The project's frame rate, which the timeline will be built at.

    Asking the model instead would build a timeline whose frames do not line up
    with the project's, and every cut would land a fraction late.
    """
    value = project.GetSetting("timelineFrameRate")
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ModelError(
            "could not read the project's frame rate (got {!r}). Set it in "
            "Project Settings first.".format(value)
        )


def source_in_timeline(project):
    """The file the operator put on the current timeline (4장 step 2-3).

    4장 has them import the broadcast and drop it on a timeline before pressing
    the button, so that clip is what the engine should analyse. Reported rather
    than guessed at: a timeline with several different files is not one
    broadcast, and picking one of them would analyse something they did not ask
    about.
    """
    timeline = project.GetCurrentTimeline()
    if timeline is None:
        raise ModelError(
            "no timeline is open. 4장: put the broadcast on a timeline first, "
            "then press the button."
        )
    paths = []
    for index in range(1, (timeline.GetTrackCount("video") or 0) + 1):
        for item in timeline.GetItemListInTrack("video", index) or []:
            pool_item = item.GetMediaPoolItem()
            if pool_item is None:
                continue
            path = (pool_item.GetClipProperty("File Path") or "").strip()
            if path and path not in paths:
                paths.append(path)
    if not paths:
        raise ModelError("this timeline has no video clip with a file behind it")
    if len(paths) > 1:
        raise ModelError(
            "this timeline holds {} different files. Put the one broadcast on a "
            "timeline of its own, or run the script with a model file.".format(len(paths))
        )
    return paths[0]


# -- 36장 4~8번 the controllers ---------------------------------------------
def import_source(resolve, project, path):
    """Put the broadcast in the media pool, or find it if it is already there."""
    media_pool = project.GetMediaPool()
    root = media_pool.GetRootFolder()
    wanted = os.path.basename(path)
    for item in root.GetClipList() or []:
        if item.GetName() == wanted:
            return item

    if not os.path.exists(path):
        raise ModelError(
            "the model's source is not at {}. Move it back, or re-run the "
            "analysis against its new location.".format(path)
        )
    added = resolve.GetMediaStorage().AddItemListToMediaPool([path])
    if not added:
        raise ModelError("Resolve would not import {}".format(path))
    return added[0]


def build_sequence(resolve, project, model, sequence, mode="new_sequence"):
    """Lay one sequence of the model onto a Resolve timeline."""
    fps = timeline_fps(project)
    item = import_source(resolve, project, media_path(model, "source"))

    entries = []
    for start_frame, end_frame, _clip in frame_ranges(sequence, fps):
        entries.append({"mediaPoolItem": item,
                        "startFrame": start_frame,
                        "endFrame": end_frame})

    media_pool = project.GetMediaPool()
    if mode == "edit_current":
        # 25장 mode B: only when the person asked for it by name.
        timeline = project.GetCurrentTimeline()
        if timeline is None:
            raise ModelError("edit_current needs a timeline open to edit")
        if not media_pool.AppendToTimeline(entries):
            raise ModelError("Resolve refused to append the clips to this timeline")
    else:
        timeline = media_pool.CreateTimelineFromClips(
            timeline_name(model, sequence), entries,
        )
        if not timeline:
            raise ModelError(
                "Resolve refused to build the timeline. The usual cause is a clip "
                "whose frame rate differs from the project's."
            )

    print(summary(model, sequence, fps))
    for start, end in dropped_spans(sequence, fps):
        print("  skipped {:.3f}-{:.3f}s: shorter than one frame at {} fps".format(
            start, end, fps))

    placed = audio_clips(sequence)
    if placed:
        # 24장 puts BGM and 효과음 on their own tracks. Resolve's scripting API
        # has no call that places an arbitrary file at an arbitrary time, so
        # this says what is missing rather than leaving a silent gap.
        print("  {} audio placement(s) the model asks for are not applied:".format(len(placed)))
        for track_name, clip in placed:
            print("    {} {} at {:.2f}s".format(track_name, clip.get("path", ""),
                                                clip.get("timeline_position_sec", 0.0)))

    lines = subtitles(sequence)
    if lines:
        print("  {} caption(s) in the model; import an .srt for them "
              "(`aicut export <plan> --format srt`)".format(len(lines)))
    return timeline


def build_from_file(model_path, mode="new_sequence"):
    """Build from a Common Edit Model already written to disk."""
    resolve = get_resolve()
    project = current_project(resolve)
    model = load(model_path)
    built = []
    for sequence in sequences(model):
        built.append(build_sequence(resolve, project, model, sequence,
                                    mode=model.get("mode", mode)))
    return built


# -- 36장 9번 AI Engine Connector + 4장's button ----------------------------
def build_from_engine(engine=None, mode="new_sequence", poll_sec=3.0, on_progress=None):
    """4장's button: analyse what is on this timeline, then build from it.

    The plugin analyses nothing (35장). It hands the engine the file the
    operator already put on the timeline, waits, and lays out what comes back.
    """
    resolve = get_resolve()
    project = current_project(resolve)
    source = source_in_timeline(project)
    engine = engine or Engine()

    say = on_progress or (lambda line: print("  " + line))
    say("analysing {}".format(os.path.basename(source)))
    job = engine.submit(source)
    job_id = job.get("job_id") or job.get("id")
    project_id = job.get("project_id") or ""
    if not job_id:
        raise EngineError("the engine did not return a job id: {!r}".format(job))

    # 26장's progress panel is this loop's output. The engine names its own
    # stages; repeating them is the panel, and inventing stage names here would
    # describe a pipeline that is not the one running.
    last = ""
    while True:
        state = engine.job(job_id)
        line = state.get("state") or state.get("status") or ""
        if line and line != last:
            say(line)
            last = line
        if not state.get("running", False):
            break
        time.sleep(poll_sec)
    failed = failure_reason(state)
    if failed:
        raise EngineError("the analysis failed: {}".format(failed))
    project_id = state.get("project_id") or project_id
    if not project_id:
        raise EngineError("the engine did not say which project it made")

    episodes = engine.episodes(project_id)
    if not episodes:
        # 16장: 제작 가치 있는 콘텐츠 없음 is a normal ending, not a failure - and
        # by here it is the only thing an empty list can mean, because a failure
        # was raised above.
        say("the engine found nothing worth producing in this broadcast (16장)")
        return []

    built = []
    for episode in episodes:
        model = validated(engine.edit_model(episode["episode_id"], mode=mode))
        for sequence in sequences(model):
            built.append(build_sequence(resolve, project, model, sequence, mode=mode))
    return built


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        if argv:
            build_from_file(argv[0])
        else:
            path = _ask_for_model()
            if path:
                build_from_file(path)
            else:
                build_from_engine()
    except (ModelError, EngineError) as exc:
        print("aicut: {}".format(exc))
        return 1
    return 0


def _ask_for_model():
    """Resolve runs scripts with no arguments from its menu, so ask there.

    Cancelling the dialog is how the operator says "just do it" - the button of
    4장 - so it falls through to the engine rather than stopping.
    """
    try:
        fusion = get_resolve().Fusion()
        dialog = fusion.RequestFile("", "", {"FReqB_SeqGather": False,
                                             "FReqS_Title": "aicut edit model (.json) - "
                                                            "cancel to analyse this timeline"})
        return dialog if isinstance(dialog, str) else None
    except Exception as exc:                       # pragma: no cover - UI path
        print("aicut: could not open a file dialog ({}); "
              "analysing what is on this timeline".format(exc))
        return None


if __name__ == "__main__":
    raise SystemExit(main())
