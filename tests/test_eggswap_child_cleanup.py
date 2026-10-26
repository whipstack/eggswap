"""A catchable parent failure must not free a still-running child's lease."""
from __future__ import annotations

import io
import os
import subprocess
import sys
import unittest
from unittest.mock import patch

from eggswap import cli
from eggswap.core.types import Available, Profile, Provider, QuotaWindow

NOW = 1_000_000.0


class _Adapter:
    provider = Provider.CODEX

    def __init__(self, profile: Profile):
        self.profile = profile

    def profiles(self):
        return [self.profile]

    def availability(self, profile, *, max_age_seconds=300.0):
        return Available(
            windows=(QuotaWindow(bucket="primary", used_percent=10.0,
                                 window_seconds=3600, resets_at=NOW + 3600,
                                 observed_at=NOW),),
            observed_at=NOW,
        )

    def launch_env(self, profile, base_env=None):
        return dict(base_env or {})


class _Store:
    def __init__(self, events):
        self.events = events
        self.lease = object()
        self.held = False

    def acquire(self, *args, **kwargs):
        self.events.append("acquire")
        self.held = True
        return self.lease

    def revalidate(self, lease):
        return lease

    def renew(self, lease, **kwargs):
        return lease

    def release(self, lease):
        self.events.append("release")
        self.held = False


class _Process:
    def __init__(self, events, failure):
        self.events = events
        self.failure = failure
        self.stopped = False
        self.returncode = None

    def wait(self, timeout=None):
        if not self.stopped:
            raise self.failure
        self.events.append("reap")
        self.returncode = -15
        return self.returncode

    def terminate(self):
        self.events.append("stop")
        self.stopped = True


class LeasedChildCleanupTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.store = _Store(self.events)
        self.profile = Profile(Provider.CODEX, "synthetic-account")

    def run_child(self, process, *, popen_factory=None):
        return cli.main(
            ["run", self.profile.key, "--", "synthetic-child"],
            adapters=[_Adapter(self.profile)], out=io.StringIO(), now=NOW,
            store=self.store, popen_factory=popen_factory or (lambda *a, **kw: process),
            quarantine=False,
        )

    def test_interrupt_stops_and_reaps_before_lease_release(self):
        failure = KeyboardInterrupt()
        process = _Process(self.events, failure)
        with self.assertRaises(KeyboardInterrupt) as caught:
            self.run_child(process)
        self.assertIs(caught.exception, failure)
        self.assertEqual(self.events, ["acquire", "stop", "reap", "release"])
        self.assertFalse(self.store.held)

    def test_unexpected_wait_failure_also_cleans_up(self):
        failure = OSError("synthetic wait failure")
        process = _Process(self.events, failure)
        with self.assertRaises(OSError) as caught:
            self.run_child(process)
        self.assertIs(caught.exception, failure)
        self.assertEqual(self.events, ["acquire", "stop", "reap", "release"])

    def test_renew_failure_cannot_leave_work_running(self):
        failure = OSError("synthetic renewal failure")
        process = _Process(self.events, subprocess.TimeoutExpired("synthetic-child", 1))
        self.store.renew = lambda *a, **kw: (_ for _ in ()).throw(failure)
        with self.assertRaises(OSError) as caught:
            self.run_child(process)
        self.assertIs(caught.exception, failure)
        self.assertEqual(self.events, ["acquire", "stop", "reap", "release"])

    def test_failed_cleanup_keeps_the_lease_until_its_normal_expiry(self):
        process = _Process(self.events, OSError("synthetic wait failure"))
        with patch.object(process, "terminate", side_effect=PermissionError("cannot stop")):
            with self.assertRaisesRegex(RuntimeError, "lease retained"):
                self.run_child(process)
        self.assertTrue(self.store.held)
        self.assertNotIn("release", self.events)

    def test_normal_exit_releases_without_stopping(self):
        process = _Process(self.events, None)
        process.wait = lambda timeout=None: 7
        self.assertEqual(self.run_child(process), 7)
        self.assertEqual(self.events, ["acquire", "release"])

    @unittest.skipUnless(os.name == "posix", "process groups are POSIX")
    def test_missing_process_group_is_still_reaped(self):
        process = _Process(self.events, None)
        process.stopped = True
        process.pid = 123456789
        with patch.object(os, "killpg", side_effect=ProcessLookupError):
            cli._stop_child(process)
        self.assertEqual(self.events, ["reap"])

    @unittest.skipUnless(os.name == "posix", "process groups are POSIX")
    def test_unresponsive_group_is_killed_then_reaped(self):
        process = _Process(self.events, None)
        process.pid = 123456789
        waits = [subprocess.TimeoutExpired("synthetic-child", 5), -9]

        def wait(timeout=None):
            result = waits.pop(0)
            if isinstance(result, BaseException):
                raise result
            self.events.append("reap")
            return result

        process.wait = wait
        with patch.object(os, "killpg") as signal_group:
            cli._stop_child(process)
        self.assertEqual([c.args[1] for c in signal_group.call_args_list],
                         [cli.signal.SIGTERM, cli.signal.SIGKILL])
        self.assertEqual(self.events, ["reap"])

    @unittest.skipUnless(os.name == "posix", "new-session children are POSIX")
    def test_real_new_session_child_is_reaped_before_release_on_interrupt(self):
        processes = []

        def spawn(argv, **kwargs):
            self.assertTrue(kwargs.get("start_new_session"))
            process = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(60)"], **kwargs
            )
            processes.append(process)
            original_wait = process.wait
            interrupted = False

            def wait(timeout=None):
                nonlocal interrupted
                if not interrupted:
                    interrupted = True
                    raise KeyboardInterrupt
                result = original_wait(timeout=timeout)
                self.events.append("reap")
                return result

            def cleanup():
                if process.poll() is None:
                    process.kill()
                original_wait(timeout=5)

            self.addCleanup(cleanup)
            process.wait = wait
            return process

        with self.assertRaises(KeyboardInterrupt):
            self.run_child(None, popen_factory=spawn)
        self.assertEqual(len(processes), 1)
        self.assertIsNotNone(processes[0].returncode)
        self.assertEqual(self.events, ["acquire", "reap", "release"])


if __name__ == "__main__":
    unittest.main()
