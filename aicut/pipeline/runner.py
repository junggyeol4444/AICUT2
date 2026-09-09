"""The pipeline runner (14장).

Drives QUEUED -> PARSING -> UNDERSTANDING -> DISCOVERING -> EVALUATING ->
PLANNING -> RENDERING -> PACKAGED -> REVIEW_PENDING and stops there. PUBLISHED is
never reached by a run: a person has to pass the gate first (11.3).

The run can also stop earlier on purpose. NO_CONTENT means the broadcast held
nothing worth producing, which is a successful outcome and is reported as one
(1.3, 16장). And every stage boundary is a resume point, so a failure costs the
stage, not the work before it (16장).
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any

from aicut.config import CalibrationProfile
from aicut.db.store import Store
from aicut.errors import AicutError, PipelineError
from aicut.llm import Producer, get_producer
from aicut.media.stt import Transcriber
from aicut.models import Episode, Project
from aicut.pipeline import discovery, evaluating, packaging, parsing, planning, rendering, review, understanding
from aicut.pipeline.context import RunContext
from aicut.pipeline.states import State

log = logging.getLogger(__name__)


@dataclass
class RunResult:
    """What a run produced, and why - the work report of 15.5 / 22.6."""

    project_id: str
    final_state: State
    episodes: list[Episode] = field(default_factory=list)
    report: dict[str, Any] = field(default_factory=dict)
    review_items: list[review.ReviewItem] = field(default_factory=list)

    @property
    def produced_nothing(self) -> bool:
        return self.final_state is State.NO_CONTENT


class Pipeline:
    def __init__(
        self,
        store: Store,
        profile: CalibrationProfile,
        producer: Producer | None = None,
        *,
        workspace: str | Path = "workspace",
        knowledge: dict[str, Any] | None = None,
    ):
        self.store = store
        self.profile = profile
        self.producer = producer or get_producer("mock")
        self.workspace = Path(workspace)
        self.knowledge = knowledge or {}

    # ---- entry points ------------------------------------------------------
    def submit(
        self,
        file_path: str,
        *,
        length_hint_sec: float | None = None,
        channel_ref: str = "",
        profile_id: str = "",
    ) -> Project:
        project = Project(
            # Absolute: the edit plan carries this path and is read back later,
            # possibly from another directory (16장 re-runs the render alone).
            # A relative path would resolve against whatever the working
            # directory happened to be then.
            file_path=str(Path(file_path).expanduser().resolve()),
            status=State.QUEUED.value,
            profile_name=self.profile.name,
            # Which stored row, not just which name (17장). Names repeat across
            # recalibrations of the same channel; ids do not.
            profile_id=profile_id,
            channel_ref=channel_ref,
            length_hint_sec=length_hint_sec,
        )
        return self.store.create_project(project)

    def run(
        self,
        project: Project,
        *,
        transcriber: Transcriber | None = None,
        stop_after: State | None = None,
        sample_frames: bool = False,
        render: bool = True,
        context: RunContext | None = None,
        resume: bool = False,
    ) -> RunResult:
        started = time.time()
        ctx = context or RunContext(
            project=project,
            store=self.store,
            profile=self.profile,
            producer=self.producer,
            workspace=self.workspace,
        )
        ctx.note("started_at", time.strftime("%Y-%m-%dT%H:%M:%S"))

        try:
            self._advance(ctx, State.PARSING)
            parsing.run(ctx, transcriber)
            if stop_after is State.PARSING:
                return self._finish(ctx, State.PARSING, [], started)

            self._advance(ctx, State.UNDERSTANDING)
            # A supplied transcriber means parsing has just replaced the stored
            # utterances, so the window summaries and the event graph describe
            # speech that is no longer there. Reusing them left discovery
            # reading the old events while every later stage read the new
            # utterances - one plan built from two different broadcasts.
            replaced_speech = resume and transcriber is not None
            if replaced_speech and self._understanding_done(ctx):
                log.info("a transcript was supplied on resume; re-reading the broadcast")
                ctx.note("resume_note",
                         "the transcript was replaced, so the stored window summaries and "
                         "event graph were rebuilt rather than reused")
            if resume and not replaced_speech and self._understanding_done(ctx):
                # The first pass is the expensive half of a run: one reasoning
                # call per window, 180 of them on a six-hour broadcast. Paying
                # for it again because a later stage failed would make 16장's
                # stage separation meaningless.
                log.info(
                    "resuming: reusing %d window summaries and %d events already stored",
                    len(ctx.store.windows(ctx.project.project_id)),
                    len(ctx.store.events(ctx.project.project_id)),
                )
                ctx.note("resumed_from", State.UNDERSTANDING.value)
            else:
                understanding.run(ctx, sample_frames=sample_frames)
            if stop_after is State.UNDERSTANDING:
                return self._finish(ctx, State.UNDERSTANDING, [], started)

            self._advance(ctx, State.DISCOVERING)
            candidates = discovery.run(ctx)
            if not candidates:
                return self._no_content(ctx, "no content candidate was found in this broadcast", started)
            if stop_after is State.DISCOVERING:
                return self._finish(ctx, State.DISCOVERING, [], started)

            self._advance(ctx, State.EVALUATING)
            keepers = evaluating.run(ctx, candidates)
            if not keepers:
                return self._no_content(ctx, "candidates were found but none was worth producing", started)
            groups = evaluating.group_for_production(keepers)
            if not groups:
                return self._no_content(ctx, "every remaining candidate needed a partner it never found", started)
            if stop_after is State.EVALUATING:
                return self._finish(ctx, State.EVALUATING, [], started)

            self._advance(ctx, State.PLANNING)
            episodes = planning.run(ctx, groups, knowledge=self.knowledge)
            if not episodes:
                return self._no_content(ctx, "no episode survived scene retrieval", started)
            if stop_after is State.PLANNING or not render:
                return self._finish(ctx, State.PLANNING, episodes, started)

            self._advance(ctx, State.RENDERING)
            rendered = rendering.run(ctx, episodes)
            if not rendered:
                raise PipelineError("every render failed; the edit plans are kept for a retry (16장)")

            self._advance(ctx, State.PACKAGED)
            packaging.run(ctx, rendered, knowledge=self.knowledge)

            self._advance(ctx, State.REVIEW_PENDING)
            items = review.pending(ctx, rendered)
            result = self._finish(ctx, State.REVIEW_PENDING, rendered, started)
            result.review_items = items
            return result

        except Exception as exc:
            # An AicutError is a condition this program recognised and wrote a
            # sentence about - a missing backend, an unusable source. Printing a
            # stack trace above that sentence buries the instruction in noise.
            # Anything else is a surprise, and the traceback is the point.
            if isinstance(exc, AicutError):
                log.error("project %s failed: %s", project.project_id, exc)
            else:
                log.exception("project %s failed", project.project_id)
            self.store.set_status(project.project_id, State.FAILED.value, str(exc))
            ctx.note("error", str(exc))
            return self._finish(ctx, State.FAILED, [], started, record_state=False)

    # ---- helpers -----------------------------------------------------------
    def _understanding_done(self, ctx: RunContext) -> bool:
        """Is there a usable event graph already stored for this project?"""
        return bool(ctx.store.windows(ctx.project.project_id)) and bool(
            ctx.store.events(ctx.project.project_id)
        )

    def resume(self, project_id: str, **kwargs: Any) -> RunResult:
        """Continue a project that stopped part way (16장).

        Understanding is reused when it is already stored; everything after it
        is decided again, because those stages are cheap and their inputs may
        have changed - a re-tuned profile, a corrected human verdict.
        """
        project = self.store.get_project(project_id)
        if project is None:
            raise PipelineError(f"unknown project {project_id}")
        kwargs.setdefault("resume", True)
        return self.run(project, **kwargs)

    def _advance(self, ctx: RunContext, state: State) -> None:
        self._close_stage(ctx)
        ctx.stage_open = (state.value, time.time())
        self.store.set_status(ctx.project.project_id, state.value)
        ctx.project.status = state.value
        log.info("project %s -> %s", ctx.project.project_id, state.value)

    def _close_stage(self, ctx: RunContext) -> None:
        """Stop the clock on the stage that was running.

        Every transition goes through `_advance` and every ending through
        `_finish`, so those two are the whole of it. R3: 1차 통과 밀도가 비용과
        시간을 결정하는 핵심 변수다 - and that cost is invisible in a total.
        """
        if ctx.stage_open is None:
            return
        name, since = ctx.stage_open
        seconds = ctx.report.setdefault("stage_seconds", {})
        seconds[name] = round(seconds.get(name, 0.0) + (time.time() - since), 1)
        ctx.stage_open = None

    def _no_content(self, ctx: RunContext, reason: str, started: float) -> RunResult:
        """A normal ending, not a failure (16장)."""
        self.store.set_status(ctx.project.project_id, State.NO_CONTENT.value, reason)
        ctx.note("no_content_reason", reason)
        return self._finish(ctx, State.NO_CONTENT, [], started, record_state=False)

    def _finish(
        self,
        ctx: RunContext,
        state: State,
        episodes: list[Episode],
        started: float,
        *,
        record_state: bool = True,
    ) -> RunResult:
        if record_state:
            ctx.project.status = state.value
        self._close_stage(ctx)
        ctx.note("elapsed_sec", round(time.time() - started, 1))
        # Report on the profile and producer that actually ran this context - a
        # resumed run may have been handed different ones than the pipeline holds.
        ctx.note("provisional_parameters_used", ctx.profile.touched_provisional())
        ctx.note("producer", ctx.producer.name)
        ctx.note("profile", ctx.profile.name)
        report = build_report(ctx, state, episodes)
        (ctx.project_dir / "report.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return RunResult(
            project_id=ctx.project.project_id,
            final_state=state,
            episodes=episodes,
            report=report,
        )


#: Written during a run but folded into another field of the report rather than
#: carried through under their own name - `signals`, `episodes`, counters that
#: only feed a log line. Everything NOT listed here must appear in the report;
#: `tests/test_consistency.py` enforces it, because a section that is recorded
#: and then dropped is worse than one that was never recorded: the code that
#: wrote it looks correct, and its test passes against the context object while
#: the operator never sees a word.
#: The stages `Pipeline.run` actually checks `stop_after` against. The CLI
#: offered every State, so `--stop-after RENDERING` ran through packaging and
#: review anyway, and terminal values like FAILED or PUBLISHED could never be
#: honoured at all — the flag was accepted and then quietly meant nothing.
#: `tests/test_consistency.py` pins this against the checks in `run`.
STOP_AFTER_STAGES = (
    State.PARSING,
    State.UNDERSTANDING,
    State.DISCOVERING,
    State.EVALUATING,
    State.PLANNING,
)


REPORT_INTERNAL_KEYS = frozenset({
    "boundary_hints", "candidates_found", "discovery_note", "edit_plans",
    "episodes_packaged", "episodes_planned", "episodes_rendered", "events",
    "first_pass_windows", "media", "second_pass_windows", "situation_mix",
    "speaker_reliability", "started_at", "upload_queue", "utterance_count",
    "vocal_bursts",
})


def build_report(ctx: RunContext, state: State, episodes: list[Episode]) -> dict[str, Any]:
    """The work report of 22.6: what was found, what was made, what was refused."""
    candidates = ctx.store.candidates(ctx.project.project_id)
    return {
        "project_id": ctx.project.project_id,
        "source": ctx.project.file_path,
        "final_state": state.value,
        "candidates_found": len(candidates),
        "decisions": ctx.report.get("decisions", {}),
        "rejections": ctx.report.get("rejections", []),
        "episodes": [
            {
                "episode_id": e.episode_id,
                "target_type": e.target_type,
                "structure": e.planned_structure.get("structure_name", ""),
                "rationale": e.planned_structure.get("rationale", ""),
                "duration_sec": round(e.planned_duration_sec, 1),
                "cuts": len(e.timeline),
                "output": e.output_mp4_path,
                "titles": e.title_candidates,
                "notes": e.notes,
            }
            for e in episodes
        ],
        "signals": {
            "utterances": ctx.report.get("utterance_count"),
            "first_pass_windows": ctx.report.get("first_pass_windows"),
            "second_pass_windows": ctx.report.get("second_pass_windows"),
            "events": ctx.report.get("events"),
            "situation_mix": ctx.report.get("situation_mix", {}),
            "speaker_reliability": ctx.report.get("speaker_reliability"),
        },
        # Every "<signal>_note" a stage wrote, carried without being named
        # one by one. These are the disclosures that matter most - no face
        # signal, no burst detector, untagged speech - and naming them
        # individually is how three of them came to be dropped. A new one now
        # reaches the operator without anybody remembering to add it here.
        "signal_notes": [
            value for key, value in sorted(ctx.report.items())
            if key.endswith("_note") and value
        ],
        "length_deviations": ctx.report.get("length_deviations", []),
        "implausible_plans": ctx.report.get("implausible_plans", []),
        "degraded": ctx.report.get("degraded", []),
        "scan_density": ctx.report.get("scan_density", []),
        "subtitles_dropped": ctx.report.get("subtitles_dropped", []),
        "repeated_spans": ctx.report.get("repeated_spans", []),
        # A refined span the provider returned outside the scene it had just
        # chosen by index (8.1). Corrected before use, and said here, because
        # a provider inventing timestamps is worth seeing.
        "out_of_scene_bounds": ctx.report.get("out_of_scene_bounds", []),
        # The reason a FAILED run failed. This was listed as internal - as
        # though some other field carried it - and nothing did, so report.json
        # and the UI showed FAILED with no cause, and the calibration harness
        # printed "could not process <source>: None".
        "error": ctx.report.get("error"),
        "render_failures": ctx.report.get("render_failures", []),
        "no_content_reason": ctx.report.get("no_content_reason"),
        "resumed_from": ctx.report.get("resumed_from"),
        "source_warnings": ctx.report.get("source_warnings", []),
        # Measured signals thrown away rather than reused, and why. A resumed
        # run under a re-tuned profile has to decode the media again (17.1), and
        # the report is where the extra hours are accounted for.
        "cache_invalidated": ctx.report.get("cache_invalidated", []),
        "elapsed_sec": ctx.report.get("elapsed_sec"),
        # 22.6 asks the work report for 처리 시간, and R3 makes it an open risk
        # to be measured rather than estimated. Per stage, because that is the
        # number that says whether a six-hour source is usable.
        "stage_seconds": ctx.report.get("stage_seconds", {}),
        # Which audio tracks were actually listened to (5.2). On a multitrack
        # recording this is how an operator sees that the guest's call track was
        # read rather than silently skipped.
        "speech_tracks": ctx.report.get("speech_tracks", []),
        "profile": ctx.report.get("profile"),
        "producer": ctx.report.get("producer"),
        # 17.4 step 4: the setup this broadcast was recorded in, and how it
        # differs from the one the profile was measured in. Drift is reported,
        # never corrected - re-measuring needs the 17.2 dataset.
        "environment": ctx.report.get("environment", {}),
        "environment_drift": ctx.report.get("environment_drift", []),
        # A judgement threshold this broadcast never crossed. The label it
        # gates was therefore impossible for the whole run, and nothing else
        # would have said so (17.4, 17.5).
        "threshold_never_reached": ctx.report.get("threshold_never_reached", []),
        # 11.2's package as written, measured against what YouTube will accept
        # and against the payload the model was given. Reported, never fixed.
        "packaging_warnings": ctx.report.get("packaging_warnings", {}),
        # A beat the plan asked for that the video will not contain, and a
        # candidate that was decided PRODUCE and then found no scene at all.
        "beats_unfilled": ctx.report.get("beats_unfilled", []),
        "episodes_not_produced": ctx.report.get("episodes_not_produced", []),
        # 6.2: cuts in a finished episode that carry none of its own events -
        # the 짜깁기 1.2 names, measured on the result rather than assumed away.
        "cuts_off_event": ctx.report.get("cuts_off_event", []),
        # Which of 12.1's nine a performance collection did not get. Empty on a
        # normal run - loop C is its own command - but the field exists so a
        # report never silently omits it.
        "performance_missing_metrics": ctx.report.get("performance_missing_metrics", {}),
        "provisional_parameters_used": ctx.report.get("provisional_parameters_used", []),
        "warning": "; ".join(part for part in (
            (
                "some judgement thresholds are still unmeasured guesses (17.5); "
                "run the calibration sweep before trusting these results"
                if ctx.report.get("provisional_parameters_used") else ""
            ),
            (
                "this broadcast's setup differs from the one the profile was "
                "measured in, so 17.4 step 4 asks for a re-measurement"
                if ctx.report.get("environment_drift") else ""
            ),
            (
                "a judgement threshold was never reached in this broadcast, so the "
                "label it gates could not occur - see threshold_never_reached"
                if ctx.report.get("threshold_never_reached") else ""
            ),
            (
                "the package of an episode would not survive contact with YouTube "
                "as written - see packaging_warnings"
                if ctx.report.get("packaging_warnings") else ""
            ),
            (
                "a candidate decided PRODUCE found no scene and was not made - "
                "see episodes_not_produced"
                if ctx.report.get("episodes_not_produced") else ""
            ),
            (
                "an episode contains cuts carrying none of its own events, which "
                "is the 짜깁기 6.2 forbids - see cuts_off_event"
                if ctx.report.get("cuts_off_event") else ""
            ),
        ) if part),
    }
