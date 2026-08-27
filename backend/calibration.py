from __future__ import annotations

from dataclasses import dataclass, asdict
from itertools import product
from typing import Iterable


class CalibrationError(ValueError):
    pass


@dataclass(frozen=True)
class PacingSample:
    silence_db: float
    silence_duration_sec: float
    previous_tension: float
    speaker_transition: bool
    meaningful_reaction: bool
    human_decision: str

    @classmethod
    def from_dict(cls, value: dict) -> "PacingSample":
        decision = str(value.get("human_decision", "")).upper()
        if decision not in {"KEEP", "TRIM", "CUT"}:
            raise CalibrationError("human_decision은 KEEP, TRIM 또는 CUT이어야 합니다.")
        tension = float(value["previous_tension"])
        if not 0 <= tension <= 1:
            raise CalibrationError("previous_tension은 0과 1 사이여야 합니다.")
        duration = float(value["silence_duration_sec"])
        if duration < 0:
            raise CalibrationError("silence_duration_sec은 음수일 수 없습니다.")
        return cls(
            silence_db=float(value["silence_db"]), silence_duration_sec=duration,
            previous_tension=tension, speaker_transition=bool(value.get("speaker_transition")),
            meaningful_reaction=bool(value.get("meaningful_reaction")), human_decision=decision,
        )


@dataclass(frozen=True)
class CalibrationResult:
    params: dict
    precision: float
    recall: float
    f1: float
    accuracy: float
    sample_count: int

    def to_dict(self) -> dict:
        return asdict(self)


def _midpoints(values: Iterable[float]) -> list[float]:
    unique = sorted(set(float(value) for value in values))
    if not unique:
        return []
    if len(unique) == 1:
        return unique
    return [unique[0], *((left + right) / 2 for left, right in zip(unique, unique[1:])), unique[-1]]


def classify(sample: PacingSample, params: dict) -> str:
    # Contextual preservation always wins over mechanical silence removal.
    if sample.meaningful_reaction or sample.speaker_transition:
        return "KEEP"
    if sample.previous_tension >= params["preserve_tension_min"]:
        return "KEEP"
    silent = sample.silence_db <= params["silence_db_max"]
    long_enough = sample.silence_duration_sec >= params["cut_duration_min_sec"]
    if silent and long_enough:
        return "CUT"
    if silent and sample.silence_duration_sec >= params["trim_duration_min_sec"]:
        return "TRIM"
    return "KEEP"


def _score(samples: list[PacingSample], params: dict) -> tuple[float, float, float, float]:
    expected_cut = [sample.human_decision == "CUT" for sample in samples]
    predicted = [classify(sample, params) for sample in samples]
    predicted_cut = [value == "CUT" for value in predicted]
    true_positive = sum(actual and guess for actual, guess in zip(expected_cut, predicted_cut))
    false_positive = sum(not actual and guess for actual, guess in zip(expected_cut, predicted_cut))
    false_negative = sum(actual and not guess for actual, guess in zip(expected_cut, predicted_cut))
    precision = true_positive / (true_positive + false_positive) if true_positive + false_positive else 0
    recall = true_positive / (true_positive + false_negative) if true_positive + false_negative else 0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0
    accuracy = sum(actual == guess for actual, guess in zip((s.human_decision for s in samples), predicted)) / len(samples)
    return precision, recall, f1, accuracy


def calibrate_pacing(raw_samples: list[dict]) -> CalibrationResult:
    samples = [PacingSample.from_dict(value) for value in raw_samples]
    if len(samples) < 4:
        raise CalibrationError("캘리브레이션에는 최소 4개의 사람 라벨 샘플이 필요합니다.")
    db_values = _midpoints(sample.silence_db for sample in samples)
    duration_values = _midpoints(sample.silence_duration_sec for sample in samples)
    tension_values = _midpoints(sample.previous_tension for sample in samples)
    best = None
    for silence_db, trim_duration, cut_duration, tension in product(
        db_values, duration_values, duration_values, tension_values
    ):
        if cut_duration < trim_duration:
            continue
        params = {
            "silence_db_max": round(silence_db, 4),
            "trim_duration_min_sec": round(trim_duration, 4),
            "cut_duration_min_sec": round(cut_duration, 4),
            "preserve_tension_min": round(tension, 4),
        }
        precision, recall, f1, accuracy = _score(samples, params)
        rank = (f1, accuracy, precision, recall, -cut_duration)
        if best is None or rank > best[0]:
            best = (rank, params, precision, recall, f1, accuracy)
    _, params, precision, recall, f1, accuracy = best
    return CalibrationResult(params, precision, recall, f1, accuracy, len(samples))
