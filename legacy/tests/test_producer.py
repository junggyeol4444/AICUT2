import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from backend.producer import ProducerError, run_producer, validate_analysis_manifest


FIXTURE = Path(__file__).parent / "fixtures" / "analysis-manifest.json"


class ProducerTest(unittest.TestCase):
    def test_fixture_contract_is_valid(self):
        payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.assertIs(validate_analysis_manifest(payload, 5000), payload)

    def test_invalid_references_and_source_ranges_are_rejected(self):
        payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
        payload["candidates"][0]["event_ids"] = ["missing-event"]
        with self.assertRaisesRegex(ProducerError, "존재하지 않는 사건"):
            validate_analysis_manifest(payload, 5000)
        payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
        payload["episodes"][0]["timeline"][0]["source_end_sec"] = 6000
        with self.assertRaisesRegex(ProducerError, "시간 범위"):
            validate_analysis_manifest(payload, 5000)

    def test_external_producer_uses_json_file_contract(self):
        payload = json.loads(FIXTURE.read_text(encoding="utf-8"))

        def runner(command, **_kwargs):
            output = Path(command[command.index("--output") + 1])
            output.write_text(json.dumps(payload), encoding="utf-8")
            return SimpleNamespace(returncode=0, stderr="")

        with tempfile.TemporaryDirectory() as directory:
            result = run_producer(["producer"], {"project": {"project_id": "p"}}, directory, 5000, runner)
        self.assertEqual(result["command"][0], "producer")
        self.assertEqual(result["manifest"]["events"][0]["event_id"], "event-operation")


if __name__ == "__main__":
    unittest.main()
