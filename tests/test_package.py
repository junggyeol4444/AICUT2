import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from backend.package import (
    MetadataPackage, PackageError, build_thumbnail_commands,
    description_with_chapters, extract_thumbnails, write_metadata_package,
)


class PackageTest(unittest.TestCase):
    def setUp(self):
        self.metadata = MetadataPackage.from_dict({
            "title_options": ["제목 A", "제목 B", "제목 C"],
            "description": "에피소드 설명",
            "tags": ["#게임", "하이라이트"],
            "chapters": [{"start_sec": 0, "title": "결과 예고"}, {"start_sec": 94, "title": "작전 시작"}],
        })

    def test_writes_reviewable_json_and_text_without_binary_assets(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = write_metadata_package(self.metadata, directory)
            payload = json.loads(Path(paths["json_path"]).read_text())
            text = Path(paths["text_path"]).read_text()
        self.assertEqual(len(payload["title_options"]), 3)
        self.assertIn("00:01:34 작전 시작", payload["description_with_chapters"])
        self.assertIn("#게임", text)

    def test_thumbnail_commands_seek_before_input_and_use_numbered_outputs(self):
        commands = build_thumbnail_commands("ffmpeg", "video.mp4", [12.5, 94], "/tmp/thumbs")
        self.assertEqual(len(commands), 2)
        self.assertLess(commands[0].index("-ss"), commands[0].index("-i"))
        self.assertTrue(commands[1][-1].endswith("thumbnail-02.jpg"))

    def test_thumbnail_extraction_runs_each_command(self):
        calls = []
        runner = lambda command, **kwargs: (calls.append(command) or SimpleNamespace(returncode=0, stderr=""))
        with tempfile.TemporaryDirectory() as directory, patch("backend.package.shutil.which", return_value="/usr/bin/ffmpeg"):
            paths = extract_thumbnails("video.mp4", [1, 2, 3], directory, runner)
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(paths), 3)

    def test_rejects_invalid_titles_and_chapter_order(self):
        with self.assertRaises(PackageError):
            MetadataPackage.from_dict({"title_options": ["하나"]})
        with self.assertRaises(PackageError):
            MetadataPackage.from_dict({
                "title_options": ["A", "B", "C"],
                "chapters": [{"start_sec": 10, "title": "뒤"}, {"start_sec": 2, "title": "앞"}],
            })
