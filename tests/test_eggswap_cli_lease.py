"""The CLI must actually TAKE the hold it advertises.

The CLI must connect the fenced lease implementation to actual launches.
These tests assert observable refusal and release behavior so the type
cannot become an inert guarantee again.

The failure it prevents is measured, not hypothetical: two Claude CLIs sharing
one account's HOME concurrently rotate a single-use refresh token and destroy
it (``invalid_grant`` / "Not logged in").

Every test here would pass again if the wiring were removed, unless it asserts
on the OBSERVABLE consequence -- a refused second run, a profile missing from
schedulable -- so that is what each one asserts.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from eggswap import cli
from eggswap.core.store import LeaseStore
from eggswap.core.types import Available, Profile, Provider, QuotaWindow

NOW = 1_000_000.0


class _FakeAdapter:
    provider = Provider.CODEX

    def __init__(self, profiles, *, observed_at=NOW):
        self._profiles = list(profiles)
        self.observed_at = observed_at

    def profiles(self):
        return list(self._profiles)

    def availability(self, profile, *, max_age_seconds=300.0):
        return Available(
            windows=(
                QuotaWindow(
                    bucket="primary",
                    used_percent=10.0,
                    window_seconds=3600,
                    resets_at=self.observed_at + 3600,
                    observed_at=self.observed_at,
                ),
            ),
            observed_at=self.observed_at,
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


class CliTakesTheLeaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = LeaseStore(Path(self.tmp.name), clock=lambda: NOW)
        self.a = Profile(Provider.CODEX, "acct-a")
        self.b = Profile(Provider.CODEX, "acct-b")
        self.adapters = [_FakeAdapter([self.a, self.b])]
        self.calls = []

    def _runner(self, argv, env=None):
        self.calls.append(argv)

        class R:
            returncode = 0

        return R()

    def test_run_acquires_and_releases(self):
        out = _Out()
        rc = cli.main(["run", "codex:acct-a", "--", "true"], adapters=self.adapters,
                      out=out, now=NOW, runner=self._runner, store=self.store)
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.calls), 1)
        self.assertIsNone(self.store.holder_of(self.a),
                          "the hold must be released when the child exits")

    def test_second_run_on_a_held_profile_is_refused_and_does_not_launch(self):
        """The whole point: a busy account does not get a second process."""
        self.store.acquire(self.a, ttl_seconds=600, holder="someone-else")
        out = _Out()
        rc = cli.main(["run", "codex:acct-a", "--", "true"], adapters=self.adapters,
                      out=out, now=NOW, runner=self._runner, store=self.store)
        self.assertEqual(rc, 10)
        self.assertEqual(self.calls, [], "refused run must not spawn anything")
        self.assertIn("is held", out.text)
        held = self.store.holder_of(self.a)
        self.assertIsNotNone(held)
        self.assertEqual(held.holder, "someone-else",
                         "the refused caller must not have stolen the hold")

    def test_status_drops_a_held_profile_from_schedulable(self):
        self.store.acquire(self.a, ttl_seconds=600, holder="someone-else")
        out = _Out()
        cli.main(["status"], adapters=self.adapters, out=out, now=NOW,
                 runner=self._runner, store=self.store)
        self.assertIn("codex:acct-b", out.text)
        schedulable_line = out.text.splitlines()[0]
        self.assertNotIn("codex:acct-a", schedulable_line)
        self.assertIn("held: codex:acct-a", out.text)

    def test_select_hands_out_the_free_account_instead(self):
        """This IS the rotation claim: held accounts are not offered."""
        self.store.acquire(self.a, ttl_seconds=600, holder="someone-else")
        out = _Out()
        rc = cli.main(["select"], adapters=self.adapters, out=out, now=NOW,
                      runner=self._runner, store=self.store)
        self.assertEqual(rc, 0)
        self.assertIn("codex:acct-b", out.text)
        self.assertNotIn("codex:acct-a", out.text)

    def test_an_expired_hold_does_not_fence_an_account_forever(self):
        """A crashed run must not require deleting a lock file by hand."""
        self.store.acquire(self.a, ttl_seconds=1, holder="crashed")
        later = NOW + 10_000
        store = LeaseStore(Path(self.tmp.name), clock=lambda: later)
        self.adapters = [_FakeAdapter([self.a, self.b], observed_at=later)]
        out = _Out()
        rc = cli.main(["run", "codex:acct-a", "--", "true"], adapters=self.adapters,
                      out=out, now=later, runner=self._runner, store=store)
        self.assertEqual(rc, 0, out.text)
        self.assertEqual(len(self.calls), 1)

    def test_run_rechecks_capacity_under_the_lease_before_spawn(self):
        class ChangesAfterFirstRead(_FakeAdapter):
            def __init__(self, profiles):
                super().__init__(profiles)
                self.reads = 0

            def availability(self, profile, *, max_age_seconds=300.0):
                self.reads += 1
                if self.reads == 1:
                    return super().availability(profile, max_age_seconds=max_age_seconds)
                from eggswap.core.types import Exhausted

                return Exhausted(
                    reset_at=NOW + 3600,
                    bucket="primary",
                    observed_at=NOW,
                )

        adapter = ChangesAfterFirstRead([self.a, self.b])
        self.adapters = [adapter]
        out = _Out()
        rc = cli.main(["run", "codex:acct-a", "--", "true"], adapters=self.adapters,
                      out=out, now=NOW, runner=self._runner, store=self.store)
        self.assertEqual(rc, 3, out.text)
        self.assertEqual(self.calls, [], "newly exhausted account must not spawn")
        self.assertIsNone(self.store.holder_of(self.a), "refusal must release its lease")

    def test_store_false_disables_the_hold_entirely(self):
        """The escape hatch must really disable it, or it is not an escape."""
        self.store.acquire(self.a, ttl_seconds=600, holder="someone-else")
        out = _Out()
        rc = cli.main(["run", "codex:acct-a", "--", "true"], adapters=self.adapters,
                      out=out, now=NOW, runner=self._runner, store=False)
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.calls), 1)


if __name__ == "__main__":
    unittest.main()
