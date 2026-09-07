import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from backend.stt import SttError, SttJob, build_stt_command, normalize_whisperx, transcribe_range, transcribe_tracks


class SttAdapterTest(unittest.TestCase):
    def test_command_is_argument_array_with_optional_language(self):
        command = build_stt_command(
            ["python3", "-m", "whisperx"],
            SttJob("track.wav", 0, "/tmp/stt/track.json", "ko"),
        )
        self.assertEqual(command[:3], ["python3", "-m", "whisperx"])
        self.assertEqual(command[-2:], ["--language", "ko"])

    def test_whisperx_words_and_speaker_are_normalized(self):
        segments = normalize_whisperx({"segments": [{
            "start": 1, "end": 3, "text": " 안녕하세요 ", "speaker": "SPEAKER_00",
            "words": [
                {"start": 1, "end": 2, "word": "안녕", "score": .9},
                {"start": 2, "end": 3, "word": "하세요", "score": .8},
            ],
        }]}, 0, 10)
        self.assertEqual(segments[0]["speaker_tag"], "SPEAKER_00")
        self.assertAlmostEqual(segments[0]["confidence"], .85)
        self.assertEqual(segments[0]["words"][0]["word"], "안녕")

    def test_multiple_tracks_are_merged_by_timestamp(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            call_index = 0
            def runner(command, **kwargs):
                nonlocal call_index
                payload = {"segments": [{
                    "start": 2 - call_index, "end": 3 - call_index,
                    "text": f"track {call_index}", "words": [],
                }]}
                (output / f"audio-track-{call_index:02d}.json").write_text(json.dumps(payload))
                call_index += 1
                return SimpleNamespace(returncode=0, stderr="")
            result = transcribe_tracks(["whisperx"], ["a.wav", "b.wav"], 10, output, runner=runner)
        self.assertEqual([segment["track_index"] for segment in result["segments"]], [1, 0])
        self.assertEqual(len(result["commands"]), 2)

    def test_missing_result_file_is_an_error(self):
        runner = lambda *args, **kwargs: SimpleNamespace(returncode=0, stderr="")
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(SttError):
            transcribe_tracks(["whisperx"], ["a.wav"], 10, directory, runner=runner)

    @patch("backend.stt.shutil.which", return_value="/usr/bin/ffmpeg")
    def test_chunk_transcription_restores_absolute_word_timestamps(self, _which):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)

            def runner(command, **_kwargs):
                if command[0] == "whisperx":
                    result = output / "transcript" / "audio-track-00.json"
                    result.parent.mkdir(parents=True, exist_ok=True)
                    result.write_text(json.dumps({"segments": [{
                        "start": 1, "end": 2, "text": "chunk", "words": [{
                            "start": 1, "end": 2, "word": "chunk", "score": .9,
                        }],
                    }]}), encoding="utf-8")
                return SimpleNamespace(returncode=0, stderr="")

            result = transcribe_range(
                ["whisperx"], ["track.wav"], 100, output,
                start_sec=40, end_sec=50, language="ko", runner=runner,
            )
            self.assertEqual((result["segments"][0]["start_sec"], result["segments"][0]["end_sec"]), (41, 42))
            self.assertEqual(result["segments"][0]["words"][0]["start_sec"], 41)
            self.assertEqual(len(result["commands"]), 2)
