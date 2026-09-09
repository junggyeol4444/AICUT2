"""Command line interface.

Mirrors the operating flow of 15.1 - submit a source, watch the analysis, review
the discovered candidates, review and publish the episodes - and exposes the
learning loops and the calibration procedure as their own commands, because they
are run on their own schedule rather than per broadcast.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from aicut.config import CalibrationProfile
from aicut.db.store import Store
from aicut.errors import AicutError
from aicut.intelligence.knowledge import ProductionKnowledge
from aicut.llm import PRODUCERS, get_producer
from aicut.media.ffmpeg_util import have_ffmpeg
from aicut.media.stt import TranscriptFileTranscriber
from aicut.pipeline.context import RunContext
from aicut.pipeline.runner import Pipeline
from aicut.pipeline.runner import STOP_AFTER_STAGES
from aicut.pipeline.states import State
from aicut.pipeline import review as review_mod
from aicut.render.editplan import EditPlan, describe

DEFAULT_WORKSPACE = Path("workspace")

# The parameters worth sweeping first: the ones 17.3 actually scores, and the
# ones a channel's own material moves most. Override with --grid.
DEFAULT_SWEEP_GRID = {
    "silence.level_db": [-45.0, -40.0, -35.0, -30.0],
    "pacing.keep_score_threshold": [0.3, 0.4, 0.5, 0.6],
    "pacing.keep_max_sec": [2.5, 3.5, 4.5],
    "pacing.cut_min_sec": [2.0, 2.5, 3.5],
    "tension.high": [0.55, 0.62, 0.7],
}


# ---------------------------------------------------------------------------
def _store(args) -> Store:
    return Store(Path(args.workspace) / "aicut.db")


def _producer(args):
    """The reasoning backend these flags name.

    One place, because a provider option honoured by `run` and ignored by
    `learn` is a provider the operator cannot actually point anywhere.
    """
    kwargs = {}
    if getattr(args, "producer", "mock") == "ollama":
        for flag, key in (("ollama_host", "host"), ("ollama_model", "model"),
                          ("ollama_num_ctx", "num_ctx")):
            value = getattr(args, flag, None)
            if value is not None:
                kwargs[key] = value
    producer = get_producer(args.producer, **kwargs)
    check = getattr(producer, "check", None)
    if check is not None:
        # Ask now whether the model is actually there. Without this a six-hour
        # broadcast is parsed and scanned before the first window discovers the
        # server was never started, and the operator waits an hour to be told
        # something a request at second zero could have said.
        state = check()
        print(f"{producer.name}: {state['model']} at {state['host']}"
              + ("" if state.get("takes_images", True)
                 else "  [WARNING] this model does not take images; 5.2 needs one that does"))
    return producer


def _profile(args) -> CalibrationProfile:
    return CalibrationProfile.load(args.profile, strict=getattr(args, "strict", False))


def _pipeline(args) -> Pipeline:
    knowledge_path = Path(args.workspace) / "knowledge.json"
    knowledge = ProductionKnowledge.load(knowledge_path).summary_for_planner()
    return Pipeline(
        _store(args),
        _profile(args),
        _producer(args),
        workspace=Path(args.workspace),
        knowledge=knowledge,
    )


def _context(args, project) -> RunContext:
    store = _store(args)
    return RunContext(
        project=project,
        store=store,
        profile=_project_profile(args, store, project),
        producer=_producer(args),
        workspace=Path(args.workspace),
    )


def _project_profile(args, store, project):
    """The profile this project was analysed under, unless one was asked for.

    `aicut review`, `upload` and `retry` build a context for a project that
    already exists, and they were building it from `--profile` - which is the
    default when nobody passes one. So an episode submitted through the UI under
    a measured channel profile was uploaded under the default one: another
    privacy setting, another language, another category, and a quota ledger
    built from a profile that had nothing to do with it.

    The same resolution the UI does: by id, then by name, then the profile on
    disk. An explicit `--profile` still wins - that is a person saying so.
    """
    asked = getattr(args, "profile", None)
    if asked:
        return _profile(args)
    if getattr(project, "profile_id", ""):
        for row in store.profiles():
            if row["profile_id"] == project.profile_id:
                return CalibrationProfile.from_mapping(row["params"])
    return _profile(args)


def _print(data) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False, default=str))


# ---------------------------------------------------------------------------
def _print_report_warnings(report: dict) -> None:
    """Say out loud what the run decided to tell you.

    Every one of these already went into report.json, and pointing at a file
    is not reporting: 2.6 requires a departure from the plan to be reported,
    16장 requires a failed render to be visible without costing the plan, and a
    source that lies about its length is worth knowing before the operator
    wonders why the last cut is empty. A person who runs one command should
    not have to open a JSON file to learn any of it.
    """
    for warning in report.get("source_warnings", []):
        print(f"\n  SOURCE: {warning}")
    for entry in report.get("scan_density", []):
        print(f"\n  SCAN DENSITY: {entry['detail']}")
    for note in report.get("signal_notes", []):
        print(f"\n  SIGNAL: {note}")
    for entry in report.get("implausible_plans", []):
        print(f"\n  IMPLAUSIBLE PLAN: {entry['detail']}")
    for entry in report.get("length_deviations", []):
        print(
            f"\n  LENGTH (2.6): {entry['episode_id']} planned {entry['planned_sec']}s"
            f" against a {entry['hint_sec']}s hint - {entry['reason']}"
        )
    for entry in report.get("degraded", []):
        print(f"\n  DEGRADED: {entry['detail']}")
    for entry in report.get("subtitles_dropped", []):
        print(f"\n  SUBTITLES: {entry['detail']}")
    for entry in report.get("repeated_spans", []):
        print(f"\n  REPEATED SHOT: {entry['detail']}")
    for entry in report.get("render_failures", []):
        print(f"\n  RENDER FAILED for {entry['episode_id']}: {entry['error']}")
        print(f"      {entry['note']}")
    for entry in report.get("episodes_not_produced", []):
        print(f"\n  NOT PRODUCED ({entry['episode_id'][:8]}): {entry['detail']}")
    for entry in report.get("cuts_off_event", []):
        print(f"\n  OFF-EVENT (6.2) for {entry['episode_id'][:8]}: {entry['detail']}")
        for cut in entry["off_event"][:6]:
            print(f"      #{cut['sequence_order']} {cut['role'] or '?'}"
                  f" {cut['span'][0]:.0f}-{cut['span'][1]:.0f}s")
    for entry in report.get("beats_unfilled", []):
        print(
            f"\n  BEAT DROPPED ({entry['episode_id'][:8]} #{entry['beat']}"
            f" {entry['role'] or '?'}): {entry['why']} - {entry['query']}"
        )
    for episode_id, problems in (report.get("packaging_warnings") or {}).items():
        for problem in problems:
            print(f"\n  PACKAGE ({episode_id[:8]}): {problem}")


def cmd_run(args) -> int:
    pipeline = _pipeline(args)
    project = pipeline.submit(
        args.source, length_hint_sec=args.length_hint, channel_ref=args.channel or ""
    )
    transcriber = TranscriptFileTranscriber(args.transcript) if args.transcript else None
    if transcriber is None and not args.no_stt:
        transcriber = _whisperx(args)

    result = pipeline.run(
        project,
        transcriber=transcriber,
        stop_after=State(args.stop_after) if args.stop_after else None,
        sample_frames=args.frames,
        render=not args.no_render,
    )

    print(f"\nproject {result.project_id} -> {result.final_state.value}")
    if result.produced_nothing:
        print(f"  nothing worth producing: {result.report.get('no_content_reason')}")
        print("  (this is a normal outcome, not a failure - 16장)")
    for episode in result.report.get("episodes", []):
        print(
            f"  [{episode['target_type'] or '?'}] {episode['duration_sec']}s, {episode['cuts']} cuts,"
            f" structure={episode['structure']}"
        )
        if episode["titles"]:
            print(f"      title candidates: {' | '.join(episode['titles'])}")
        if episode["output"]:
            print(f"      output: {episode['output']}")
    _print_report_warnings(result.report)
    stages = result.report.get("stage_seconds") or {}
    if stages:
        total = result.report.get("elapsed_sec") or sum(stages.values())
        # R3 is an open risk about 처리 시간 and 22.6 asks the report for it.
        # Printing it makes every run a measurement instead of an estimate.
        print(f"\n  time {total:.1f}s: " + ", ".join(
            f"{name} {seconds:.1f}s" for name, seconds in stages.items()
        ))
    if result.report.get("provisional_parameters_used"):
        print(f"\n  {result.report['warning']}")
        print(f"  provisional: {', '.join(result.report['provisional_parameters_used'])}")
    print(f"\n  report: {Path(args.workspace) / result.project_id / 'report.json'}")
    return 0 if result.final_state is not State.FAILED else 1


def cmd_resume(args) -> int:
    """Continue a project that stopped part way, without re-deciding what it knows."""
    pipeline = _pipeline(args)
    project = pipeline.store.get_project(args.project)
    if project is None:
        print(f"unknown project {args.project}", file=sys.stderr)
        return 1

    windows = len(pipeline.store.windows(args.project))
    events = len(pipeline.store.events(args.project))
    print(f"resuming {args.project} from {project.status}")
    if windows and events:
        print(f"  reusing {windows} window summaries and {events} events already understood")
    else:
        print("  nothing understood yet; this will read the broadcast from the start")

    if not args.transcript and not pipeline.store.utterances(args.project):
        # Without this the empty utterance list reads as "this broadcast has no
        # speech", parsing continues, and a transcription failure that is fully
        # recoverable comes back as NO_CONTENT - a normal result under 2.2, so
        # nothing about the output says the transcript was the thing missing.
        print(
            f"{args.project} has no stored utterances, and resume does not start STT.\n"
            "  aicut transcribe <source> -o t.json   then  aicut resume --transcript t.json\n"
            "  aicut run <source> ...                to read the broadcast again from STT\n"
            "Resuming now would read it as a broadcast with no speech and most likely\n"
            "report NO_CONTENT (2.2), which would not be true.",
            file=sys.stderr,
        )
        return 1

    result = pipeline.resume(
        args.project,
        transcriber=TranscriptFileTranscriber(args.transcript) if args.transcript else None,
        render=not args.no_render,
    )
    print(f"\n{args.project} -> {result.final_state.value}")
    for episode in result.report.get("episodes", []):
        print(f"  [{episode['target_type'] or '?'}] {episode['duration_sec']}s, {episode['cuts']} cuts")
    _print_report_warnings(result.report)
    return 0 if result.final_state is not State.FAILED else 1


def cmd_status(args) -> int:
    store = _store(args)
    if args.project:
        project = store.get_project(args.project)
        if project is None:
            print(f"unknown project {args.project}", file=sys.stderr)
            return 1
        _print({
            "project": project.__dict__,
            "state_log": store.state_log(project.project_id),
            "episodes": [
                {"episode_id": e.episode_id, "render": e.render_status, "review": e.review_status,
                 "titles": e.title_candidates}
                for e in store.episodes(project.project_id)
            ],
        })
        return 0
    for project in store.list_projects():
        print(f"{project.project_id}  {project.status:<15} {project.file_path}")
    return 0


def cmd_candidates(args) -> int:
    """15.4: the candidate review screen, with the reasoning behind each decision."""
    if getattr(args, "assess_items", False):
        print("원본 32장 / 19장 MVP 3 - each candidate is evaluated on these:")
        for index, item in enumerate(review_mod.ASSESSMENT_ITEMS, start=1):
            print(f"  {index}. {item}")
        print("answers: " + ", ".join(review_mod.ASSESSMENT_VERDICTS))
        return 0
    store = _store(args)
    project = store.get_project(args.project)
    if project is None:
        print(f"unknown project {args.project}", file=sys.stderr)
        return 1
    ctx = _context(args, project)
    if args.assess:
        try:
            assessment = _parse_assessment(args.assess)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        if not args.candidate:
            print("--assess needs --candidate: 원본 32장 evaluates each candidate,"
                  " not the run", file=sys.stderr)
            return 1
        review_mod.record_candidate_assessment(ctx, args.candidate, assessment)
        for item, answer in assessment.items():
            print(f"recorded '{answer}' for {args.candidate[:8]} - {item}")
        _print(review_mod.assessment_rates(ctx))
        return 0
    if args.verdict:
        review_mod.record_candidate_verdict(ctx, args.candidate, args.verdict, args.note or "")
        print(f"recorded '{args.verdict}' for {args.candidate}")
        _print(review_mod.agreement_rate(ctx))
        return 0
    for row in review_mod.candidate_review(ctx):
        mark = {"produce": "+", "combine": "~", "hold": "?", "reject": "-"}.get(row["decision"], " ")
        form = f" [{row['suggested_form']}]" if row.get("suggested_form") else ""
        print(f"{mark} {row['candidate_id'][:8]}  {row['decision']:<8}{form} {row['core_summary'][:70]}")
        print(f"    why: {row['reason']}")
        # 6.1 in the order the clause lists it, so a reviewer can check the
        # decision against the content rather than against two scores (15.4).
        if row.get("people"):
            print(f"    인물: {', '.join(row['people'][:6])}")
        if row.get("start_point"):
            at = f" @{row['start_sec']:.0f}s" if row.get("start_sec") is not None else ""
            print(f"    시작 지점{at}: {row['start_point'][:70]}")
        if row.get("key_changes"):
            print(f"    주요 변화: {' / '.join(str(k) for k in row['key_changes'][:4])}")
        if row.get("outcome"):
            print(f"    결과: {row['outcome'][:70]}")
        if row.get("required_context"):
            print(f"    필요한 맥락: {row['required_context'][:70]}")
        if row.get("event_relations"):
            print(f"    다른 사건과의 관계: {len(row['event_relations'])}건")
        if row.get("scenes"):
            print(f"    관련 장면: {len(row['scenes'])}개")
        print(
            f"    independence={row['independence_score']:.2f} density={row['density_score']:.2f}"
            f" resolution={'yes' if row['has_resolution'] else 'no'}"
            + (f" human={row['human_verdict']}" if row["human_verdict"] else "")
        )
        assessed = row.get("human_assessment") or {}
        if assessed:
            # Shown by index against the printed list, so four long sentences do
            # not push the decision they belong to off the screen.
            marks = " ".join(
                f"{index}={assessed[item]}"
                for index, item in enumerate(review_mod.ASSESSMENT_ITEMS, start=1)
                if item in assessed
            )
            print(f"    항목별 평가: {marks}")
    _print(review_mod.agreement_rate(ctx))
    # 19장 scores MVP 3 on the four items of 원본 32장, one at a time. An overall
    # agreement rate cannot say which of the four is failing, so both are shown.
    _print(review_mod.assessment_rates(ctx))
    return 0


def _parse_assessment(pairs: list[str]) -> dict[str, str]:
    """Turn `--assess 2=no` into the item 원본 32장 names.

    The items are long Korean sentences. Typing one exactly at a shell prompt is
    a transcription test, so the index works too - but the index is only ever a
    way to name the clause's item, never a different set.
    """
    items = review_mod.ASSESSMENT_ITEMS
    out: dict[str, str] = {}
    for pair in pairs:
        if "=" not in pair:
            raise ValueError(f"--assess wants N=ANSWER, got {pair!r}")
        key, _, answer = pair.partition("=")
        key, answer = key.strip(), answer.strip()
        if key.isdigit():
            index = int(key)
            if not 1 <= index <= len(items):
                raise ValueError(
                    f"--assess index must be 1-{len(items)}, got {index}"
                    " (aicut candidates <project> --assess-items lists them)"
                )
            item = items[index - 1]
        elif key in items:
            item = key
        else:
            raise ValueError(
                f"unknown assessment item {key!r};"
                " see aicut candidates <project> --assess-items"
            )
        if answer not in review_mod.ASSESSMENT_VERDICTS:
            raise ValueError(
                f"answer must be one of {', '.join(review_mod.ASSESSMENT_VERDICTS)}, got {answer!r}"
            )
        out[item] = answer
    return out


def cmd_gate(args) -> int:
    """19장's gates, measured on real material rather than assumed.

    19장 says each MVP must pass its success criterion before the next one is
    worth building. None of the criteria is a number this code may pick: MVP 1
    says 비율 확보 without saying which ratio, MVP 2 asks whether a person's
    remembered events were all caught, MVP 3 asks four questions of a person.
    So every gate here measures and reports; the verdict stays with the reader.
    """
    if args.gate == "mvp1":
        return _gate_mvp1(args)
    if args.gate == "mvp3":
        return _gate_mvp3(args)
    return _gate_mvp2(args)


def _gate_mvp1(args) -> int:
    """MVP 1: 분석 결과가 실제 영상의 제작 의도와 일치하는가 (19장)."""
    from aicut.intelligence import reference as reference_mod

    store = _store(args)
    if args.verdict:
        if not args.reference:
            print("--verdict needs --reference <ref_id>", file=sys.stderr)
            return 1
        try:
            reference_mod.record_reference_verdict(
                store, args.reference, args.verdict, args.note or "",
            )
        except KeyError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(f"recorded '{args.verdict}' for {args.reference[:8]}")
        _print(reference_mod.reference_agreement(store))
        return 0

    references = store.references()
    if not references:
        print("no reference analyses stored. `aicut learn reference` runs loop A (4장)")
        return 1
    for row in references:
        mark = (row.get("human_verdict") or "")[:8] or "-"
        print(f"  {row['ref_id'][:8]}  {mark:<8}  {row['video_id']}")
        patterns = row.get("extracted_patterns") or {}
        # 19장 asks whether the analysis matches the video's 제작 의도, so the
        # analysis has to be on the screen next to the id being judged.
        for key, value in list(patterns.items())[:6]:
            text = json.dumps(value, ensure_ascii=False, default=str)
            print(f"      {key}: {text[:100]}")
    _print(reference_mod.reference_agreement(store))
    print("19장 MVP 1: 사람이 봤을 때 분석 결과가 실제 영상의 제작 의도와 일치한다고"
          " 판단되는 비율 확보. 어느 비율이면 확보인지는 19장이 정하지 않았다.")
    return 0


def _gate_mvp3(args) -> int:
    """MVP 3: 항목별 평가 (19장, 원본 32장). The numbers, in one place."""
    store = _store(args)
    project = store.get_project(args.project) if args.project else None
    if project is None:
        print(f"gate mvp3 needs a project: {args.project or '(none given)'}", file=sys.stderr)
        return 1
    ctx = _context(args, project)
    _print(review_mod.agreement_rate(ctx))
    _print(review_mod.assessment_rates(ctx))
    print("입력: aicut candidates <project> --candidate <id> --assess 1=yes")
    return 0


def _gate_mvp2(args) -> int:
    """MVP 2 실측 항목: 1차 통과 밀도별 사건 검출률과 처리 시간 (19장)."""
    from aicut.calibration import mvp2 as mvp2_mod
    from aicut.pipeline import understanding
    from aicut.pipeline.context import SignalBundle

    store = _store(args)
    if not args.project:
        print("gate mvp2 needs a processed project", file=sys.stderr)
        return 1
    project = store.get_project(args.project)
    if project is None:
        print(f"unknown project {args.project}", file=sys.stderr)
        return 1

    signals_path = Path(args.workspace) / project.project_id / "signals.json"
    if not signals_path.exists():
        # Re-decoding the source per density would make the timing a measurement
        # of ffmpeg, not of the pass 19장 asks about.
        print(
            f"{args.project} has no cached signals at {signals_path}.\n"
            "  aicut run <source> --no-render     reads the broadcast once and caches them",
            file=sys.stderr,
        )
        return 1
    if not store.utterances(project.project_id):
        print(f"{args.project} has no stored utterances; the pass would read it as silent",
              file=sys.stderr)
        return 1

    ctx = _context(args, project)
    ctx.signals = SignalBundle.load(signals_path)
    densities = [float(d) for d in args.density.split(",") if d.strip()]
    print(f"\nsource {project.duration_sec / 60:.1f} min,"
          f" densities {', '.join(f'{d:g}s' for d in densities)}")

    rows = mvp2_mod.measure_densities(
        ctx, densities,
        understand=lambda c: understanding.run(c, sample_frames=args.frames),
    )

    print(f"\n  {'pass1_window':>12}  {'events':>6}  {'seconds':>8}  {'xRT':>6}")
    for row in rows:
        data = row.as_dict()
        factor = data["realtime_factor"] or 0.0
        print(
            f"  {data['pass1_window_sec']:>11g}s  {data['events']:>6}  {data['seconds']:>8.1f}"
            f"  {factor:>5.1f}x"
        )

    out = Path(args.workspace) / project.project_id / "mvp2_density.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps([r.as_dict() for r in rows], indent=2, ensure_ascii=False), encoding="utf-8",
    )
    print(f"\nwritten to {out}")
    # 19장 leaves the pass/fail to a person: it says the criterion, not a number
    # that meets it. Printing a verdict here would invent one.
    print("19장 leaves the verdict to you: 사람이 기억하는 주요 사건을 누락 없이 잡아내는가.")
    return 0


def cmd_plan(args) -> int:
    """Read an edit plan the way MVP 5's success test asks a person to."""
    print(describe(EditPlan.load(args.plan)))
    return 0


def cmd_render(args) -> int:
    """Re-run only the render, from a plan that survived a failure (16장)."""
    from aicut.pipeline import rendering

    store = _store(args)
    plan = EditPlan.load(args.plan)
    episode = store.get_episode(plan.episode_id)
    if episode is None:
        print(f"plan references unknown episode {plan.episode_id}", file=sys.stderr)
        return 1
    project = store.get_project(episode.project_id)
    ctx = _context(args, project)
    rendering.render_episode(ctx, episode, plan_path=args.plan)
    print(f"rendered {episode.output_mp4_path}")
    # 2.6: a departure from the plan is reported. Without this the operator gets
    # a caption-less video and a line saying it rendered.
    for entry in ctx.report.get("degraded", []):
        print(f"  WARNING: {entry['detail']}")
    return 0


def cmd_export(args) -> int:
    """Hand the plan to a video editor instead of rendering it (22.5)."""
    from aicut.render.exchange import export

    plan = EditPlan.load(args.plan)

    # The source's real shape and length, when it can be read. Guessing them
    # makes an importer letterbox the clip or cut the timeline short.
    size = duration = source_fps = None
    if have_ffmpeg() and Path(plan.source_path).exists():
        from aicut.media.probe import probe

        media = probe(plan.source_path)
        if media.width and media.height:
            size = (media.width, media.height)
        duration = media.duration_sec or None
        # A plan the pipeline wrote carries no render settings, so without this
        # every real export asked for a --fps the operator had to go look up.
        source_fps = media.fps or None
    else:
        print("  note: the source file was not readable here, so the FCPXML declares the "
              "sequence's own size for it - relink in the editor if the clip looks stretched")

    formats = args.format or ["fcpxml"]
    out = Path(args.out) if args.out else Path(args.plan).parent
    # --out is a file only when one format is asked for AND it is not an
    # existing directory; otherwise it names where the files go.
    as_directory = out.is_dir() or len(formats) > 1 or not args.out

    written = []
    for fmt in formats:
        suffix = {"edl": ".edl", "fcpxml": ".fcpxml", "srt": ".srt"}[fmt]
        target = (out / f"{plan.episode_id}{suffix}") if as_directory else out
        written.append(export(plan, target, fmt=fmt, fps=args.fps,
                              source_size=size, source_duration_sec=duration,
                              source_fps=source_fps))
    for path in written:
        print(f"wrote {path}")
    print("  open it in Premiere Pro, DaVinci Resolve or Final Cut; the cuts are the "
          "same ones `aicut render` would make")
    return 0


def cmd_review(args) -> int:
    """The mandatory gate of 11.3: nothing is published without passing here."""
    store = _store(args)
    episode = store.get_episode(args.episode)
    if episode is None:
        print(f"unknown episode {args.episode}", file=sys.stderr)
        return 1
    project = store.get_project(episode.project_id)
    ctx = _context(args, project)
    if args.action == "thumbnail":
        # 11.1 offers the frames 사용자에게; this is where a person says which.
        try:
            updated = review_mod.choose_thumbnail(ctx, args.episode, args.index)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(f"{args.episode} will upload thumbnail {args.index}: {updated.thumbnail_path}")
        return 0
    if not args.reviewer:
        print("--reviewer is required; the gate records who released the video (11.3)",
              file=sys.stderr)
        return 1
    if args.action == "approve":
        review_mod.approve(ctx, args.episode, reviewer=args.reviewer, note=args.note or "")
        print(f"{args.episode} approved by {args.reviewer}; it may now be published")
    else:
        review_mod.reject(ctx, args.episode, reviewer=args.reviewer, reason=args.note or "")
        print(f"{args.episode} rejected by {args.reviewer}")
    return 0


def cmd_quota(args) -> int:
    from aicut.intelligence.quota import QuotaLedger

    store = _store(args)
    profile = _profile(args)
    ledger = QuotaLedger(
        store,
        daily_limit=profile.get_int("upload.daily_quota_units"),
        timezone_name=profile.get("upload.quota_reset_timezone"),
    )
    state = ledger.state()
    _print({
        "pt_date": state.pt_date,
        "used_units": state.used,
        "limit_units": state.limit,
        "remaining_units": state.remaining,
        "uploads_left_today": ledger.uploads_left_today(),
        "next_reset": ledger.next_reset().isoformat(),
        "queued_uploads": store.upload_queue(),
    })
    return 0


def cmd_calibrate(args) -> int:
    """17.4: measure a starting point, sweep, score, save the channel's profile."""
    from aicut.calibration import ReplayHarness, build_evaluator, sweep
    from aicut.calibration.dataset import Dataset

    if args.init:
        return _calibrate_init(args)

    if not args.dataset:
        print("a sweep needs --dataset (see `aicut dataset init`), or use --init", file=sys.stderr)
        return 1

    dataset = Dataset.load(args.dataset)
    base = _profile(args)
    grid = json.loads(Path(args.grid).read_text(encoding="utf-8")) if args.grid else DEFAULT_SWEEP_GRID
    if not args.grid:
        print(f"no --grid given; sweeping the default grid over {', '.join(grid)}")

    if args.harness:
        # An operator with their own replay keeps using it.
        from importlib import util

        spec = util.spec_from_file_location("aicut_eval_harness", args.harness)
        module = util.module_from_spec(spec)
        spec.loader.exec_module(module)                      # type: ignore[union-attr]
        evaluate = lambda profile: module.run(profile, dataset.to_dict())    # noqa: E731
        harness = None
    else:
        harness = ReplayHarness(
            dataset, workspace=Path(args.workspace), project_id=args.project,
            producer=_producer(args),
        )
        evaluate = build_evaluator(harness)

    try:
        result = sweep(base, grid, evaluate, channel_ref=args.channel or dataset.channel_ref)
    finally:
        if harness is not None:
            harness.close()

    # 17.4 step 4 needs to know what setup this profile was measured in, or a
    # later run has nothing to compare against.
    result.profile.environment = _dataset_environment(args, dataset)
    out = Path(args.out or Path(args.workspace) / "profiles" / f"{result.profile.name}.json")
    result.profile.save(out)
    result.save_trials(out.with_suffix(".trials.json"))
    _record_profile(args, result.profile)
    print(f"best score {result.best_score}: {result.best_params}")
    print(f"  {len(result.trials)} trials scored against {args.dataset}")
    print(f"  {', '.join(dataset.coverage()['ready_for'])}")
    print(f"profile saved to {out}")
    print(f"use it with: aicut --profile {out} run <source>")
    return 0


def _calibrate_init(args) -> int:
    """17.4 step 1: read a starting silence level off this channel's own audio.

    Rather than adopting somebody else's -40 dB, take the level distribution of
    a real broadcast from this setup and put the silence line low in it. Still a
    starting point for the sweep, not a measurement of what sounds silent.
    """
    from aicut.calibration.sweep import initial_estimates
    from aicut.pipeline.context import SignalBundle

    store = _store(args)
    project = store.get_project(args.project) if args.project else (store.list_projects() or [None])[-1]
    if project is None:
        print("no project to measure; run one first (its signals are cached)", file=sys.stderr)
        return 1
    cache = Path(args.workspace) / project.project_id / "signals.json"
    if not cache.exists():
        print(f"no cached signals for {project.project_id}", file=sys.stderr)
        return 1

    signals = SignalBundle.load(cache)
    if not signals.rms:
        print("the cached signals hold no RMS envelope to measure", file=sys.stderr)
        return 1

    estimates = initial_estimates(level for _, level in signals.rms)
    profile = _profile(args).with_overrides(estimates, measured=estimates.keys())
    profile.name = args.channel or f"{profile.name}-init"
    out = Path(args.out or Path(args.workspace) / "profiles" / f"{profile.name}.json")
    profile.save(out)
    _record_profile(args, profile)
    print(f"measured from {project.file_path}: {estimates}")
    print(f"profile saved to {out}")
    print("this is step 1 of 17.4; run the sweep before treating these as final")
    return 0


def _dataset_environment(args, dataset) -> dict:
    """The broadcast setup a sweep was run against (17.4 step 4).

    Read from the dataset's own source. It is best effort: a sweep is still a
    valid sweep if the source has since moved, and an empty environment simply
    means later runs have nothing to compare against rather than a failure.
    """
    from aicut.calibration import environment as environment_mod

    try:
        from aicut.media.probe import probe

        store = _store(args)
        media = probe(dataset.source_path)
        source = str(Path(dataset.source_path).resolve())
        project = next(
            (p for p in reversed(store.list_projects())
             if p.file_path and str(Path(p.file_path).resolve()) == source),
            None,
        )
        situations = store.situations(project.project_id) if project else []
        utterances = store.utterances(project.project_id) if project else []
        return environment_mod.fingerprint(media, situations, utterances)
    except Exception as exc:
        log.warning("could not record the measurement environment: %s", exc)
        return {}


def _record_profile(args, profile: CalibrationProfile) -> str:
    """Keep the profile in TB_CALIBRATION_PROFILE too (13장), not only as a file.

    22.7 lists the channel profile as a deliverable, and 17.4 says a profile is
    re-measured whenever the setup changes - so the history of what was measured
    when has to live somewhere queryable, not only in whichever file was written
    last.
    """
    return _store(args).save_profile(
        name=profile.name,
        channel_ref=args.channel or "",
        params=profile.to_mapping(),
        measured_at=profile.measured_at,
        eval_score=profile.eval_score,
    )


def cmd_profile(args) -> int:
    if args.list:
        _print([
            {"profile_id": row["profile_id"], "name": row["name"], "channel_ref": row["channel_ref"],
             "measured_at": row["measured_at"], "eval_score": row["eval_score"]}
            for row in _store(args).profiles()
        ])
        return 0
    profile = _profile(args)
    _print({
        "name": profile.name,
        "source": str(profile.source_path),
        "measured_at": profile.measured_at,
        "eval_score": profile.eval_score,
        "provisional_parameters": sorted(profile.provisional),
        "measured_parameters": sorted(profile.measured),
        "notes": profile.notes,
    })
    return 0


def _source_duration(store, args) -> float:
    """The source broadcast's length, for the whole-timeline half of 12.3 B.

    A project that has been run knows it from ffprobe. Failing that the operator
    can state it. Failing that the transcript's last word is a floor: it is not
    the true length - the broadcast almost certainly continued past the last
    word - so it is reported as such rather than passed off as measured (17.5).
    """
    explicit = getattr(args, "source_duration", None)
    if explicit:
        return float(explicit)
    reference = getattr(args, "source_ref", None) or getattr(args, "source", "") or ""
    for project in store.list_projects():
        if project.file_path and Path(project.file_path).name == Path(str(reference)).name:
            if project.duration_sec:
                return float(project.duration_sec)
    return 0.0


def _listen(args, path: str, which: str):
    """Transcribe one side of a 12.3 B pair. Returns the utterances and the length.

    18장 lists STT 처리 under [프로그램이 담당]. The operator hands over videos;
    turning them into words is this program's job, not theirs.

    A transcript written earlier by `aicut transcribe` is reused when it sits
    next to the video, because a 6-hour broadcast is not worth transcribing
    twice.
    """
    from aicut.media.probe import probe
    from aicut.media.stt import TranscriptFileTranscriber, write_transcript

    media = probe(path)
    media.validate(require_video=False)
    cached = Path(path).with_suffix(".transcript.json")
    if cached.exists():
        print(f"{which}: reusing {cached.name}")
        return TranscriptFileTranscriber(str(cached)).transcribe(), media.duration_sec
    print(f"{which}: transcribing {path} ({media.duration_sec / 60:.1f} min) with {args.backend}")
    utterances = _transcriber(args).transcribe(path, media)
    write_transcript(utterances, cached)
    print(f"  {len(utterances)} segments -> {cached.name}")
    return utterances, media.duration_sec


def _pair_frames(args, path, name: str) -> list[str]:
    """Sample frames from one side of a 12.3 B pair, or nothing if no file."""
    if not path:
        return []
    from aicut.intelligence import reference as reference_mod

    frames_dir = Path(args.workspace) / name
    frames_dir.mkdir(parents=True, exist_ok=True)
    watched = reference_mod.watch(path, _profile(args), frames_dir=frames_dir)
    print(f"{name}: {len(watched['frames'])} frames from {path}")
    return watched["frames"]


def cmd_learn(args) -> int:
    """Run one of the three learning loops (12.3)."""
    from aicut.intelligence import reference as reference_mod
    from aicut.intelligence.knowledge import ProductionKnowledge

    store = _store(args)
    producer = _producer(args)
    knowledge_path = Path(args.workspace) / "knowledge.json"

    if args.loop == "reference":
        # Loop A (4장). The system finds its own references in the neighbourhood
        # 4.1 names, fetches them, and analyses how they were made. What it can
        # see is a finished video - the broadcast behind it is not on YouTube,
        # so nothing here can say what was cut. That is loop B's question, and
        # loop B is the one the operator feeds.
        #
        # 4.6 leaves the media policy to the operator and they settled it:
        # what is fetched is kept.
        client = _youtube(args, store)
        queries = args.query or reference_mod.DEFAULT_QUERIES
        references = reference_mod.collect_references(client, queries, per_query=args.per_query)
        print(f"found {len(references)} references; reading")
        watched = reference_mod.watch_all(
            references, _profile(args), args.workspace,
            download=args.download,
            comments=args.comments, comment_limit=args.comment_limit,
        )
        for video_id, seen in watched.items():
            print(f"  {video_id}: {len(seen.get('frames', []))} frames"
                  f"{', thumbnail' if seen.get('thumbnail') else ''}")
        print(f"analysing {len(references)}")
        reference_mod.analyze(producer, store, references, watched=watched)
        knowledge = reference_mod.build_knowledge(store).carry_over_learning(
            ProductionKnowledge.load(knowledge_path)
        )
        knowledge.save(knowledge_path)
        print(f"knowledge from {knowledge.sample_size} references -> {knowledge_path}")
        return 0

    if args.loop == "pairs":
        # Loop B, the differentiator (12.3 B). The operator hands over the two
        # videos - 원본 생방송 and the 완성본 made from it - and nothing else.
        # 18장 puts STT 처리 under [프로그램이 담당], so it runs here.
        from aicut.intelligence.source_output import align_by_transcript, learn as learn_pair

        if not args.source or not args.output:
            print("loop B needs the two videos: --source <원본> --output <완성본>",
                  file=sys.stderr)
            return 1
        source, source_duration = _listen(args, args.source, "원본")
        output, _ = _listen(args, args.output, "완성본")
        duration = _source_duration(store, args) or source_duration
        alignment = align_by_transcript(source, output, source_duration_sec=duration)
        # 5.2: 화면과 소리를 분리하지 않고 같이 본다. Speech says which sentences
        # survived; it cannot show a cut inside one, a caption or an effect.
        source_frames = _pair_frames(args, args.source, "pair_source")
        output_frames = _pair_frames(args, args.output, "pair_output")
        analysis = learn_pair(
            producer, store, alignment,
            source_ref=args.source_ref or args.source,
            output_ref=args.output_ref or args.output,
            source_frames=source_frames,
            output_frames=output_frames,
        )
        measured = analysis["measured"]
        print(
            f"kept {measured['kept_spans']} spans, dropped {measured['dropped_spans']},"
            f" keep_ratio {measured['keep_ratio']}, reordered {measured['reordered']}"
        )
        if measured["source_duration_sec"]:
            print(
                f"  of the whole broadcast: {measured['selection_ratio']:.1%} used,"
                f" {measured['removed_sec']:.0f}s removed across"
                f" {measured['removed_segments']} stretches"
            )
        for rule in analysis.get("inferred_rules", []):
            print(f"  rule: {rule}")
        knowledge = ProductionKnowledge.load(knowledge_path)
        knowledge.source_output_rules.extend(analysis.get("inferred_rules", []))
        knowledge.save(knowledge_path)

        # 17.2: "(b)가 그대로 12.3 B의 학습 데이터가 된다. 따라서 이 작업은
        # 캘리브레이션과 학습에 이중으로 쓰인다." So write the entry, rather than
        # printing that one exists.
        from aicut.calibration import dataset as dataset_mod

        target = Path(args.dataset or
                      Path(args.workspace) / "datasets" / f"{Path(args.source).stem}.json")
        prior = dataset_mod.Dataset.load(target) if target.exists() else None
        entry = dataset_mod.from_pair(
            args.source, args.output, alignment,
            transcript_path=str(Path(args.source).with_suffix(".transcript.json")),
            existing=prior,
        )
        entry.save(target)
        print(f"17.2 dataset: {len(entry.content_spans)} content spans -> {target}")
        print("  run `aicut dataset derive-silences` on it once this source has been"
              " processed, to add the 호흡 labels 17.3 scores against")
        return 0

    # Loop C.
    from aicut.pipeline import performance

    project = store.get_project(args.project)
    if project is None:
        print(f"unknown project {args.project}", file=sys.stderr)
        return 1
    ctx = _context(args, project)
    client = _youtube(args, store)
    collected = performance.collect(ctx, client, days=args.days)
    print(f"collected metrics for {len(collected)} published episodes")
    for episode_id, absent in (ctx.report.get("performance_missing_metrics") or {}).items():
        # 12.2 reasons from whatever came back. A strategy update built without
        # 클릭률 reads no differently from one built with it.
        print(f"  MISSING (12.1) for {episode_id[:8]}: {', '.join(absent)}")
    result = performance.learn(ctx, knowledge_path)
    for observation in result.get("observations", []):
        print(f"  {observation}")
    return 0


def cmd_upload(args) -> int:
    """Upload rendered episodes privately, or drain the retry queue (11.3, 11.4)."""
    from aicut.intelligence.quota import QuotaLedger
    from aicut.pipeline import publishing

    store = _store(args)
    profile = _profile(args)
    ledger = QuotaLedger(
        store,
        daily_limit=profile.get_int("upload.daily_quota_units"),
        timezone_name=profile.get("upload.quota_reset_timezone"),
    )
    client = _youtube(args, store, ledger=ledger)

    if args.retry:
        project = store.get_project(args.project) if args.project else None
        if project is None:
            projects = store.list_projects()
            if not projects:
                print("no projects", file=sys.stderr)
                return 1
            project = projects[-1]
        ctx = _context(args, project)
        done = publishing.process_retry_queue(ctx, client, ledger)
        print(f"uploaded {len(done)} queued episodes; {ledger.uploads_left_today()} uploads left today")
        return 0

    episode = store.get_episode(args.episode)
    if episode is None:
        print(f"unknown episode {args.episode}", file=sys.stderr)
        return 1
    ctx = _context(args, store.get_project(episode.project_id))
    if args.publish:
        publishing.publish_approved(ctx, episode, client)
        print(f"{episode.episode_id} is now public")
        return 0
    result = publishing.upload_episode(ctx, episode, client)
    print(f"uploaded {result['url']} as {result['privacy_status']}")
    print("it stays private until a person approves it: aicut review <episode> approve --reviewer <name>")
    return 0


def _youtube(args, store, ledger=None):
    from aicut.intelligence.quota import QuotaLedger
    from aicut.intelligence.youtube import YouTubeClient, load_credentials

    profile = _profile(args)
    ledger = ledger or QuotaLedger(
        store,
        daily_limit=profile.get_int("upload.daily_quota_units"),
        timezone_name=profile.get("upload.quota_reset_timezone"),
    )
    credentials = load_credentials(
        args.client_secrets, args.token or str(Path(args.workspace) / "youtube_token.json")
    )
    return YouTubeClient(credentials, ledger)


def cmd_ui(args) -> int:
    """The operator screens of 15.1, served on localhost."""
    from aicut.ui import serve
    from aicut.ui.auth import ENV_VAR, ApiKeyGuard, guard_from_environment

    guard = ApiKeyGuard(args.api_key) if args.api_key else guard_from_environment()
    httpd, ui = serve(
        Path(args.workspace), host=args.host, port=args.port,
        profile_path=args.profile, producer_name=args.producer,
        guard=guard,
        backup_interval_sec=args.backup_every,
        backup_retention=args.backup_keep,
        client_secrets=args.client_secrets, token_path=args.token,
        # 18장: the server transcribes what it is given, like `aicut run` does.
        stt={
            "backend": args.backend, "model_size": args.stt_model,
            "device": args.device, "compute_type": args.compute_type,
            "language": args.language, "hf_token": args.hf_token,
            "diarize": not args.no_diarize,
        },
    )
    print(f"aicut ui on http://{args.host}:{args.port}  (workspace {args.workspace})")
    if guard.enabled:
        print("/api/* requires  Authorization: Bearer <key>")
    else:
        print(f"no authentication - do not expose this port (set {ENV_VAR} or --api-key)")
    if ui.scheduler:
        print(f"snapshots every {args.backup_every:g}s, keeping {args.backup_keep}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        httpd.server_close()
        ui.close()
    return 0


def cmd_backup(args) -> int:
    """Snapshot the workspace database, or list the snapshots already taken.

    The database is where the expensive work lives: the window summaries and
    event graph of 5장, every human verdict from 15.4, the calibration
    profiles of 17장. A render can be re-run from a saved plan (16장); none of
    that can. SQLite's online backup API is used rather than a file copy, so
    this is safe to run while a pipeline is writing.
    """
    from aicut.db.backup import DatabaseBackup

    workspace = Path(args.workspace)
    backup = DatabaseBackup(workspace / "aicut.db", workspace / "backups", retention=args.keep)
    if args.list:
        snapshots = backup.list()
        if not snapshots:
            print(f"no snapshots in {workspace / 'backups'}")
            return 0
        for item in snapshots:
            print(f"{item['name']}  {item['size_bytes']:>12,} bytes  {item['modified_at']}")
        return 0

    result = backup.create()
    if result.get("status") != "COMPLETE":
        print(f"skipped: {result.get('reason', 'unknown')}")
        return 0
    print(f"{result['path']}")
    print(f"  {result['size_bytes']:,} bytes  quick_check={result['integrity']}")
    print(f"  sha256 {result['sha256']}")
    for name in result["pruned"]:
        print(f"  pruned {name}")
    return 0


def cmd_transcribe(args) -> int:
    """Produce a transcript and stop (20.2).

    STT is the one stage that wants a GPU, and it does not have to run where the
    editing runs. Splitting it out means the transcript can be made on whatever
    machine has the hardware and carried to the one that does the work - which
    is also how the transcript for a source/output pair gets made (17.2).
    """
    from aicut.media.probe import probe
    from aicut.media.stt import write_transcript

    media = probe(args.source)
    # STT only listens, so an extracted audio track or a WAV is a valid input
    # here even though it would not be a valid broadcast source.
    media.validate(require_video=False)
    transcriber = _transcriber(args)
    print(f"transcribing {args.source} ({media.duration_sec / 60:.1f} min) with {args.backend}")

    import time

    started = time.time()
    utterances = transcriber.transcribe(args.source, media)
    elapsed = time.time() - started

    out = Path(args.out or Path(args.source).with_suffix(".transcript.json"))
    write_transcript(utterances, out)
    words = sum(len(u.words) for u in utterances)
    factor = media.duration_sec / elapsed if elapsed else 0.0
    print(f"  {len(utterances)} segments, {words} word timings, {elapsed:.1f}s ({factor:.1f}x realtime)")
    if not words:
        print("  WARNING: no word timings came out; pacing measures gaps between words and"
              " subtitle timing depends on them")
    print(f"  written to {out}")
    return 0


def _transcriber(args):
    """The recogniser these flags name. The building itself is stt.build_transcriber,
    so the UI produces the same one from its own settings (18장)."""
    from aicut.media.stt import build_transcriber

    if args.backend == "pocketsphinx" and args.language and args.language != "en":
        # No GPU, no download - the model is in the package. Poor accuracy, and
        # English only, but it runs on a machine that can host nothing else.
        print(f"pocketsphinx is English-only; ignoring --language {args.language}",
              file=sys.stderr)
    return build_transcriber(
        args.backend,
        model_size=args.stt_model, device=args.device, language=args.language,
        # Dropping compute_type left every whisperx run on the class default
        # (float16), so --compute-type int8 was accepted and ignored - which on
        # a CPU run is the difference between working and an unsupported-
        # precision failure.
        compute_type=args.compute_type,
        hf_token=args.hf_token, diarize=not args.no_diarize,
    )


def cmd_dataset(args) -> int:
    """Build the labelled dataset of 17.2 - the thing every threshold waits on."""
    from aicut.calibration.dataset import Dataset

    path = Path(args.file)

    if args.action == "init":
        if path.exists() and not args.force:
            print(f"{path} already exists; pass --force to start over", file=sys.stderr)
            return 1
        dataset = Dataset(
            source_path=args.source or "",
            transcript_path=args.transcript,
            output_path=args.output,
            channel_ref=args.channel or "",
        )
        if not dataset.source_path:
            print("a dataset must name its source broadcast: --source <file>", file=sys.stderr)
            return 1
        dataset.save(path)
        print(f"dataset created at {path}")
        print("next: mark the stretches a person would make a video out of, with")
        print(f"  aicut dataset add-content {path} --start 01:12:30 --end 01:19:05 --note '...'")
        return 0

    dataset = Dataset.load(path)

    if args.action == "add-content":
        span = dataset.add_content(_seconds(args.start), _seconds(args.end), args.note or "")
        dataset.save(path)
        print(f"content span {_hms(span.start_sec)}-{_hms(span.end_sec)} added"
              f" ({len(dataset.content_spans)} total)")
        return 0

    if args.action == "add-silence":
        verdict = dataset.add_silence_verdict(
            _seconds(args.start), _seconds(args.end), kept=args.kept, note=args.note or ""
        )
        dataset.save(path)
        print(f"silence {_hms(verdict.start_sec)}-{_hms(verdict.end_sec)}"
              f" marked {'kept' if verdict.kept else 'cut'}")
        return 0

    if args.action == "derive-silences":
        return _derive_silences(args, dataset, path)

    _print({"dataset": dataset.to_dict(), "coverage": dataset.coverage()})
    return 0


def _derive_silences(args, dataset, path: Path) -> int:
    """Read the pacing labels out of the human edit instead of typing them (12.3 B, 17.2)."""
    from aicut.intelligence.source_output import align_by_transcript
    from aicut.media.stt import TranscriptFileTranscriber
    from aicut.pipeline.context import SignalBundle

    if not dataset.transcript_path or not args.output_transcript:
        print("deriving needs the source transcript in the dataset and --output-transcript",
              file=sys.stderr)
        return 1

    store = _store(args)
    source_resolved = str(Path(dataset.source_path).resolve())
    project = next(
        (p for p in reversed(store.list_projects())
         if str(Path(p.file_path).resolve()) == source_resolved),
        None,
    )
    if project is None:
        print(f"run {dataset.source_path} once first so its silences are measured", file=sys.stderr)
        return 1

    cache = Path(args.workspace) / project.project_id / "signals.json"
    if not cache.exists():
        print(f"no cached signals for {project.project_id}", file=sys.stderr)
        return 1

    signals = SignalBundle.load(cache)
    alignment = align_by_transcript(
        TranscriptFileTranscriber(dataset.transcript_path).transcribe(),
        TranscriptFileTranscriber(args.output_transcript).transcribe(),
        source_duration_sec=project.duration_sec or 0.0,
    )
    verdicts = dataset.derive_silence_verdicts(
        signals.silences, alignment, profile=_profile(args),
    )
    dataset.save(path)
    kept = sum(1 for v in verdicts if v.kept)
    print(f"derived {len(verdicts)} silence verdicts from the human edit: {kept} kept, {len(verdicts) - kept} cut")
    print(f"  keep ratio of the edit itself: {alignment.keep_ratio:.2f}")
    return 0


def _seconds(value: str) -> float:
    """Accept 91.5, 1:31.5 or 01:12:30."""
    parts = str(value).split(":")
    total = 0.0
    for part in parts:
        total = total * 60 + float(part)
    return total


def _hms(seconds: float) -> str:
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def cmd_benchmark(args) -> int:
    """Measure this machine against a real source (R3, 20.2).

    R3 names processing time as an open risk and 20.2 lists measuring it as a
    prerequisite, so the measurement is a command rather than a paragraph. What
    it reports is the signal-extraction cost - the part that scales with source
    length and runs on every project - and what that extrapolates to for a
    six-hour broadcast on this hardware.
    """
    import time

    from aicut.media import audio as audio_mod
    from aicut.media import vision as vision_mod
    from aicut.media.probe import probe

    profile = _profile(args)
    media = probe(args.source)
    media.validate()
    print(f"source: {args.source}")
    print(f"  {media.duration_sec / 60:.1f} min, {media.width}x{media.height},"
          f" {len(media.audio_tracks)} audio track(s)")

    steps: dict[str, float] = {}

    def timed(label: str, fn):
        started = time.time()
        result = fn()
        steps[label] = time.time() - started
        print(f"  {label:22s} {steps[label]:7.2f}s")
        return result

    silences = timed("silence detection", lambda: audio_mod.detect_silences(args.source, profile))
    rms = timed("loudness envelope", lambda: audio_mod.rms_envelope(args.source))
    motion = timed("visual change", lambda: vision_mod.motion_curve(
        args.source, interval_sec=profile.get_float("scan.pass1_frame_interval_sec")))

    if args.frames:
        frames = timed("frame sampling", lambda: vision_mod.sample_frames(
            args.source, Path(args.workspace) / "benchmark_frames",
            start_sec=0, duration_sec=media.duration_sec,
            interval_sec=profile.get_float("scan.pass1_frame_interval_sec")))
        from aicut.media import faces as faces_mod

        detector = faces_mod.build_detector()
        if detector is not None:
            timed("face detection", lambda: detector.read_frames([(f.at_sec, f.path) for f in frames]))
        else:
            print("  face detection         skipped (no detector available)")

    total = sum(steps.values())
    factor = media.duration_sec / total if total else 0.0
    print()
    print(f"  measured {len(silences)} silences, {len(rms)} loudness frames, {len(motion)} motion samples")
    print(f"  total {total:.1f}s for {media.duration_sec / 60:.1f} min of source ({factor:.1f}x realtime)")
    if factor:
        print(f"  a six-hour broadcast would take about {6 * 3600 / factor / 60:.1f} min of signal extraction"
              " on this machine")
    print("  STT and the reasoning passes are extra and dominate on a real run;"
          " measure those on the hardware that will run them (20.2).")
    return 0


def cmd_fetch_ffmpeg(args) -> int:
    """Install a static ffmpeg beside the projects (20.1)."""
    from aicut.media.ffmpeg_fetch import FetchRefused, bundled_ffmpeg, fetch, platform_build

    if have_ffmpeg() and not args.force:
        print("ffmpeg is already on PATH; nothing to do (--force to fetch anyway)")
        return 0
    already = bundled_ffmpeg(args.workspace)
    if already and not args.force:
        print(f"already installed at {already.parent}")
        return 0
    build = platform_build()
    print(f"fetching {build.url}")
    if not build.sha256 and not args.sha256:
        print(f"  no digest is recorded for this build. {build.note}" if build.note else "")
        print("  read the publisher's checksum and pass it: --sha256 <digest>")
    try:
        target = fetch(args.workspace, sha256=args.sha256)
    except FetchRefused as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"installed into {target}")
    return 0


def _check_face_detector() -> None:
    """OpenCV importing is not the same as a face detector existing (5.3, 9.2, 11.1).

    OpenCV 5.0 removed CascadeClassifier and ships no face model, so on 5.x the
    package check above says [ok] while nothing can actually detect a face - and
    the run then labels every screen UNKNOWN and drops the 표정 signal, quietly.
    """
    from aicut.media import faces as faces_mod

    if not faces_mod.available():
        return
    detector = faces_mod.build_detector()
    if detector is not None:
        print(f"  [ok] face detector ({detector.backend}) - 화면 상황 라벨 (5.3), 표정 (9.2, 11.1)")
        return
    print("  [--] face detector - OpenCV is installed but has no usable face model")
    print("       OpenCV 5 removed CascadeClassifier. Point AICUT_FACE_MODEL at a")
    print("       YuNet face_detection_yunet .onnx (opencv_zoo), or use opencv-python<5.")
    print("       Without it 5.3 leaves every screen UNKNOWN and 9.2 loses 표정.")


def _check_reasoning_backend(args) -> None:
    """Whether the judgement half of 18장 can actually run.

    `mock` decides nothing. It exists so the pipeline can be exercised without a
    model, and a run on it is a wiring test, not an edit - which is worth saying
    out loud, because the output looks the same either way.
    """
    producer = getattr(args, "producer", "mock")
    if producer == "mock":
        print("  [--] reasoning backend is 'mock', which decides nothing (18장)")
        print("       Every judgement - what is content, what to cut, what to say -")
        print("       comes back canned. Use --producer ollama (local) or anthropic.")
        return
    if producer == "anthropic":
        has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
        print(f"  [{'ok' if has_key else '--'}] ANTHROPIC_API_KEY set")
        if not has_key:
            print("       --producer anthropic sends every judgement to the API and needs")
            print("       a key: export ANTHROPIC_API_KEY=...")
        return
    if producer == "ollama":
        from aicut.llm.ollama_provider import DEFAULT_MODEL, OllamaProducer

        model = getattr(args, "ollama_model", None) or DEFAULT_MODEL
        try:
            state = OllamaProducer(
                model=model, host=getattr(args, "ollama_host", None),
                warn_on_text_only=False,
            ).check()
        except Exception as exc:
            print(f"  [--] ollama - {exc}")
            return
        print(f"  [ok] ollama {state['model']} at {state['host']}")
        if state["takes_images"]:
            print("       takes images, so 5.2 can read 화면 and 소리 together")
        else:
            print(f"  [--] {state['model']} is not known to take images.")
            print("       5.2 has the passes read screen and sound together; a text-only")
            print("       model drops the frames and judges from words alone, which is")
            print("       the editor 1.2 rejects. Try llava, qwen2.5vl or gemma3.")


def _check_subtitle_fonts(args) -> None:
    """20.2's font question, asked of the profile that would actually be used.

    20.2 lists 자막 폰트의 임베딩·상업 사용 허용 여부 확인 as a pre-start item and
    10.3 admits only fonts that permit both. Separately, a font the machine does
    not have is substituted by libass without a word, so the burned captions are
    not the style the profile describes - and that is only visible by watching
    the finished video.
    """
    from aicut.render.subtitles import SubtitleStyleProfile

    # The profile the render would actually load (17.1 keeps the choice there),
    # not a guess - checking a different profile from the one that will be used
    # is worse than not checking.
    name = _profile(args).get("render.subtitle_style_profile")
    try:
        profile = SubtitleStyleProfile.load(name)
    except Exception as exc:
        print(f"  [--] subtitle style '{name}' - {exc}")
        return
    problems = profile.licence_problems()
    print(f"  [{'ok' if not problems else '--'}] subtitle fonts"
          f" ({', '.join(profile.fonts) or 'none named'})")
    for problem in problems:
        print(f"       {problem}")


def cmd_doctor(args) -> int:
    """Check the preconditions of 20.2 before a run rather than during one."""
    #: What to type when a check comes back missing. A report that says a thing
    #: is absent and not how to get it sends the operator to a search engine.
    checks = (
        ("ffmpeg/ffprobe on PATH", have_ffmpeg(), _ffmpeg_remedy()),
        ("whisperx installed (best word timings, wants a GPU)", _importable("whisperx"),
         "pip install 'aicut[stt]'"),
        ("faster-whisper installed (word timings on a CPU)", _importable("faster_whisper"),
         "pip install faster-whisper"),
        ("pyannote installed (gated model, needs HF approval - 20.2)",
         _importable("pyannote.audio"), "pip install 'aicut[diarization]'"),
        ("anthropic sdk installed", _importable("anthropic"), "pip install 'aicut[llm]'"),
        ("google api client installed", _importable("googleapiclient"),
         "pip install 'aicut[youtube]'"),
        ("opencv installed", _importable("cv2"), "pip install 'aicut[vision]'"),
    )
    for name, ok, remedy in checks:
        print(f"  [{'ok' if ok else '--'}] {name}")
        if not ok:
            print(f"       {remedy}")

    _check_face_detector()
    _check_reasoning_backend(args)
    _check_subtitle_fonts(args)

    # Having ffmpeg is not the same as having the ffmpeg this needs: the plain
    # Homebrew bottle links no libass, so `subtitles` is absent and captions
    # cannot be burned. Better to learn that here than after a six-hour run.
    if have_ffmpeg():
        from aicut.media.ffmpeg_util import LIBASS_HINT, filters_known, has_filter

        known = filters_known()
        for filt, purpose in (("subtitles", "burn-in captions (10.3)"),
                              ("crop", "reframing and zoom (10.4-1)"),
                              ("loudnorm", "two-pass loudness (10.4-3)")):
            if not known:
                # Reporting "missing" here would be a lie with consequences:
                # the operator would go install an ffmpeg they already have.
                print(f"  [??] ffmpeg filter '{filt}' - {purpose}"
                      " (this ffmpeg's filter list could not be read)")
                continue
            ok = has_filter(filt)
            print(f"  [{'ok' if ok else '--'}] ffmpeg filter '{filt}' - {purpose}")
            if not ok and filt == "subtitles":
                print(f"       {LIBASS_HINT}")

    profile = _profile(args)
    measured = profile.measured_parameters()
    if measured:
        print(f"\n  measured in profile '{profile.name}': {', '.join(measured)}")
    if profile.provisional:
        print(
            f"\n  {len(profile.provisional)} parameter groups are still unmeasured guesses in"
            f" profile '{profile.name}' (17.5). Run `aicut calibrate` before relying on output."
        )
        if measured:
            # Without this the count never moves and step 1 of 17.4 looks inert:
            # a group stays listed while one value inside it has been measured.
            print("  (a group stays listed while any value inside it is still a guess)")
    return 0


def _ffmpeg_remedy() -> str:
    """What to type to get an ffmpeg, and only what will work.

    `aicut fetch-ffmpeg` refuses to install a build whose digest was never
    recorded, and none of the three platform builds has one. Offering it as the
    remedy sent an operator with no ffmpeg to a command that cannot succeed, so
    it is only offered when it can - and otherwise the way to make it work is
    named instead.
    """
    from aicut.media.ffmpeg_fetch import has_recorded_checksum

    if has_recorded_checksum():
        return "install ffmpeg, or `aicut fetch-ffmpeg`"
    return (
        "install ffmpeg yourself (apt install ffmpeg / brew install ffmpeg /"
        " winget install ffmpeg).\n"
        "       `aicut fetch-ffmpeg` needs a digest to verify the download against:"
        " no build here has one recorded, so pass the publisher's own with"
        " `--sha256 <digest>`"
    )


def _importable(name: str) -> bool:
    from importlib import util

    try:
        return util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _whisperx(args):
    return _transcriber(args)


# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aicut", description=__doc__)
    parser.add_argument("--workspace", default=str(DEFAULT_WORKSPACE), help="where outputs and the database live")
    parser.add_argument("--profile", default=None, help="calibration profile json (17장)")
    parser.add_argument("--producer", default="mock", choices=list(PRODUCERS),
                        help="reasoning backend; mock decides nothing (18장)")
    parser.add_argument("--ollama-host", default=None, metavar="URL",
                        help="where Ollama is (default $OLLAMA_HOST or http://localhost:11434)")
    parser.add_argument("--ollama-model", default=None, metavar="NAME",
                        help="Ollama model. 5.2 needs one that takes images - llava, "
                             "qwen2.5vl, gemma3. A text-only model judges from words alone")
    parser.add_argument("--ollama-num-ctx", type=int, default=None, metavar="N",
                        help="context window for the Ollama model")
    parser.add_argument("--strict", action="store_true", help="refuse to read provisional parameters (17.5)")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    # The same global flags again, accepted AFTER the subcommand as well:
    # `aicut run film.mkv --producer anthropic` is what people type, and
    # argparse's answer to it is "unrecognized arguments", which reads like the
    # option does not exist. SUPPRESS is what makes this safe - without it each
    # subparser would write its own default over the value given before the
    # subcommand, so `aicut --producer anthropic run ...` would silently run on
    # the mock.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--workspace", default=argparse.SUPPRESS)
    common.add_argument("--profile", default=argparse.SUPPRESS)
    common.add_argument("--producer", default=argparse.SUPPRESS, choices=list(PRODUCERS))
    common.add_argument("--strict", action="store_true", default=argparse.SUPPRESS)
    common.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS)

    def _sub(name, **kwargs):
        kwargs["parents"] = list(kwargs.get("parents", ())) + [common]
        return sub.add_parser(name, **kwargs)

    run = _sub("run", help="process one broadcast end to end")
    run.add_argument("source", help="the livestream file (.mp4/.mkv)")
    run.add_argument("--transcript", help="use an existing WhisperX-shaped transcript instead of running STT")
    run.add_argument("--no-stt", action="store_true", help="skip STT entirely (uses stored utterances)")
    run.add_argument("--length-hint", type=float, default=None, help="target length hint in seconds (2.6: a hint)")
    run.add_argument("--channel", default=None)
    run.add_argument(
        "--stop-after", default=None,
        choices=[s.value for s in STOP_AFTER_STAGES],
        help="stop once this stage is done (only the stages the runner checks)",
    )
    run.add_argument("--no-render", action="store_true", help="stop after the edit plan (MVP 5)")
    run.add_argument("--frames", action="store_true", help="sample frames for the visual half of each pass")
    run.add_argument("--backend", choices=["faster-whisper", "whisperx", "pocketsphinx"], default="whisperx")
    run.add_argument("--stt-model", default="large-v3")
    run.add_argument("--compute-type", default="int8")
    run.add_argument("--language", default=None)
    run.add_argument("--device", default="cuda")
    run.add_argument("--hf-token", default=None)
    run.add_argument("--no-diarize", action="store_true")
    run.set_defaults(func=cmd_run)

    resume = _sub("resume", help="continue a project that stopped part way (16장)")
    resume.add_argument("project")
    resume.add_argument("--transcript")
    resume.add_argument("--no-render", action="store_true")
    resume.set_defaults(func=cmd_resume)

    status = _sub("status", help="list projects or show one")
    status.add_argument("project", nargs="?")
    status.set_defaults(func=cmd_status)

    candidates = _sub("candidates", help="review discovered candidates (15.4)")
    candidates.add_argument("project")
    candidates.add_argument("--candidate")
    candidates.add_argument("--verdict", choices=["agree", "disagree"])
    candidates.add_argument("--note")
    candidates.add_argument(
        "--assess", action="append", metavar="N=ANSWER",
        help="19장 MVP 3 항목별 평가 on --candidate. N is 1-4 (or the item's own "
             "words), ANSWER is yes, no or unclear. Repeatable",
    )
    candidates.add_argument("--assess-items", action="store_true",
                            help="print the four items 원본 32장 evaluates and stop")
    candidates.set_defaults(func=cmd_candidates)

    gate = _sub("gate", help="measure a 19장 MVP gate")
    gate.add_argument("gate", choices=["mvp1", "mvp2", "mvp3"])
    gate.add_argument("project", nargs="?", help="mvp2 and mvp3: which project")
    gate.add_argument("--reference", metavar="REF_ID",
                      help="mvp1: which stored reference analysis to judge")
    gate.add_argument("--verdict", choices=["agree", "disagree"],
                      help="mvp1: does the analysis match why the video was made")
    gate.add_argument("--note", help="mvp1: kept with the verdict")
    gate.add_argument("--density", default="60,120,240", metavar="SEC,SEC",
                      help="scan.pass1_window_sec values to measure (19장 MVP 2 실측 항목)")
    gate.add_argument("--frames", action="store_true",
                      help="sample frames for the pass (5.2 reads 화면 and 소리 together)")
    gate.set_defaults(func=cmd_gate)

    plan = _sub("plan", help="print an edit plan in human form")
    plan.add_argument("plan")
    plan.set_defaults(func=cmd_plan)

    render = _sub("render", help="render (or re-render) from an edit plan")
    render.add_argument("plan")
    render.set_defaults(func=cmd_render)

    export_p = _sub("export", help="write the plan as EDL/FCPXML/SRT for a video editor")
    export_p.add_argument("plan")
    export_p.add_argument("--format", action="append",
                          choices=["edl", "fcpxml", "srt"],
                          help="repeatable; defaults to fcpxml")
    export_p.add_argument("--fps", type=float, default=None,
                          help="the editor sequence's frame rate (24, 25, 30, 29.97, ...)")
    export_p.add_argument("--out", default=None, help="output file, or a directory for several")
    export_p.set_defaults(func=cmd_export)

    review = _sub("review", help="approve or reject an episode (11.3 gate)")
    review.add_argument("episode")
    review.add_argument("action", choices=["approve", "reject", "thumbnail"])
    review.add_argument("--reviewer", help="required for approve and reject (11.3)")
    review.add_argument("--note")
    review.add_argument("--index", type=int, default=0, metavar="N",
                        help="thumbnail: which candidate (0-based) 11.1 offered")
    review.set_defaults(func=cmd_review)

    quota = _sub("quota", help="YouTube quota state and the next PT reset (11.4)")
    quota.set_defaults(func=cmd_quota)

    calibrate = _sub("calibrate", help="sweep parameters against a labelled dataset (17.4)")
    calibrate.add_argument("--init", action="store_true",
                           help="17.4 step 1: measure starting values from a processed broadcast")
    calibrate.add_argument("--project", help="which project's cached signals to measure (--init)")
    calibrate.add_argument("--dataset", help="17.2 dataset json (see `aicut dataset`)")
    calibrate.add_argument("--grid", help="json of dotted parameter path -> values (default: a built-in grid)")
    calibrate.add_argument("--harness", help="optional: your own python file exposing run(profile, dataset)")
    calibrate.add_argument("--channel")
    calibrate.add_argument("--out")
    calibrate.set_defaults(func=cmd_calibrate)

    prof = _sub("profile", help="show a calibration profile and what is still a guess")
    prof.add_argument("--list", action="store_true", help="list the profiles measured so far (13장)")
    prof.set_defaults(func=cmd_profile)

    learn = _sub("learn", help="run a learning loop (12.3)")
    learn.add_argument("loop", choices=["reference", "pairs", "performance"])
    learn.add_argument("--query", action="append", help="reference search query (loop A, repeatable)")
    learn.add_argument("--per-query", type=int, default=25)
    learn.add_argument(
        "--download", action=argparse.BooleanOptionalAction, default=True,
        help="loop A: fetch each reference video with yt-dlp (default: on)",
    )
    learn.add_argument(
        "--comments", action=argparse.BooleanOptionalAction, default=True,
        help="loop A: read each reference's comments through a browser, not the "
             "API, so it costs no quota (4.2). Default: on",
    )
    learn.add_argument(
        "--comment-limit", type=int, default=None, metavar="N",
        help="loop A: stop after N comments per video. Default: all of them",
    )
    learn.add_argument("--source", help="loop B: the source broadcast, 원본 (video file)")
    learn.add_argument("--output", help="loop B: the finished video made from it, 완성본")
    learn.add_argument(
        "--source-duration", type=float, default=None, metavar="SEC",
        help="loop B: length of the source broadcast, when it has not been run here",
    )
    learn.add_argument(
        "--dataset", help="loop B: where to write the 17.2 dataset entry "
                          "(default: <workspace>/datasets/<source>.json)",
    )
    learn.add_argument("--source-ref")
    learn.add_argument("--output-ref")
    learn.add_argument("--project", help="loop C: which project's published episodes")
    learn.add_argument("--days", type=int, default=28)
    learn.add_argument("--client-secrets", default="client_secrets.json")
    learn.add_argument("--token")
    learn.set_defaults(func=cmd_learn)

    upload = _sub("upload", help="upload privately, publish an approved episode, or retry (11.3, 11.4)")
    upload.add_argument("episode", nargs="?")
    upload.add_argument("--publish", action="store_true", help="make an approved episode public")
    upload.add_argument("--retry", action="store_true", help="drain the quota retry queue")
    upload.add_argument("--project")
    upload.add_argument("--client-secrets", default="client_secrets.json")
    upload.add_argument("--token")
    upload.set_defaults(func=cmd_upload)

    ui = _sub("ui", help="operator screens: submit, monitor, review (15장)")
    # 15.2 lets an operator submit a file and nothing else, so the server needs
    # the same recogniser settings `aicut run` takes (18장).
    ui.add_argument("--backend", choices=["faster-whisper", "whisperx", "pocketsphinx"],
                    default="whisperx", help="STT backend for submissions with no transcript")
    ui.add_argument("--stt-model", default="large-v3")
    ui.add_argument("--compute-type", default="int8")
    ui.add_argument("--language", default=None)
    ui.add_argument("--device", default="cuda")
    ui.add_argument("--hf-token", default=None)
    ui.add_argument("--no-diarize", action="store_true")
    ui.add_argument("--client-secrets", default="client_secrets.json",
                    help="OAuth client secrets, for 15.5's upload button")
    ui.add_argument("--token", help="stored OAuth token (11.4)")
    ui.add_argument("--host", default="127.0.0.1")
    ui.add_argument("--port", type=int, default=8765)
    ui.add_argument(
        "--api-key",
        help="require Authorization: Bearer <key> on /api/* (or set AICUT_UI_API_KEY)",
    )
    ui.add_argument(
        "--backup-every", type=float, default=None, metavar="SEC",
        help="take a database snapshot this often while the UI runs (default: never)",
    )
    ui.add_argument("--backup-keep", type=int, default=7, metavar="N",
                    help="how many snapshots to retain (default: 7)")
    ui.set_defaults(func=cmd_ui)

    backup = _sub("backup", help="snapshot the workspace database")
    backup.add_argument("--list", action="store_true", help="list snapshots instead of taking one")
    backup.add_argument("--keep", type=int, default=7, metavar="N",
                        help="how many snapshots to retain (default: 7)")
    backup.set_defaults(func=cmd_backup)

    transcribe = _sub("transcribe", help="run STT and write a transcript (20.2)")
    transcribe.add_argument("source")
    transcribe.add_argument("-o", "--out")
    transcribe.add_argument("--backend", choices=["faster-whisper", "whisperx", "pocketsphinx"], default="faster-whisper",
                            help="faster-whisper runs on a CPU; whisperx wants a GPU")
    transcribe.add_argument("--stt-model", default="base")
    transcribe.add_argument("--device", default="cpu")
    transcribe.add_argument("--compute-type", default="int8")
    transcribe.add_argument("--language", default=None)
    transcribe.add_argument("--hf-token", default=None)
    transcribe.add_argument("--no-diarize", action="store_true")
    transcribe.set_defaults(func=cmd_transcribe)

    dataset = _sub("dataset", help="build the labelled calibration dataset (17.2)")
    dataset.add_argument("action", choices=["init", "add-content", "add-silence", "derive-silences", "show"])
    dataset.add_argument("file")
    dataset.add_argument("--source", help="the broadcast this dataset labels")
    dataset.add_argument("--transcript")
    dataset.add_argument("--output", help="the video a human cut from it (17.2 b)")
    dataset.add_argument("--output-transcript", help="its transcript, for derive-silences")
    dataset.add_argument("--channel")
    dataset.add_argument("--start", help="timestamp: 91.5, 1:31.5 or 01:12:30")
    dataset.add_argument("--end")
    dataset.add_argument("--note")
    dataset.add_argument("--kept", action="store_true", help="add-silence: the human kept this pause")
    dataset.add_argument("--force", action="store_true")
    dataset.set_defaults(func=cmd_dataset)

    benchmark = _sub("benchmark", help="measure signal extraction on this machine (R3, 20.2)")
    benchmark.add_argument("source")
    benchmark.add_argument("--frames", action="store_true",
                           help="also time frame sampling and face detection")
    benchmark.set_defaults(func=cmd_benchmark)

    fetch_p = _sub("fetch-ffmpeg", help="download a static ffmpeg into the workspace")
    fetch_p.add_argument("--sha256", default=None, metavar="DIGEST",
                         help="the publisher's SHA-256 for this build; required when "
                              "none is recorded here, because an unverified ffmpeg "
                              "runs whatever the network returns")
    fetch_p.add_argument("--force", action="store_true", help="fetch even if one is already present")
    fetch_p.set_defaults(func=cmd_fetch_ffmpeg)

    doctor = _sub("doctor", help="check the prerequisites of 20.2")
    doctor.set_defaults(func=cmd_doctor)
    return parser


def _force_utf8_console() -> None:
    """Print Korean paths and titles without dying on the console codepage.

    A Windows console defaults to the system codepage (cp949 for a Korean
    install), and printing a path this tool routinely handles -
    방송_2026-08-19.mkv - raises UnicodeEncodeError there. Reconfiguring the
    streams costs nothing on POSIX, where they are already UTF-8.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):          # a redirected or closed stream
            pass


def main(argv: list[str] | None = None) -> int:
    _force_utf8_console()
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    try:
        return args.func(args)
    except AicutError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
