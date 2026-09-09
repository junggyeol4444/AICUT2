import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from backend.retrieval import RetrievalError, run_scene_retrieval


class SceneRetrievalTest(unittest.TestCase):
    def test_semantic_scene_results_keep_score_role_and_reasons(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)

            def runner(_command, **_kwargs):
                (output / "retrieval-output.json").write_text(json.dumps({"scenes": [{
                    "candidate_id": "candidate-1", "query": "first reaction", "start_sec": 10,
                    "end_sec": 12, "score": .91, "scene_role": "reaction",
                    "reasons": ["speaker_match", "face_reaction"],
                }]}), encoding="utf-8")
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            result = run_scene_retrieval(
                ["retriever"], {"candidates": [{"candidate_id": "candidate-1"}]}, output, 100, runner,
            )
            self.assertEqual(result["scenes"][0]["scene_role"], "reaction")
            self.assertEqual(result["scenes"][0]["reasons"], ["speaker_match", "face_reaction"])

    def test_scene_cannot_reference_an_unknown_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)

            def runner(_command, **_kwargs):
                (output / "retrieval-output.json").write_text(json.dumps({"scenes": [{
                    "candidate_id": "missing", "start_sec": 1, "end_sec": 2, "score": .5,
                    "reasons": ["keyword"],
                }]}))
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with self.assertRaises(RetrievalError):
                run_scene_retrieval(
                    ["retriever"], {"candidates": [{"candidate_id": "candidate-1"}]}, output, 100, runner,
                )


if __name__ == "__main__":
    unittest.main()
