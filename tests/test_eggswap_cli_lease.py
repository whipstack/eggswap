"""The CLI must actually TAKE the hold it advertises.

A README honesty audit (docs/research/eggswap/readme-honesty-audit.md, row 14)
found the most consequential gap in this project: ``eggswap/core/store.py``
implemented a fenced exclusive lease, ``tests/test_eggswap_store.py`` proved
it, the README described the guarantee -- and ``_cmd_run`` never called
``acquire``. The type was doing nothing at the layer where the failure
actually happens.

The failure it prevents is measured, not hypothetical: two Claude CLIs sharing
one account's HOME concurrently rotate a single-use refresh token and destroy
it (whipstack #581, ``invalid_grant`` / "Not logged in").

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

    def __init__(self, profiles):
        self._profiles = list(profiles)

    def profiles(self):
        return list(self._profiles)

    def availability(self, profile, *, max_age_seconds=300.0):
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
        out = _Out()
        rc = cli.main(["run", "codex:acct-a", "--", "true"], adapters=self.adapters,
                      out=out, now=later, runner=self._runner, store=store)
        self.assertEqual(rc, 0, out.text)
        self.assertEqual(len(self.calls), 1)

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
