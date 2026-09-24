"""Release tag verification refuses a tag moved after the push event."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bin" / "eggswap-verify-release-tag"


class ReleaseTagVerificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name) / "remote"
        self.repo.mkdir()
        self._git("init", "-q")
        self._git("config", "user.name", "Eggswap test")
        self._git("config", "user.email", "eggswap@example.invalid")
        (self.repo / "payload").write_text("first\n", encoding="utf-8")
        self._git("add", "payload")
        self._git("commit", "-qm", "first")
        self.first_sha = self._git("rev-parse", "HEAD")
        self._git("tag", "-a", "v0.1.1", "-m", "release")

    def tearDown(self):
        self.temp.cleanup()

    def _git(self, *args):
        result = subprocess.run(
            ["git", *args], cwd=self.repo, text=True, capture_output=True, check=True
        )
        return result.stdout.strip()

    def _verify(self, event_sha):
        env = os.environ.copy()
        env.update(
            RELEASE_TAG="v0.1.1",
            EVENT_SHA=event_sha,
            REPOSITORY_URL=str(self.repo),
        )
        return subprocess.run(
            [sys.executable, str(SCRIPT)], cwd=ROOT, env=env, text=True, capture_output=True
        )

    def test_current_annotated_tag_target_matches_event_sha(self):
        result = self._verify(self.first_sha)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("verified v0.1.1", result.stdout)

    def test_moved_tag_target_refuses_rerun_for_original_event_sha(self):
        (self.repo / "payload").write_text("second\n", encoding="utf-8")
        self._git("commit", "-qam", "second")
        self._git("tag", "-fa", "v0.1.1", "-m", "moved release tag")

        result = self._verify(self.first_sha)
        self.assertEqual(result.returncode, 1)
        self.assertIn("not event SHA", result.stderr)
        self.assertIn("refusing rerun", result.stderr)

    def test_wrong_tag_version_refuses_before_remote_lookup(self):
        env = os.environ.copy()
        env.update(RELEASE_TAG="v9.9.9", EVENT_SHA=self.first_sha, REPOSITORY_URL="/missing")
        result = subprocess.run(
            [sys.executable, str(SCRIPT)], cwd=ROOT, env=env, text=True, capture_output=True
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("does not match package version 'v0.1.1'", result.stderr)


if __name__ == "__main__":
    unittest.main()
