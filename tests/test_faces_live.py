"""Face detection with OpenCV actually installed (5.3, 9.2, 11.1).

Everything else about the face signal is tested through injected values. This
runs the detector, on both of the backends OpenCV has offered across versions,
and then checks the thing the signal exists for: that a talking-head stretch
and a gameplay stretch stop being labelled UNKNOWN and start being labelled
differently from each other.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from aicut.analysis.signals import label_situations
from aicut.config import CalibrationProfile
from aicut.media import faces as faces_mod
from aicut.media.ffmpeg_util import have_ffmpeg
from aicut.models import SituationLabel, Utterance


def backend_name() -> str | None:
    detector = faces_mod.build_detector()
    return detector.backend if detector else None


def a_drawn_face_is_enough() -> bool:
    """Whether the drawn fixtures below can exercise the detector that is loaded.

    The cascade backend fires on the geometry of a face - an oval with two dark
    patches and a line - which is why these fixtures could be drawn at all.
    YuNet is a learned detector trained on photographs and does not fire on a
    drawing; measured here, it returns a face ratio of exactly 0.0 for the
    fixture below while finding faces in real footage.

    So the drawn tests gate on the cascade, and the YuNet path is exercised by
    AICUT_TEST_FACE_IMAGE - a real photograph the runner supplies. Pretending a
    drawing is a face would make the yunet backend look tested when it is not.
    """
    return backend_name() == "cascade"


def a_detector_can_be_built() -> bool:
    """OpenCV being importable is not the same as a detector existing.

    OpenCV 5.0 removed CascadeClassifier and ships no face model, so on 5.x a
    detector needs a YuNet .onnx supplied through AICUT_FACE_MODEL. Gating these
    on `available()` alone made every one of them fail the moment OpenCV 5 was
    installed - against code that was answering correctly, by declining to build
    a detector it has no model for.
    """
    return faces_mod.build_detector() is not None


def _draw_face(path: Path, *, size=(640, 360), radius=(90, 120), center=(320, 180)) -> None:
    """A crude frontal face: light oval, dark brows, eyes, nose, mouth."""
    import cv2
    import numpy as np

    width, height = size
    cx, cy = center
    rx, ry = radius
    image = np.full((height, width, 3), 140, np.uint8)
    cv2.ellipse(image, (cx, cy), (rx, ry), 0, 0, 360, (215, 215, 215), -1)
    eye_dx, eye_dy = int(rx * 0.36), int(ry * 0.25)
    eye_r = (max(4, int(rx * 0.18)), max(3, int(ry * 0.075)))
    cv2.ellipse(image, (cx - eye_dx, cy - eye_dy), eye_r, 0, 0, 360, (40, 40, 40), -1)
    cv2.ellipse(image, (cx + eye_dx, cy - eye_dy), eye_r, 0, 0, 360, (40, 40, 40), -1)
    brow_y = cy - int(ry * 0.43)
    cv2.rectangle(image, (cx - eye_dx - eye_r[0], brow_y),
                  (cx - eye_dx + eye_r[0], brow_y + max(3, int(ry * 0.05))), (30, 30, 30), -1)
    cv2.rectangle(image, (cx + eye_dx - eye_r[0], brow_y),
                  (cx + eye_dx + eye_r[0], brow_y + max(3, int(ry * 0.05))), (30, 30, 30), -1)
    cv2.ellipse(image, (cx, cy + int(ry * 0.08)), (max(4, int(rx * 0.11)), max(8, int(ry * 0.18))),
                0, 0, 360, (170, 170, 170), -1)
    cv2.ellipse(image, (cx, cy + int(ry * 0.43)), (int(rx * 0.42), max(6, int(ry * 0.1))),
                0, 0, 180, (50, 50, 50), -1)
    cv2.imwrite(str(path), image)


def _draw_gameplay(path: Path, size=(640, 360)) -> None:
    """A busy screen with no face in it."""
    import cv2
    import numpy as np

    width, height = size
    image = np.full((height, width, 3), 60, np.uint8)
    for i in range(0, width, 40):
        cv2.line(image, (i, 0), (i + 60, height), (90, 120, 90), 3)
    cv2.rectangle(image, (20, 20), (200, 60), (200, 60, 60), -1)
    cv2.rectangle(image, (width - 220, height - 70), (width - 20, height - 20), (60, 60, 200), -1)
    cv2.imwrite(str(path), image)


@unittest.skipUnless(a_detector_can_be_built(),
                     "no face detector: OpenCV<5, or OpenCV 5 with AICUT_FACE_MODEL set")
class FaceDetectorLiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.dir = Path(cls._tmp.name)
        cls.face = cls.dir / "face.png"
        cls.big_face = cls.dir / "big_face.png"
        cls.game = cls.dir / "game.png"
        _draw_face(cls.face)
        _draw_face(cls.big_face, radius=(150, 170))
        _draw_gameplay(cls.game)
        cls.detector = faces_mod.build_detector()

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_a_backend_was_chosen_and_named(self):
        self.assertIsNotNone(self.detector)
        self.assertIn(self.detector.backend, {"cascade", "yunet"})

    @unittest.skipUnless(a_drawn_face_is_enough(),
                         "the drawn fixture only exercises the cascade backend")
    def test_a_face_is_found_and_measured(self):
        reading = self.detector.read_frame(str(self.face), 12.0)
        self.assertEqual(reading.at_sec, 12.0)
        self.assertGreater(reading.face_ratio, 0.05)
        self.assertIsNotNone(reading.box)
        cx, cy = reading.center
        self.assertAlmostEqual(cx, 320, delta=60)
        self.assertAlmostEqual(cy, 180, delta=60)

    def test_a_screen_with_no_face_reads_zero(self):
        reading = self.detector.read_frame(str(self.game), 0.0)
        self.assertEqual(reading.face_ratio, 0.0)
        self.assertIsNone(reading.box)

    @unittest.skipUnless(a_drawn_face_is_enough(),
                         "the drawn fixture only exercises the cascade backend")
    def test_a_closer_face_fills_more_of_the_frame(self):
        near = self.detector.read_frame(str(self.big_face), 0.0)
        far = self.detector.read_frame(str(self.face), 0.0)
        self.assertGreater(near.face_ratio, far.face_ratio)

    def test_an_unreadable_file_reads_zero_rather_than_raising(self):
        broken = self.dir / "broken.png"
        broken.write_bytes(b"not a png")
        self.assertEqual(self.detector.read_frame(str(broken), 3.0).face_ratio, 0.0)

    @unittest.skipUnless(a_drawn_face_is_enough(),
                         "the drawn fixture only exercises the cascade backend")
    def test_expression_change_moves_with_the_face(self):
        still = [self.detector.read_frame(str(self.face), t) for t in (0.0, 1.0)]
        moved = [self.detector.read_frame(str(self.face), 0.0),
                 self.detector.read_frame(str(self.big_face), 1.0)]
        self.assertLess(faces_mod.expression_change(still, 0.5),
                        faces_mod.expression_change(moved, 0.5))

    @unittest.skipUnless(a_drawn_face_is_enough(),
                         "the drawn fixture only exercises the cascade backend")
    def test_the_signal_separates_talk_from_gameplay(self):
        """5.3: this is the whole reason the face signal exists."""
        profile = CalibrationProfile.load()
        speech = [Utterance(0, 40, "talking", speaker="HOST"),
                  Utterance(50, 88, "still talking", speaker="HOST")]

        face_readings = [self.detector.read_frame(str(self.big_face), float(t)) for t in range(0, 90, 10)]
        game_readings = [self.detector.read_frame(str(self.game), float(t)) for t in range(0, 90, 10)]

        talk = label_situations(90.0, speech, [], profile,
                                face_ratio=faces_mod.face_ratio_lookup(face_readings))
        game = label_situations(90.0, speech, [], profile,
                                face_ratio=faces_mod.face_ratio_lookup(game_readings))

        self.assertTrue(any(s.label is SituationLabel.SOLO_TALK for s in talk))
        self.assertTrue(any(s.label is SituationLabel.GAMEPLAY for s in game))
        self.assertFalse(any(s.label is SituationLabel.UNKNOWN for s in talk + game))


@unittest.skipUnless(a_detector_can_be_built() and have_ffmpeg(),
                     "needs ffmpeg and a usable face detector")
class FaceOnSampledFramesTests(unittest.TestCase):
    """Frames come off the source through ffmpeg before the detector sees them."""

    @unittest.skipUnless(a_drawn_face_is_enough(),
                         "the drawn fixture only exercises the cascade backend")
    def test_frames_sampled_from_a_video_carry_a_face_signal(self):
        from aicut.media.vision import sample_frames

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            work = Path(tmp)
            frame = work / "f.png"
            _draw_face(frame, size=(640, 360), radius=(140, 160))
            video = work / "talking.mp4"
            subprocess.run([
                "ffmpeg", "-hide_banner", "-v", "error", "-y", "-loop", "1", "-t", "6",
                "-i", str(frame), "-r", "10", "-c:v", "libx264", "-preset", "ultrafast",
                "-crf", "30", "-pix_fmt", "yuv420p", str(video),
            ], check=True)

            sampled = sample_frames(str(video), work / "frames", start_sec=0, duration_sec=6,
                                    interval_sec=2, width=640)
            self.assertTrue(sampled)
            detector = faces_mod.build_detector()
            readings = detector.read_frames([(f.at_sec, f.path) for f in sampled])
            self.assertTrue(any(r.face_ratio > 0.05 for r in readings),
                            "no face survived the ffmpeg round trip")


if __name__ == "__main__":
    unittest.main()


REAL_FACE = os.environ.get("AICUT_TEST_FACE_IMAGE", "")


@unittest.skipUnless(REAL_FACE and Path(REAL_FACE).is_file(),
                     "set AICUT_TEST_FACE_IMAGE to a photograph of a face")
class RealPhotographTests(unittest.TestCase):
    """The learned backend, on the only thing it recognises: an actual face.

    A drawn oval is not a face to YuNet, so the fixtures above cannot reach it.
    This does, when the runner supplies a photograph - which is also how the
    5.3 label and the 9.2 expression signal behave in production, where every
    frame is a photograph.
    """

    @classmethod
    def setUpClass(cls):
        cls.detector = faces_mod.build_detector()
        if cls.detector is None:
            raise unittest.SkipTest("no face detector could be built")

    def test_a_real_face_is_found(self):
        reading = self.detector.read_frame(REAL_FACE, 0.0)
        self.assertGreater(reading.face_ratio, 0.0, "no face found in the supplied photograph")
        self.assertIsNotNone(reading.box)

    def test_the_box_is_inside_the_frame(self):
        reading = self.detector.read_frame(REAL_FACE, 0.0)
        x, y, w, h = reading.box
        self.assertGreaterEqual(x, 0)
        self.assertGreaterEqual(y, 0)
        self.assertGreater(w, 0)
        self.assertGreater(h, 0)

    def test_a_face_that_does_not_move_reports_no_expression_change(self):
        """9.2's 표정: the same frame twice is a still face, not a missing one."""
        readings = [self.detector.read_frame(REAL_FACE, t) for t in (0.0, 1.0)]
        self.assertAlmostEqual(faces_mod.expression_change(readings, 0.5), 0.0, places=3)

    def test_a_frame_with_no_face_reads_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            blank = Path(tmp) / "blank.png"
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                            "-i", "color=c=gray:s=320x180:d=1", "-frames:v", "1", str(blank)],
                           check=True, capture_output=True)
            self.assertEqual(self.detector.read_frame(str(blank), 0.0).face_ratio, 0.0)
