"""Operator profile enable/disable survives adapter rediscovery."""
from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from eggswap.cli import main
from eggswap.core.store import LeaseStore
from eggswap.core.types import Available, Profile, ProfileDisabled, Provider


class FixtureAdapter:
    provider = Provider.CLAUDE

    def __init__(self):
        self.profile = Profile(Provider.CLAUDE, "1", label="fixture")
        self.availability_calls = 0

    def profiles(self):
        # Simulate ordinary adapter rediscovery: it has no stored operator
        # preference and therefore emits the default enabled profile.
        return [self.profile]

    def availability(self, _profile, **_kwargs):
        self.availability_calls += 1
        return Available(windows=(), observed_at=1_800_000_000.0)

    def launch_argv(self, _profile, args):
        return ["fixture-command", *args]


class ProfileControlCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = LeaseStore(Path(self.temp.name) / "leases")
        self.adapter = FixtureAdapter()
        self.out = io.StringIO()

    def _main(self, argv, *, runner=None):
        run_child = runner or (lambda *_a, **_kw: SimpleNamespace(returncode=0))

        def popen_factory(args, **kwargs):
            result = run_child(args, **kwargs)

            class Process:
                returncode = result.returncode

                def wait(self, timeout=None):
                    return self.returncode

            return Process()

        return main(
            argv,
            adapters=[self.adapter],
            out=self.out,
            store=self.store,
            quarantine=False,
            runner=runner or (lambda *_a, **_kw: SimpleNamespace(returncode=0)),
            popen_factory=popen_factory,
        )

    def test_disable_is_durable_across_discovery_and_blocks_select_and_run(self):
        key = self.adapter.profile.key
        self.assertEqual(self._main(["disable", key]), 0)
        self.assertIn("disabled", self.out.getvalue())

        self.adapter = FixtureAdapter()  # fresh adapter inventory, default enabled

        self.out.seek(0)
        self.out.truncate(0)
        self.assertEqual(self._main(["list"]), 0)
        self.assertIn("\tdisabled", self.out.getvalue())
        self.assertEqual(self.adapter.availability_calls, 0)

        self.out.seek(0)
        self.out.truncate(0)
        self.assertEqual(self._main(["status"]), 3)
        self.assertIn("disabled: claude:1", self.out.getvalue())

        self.out.seek(0)
        self.out.truncate(0)
        self.assertEqual(self._main(["select"]), 3)
        self.assertIn("disabled", self.out.getvalue())

        launches = []

        def runner(argv, **kwargs):
            launches.append((argv, kwargs))
            return SimpleNamespace(returncode=0)

        self.assertEqual(self._main(["run", key, "--", "work"], runner=runner), 3)
        self.assertEqual(launches, [])

    def test_explicit_enable_restores_selection_and_launch(self):
        key = self.adapter.profile.key
        self.assertEqual(self._main(["disable", key]), 0)
        self.adapter = FixtureAdapter()
        self.out.seek(0)
        self.out.truncate(0)
        self.assertEqual(self._main(["enable", key]), 0)
        self.out.seek(0)
        self.out.truncate(0)

        launches = []

        def runner(argv, **kwargs):
            launches.append((argv, kwargs))
            return SimpleNamespace(returncode=0)

        self.assertEqual(self._main(["run", key, "--", "work"], runner=runner), 0)
        self.assertEqual(len(launches), 1)

    def test_disable_during_live_lease_preserves_holder_but_blocks_new_lease(self):
        profile = self.adapter.profile
        lease = self.store.acquire(profile, ttl_seconds=60, holder="fixture")
        self.store.set_enabled(profile, False)

        self.assertEqual(self.store.revalidate(lease).lease_id, lease.lease_id)
        with self.assertRaises(ProfileDisabled):
            self.store.acquire(profile, ttl_seconds=60, holder="second")
        self.store.release(lease)

    def test_non_boolean_profile_state_is_rejected(self):
        profile = self.adapter.profile
        with self.assertRaises(TypeError):
            self.store.set_enabled(profile, "false")
        self.assertTrue(self.store.apply_profile_state(profile).enabled)


if __name__ == "__main__":
    unittest.main()
