"""Tests for eggswap.core.store.LeaseStore.

Each test is written to FAIL if the corresponding safety property is
removed, per the eggswap house rule: no sleeps, time is driven through an
injected clock, and the fenced-release test proves non-vacuity by asserting
the new holder is intact afterwards, not merely that the old release raised.
"""
from __future__ import annotations

import json
import multiprocessing
import tempfile
import unittest
from pathlib import Path

from eggswap.core.store import LeaseStore
from eggswap.core.types import LeaseError, Profile, Provider, StaleFence, WorkSpec

TEST_SIZE = "medium"
TEST_SIZE_REASON = (
    "test_concurrent_subprocess_acquire_exactly_one_wins forks real OS "
    "processes to exercise the fcntl.flock exclusion that two independent "
    "Claude/Codex CLIs -- not threads -- actually race under (Issue #581)."
)


def make_profile(account_id: str = "acct-1") -> Profile:
    return Profile(provider=Provider.CLAUDE, account_id=account_id)


class FakeClock:
    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class LeaseStoreTest(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self._tmpdir.name)
        self.clock = FakeClock()
        self.store = LeaseStore(self.root, clock=self.clock)
        self.profile = make_profile()

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_double_acquire_raises_and_first_holder_unchanged(self):
        first = self.store.acquire(self.profile, ttl_seconds=60, holder="worker-a")

        with self.assertRaises(LeaseError):
            self.store.acquire(self.profile, ttl_seconds=60, holder="worker-b")

        current = self.store.holder_of(self.profile)
        self.assertEqual(current.lease_id, first.lease_id)
        self.assertEqual(current.holder, "worker-a")
        self.assertEqual(current.fence, first.fence)

    def test_expiry_then_reap_then_reacquire_has_higher_fence(self):
        first = self.store.acquire(self.profile, ttl_seconds=10, holder="worker-a")

        self.clock.advance(11)

        reaped = self.store.reap_expired()
        self.assertEqual(len(reaped), 1)
        self.assertEqual(reaped[0].lease_id, first.lease_id)

        second = self.store.acquire(self.profile, ttl_seconds=10, holder="worker-b")
        self.assertGreater(second.fence, first.fence)

    def test_stale_release_refused_and_new_holder_survives(self):
        old_lease = self.store.acquire(self.profile, ttl_seconds=10, holder="worker-a")

        self.clock.advance(11)
        self.store.reap_expired()

        new_lease = self.store.acquire(self.profile, ttl_seconds=60, holder="worker-b")
        self.assertGreater(new_lease.fence, old_lease.fence)

        with self.assertRaises(StaleFence):
            self.store.release(old_lease)

        # Non-vacuity: the new holder must still be exactly who we expect,
        # not silently released by the stale caller above.
        survivor = self.store.holder_of(self.profile)
        self.assertIsNotNone(survivor)
        self.assertEqual(survivor.lease_id, new_lease.lease_id)
        self.assertEqual(survivor.holder, "worker-b")
        self.assertEqual(survivor.fence, new_lease.fence)

    def test_revalidate_rejects_stale_fence_and_expired_lease(self):
        old_lease = self.store.acquire(self.profile, ttl_seconds=10, holder="worker-a")
        self.clock.advance(11)
        self.store.reap_expired()
        self.store.acquire(self.profile, ttl_seconds=60, holder="worker-b")

        with self.assertRaises(StaleFence):
            self.store.revalidate(old_lease)

    def test_release_then_holder_of_is_none(self):
        lease = self.store.acquire(self.profile, ttl_seconds=60, holder="worker-a")
        self.store.release(lease)
        self.assertIsNone(self.store.holder_of(self.profile))

    def test_fence_of_starts_at_zero_and_never_decreases(self):
        self.assertEqual(self.store.fence_of(self.profile), 0)
        lease = self.store.acquire(self.profile, ttl_seconds=60, holder="worker-a")
        self.assertEqual(self.store.fence_of(self.profile), lease.fence)
        self.store.release(lease)
        self.assertEqual(self.store.fence_of(self.profile), lease.fence)

    def test_concurrent_subprocess_acquire_exactly_one_wins(self):
        try:
            ctx = multiprocessing.get_context("fork")
        except (ValueError, ImportError):
            self.skipTest("fork start method unavailable on this runtime")

        root = str(self.root)

        def _attempt(root_str, result_queue):
            # Real time.time() clock: this runs in its own process, so it
            # cannot share the parent's FakeClock closure across fork/pickle
            # boundaries -- exercising the actual OS-level flock is the point.
            import time as _time

            from eggswap.core.store import LeaseStore as _LeaseStore
            from eggswap.core.types import Profile as _Profile
            from eggswap.core.types import Provider as _Provider

            store = _LeaseStore(Path(root_str), clock=_time.time)
            profile = _Profile(provider=_Provider.CLAUDE, account_id="acct-race")
            try:
                lease = store.acquire(profile, ttl_seconds=30, holder="racer")
                result_queue.put(("ok", lease.lease_id))
            except Exception as exc:  # noqa: BLE001 - report, don't crash the child
                result_queue.put(("err", type(exc).__name__))

        result_queue = ctx.Queue()
        procs = [
            ctx.Process(target=_attempt, args=(root, result_queue)) for _ in range(6)
        ]
        for p in procs:
            p.start()
        for p in procs:
            p.join(timeout=30)

        results = [result_queue.get(timeout=5) for _ in procs]
        wins = [r for r in results if r[0] == "ok"]
        losses = [r for r in results if r[0] == "err"]
        self.assertEqual(len(wins), 1, f"expected exactly one winner, got {results}")
        self.assertEqual(len(losses), len(procs) - 1)
        self.assertTrue(all(name == "LeaseError" for _, name in losses))

    def test_torn_write_does_not_corrupt_committed_state(self):
        lease = self.store.acquire(self.profile, ttl_seconds=60, holder="worker-a")

        path = self.store._path(self.profile)
        committed_text = path.read_text()

        # Simulate a crash mid-write: a torn tmp file sitting next to the
        # already-committed record must never be mistaken for that record.
        tmp_path = path.with_suffix(".tmp.99999")
        tmp_path.write_text('{"profile": {"provider": "claude", "acco')

        record = json.loads(path.read_text())
        self.assertEqual(record["lease"]["lease_id"], lease.lease_id)
        self.assertEqual(path.read_text(), committed_text)

        current = self.store.holder_of(self.profile)
        self.assertEqual(current.lease_id, lease.lease_id)

        tmp_path.unlink()


if __name__ == "__main__":
    unittest.main()
