"""Tests for eggswap.core.policy -- cross-provider ranking strategies."""
from __future__ import annotations

import unittest

from eggswap.core.policy import (
    CrossProviderStrategy,
    IncomparableWindows,
    cross_provider_key,
    rank_cross_provider,
)
from eggswap.core.types import (
    Available,
    AuthDead,
    Candidate,
    Profile,
    Provider,
    QuotaWindow,
    Unknown,
)

NOW = 1_000_000.0
FIVE_HOURS = 5 * 60 * 60
SEVEN_DAYS = 7 * 24 * 60 * 60


def profile(provider, account_id):
    return Profile(provider=provider, account_id=account_id)


def window(bucket, used_percent, *, window_seconds=FIVE_HOURS, resets_at=None):
    return QuotaWindow(
        bucket=bucket,
        used_percent=used_percent,
        window_seconds=window_seconds,
        resets_at=resets_at,
        observed_at=NOW,
    )


def candidate(provider, account_id, *windows):
    return Candidate(
        profile=profile(provider, account_id),
        availability=Available(windows=windows, observed_at=NOW),
    )


class ResurrectionGuardTests(unittest.TestCase):
    def test_unknown_candidate_is_rejected_not_ranked(self):
        dead = Candidate(profile(Provider.CLAUDE, "a"), Unknown(stale_since=NOW - 10))
        with self.assertRaises(TypeError):
            cross_provider_key(dead, CrossProviderStrategy.PROVIDER_ORDER, now=NOW)

    def test_authdead_candidate_is_rejected_not_ranked(self):
        dead = Candidate(profile(Provider.CODEX, "b"), AuthDead(observed_at=NOW))
        with self.assertRaises(TypeError):
            rank_cross_provider([dead], CrossProviderStrategy.SPREAD, now=NOW)


class ProviderOrderStrategyTests(unittest.TestCase):
    def test_is_a_neutral_tie_deciding_nothing_itself(self):
        a = candidate(Provider.CLAUDE, "a", window("session", 10.0))
        b = candidate(Provider.CODEX, "b", window("weekly", 90.0, window_seconds=SEVEN_DAYS))
        # Headroom hugely favours "a", but PROVIDER_ORDER must not look at it.
        self.assertEqual(
            cross_provider_key(a, CrossProviderStrategy.PROVIDER_ORDER, now=NOW)[0],
            cross_provider_key(b, CrossProviderStrategy.PROVIDER_ORDER, now=NOW)[0],
        )

    def test_deterministic_tie_break_on_profile_key(self):
        a = candidate(Provider.CLAUDE, "a", window("session", 50.0))
        z = candidate(Provider.CLAUDE, "z", window("session", 50.0))
        ranked = rank_cross_provider([z, a], CrossProviderStrategy.PROVIDER_ORDER, now=NOW)
        self.assertEqual([c.profile.account_id for c in ranked], ["a", "z"])


class MostAbsoluteHeadroomTests(unittest.TestCase):
    def test_prefers_more_headroom_when_windows_are_comparable(self):
        roomy = candidate(Provider.CLAUDE, "roomy", window("session", 10.0))
        tight = candidate(Provider.CODEX, "tight", window("session", 90.0))
        ranked = rank_cross_provider(
            [tight, roomy], CrossProviderStrategy.MOST_ABSOLUTE_HEADROOM, now=NOW
        )
        self.assertEqual([c.profile.account_id for c in ranked], ["roomy", "tight"])

    def test_refuses_to_compare_a_5h_window_against_a_7d_window(self):
        claude_5h = candidate(Provider.CLAUDE, "a", window("session", 10.0, window_seconds=FIVE_HOURS))
        codex_7d = candidate(Provider.CODEX, "b", window("weekly", 10.0, window_seconds=SEVEN_DAYS))
        with self.assertRaises(IncomparableWindows):
            rank_cross_provider(
                [claude_5h, codex_7d], CrossProviderStrategy.MOST_ABSOLUTE_HEADROOM, now=NOW
            )

    def test_refuses_a_candidate_with_no_window_seconds_at_all(self):
        unmeasured = candidate(
            Provider.CLAUDE, "a", window("session", 10.0, window_seconds=None)
        )
        with self.assertRaises(IncomparableWindows):
            cross_provider_key(unmeasured, CrossProviderStrategy.MOST_ABSOLUTE_HEADROOM, now=NOW)

    def test_model_scoped_diagnostics_do_not_bind_an_unspecified_query(self):
        shared = window("codex:primary", 25.0, window_seconds=SEVEN_DAYS)
        exhausted_scoped = window("fable:model:fable:primary", 100.0)
        a = Candidate(
            profile=profile(Provider.CODEX, "a"),
            availability=Available(
                windows=(shared, exhausted_scoped), observed_at=NOW,
                binding_windows=(shared,),
            ),
        )
        b = candidate(
            Provider.CLAUDE, "b", window("weekly", 40.0, window_seconds=SEVEN_DAYS)
        )

        ranked = rank_cross_provider(
            [b, a], CrossProviderStrategy.MOST_ABSOLUTE_HEADROOM, now=NOW
        )

        self.assertEqual([item.profile.account_id for item in ranked], ["a", "b"])

    def test_binding_window_is_the_tightest_CAPACITY_not_the_shortest(self):
        """A code review's counterexample, pinned so it cannot come back.

        The first implementation defined "binding" as the shortest
        window_seconds. An account at 5h=0% used, 7d=99% used is one point
        from its weekly wall, yet that rule reported "5h, 100% headroom" and
        ranked it ABOVE an account with room everywhere -- the scheduler would
        have picked the one about to hit a wall. Shortest duration is not
        binding capacity.
        """
        about_to_hit_the_weekly_wall = candidate(
            Provider.CLAUDE, "near-wall",
            window("session", 0.0, window_seconds=FIVE_HOURS),
            window("weekly", 99.0, window_seconds=SEVEN_DAYS),
        )
        key = cross_provider_key(
            about_to_hit_the_weekly_wall,
            CrossProviderStrategy.MOST_ABSOLUTE_HEADROOM, now=NOW,
        )
        self.assertEqual(key[0], SEVEN_DAYS, "the weekly window is what binds")
        self.assertAlmostEqual(key[1], -1.0, msg="1% headroom, not 100%")

    def test_a_weekly_bound_and_a_session_bound_account_are_NOT_comparable(self):
        """And the refusal is what protects the caller from the old bug.

        "1% of a weekly window left" against "50% of a five-hour window left"
        is still unlike units. Having fixed which window binds, the honest
        move is to refuse the comparison rather than rank across it.
        """
        near_wall = candidate(
            Provider.CLAUDE, "near-wall",
            window("session", 0.0, window_seconds=FIVE_HOURS),
            window("weekly", 99.0, window_seconds=SEVEN_DAYS),
        )
        roomy = candidate(
            Provider.CLAUDE, "roomy",
            window("session", 50.0, window_seconds=FIVE_HOURS),
            window("weekly", 0.0, window_seconds=SEVEN_DAYS),
        )
        with self.assertRaises(IncomparableWindows):
            rank_cross_provider(
                [near_wall, roomy],
                CrossProviderStrategy.MOST_ABSOLUTE_HEADROOM, now=NOW,
            )

    def test_shortest_window_still_wins_when_it_IS_the_binding_one(self):
        # Negative control for the two tests above: the fix must not invert
        # the ordinary case, where the short bucket really is the tight one.
        mixed = candidate(
            Provider.CLAUDE,
            "a",
            window("session", 80.0, window_seconds=FIVE_HOURS),
            window("weekly", 5.0, window_seconds=SEVEN_DAYS),
        )
        key = cross_provider_key(mixed, CrossProviderStrategy.MOST_ABSOLUTE_HEADROOM, now=NOW)
        self.assertEqual(key[0], FIVE_HOURS)
        self.assertAlmostEqual(key[1], -20.0)


class LongestUntilResetTests(unittest.TestCase):
    def test_prefers_the_furthest_reset(self):
        soon = candidate(Provider.CLAUDE, "soon", window("session", 50.0, resets_at=NOW + 100))
        far = candidate(Provider.CODEX, "far", window("session", 50.0, resets_at=NOW + 99_999))
        ranked = rank_cross_provider(
            [soon, far], CrossProviderStrategy.LONGEST_UNTIL_RESET, now=NOW
        )
        self.assertEqual([c.profile.account_id for c in ranked], ["far", "soon"])

    def test_candidate_missing_resets_at_is_ranked_last_not_soonest_or_never(self):
        known = candidate(Provider.CLAUDE, "known", window("session", 50.0, resets_at=NOW + 1))
        unknown = candidate(Provider.CODEX, "unknown", window("session", 50.0, resets_at=None))
        ranked = rank_cross_provider(
            [unknown, known], CrossProviderStrategy.LONGEST_UNTIL_RESET, now=NOW
        )
        self.assertEqual([c.profile.account_id for c in ranked], ["known", "unknown"])


class SpreadStrategyTests(unittest.TestCase):
    def test_prefers_the_provider_with_fewer_held_leases(self):
        claude = candidate(Provider.CLAUDE, "a", window("session", 50.0))
        codex = candidate(Provider.CODEX, "b", window("session", 50.0))
        held = {Provider.CLAUDE: 3, Provider.CODEX: 0}
        ranked = rank_cross_provider(
            [claude, codex], CrossProviderStrategy.SPREAD, now=NOW, held_counts=held
        )
        self.assertEqual([c.profile.account_id for c in ranked], ["b", "a"])

    def test_degrades_to_provider_order_when_no_counts_supplied(self):
        claude = candidate(Provider.CLAUDE, "a", window("session", 50.0))
        codex = candidate(Provider.CODEX, "b", window("session", 50.0))
        without_counts = cross_provider_key(claude, CrossProviderStrategy.SPREAD, now=NOW)
        neutral_tie = cross_provider_key(claude, CrossProviderStrategy.PROVIDER_ORDER, now=NOW)
        self.assertEqual(without_counts[0], neutral_tie[0])
        other = cross_provider_key(codex, CrossProviderStrategy.SPREAD, now=NOW)
        self.assertEqual(without_counts[0], other[0])


class UnknownStrategyTests(unittest.TestCase):
    def test_unrecognised_strategy_value_is_rejected(self):
        a = candidate(Provider.CLAUDE, "a", window("session", 50.0))
        with self.assertRaises(ValueError):
            cross_provider_key(a, "not-a-real-strategy", now=NOW)


if __name__ == "__main__":
    unittest.main()
