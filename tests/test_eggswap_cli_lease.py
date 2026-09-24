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
import subprocess
import os
import sys
from pathlib import Path

from eggswap import cli
from eggswap.core.store import LeaseStore
from eggswap.core.types import Available, Exhausted, Profile, Provider, QuotaWindow, Unknown

NOW = 1_000_000.0


class _FakeAdapter:
    provider = Provider.CODEX

    def __init__(self, profiles):
        self._profiles = list(profiles)
        self.availability_value = None

    def profiles(self):
        return list(self._profiles)

    def availability(self, profile, *, max_age_seconds=300.0):
        if self.availability_value is not None:
            return self.availability_value
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

    def _popen(self, argv, **kwargs):
        self.calls.append(argv)

        class Process:
            returncode = None

            def wait(self, timeout=None):
                self.returncode = 0
                return 0

        return Process()

    def test_run_acquires_and_releases(self):
        out = _Out()
        rc = cli.main(["run", "codex:acct-a", "--", "true"], adapters=self.adapters,
                      out=out, now=NOW, runner=self._runner, popen_factory=self._popen, store=self.store,
                      quarantine=False)
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.calls), 1)
        self.assertIsNone(self.store.holder_of(self.a),
                          "the hold must be released when the child exits")

    def test_leased_run_renews_during_long_child(self):
        clock = [NOW]
        store = LeaseStore(Path(self.tmp.name) / "renew", clock=lambda: clock[0])

        class LongProcess:
            returncode = None

            def __init__(self):
                self.waits = 0

            def wait(self, timeout=None):
                self.waits += 1
                if self.waits == 1:
                    clock[0] += timeout + 1
                    raise subprocess.TimeoutExpired("long-child", timeout)
                self.returncode = 0
                return 0

        process = LongProcess()
        out = _Out()
        rc = cli.main(["run", "codex:acct-a", "--", "long-child"], adapters=self.adapters,
                      out=out, now=NOW, store=store, ttl_seconds=3,
                      popen_factory=lambda *args, **kwargs: process, quarantine=False)
        self.assertEqual(rc, 0, out.text)
        self.assertEqual(process.waits, 2)
        self.assertIsNone(store.holder_of(self.a))

    def test_renew_preserves_fence_and_rejects_expired_or_replaced_lease(self):
        clock = [NOW]
        store = LeaseStore(Path(self.tmp.name) / "store-renew", clock=lambda: clock[0])
        first = store.acquire(self.a, ttl_seconds=2, holder="first")
        clock[0] += 1
        renewed = store.renew(first, ttl_seconds=5)
        self.assertEqual(renewed.lease_id, first.lease_id)
        self.assertEqual(renewed.fence, first.fence)
        self.assertEqual(renewed.expires_at, clock[0] + 5)
        clock[0] += 6
        with self.assertRaises(cli.LeaseError):
            store.renew(renewed, ttl_seconds=5)
        replacement = store.acquire(self.a, ttl_seconds=5, holder="replacement")
        with self.assertRaises(cli.StaleFence):
            store.renew(renewed, ttl_seconds=5)
        self.assertEqual(store.holder_of(self.a).lease_id, replacement.lease_id)

    def test_child_is_stopped_when_lease_is_superseded_mid_run(self):
        clock = [NOW]
        store = LeaseStore(Path(self.tmp.name) / "lost", clock=lambda: clock[0])
        profile = self.a

        class LostProcess:
            returncode = None
            terminated = False

            def wait(self, timeout=None):
                if self.terminated:
                    self.returncode = -15
                    return self.returncode
                clock[0] += 4
                store.reap_expired()
                store.acquire(profile, ttl_seconds=60, holder="replacement")
                raise subprocess.TimeoutExpired("long-child", timeout)

            def terminate(self):
                self.terminated = True

        process = LostProcess()
        out = _Out()
        rc = cli.main(["run", "codex:acct-a", "--", "long-child"], adapters=self.adapters,
                      out=out, now=NOW, store=store, ttl_seconds=3,
                      popen_factory=lambda *args, **kwargs: process, quarantine=False)
        self.assertEqual(rc, 10, out.text)
        self.assertTrue(process.terminated)
        held = store.holder_of(self.a)
        self.assertEqual(held.holder, "replacement")
        self.assertIn("child stopped", out.text)

    @unittest.skipUnless(os.name == "posix", "lease process-group termination is POSIX")
    def test_real_child_process_stops_after_fence_loss(self):
        clock = [NOW]
        store = LeaseStore(Path(self.tmp.name) / "real-child", clock=lambda: clock[0])
        processes = []

        def spawn(args, **kwargs):
            process = subprocess.Popen(args, **kwargs)
            processes.append(process)
            clock[0] += 2
            store.reap_expired()
            store.acquire(self.a, ttl_seconds=60, holder="replacement")
            return process

        out = _Out()
        rc = cli.main(
            ["run", "codex:acct-a", "--", sys.executable, "-c", "import time; time.sleep(30)"],
            adapters=self.adapters, out=out, now=NOW, store=store,
            ttl_seconds=0.15, popen_factory=spawn, quarantine=False,
        )
        self.assertEqual(rc, 10, out.text)
        self.assertEqual(len(processes), 1)
        self.assertIsNotNone(processes[0].returncode)
        self.assertNotEqual(processes[0].returncode, 0)
        self.assertEqual(store.holder_of(self.a).holder, "replacement")

    def test_run_refuses_exhausted_profile_before_lease_or_launch(self):
        self.adapters[0].availability_value = Exhausted(
            bucket="primary", observed_at=NOW
        )
        out = _Out()
        rc = cli.main(["run", "codex:acct-a", "--", "true"], adapters=self.adapters,
                      out=out, now=NOW, runner=self._runner, popen_factory=self._popen, store=self.store,
                      quarantine=False)
        self.assertEqual(rc, 3)
        self.assertEqual(self.calls, [])
        self.assertIsNone(self.store.holder_of(self.a))
        self.assertIn("refusing codex:acct-a", out.text)

    def test_run_refuses_unknown_profile_before_launch_even_in_dry_run(self):
        self.adapters[0].availability_value = Unknown(
            stale_since=NOW, reason="quota unavailable"
        )
        out = _Out()
        rc = cli.main(["run", "codex:acct-a", "--dry-run", "--", "true"],
                      adapters=self.adapters, out=out, now=NOW,
                      runner=self._runner, popen_factory=self._popen, store=self.store, quarantine=False)
        self.assertEqual(rc, 3)
        self.assertEqual(self.calls, [])
        self.assertNotIn('"argv"', out.text)

    def test_run_refuses_quota_exhausted_during_reservation(self):
        class ChangingAdapter(_FakeAdapter):
            def __init__(self, profiles):
                super().__init__(profiles)
                self.probes = 0

            def availability(self, profile, *, max_age_seconds=300.0):
                self.probes += 1
                if self.probes == 1:
                    return Available(windows=(), observed_at=NOW)
                return Exhausted(bucket="primary", observed_at=NOW)

        adapter = ChangingAdapter([self.a])
        out = _Out()
        rc = cli.main(["run", "codex:acct-a", "--", "true"], adapters=[adapter],
                      out=out, now=NOW, runner=self._runner, popen_factory=self._popen, store=self.store,
                      quarantine=False)
        self.assertEqual(rc, 3)
        self.assertEqual(adapter.probes, 2)
        self.assertEqual(self.calls, [])
        self.assertIsNone(self.store.holder_of(self.a))
        self.assertIn("after reservation", out.text)

    def test_run_refuses_superseded_lease_before_launch(self):
        clock = [NOW]
        store = LeaseStore(Path(self.tmp.name), clock=lambda: clock[0])

        class RebindingAdapter(_FakeAdapter):
            def __init__(self, profiles):
                super().__init__(profiles)
                self.probes = 0

            def availability(self, profile, *, max_age_seconds=300.0):
                self.probes += 1
                if self.probes == 2:
                    clock[0] += 4000
                    store.reap_expired()
                    store.acquire(profile, ttl_seconds=600, holder="replacement")
                return Available(windows=(), observed_at=NOW)

        adapter = RebindingAdapter([self.a])
        out = _Out()
        rc = cli.main(["run", "codex:acct-a", "--", "true"], adapters=[adapter],
                      out=out, now=NOW, runner=self._runner, popen_factory=self._popen, store=store,
                      quarantine=False)
        self.assertEqual(rc, 10, out.text)
        self.assertEqual(self.calls, [])
        held = store.holder_of(self.a)
        self.assertIsNotNone(held)
        self.assertEqual(held.holder, "replacement")
        self.assertIn("lease lost before launch", out.text)

    def test_run_treats_probe_failure_as_unknown(self):
        class BrokenAdapter(_FakeAdapter):
            def availability(self, profile, *, max_age_seconds=300.0):
                raise OSError("probe failed")

        adapter = BrokenAdapter([self.a])
        out = _Out()
        rc = cli.main(["run", "codex:acct-a", "--", "true"], adapters=[adapter],
                      out=out, now=NOW, runner=self._runner, popen_factory=self._popen, store=self.store,
                      quarantine=False)
        self.assertEqual(rc, 3)
        self.assertEqual(self.calls, [])
        self.assertIn("Unknown", out.text)

    def test_second_run_on_a_held_profile_is_refused_and_does_not_launch(self):
        """The whole point: a busy account does not get a second process."""
        self.store.acquire(self.a, ttl_seconds=600, holder="someone-else")
        out = _Out()
        rc = cli.main(["run", "codex:acct-a", "--", "true"], adapters=self.adapters,
                      out=out, now=NOW, runner=self._runner, popen_factory=self._popen, store=self.store,
                      quarantine=False)
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
                 runner=self._runner, popen_factory=self._popen, store=self.store, quarantine=False)
        self.assertIn("codex:acct-b", out.text)
        schedulable_line = out.text.splitlines()[0]
        self.assertNotIn("codex:acct-a", schedulable_line)
        self.assertIn("held: codex:acct-a", out.text)

    def test_select_hands_out_the_free_account_instead(self):
        """This IS the rotation claim: held accounts are not offered."""
        self.store.acquire(self.a, ttl_seconds=600, holder="someone-else")
        out = _Out()
        rc = cli.main(["select"], adapters=self.adapters, out=out, now=NOW,
                      runner=self._runner, popen_factory=self._popen, store=self.store, quarantine=False)
        self.assertEqual(rc, 0)
        self.assertIn("codex:acct-b", out.text)
        self.assertNotIn("codex:acct-a", out.text)

    def test_an_expired_hold_does_not_fence_an_account_forever(self):
        """A crashed run must not require deleting a lock file by hand."""
        self.store.acquire(self.a, ttl_seconds=1, holder="crashed")
        later = NOW + 10_000
        self.adapters[0].availability = lambda profile, max_age_seconds=300.0: Available(
            windows=(), observed_at=later
        )
        store = LeaseStore(Path(self.tmp.name), clock=lambda: later)
        out = _Out()
        rc = cli.main(["run", "codex:acct-a", "--", "true"], adapters=self.adapters,
                      out=out, now=later, runner=self._runner, popen_factory=self._popen, store=store,
                      quarantine=False)
        self.assertEqual(rc, 0, out.text)
        self.assertEqual(len(self.calls), 1)

    def test_store_false_disables_the_hold_entirely(self):
        """The escape hatch must really disable it, or it is not an escape."""
        self.store.acquire(self.a, ttl_seconds=600, holder="someone-else")
        out = _Out()
        rc = cli.main(["run", "codex:acct-a", "--", "true"], adapters=self.adapters,
                      out=out, now=NOW, runner=self._runner, popen_factory=self._popen, store=False,
                      quarantine=False)
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.calls), 1)


if __name__ == "__main__":
    unittest.main()
