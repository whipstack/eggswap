"""Wire eggswap/core/quarantine.py into the CLI, or it is a second instance
of the standing failure mode this estate keeps finding: an actuator built and
never called (whipstack #544, #543; the lease primitive found unwired in this
very project). Before this module, nothing in eggswap/cli.py referenced
Quarantine at all.

Every test drives ``cli.main`` in-process with a fake adapter and a fake
runner -- no subprocess, no real account, no wall-clock dependence.
"""
from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path

from eggswap import cli
from eggswap.core.quarantine import AUTH_DEAD, Quarantine, UNKNOWN
from eggswap.core.types import Available, AuthDead, Profile, Provider, QuotaWindow

NOW = 1_000_000.0


class _FakeAdapter:
    provider = Provider.CODEX

    def __init__(self, profiles, availabilities=None):
        self._profiles = list(profiles)
        self._availabilities = availabilities or {}

    def profiles(self):
        return list(self._profiles)

    def availability(self, profile, *, max_age_seconds=300.0):
        if profile.key in self._availabilities:
            return self._availabilities[profile.key]
        return Available(
            windows=(
                QuotaWindow(
                    bucket="primary",
                    used_percent=10.0,
                    window_seconds=3600,
                    resets_at=NOW + 3600,
                    observed_at=NOW,
                ),
            ),
            observed_at=NOW,
        )

    def launch_env(self, profile, base_env=None):
        env = dict(base_env or {})
        env["CODEX_HOME"] = f"/fake/{profile.account_id}"
        return env


class _Out:
    def __init__(self):
        self.lines = []

    def write(self, s):
        self.lines.append(s)

    @property
    def text(self):
        return "".join(self.lines)


def _runner_returning(returncode):
    calls = []

    def runner(argv, env=None):
        calls.append(argv)

        class R:
            pass

        R.returncode = returncode
        return R()

    return runner, calls


class QuarantineWiringTests(unittest.TestCase):
    def setUp(self):
        self.a = Profile(Provider.CODEX, "acct-a")
        self.b = Profile(Provider.CODEX, "acct-b")
        self.adapters = [_FakeAdapter([self.a, self.b])]
        self.quarantine = Quarantine(clock=lambda: NOW)

    def test_a_failing_run_is_quarantined_and_select_skips_it(self):
        runner, calls = _runner_returning(1)
        out = _Out()
        rc = cli.main(
            ["run", "codex:acct-a", "--", "true"], adapters=self.adapters, out=out,
            now=NOW, runner=runner, store=False, quarantine=self.quarantine,
        )
        self.assertEqual(rc, 1)
        self.assertEqual(len(calls), 1)
        self.assertTrue(self.quarantine.is_quarantined(self.a.key))

        out2 = _Out()
        rc2 = cli.main(
            ["select"], adapters=self.adapters, out=out2, now=NOW,
            runner=runner, store=False, quarantine=self.quarantine,
        )
        self.assertEqual(rc2, 0)
        self.assertIn("codex:acct-b", out2.text)
        self.assertNotIn("codex:acct-a", out2.text)

    def test_a_succeeding_run_clears_an_existing_quarantine(self):
        self.quarantine.record(self.a.key, UNKNOWN, "exit 1")
        self.assertTrue(self.quarantine.is_quarantined(self.a.key))

        runner, calls = _runner_returning(0)
        out = _Out()
        rc = cli.main(
            ["run", "codex:acct-a", "--", "true"], adapters=self.adapters, out=out,
            now=NOW, runner=runner, store=False, quarantine=self.quarantine,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(len(calls), 1)
        self.assertFalse(self.quarantine.is_quarantined(self.a.key))

    def test_status_shows_quarantined_profile_on_its_own_line_and_excludes_it(self):
        self.quarantine.record(self.a.key, UNKNOWN, "exit 1")
        out = _Out()
        rc = cli.main(
            ["status"], adapters=self.adapters, out=out, now=NOW,
            runner=lambda *a, **k: None, store=False, quarantine=self.quarantine,
        )
        self.assertEqual(rc, 0)
        schedulable_line = out.text.splitlines()[0]
        self.assertIn("1 schedulable", schedulable_line)
        self.assertIn("codex:acct-b", schedulable_line)
        self.assertNotIn("codex:acct-a", schedulable_line)
        self.assertIn(f"quarantined: {self.a.key} until", out.text)
        self.assertIn("UNKNOWN", out.text)

    def test_clear_releases_a_quarantine_by_hand(self):
        self.quarantine.record(self.a.key, AUTH_DEAD, "needs human login")
        out = _Out()
        rc = cli.main(
            ["clear", self.a.key], adapters=self.adapters, out=out, now=NOW,
            runner=lambda *a, **k: None, store=False, quarantine=self.quarantine,
        )
        self.assertEqual(rc, 0)
        self.assertFalse(self.quarantine.is_quarantined(self.a.key))

    def test_clear_of_an_unknown_key_is_a_clean_no_op(self):
        out = _Out()
        rc = cli.main(
            ["clear", "codex:never-existed"], adapters=self.adapters, out=out, now=NOW,
            runner=lambda *a, **k: None, store=False, quarantine=self.quarantine,
        )
        self.assertEqual(rc, 0)

    def test_an_unclassifiable_failure_records_unknown_not_a_guess(self):
        runner, calls = _runner_returning(1)
        out = _Out()
        cli.main(
            ["run", "codex:acct-a", "--", "true"], adapters=self.adapters, out=out,
            now=NOW, runner=runner, store=False, quarantine=self.quarantine,
        )
        failure = self.quarantine.reason(self.a.key)
        self.assertEqual(failure.kind, UNKNOWN)

    def test_a_preexisting_auth_dead_report_is_recorded_as_auth_dead(self):
        adapters = [
            _FakeAdapter(
                [self.a, self.b],
                availabilities={self.a.key: AuthDead(reason="token revoked", observed_at=NOW)},
            )
        ]
        runner, calls = _runner_returning(1)
        out = _Out()
        rc = cli.main(
            ["run", "codex:acct-a", "--", "true"], adapters=adapters, out=out,
            now=NOW, runner=runner, store=False, quarantine=self.quarantine,
        )
        failure = self.quarantine.reason(self.a.key)
        self.assertEqual(rc, 3)
        self.assertEqual(calls, [], "AuthDead profiles must be refused before spawn")
        self.assertEqual(failure.kind, AUTH_DEAD)


class QuarantinePersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "quarantine.json"
        self.a = Profile(Provider.CODEX, "acct-a")
        self.b = Profile(Provider.CODEX, "acct-b")
        self.adapters = [_FakeAdapter([self.a, self.b])]

    def test_a_corrupt_quarantine_file_degrades_to_empty_and_launch_proceeds(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(b"\x00\x01 not json at all {{{")

        runner, calls = _runner_returning(0)
        out = _Out()
        rc = cli.main(
            ["run", "codex:acct-a", "--", "true"], adapters=self.adapters, out=out,
            now=NOW, runner=runner, store=False, quarantine=None,
            quarantine_path=self.path,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(len(calls), 1, "a corrupt quarantine file must never block a launch")

    def test_persisted_failure_survives_a_fresh_process_and_select_skips_it(self):
        runner, calls = _runner_returning(1)
        out = _Out()
        cli.main(
            ["run", "codex:acct-a", "--", "true"], adapters=self.adapters, out=out,
            now=NOW, runner=runner, store=False, quarantine=None,
            quarantine_path=self.path,
        )

        out2 = _Out()
        rc2 = cli.main(
            ["select"], adapters=self.adapters, out=out2, now=NOW,
            runner=runner, store=False, quarantine=None, quarantine_path=self.path,
        )
        self.assertEqual(rc2, 0)
        self.assertIn("codex:acct-b", out2.text)
        self.assertNotIn("codex:acct-a", out2.text)


if __name__ == "__main__":
    unittest.main()
