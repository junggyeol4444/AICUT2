import subprocess
import unittest


class PullRequestReadinessTest(unittest.TestCase):
    def test_repository_has_no_binary_diff_or_conflict_markers(self):
        result = subprocess.run(
            ["python3", "scripts/check_pr_ready.py"], capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PR-ready", result.stdout)


if __name__ == "__main__":
    unittest.main()
