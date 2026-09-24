"""Tests for eggswap.core.quarantine -- route-failure memory (Issue #27).

TEST_SIZE = "small": no subprocess, no sockets, no threads, no sleeps. Time
is entirely driven by the injected FakeClock below.
"""
from __future__ import annotations

import unittest

from eggswap.core.quarantine import (
    AUTH_DEAD,
    EXHAUSTED,
    LAUNCH_FAILED,
    NO_RECEIPT,
    UNKNOWN,
    Failure,
    Quarantine,
)
from eggswap.core.types import Available, Candidate, Profile, Provider


class FakeClock:
    """Monotonic-ish clock a test can push forward without sleeping."""

    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_candidate(account_id: str) -> Candidate:
    profile = Profile(provider=Provider.CLAUDE, account_id=account_id)
    return Candidate(profile=profile, availability=Available())


class QuarantineBasicsTest(unittest.TestCase):
    def test_unrecorded_profile_is_not_quarantined(self) -> None:
        q = Quarantine(clock=FakeClock())
        self.assertFalse(q.is_quarantined("claude:a"))
        self.assertIsNone(q.until("claude:a"))
        self.assertIsNone(q.reason("claude:a"))

    def test_record_quarantines_immediately(self) -> None:
        clock = FakeClock()
        q = Quarantine(clock=clock, base_backoff=30.0)
        q.record("claude:a", LAUNCH_FAILED, "spawn returned no pid")
        self.assertTrue(q.is_quarantined("claude:a"))
        reason = q.reason("claude:a")
        self.assertIsInstance(reason, Failure)
        self.assertEqual(reason.kind, LAUNCH_FAILED)
        self.assertEqual(reason.detail, "spawn returned no pid")

    def test_quarantine_expires_after_backoff(self) -> None:
        clock = FakeClock()
        q = Quarantine(clock=clock, base_backoff=30.0)
        q.record("claude:a", NO_RECEIPT)
        self.assertTrue(q.is_quarantined("claude:a"))
        clock.advance(29.0)
        self.assertTrue(q.is_quarantined("claude:a"))
        clock.advance(2.0)
        self.assertFalse(q.is_quarantined("claude:a"))
        self.assertIsNone(q.until("claude:a"))
        self.assertIsNone(q.reason("claude:a"))


class ExponentialBackoffTest(unittest.TestCase):
    def test_backoff_doubles_per_kind_and_hits_ceiling(self) -> None:
        clock = FakeClock()
        q = Quarantine(clock=clock, base_backoff=10.0, max_backoff=45.0)
        q.record("claude:a", LAUNCH_FAILED)
        self.assertEqual(q.until("claude:a"), clock.now + 10.0)

        clock.advance(10.0)
        q.record("claude:a", LAUNCH_FAILED)
        self.assertEqual(q.until("claude:a"), clock.now + 20.0)

        clock.advance(20.0)
        q.record("claude:a", LAUNCH_FAILED)
        self.assertEqual(q.until("claude:a"), clock.now + 40.0)

        clock.advance(40.0)
        q.record("claude:a", LAUNCH_FAILED)
        # 4th failure would compute 80.0 uncapped; ceiling holds it at 45.0.
        self.assertEqual(q.until("claude:a"), clock.now + 45.0)

    def test_different_kinds_do_not_compound_on_one_profile(self) -> None:
        clock = FakeClock()
        q = Quarantine(clock=clock, base_backoff=10.0, max_backoff=1000.0)
        # Escalate LAUNCH_FAILED three times.
        q.record("claude:a", LAUNCH_FAILED)
        clock.advance(10.0)
        q.record("claude:a", LAUNCH_FAILED)
        clock.advance(20.0)
        q.record("claude:a", LAUNCH_FAILED)
        # A fresh kind on the SAME profile must start at base_backoff, not
        # inherit LAUNCH_FAILED's escalated count.
        q.record("claude:a", UNKNOWN)
        self.assertEqual(q.until("claude:a"), clock.now + 10.0)
        self.assertEqual(q.reason("claude:a").kind, UNKNOWN)


class AuthDeadTest(unittest.TestCase):
    def test_auth_dead_never_expires_on_a_timer(self) -> None:
        clock = FakeClock()
        q = Quarantine(clock=clock, base_backoff=1.0, max_backoff=2.0)
        q.record("claude:a", AUTH_DEAD, "invalid_grant")
        self.assertTrue(q.is_quarantined("claude:a"))
        # Advance far beyond any conceivable backoff ceiling.
        clock.advance(10_000_000.0)
        self.assertTrue(q.is_quarantined("claude:a"))
        self.assertEqual(q.reason("claude:a").kind, AUTH_DEAD)

    def test_auth_dead_lifts_only_on_explicit_clear(self) -> None:
        clock = FakeClock()
        q = Quarantine(clock=clock)
        q.record("claude:a", AUTH_DEAD, "invalid_grant")
        q.clear("claude:a")
        self.assertFalse(q.is_quarantined("claude:a"))


class ExhaustedResetTest(unittest.TestCase):
    def test_exhausted_uses_supplied_reset_time(self) -> None:
        clock = FakeClock()
        q = Quarantine(clock=clock, base_backoff=30.0)
        reset_at = clock.now + 5_000.0
        q.record("claude:a", EXHAUSTED, reset_at=reset_at)
        self.assertEqual(q.until("claude:a"), reset_at)

    def test_exhausted_never_outlasts_the_reset_even_after_escalation(self) -> None:
        clock = FakeClock()
        q = Quarantine(clock=clock, base_backoff=30.0, max_backoff=100_000.0)
        # Escalate the EXHAUSTED count first, without a reset time.
        for _ in range(5):
            q.record("claude:a", EXHAUSTED)
            clock.advance(1.0)
        reset_at = clock.now + 42.0
        q.record("claude:a", EXHAUSTED, reset_at=reset_at)
        self.assertEqual(q.until("claude:a"), reset_at)

    def test_exhausted_falls_back_to_backoff_without_reset_time(self) -> None:
        clock = FakeClock()
        q = Quarantine(clock=clock, base_backoff=30.0)
        q.record("claude:a", EXHAUSTED)
        self.assertEqual(q.until("claude:a"), clock.now + 30.0)


class ClearTest(unittest.TestCase):
    def test_clear_removes_the_record_entirely(self) -> None:
        clock = FakeClock()
        q = Quarantine(clock=clock)
        q.record("claude:a", LAUNCH_FAILED)
        q.clear("claude:a")
        self.assertFalse(q.is_quarantined("claude:a"))
        self.assertIsNone(q.reason("claude:a"))

    def test_clear_resets_escalation_not_just_the_current_quarantine(self) -> None:
        clock = FakeClock()
        q = Quarantine(clock=clock, base_backoff=10.0)
        q.record("claude:a", LAUNCH_FAILED)
        clock.advance(10.0)
        q.record("claude:a", LAUNCH_FAILED)  # escalated to 20.0
        q.clear("claude:a")
        # A recovered account carries no grudge: the NEXT failure after
        # clear() must start back at base_backoff, not the old escalated
        # value -- a sticky escalation is a frozen negative.
        q.record("claude:a", LAUNCH_FAILED)
        self.assertEqual(q.until("claude:a"), clock.now + 10.0)


class FilterTest(unittest.TestCase):
    def test_filter_drops_quarantined_and_preserves_order(self) -> None:
        clock = FakeClock()
        q = Quarantine(clock=clock)
        a = make_candidate("a")
        b = make_candidate("b")
        c = make_candidate("c")
        q.record(b.profile.key, LAUNCH_FAILED)

        result = q.filter([a, b, c])

        self.assertEqual(result, [a, c])

    def test_filter_does_not_mutate_input(self) -> None:
        clock = FakeClock()
        q = Quarantine(clock=clock)
        a = make_candidate("a")
        b = make_candidate("b")
        candidates = [a, b]
        q.record(b.profile.key, LAUNCH_FAILED)

        q.filter(candidates)

        self.assertEqual(candidates, [a, b])


if __name__ == "__main__":
    unittest.main()
