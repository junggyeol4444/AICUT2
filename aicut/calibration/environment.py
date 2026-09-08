"""17.4 step 4: 방송 환경 변경(마이크·게임·합방 여부) 시 재측정.

A profile is measured against one channel's material in one setup. 17.4 says
that when the setup changes the values have to be measured again — which is
only actionable if somebody notices the change. Nobody watches for it, so the
program does: a profile records the environment it was measured in, and each
run compares the broadcast in front of it against that record.

This raises the question, it does not answer it. 17.4 step 4 asks for a
re-measurement, and re-measuring needs a labelled dataset (17.2) that only a
person can supply. So a drift is reported on the run and left there; nothing
here adjusts a parameter, which would be the code deciding a value 17.1 says
belongs to the profile.

The three things 17.4 names, and how each is read off the source:

  마이크    the audio track layout — how many, their roles, their sample rate.
            A profile measured on a four-track OBS recording does not describe
            a single mixed track, and the silence level least of all.
  게임      how much of the broadcast is 게임 플레이, from the 5.3 labels.
  합방      how much of it is 다인원, and how many distinct speakers appear.
"""

from __future__ import annotations

from typing import Any, Sequence

from aicut.models import SituationLabel, UNKNOWN_SPEAKER

#: How far a proportion may move before it is worth mentioning. A broadcast is
#: never twice the same, so a small drift is noise; this is the line between
#: noise and a different setup, and like every other line it is provisional.
DEFAULT_MIX_TOLERANCE = 0.25


def fingerprint(
    media: Any,
    situations: Sequence[Any] = (),
    utterances: Sequence[Any] = (),
) -> dict[str, Any]:
    """What kind of broadcast this is, in the three terms 17.4 names."""
    tracks = list(getattr(media, "audio_tracks", []) or [])
    total = sum(
        max(0.0, float(s.end_sec) - float(s.start_sec)) for s in situations
    ) or 0.0

    def share(label: SituationLabel) -> float:
        if total <= 0:
            return 0.0
        inside = sum(
            max(0.0, float(s.end_sec) - float(s.start_sec))
            for s in situations if s.label == label
        )
        return round(inside / total, 4)

    speakers = {
        u.speaker for u in utterances
        if getattr(u, "speaker", UNKNOWN_SPEAKER) != UNKNOWN_SPEAKER
    }
    return {
        "mic": {
            "track_count": len(tracks),
            "roles": sorted({t.role for t in tracks if t.role}),
            "multitrack": bool(getattr(media, "is_multitrack", False)),
        },
        "game": {"gameplay_share": share(SituationLabel.GAMEPLAY)},
        "collab": {
            "multi_person_share": share(SituationLabel.MULTI_PERSON),
            "speaker_count": len(speakers),
        },
    }


def compare(
    measured: dict[str, Any] | None,
    current: dict[str, Any],
    *,
    tolerance: float | None = None,
) -> list[str]:
    """What changed since the profile was measured, in plain sentences.

    Empty means nothing worth re-measuring for. A profile with no recorded
    environment is not a mismatch — it is a profile from before this existed,
    and saying so every run would be noise.
    """
    if not measured:
        return []
    if tolerance is None:
        tolerance = DEFAULT_MIX_TOLERANCE

    drift: list[str] = []
    was, now = measured.get("mic", {}), current.get("mic", {})
    if was.get("track_count") != now.get("track_count"):
        drift.append(
            f"마이크: the profile was measured on {was.get('track_count')} audio track(s), "
            f"this broadcast has {now.get('track_count')}"
        )
    elif sorted(was.get("roles") or []) != sorted(now.get("roles") or []):
        drift.append(
            f"마이크: track roles were {was.get('roles')}, now {now.get('roles')}"
        )

    for key, section, label in (
        ("gameplay_share", "game", "게임"),
        ("multi_person_share", "collab", "합방"),
    ):
        before = float((measured.get(section) or {}).get(key, 0.0))
        after = float((current.get(section) or {}).get(key, 0.0))
        if abs(after - before) > tolerance:
            drift.append(
                f"{label}: {key.replace('_', ' ')} was {before:.0%} when the profile was "
                f"measured, this broadcast is {after:.0%}"
            )
    return drift
