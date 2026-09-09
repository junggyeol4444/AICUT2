"""PARSING: get the measurable facts out of the media (5.2, program side).

Speech, silence, loudness and visual change are extracted once and cached. The
multi-track assumption of 5.2/20장 is honoured here: speech is read from the
speech tracks, so diarisation is only ever asked for on a track that really
carries more than one person.
"""

from __future__ import annotations

import logging

from aicut.analysis.tension import build_tension_curve
from aicut.analysis.vocalburst import build_detector
from aicut.config import CalibrationProfile
from aicut.errors import ConfigError
from aicut.media import audio as audio_mod
from aicut.media import vision as vision_mod
from aicut.media.probe import probe, verify_tail
from aicut.media.stt import Transcriber, speaker_reliability
from aicut.models import UNKNOWN_SPEAKER
from aicut.pipeline.context import RunContext, SignalBundle

log = logging.getLogger(__name__)


#: The profile numbers that decide what the measuring itself produces. Anything
#: derived afterwards (tension weights, laughter) is rebuilt every run from the
#: cached RMS, so only these force the media to be decoded again.
MEASUREMENT_KEYS = (
    "silence.level_db",
    "silence.min_duration_sec",
    "silence.merge_gap_sec",
    "scan.pass1_frame_interval_sec",
)


def measurement_fingerprint(profile: CalibrationProfile) -> dict[str, float]:
    """What the cached signals were measured under (17.1).

    ``resume --profile`` with a re-tuned noise floor kept the old cache: the run
    reported the new profile and used silences found under the old one.

    A profile that does not carry one of these keys leaves it out rather than
    failing: an older profile file is still usable, and a key that is absent on
    both sides compares equal.
    """
    out: dict[str, float] = {}
    for key in MEASUREMENT_KEYS:
        try:
            out[key] = profile.get_float(key)
        except ConfigError:
            continue
    return out


def _transcribe_tracks(ctx: RunContext, transcriber, tracks) -> list:
    """Transcribe every speech-bearing track and merge them in time order (5.2).

    A single-track source, or one whose tracks the recorder did not label, keeps
    the old behaviour: one pass over the default stream.

    5.2 says 통화 트랙에 2인 이상이 있는 경우에만 트랙 내 화자 구분을 적용한다.
    A track that does not need diarisation carries one person, and that person is
    named by the recorder's own track title - not invented here. A track that
    does need it keeps whatever the recogniser worked out, and its label is
    prefixed with the track so two guests on a call are not confused with two
    people on a mixed recording.
    """
    if not tracks:
        return transcriber.transcribe(ctx.project.file_path, ctx.media, track_index=None)

    # A recogniser that cannot select a stream returns the same transcript for
    # every track. Asking it once per track merged that transcript into itself,
    # so a two-track broadcast came out with every utterance duplicated.
    if not getattr(transcriber, "separates_tracks", True):
        log.info(
            "%s does not read tracks separately: transcribing once for %d speech tracks",
            type(transcriber).__name__, len(tracks),
        )
        return transcriber.transcribe(ctx.project.file_path, ctx.media, track_index=None)

    merged: list = []
    for track in tracks:
        try:
            spoken = transcriber.transcribe(
                ctx.project.file_path, ctx.media, track_index=track.index,
            )
        except Exception as exc:
            # One unreadable track must not cost the others: a broadcast with a
            # damaged call stream is still a broadcast with a host on it (16장).
            log.warning("could not transcribe track %s (%s): %s", track.index, track.role, exc)
            ctx.report.setdefault("degraded", []).append({
                "reason": "track_not_transcribed",
                "detail": f"track {track.index} ({track.role or 'unknown'}): {exc}",
            })
            continue
        label = (track.title or track.role or f"track{track.index}").strip()
        for utterance in spoken:
            utterance.track = track.role or "unknown"
            if not track.needs_diarization:
                utterance.speaker = label
            elif utterance.speaker and utterance.speaker != UNKNOWN_SPEAKER:
                utterance.speaker = f"{label}:{utterance.speaker}"
        merged.extend(spoken)
        log.info("track %s (%s): %d utterances", track.index, track.role, len(spoken))
    merged.sort(key=lambda u: (u.start_sec, u.end_sec))
    return merged


def run(ctx: RunContext, transcriber: Transcriber | None = None, *, use_cache: bool = True) -> RunContext:
    # A caller that already probed the media (a resumed run, a test fixture, a
    # GPU box that did the measuring elsewhere) passes it in and skips decoding.
    # The file checks go with it: they open the file, and a caller holding the
    # media has already looked at it.
    inspect_file = ctx.media is None
    if inspect_file:
        ctx.media = probe(ctx.project.file_path)

    warnings: list[str] = []
    if inspect_file:
        warnings.extend(ctx.media.validate())
        truncated = verify_tail(ctx.project.file_path, ctx.media.duration_sec)
        if truncated:
            warnings.append(truncated)
    for warning in warnings:
        log.warning("%s", warning)
        ctx.report.setdefault("source_warnings", []).append(warning)

    if ctx.media.duration_sec:
        ctx.project.duration_sec = ctx.media.duration_sec
        ctx.store.set_duration(ctx.project.project_id, ctx.media.duration_sec)

    fingerprint = measurement_fingerprint(ctx.profile)
    cached = None
    if use_cache and ctx.signal_cache_path.exists():
        cached = SignalBundle.load(ctx.signal_cache_path)
        if cached.measured_with and cached.measured_with != fingerprint:
            changed = sorted(
                k for k in set(cached.measured_with) | set(fingerprint)
                if cached.measured_with.get(k) != fingerprint.get(k)
            )
            log.info("re-measuring: the profile changed %s since the cache", ", ".join(changed))
            ctx.report.setdefault("cache_invalidated", []).append({
                "reason": "profile_changed",
                "keys": changed,
            })
            cached = None

    if cached is not None:
        ctx.signals = cached
        log.info("reusing cached signals from %s", ctx.signal_cache_path)
    else:
        # 5.2 separates 내 마이크 / 통화 / 게임 / BGM, and people talk on two of
        # them. Measuring from the mic alone made a stretch where only the guest
        # spoke look like dead air (9장 then cut it) - so silence is where every
        # speech track is quiet together, and the tension envelope is the
        # loudest of them at each moment.
        tracks = ctx.media.speech_tracks() if ctx.media.is_multitrack else []
        indexes: list[int | None] = [t.index for t in tracks] or [None]

        per_track = [
            audio_mod.detect_silences(ctx.project.file_path, ctx.profile, track_index=i)
            for i in indexes
        ]
        silences = (
            per_track[0] if len(per_track) == 1
            else audio_mod.intersect_silences(
                per_track, min_duration_sec=ctx.profile.get_float("silence.min_duration_sec"),
            )
        )
        envelopes = [
            audio_mod.rms_envelope(ctx.project.file_path, track_index=i) for i in indexes
        ]
        rms = envelopes[0] if len(envelopes) == 1 else audio_mod.loudest_envelope(envelopes)
        motion = vision_mod.motion_curve(
            ctx.project.file_path,
            interval_sec=ctx.profile.get_float("scan.pass1_frame_interval_sec"),
        )
        ctx.signals = SignalBundle(
            motion=motion, silences=silences, rms=rms, measured_with=fingerprint,
        )

    # The same tracks the silence and RMS above were measured from. Without the
    # index a recogniser decodes the container's default stream, so on a
    # multi-track source the signals and the transcript can come from different
    # audio (5.2).
    speech_tracks = ctx.media.speech_tracks() if ctx.media.is_multitrack else []

    utterances = []
    if transcriber is not None:
        utterances = _transcribe_tracks(ctx, transcriber, speech_tracks)
        ctx.store.replace_utterances(ctx.project.project_id, utterances)
    else:
        utterances = ctx.store.utterances(ctx.project.project_id)

    # Laughter and screams are derived from the cached RMS plus the transcript,
    # so a re-tuned profile changes them without touching the media (9.1).
    detector = build_detector(ctx.profile.get("laughter.detector"))
    laughter = None
    if detector is not None and ctx.signals.rms:
        ctx.signals.bursts = detector.detect(ctx.signals.rms, utterances, ctx.profile)
        laughter = detector.as_signal(ctx.signals.bursts)
        ctx.note("vocal_bursts", len(ctx.signals.bursts))
    else:
        ctx.note(
            "laughter_note",
            "no vocal burst detector: the laughter weight is redistributed rather than scored zero (9.1)",
        )

    # The tension curve is derived, not measured: rebuild it from the cached RMS
    # every run so a re-tuned profile takes effect without touching the media.
    ctx.signals.tension = build_tension_curve(ctx.signals.rms, utterances, ctx.profile, laughter=laughter)
    if utterances:
        ctx.signals.speaker_reliability = speaker_reliability(utterances)

    ctx.signals.save(ctx.signal_cache_path)
    ctx.note("media", {
        "duration_sec": ctx.media.duration_sec,
        "resolution": f"{ctx.media.width}x{ctx.media.height}",
        "audio_tracks": [{"index": t.index, "role": t.role, "title": t.title} for t in ctx.media.audio_tracks],
        "multitrack": ctx.media.is_multitrack,
    })
    ctx.note("utterance_count", len(utterances))
    if speech_tracks:
        ctx.note("speech_tracks", [
            {"index": t.index, "role": t.role, "title": t.title} for t in speech_tracks
        ])
    ctx.note("speaker_reliability", round(ctx.signals.speaker_reliability, 3))
    if ctx.signals.speaker_reliability < 1.0:
        ctx.note(
            "speaker_note",
            "some speech carries no speaker tag; speaker-dependent staging is disabled for those parts (16장)",
        )
    return ctx
