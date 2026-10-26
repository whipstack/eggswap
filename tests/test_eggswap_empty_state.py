"""Only an absent record may mean a new profile; corrupt state must fail closed."""
from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from eggswap import cli
from eggswap.core.store import LeaseStore
from eggswap.core.types import Profile, Provider


class StateReadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = LeaseStore(Path(self.temp.name), clock=lambda: 1000.0)
        self.path = Path(self.temp.name) / "synthetic.json"

    def test_only_a_missing_file_returns_an_absent_record(self):
        self.assertIsNone(self.store._read_raw(self.path))
        self.assertFalse(self.path.exists())

    def test_existing_zero_bytes_are_corruption_and_never_rewritten(self):
        self.path.write_bytes(b"")
        with self.assertRaisesRegex(ValueError, "empty lease state"):
            self.store._read_raw(self.path)
        self.assertEqual(self.path.read_bytes(), b"")

    def test_malformed_and_non_object_state_are_not_absence(self):
        for text in ("   ", "{", "null", "[]", "false", "0", '""'):
            with self.subTest(text=text):
                self.path.write_text(text, encoding="utf-8")
                with self.assertRaises(ValueError):
                    self.store._read_raw(self.path)
                self.assertEqual(self.path.read_text(encoding="utf-8"), text)

    def test_existing_object_stays_readable(self):
        record = {"profile": {"operator_enabled": False}, "fence": 7, "lease": None}
        self.path.write_text(json.dumps(record), encoding="utf-8")
        self.assertEqual(self.store._read_raw(self.path), record)


@unittest.skipUnless(os.name == "posix", "durable state operations require POSIX flock")
class EmptyDurableStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = LeaseStore(Path(self.temp.name), clock=lambda: 1000.0)
        self.profile = Profile(Provider.CODEX, "synthetic-account")
        self.path = self.store._path(self.profile)

    def test_empty_disabled_profile_cannot_be_enabled_or_acquired(self):
        self.store.set_enabled(self.profile, False)
        self.path.write_bytes(b"")
        for operation in (
            lambda: self.store.apply_profile_state(self.profile),
            lambda: self.store.acquire(self.profile, ttl_seconds=60, holder="synthetic"),
            lambda: self.store.set_enabled(self.profile, True),
        ):
            with self.subTest(operation=operation):
                with self.assertRaises(ValueError):
                    operation()
                self.assertEqual(self.path.read_bytes(), b"")

    def test_empty_held_record_cannot_reset_or_settle_the_fence(self):
        lease = self.store.acquire(self.profile, ttl_seconds=60, holder="synthetic")
        self.path.write_bytes(b"")
        for operation in (
            lambda: self.store.fence_of(self.profile),
            lambda: self.store.holder_of(self.profile),
            lambda: self.store.revalidate(lease),
            lambda: self.store.renew(lease, ttl_seconds=60),
            lambda: self.store.release(lease),
            self.store.reap_expired,
        ):
            with self.subTest(operation=operation):
                with self.assertRaises(ValueError):
                    operation()
                self.assertEqual(self.path.read_bytes(), b"")

    def test_new_missing_profile_still_initializes_and_preserves_fences(self):
        self.assertTrue(self.store.apply_profile_state(self.profile).enabled)
        first = self.store.acquire(self.profile, ttl_seconds=60, holder="first")
        self.store.release(first)
        second = self.store.acquire(self.profile, ttl_seconds=60, holder="second")
        self.assertGreater(second.fence, first.fence)

    def test_cli_refuses_empty_state_before_probing_or_launching(self):
        self.path.write_bytes(b"")
        profile = self.profile

        class Adapter:
            provider = Provider.CODEX

            def profiles(self):
                return [profile]

            def availability(self, *args, **kwargs):
                raise AssertionError("corrupt profile must be refused before probing")

        launch = Mock(side_effect=AssertionError("must not launch"))
        with self.assertRaises(ValueError):
            cli.main(
                ["run", profile.key, "--", "synthetic-child"],
                adapters=[Adapter()], out=io.StringIO(), now=1000.0,
                store=self.store, popen_factory=launch, quarantine=False,
            )
        launch.assert_not_called()
        self.assertEqual(self.path.read_bytes(), b"")


if __name__ == "__main__":
    unittest.main()
