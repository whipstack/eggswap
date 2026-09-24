"""Tests for eggswap.core.select -- provider-neutral ranking/selection."""
from __future__ import annotations

import unittest

from eggswap.core.select import Policy, rank, select
from eggswap.core.types import (
    Available,
    AuthDead,
    Candidate,
    Exhausted,
    NoCapacity,
    Profile,
    Provider,
    Unknown,
)

NOW = 1_000_000.0


def profile(provider, account_id, *, is_api_key=False, enabled=True):
    return Profile(provider=provider, account_id=account_id, is_api_key=is_api_key, enabled=enabled)


def available(observed_at=NOW):
    return Available(windows=(), observed_at=observed_at)


class RankSelectTests(unittest.TestCase):
    def test_only_available_is_selectable(self):
        claude = Candidate(profile(Provider.CLAUDE, "a"), Unknown(stale_since=NOW - 10), score=99.0)
        codex = Candidate(profile(Provider.CODEX, "b"), Exhausted(observed_at=NOW), score=50.0)
        dead = Candidate(profile(Provider.CLAUDE, "c"), AuthDead(observed_at=NOW), score=10.0)
        with self.assertRaises(NoCapacity):
            select([claude, codex, dead], Policy())

    def test_unknown_never_returned_even_as_sole_candidate(self):
        lone = Candidate(profile(Provider.CLAUDE, "solo"), Unknown(stale_since=NOW - 5), score=100.0)
        with self.assertRaises(NoCapacity) as ctx:
            select([lone], Policy(), now=NOW)
        self.assertIn("claude:solo", ctx.exception.reasons)
        self.assertIsInstance(ctx.exception.reasons["claude:solo"], Unknown)

    def test_no_capacity_reasons_distinguish_exhausted_from_unknown(self):
        exhausted = Candidate(profile(Provider.CLAUDE, "x"), Exhausted(reset_at=NOW + 3600, observed_at=NOW), score=0.0)
        unknown = Candidate(profile(Provider.CODEX, "y"), Unknown(stale_since=NOW - 5), score=0.0)
        with self.assertRaises(NoCapacity) as ctx:
            select([exhausted, unknown], Policy(), now=NOW)
        reasons = ctx.exception.reasons
        self.assertIsInstance(reasons["claude:x"], Exhausted)
        self.assertIsInstance(reasons["codex:y"], Unknown)

    def test_most_headroom_within_provider(self):
        low = Candidate(profile(Provider.CLAUDE, "low"), available(), score=10.0)
        high = Candidate(profile(Provider.CLAUDE, "high"), available(), score=90.0)
        winner = select([low, high], Policy(), now=NOW)
        self.assertEqual(winner.profile.account_id, "high")

    def test_least_headroom_preference(self):
        low = Candidate(profile(Provider.CLAUDE, "low"), available(), score=10.0)
        high = Candidate(profile(Provider.CLAUDE, "high"), available(), score=90.0)
        winner = select([low, high], Policy(prefer="least_headroom"), now=NOW)
        self.assertEqual(winner.profile.account_id, "low")

    def test_cross_provider_tiebreak_uses_allow_providers_order(self):
        claude = Candidate(profile(Provider.CLAUDE, "a"), available(), score=10.0)
        codex = Candidate(profile(Provider.CODEX, "a"), available(), score=99.0)
        winner = select(
            [claude, codex],
            Policy(allow_providers=(Provider.CLAUDE, Provider.CODEX)),
            now=NOW,
        )
        # CLAUDE ranks first by policy order even though codex has more
        # headroom -- cross-provider order is a stated policy choice, not a
        # comparison of the (incomparable) score units.
        self.assertEqual(winner.profile.provider, Provider.CLAUDE)

    def test_provider_not_in_allow_list_is_excluded(self):
        codex = Candidate(profile(Provider.CODEX, "only"), available(), score=100.0)
        with self.assertRaises(NoCapacity):
            select([codex], Policy(allow_providers=(Provider.CLAUDE,)), now=NOW)

    def test_ties_broken_by_profile_key(self):
        b = Candidate(profile(Provider.CLAUDE, "b"), available(), score=50.0)
        a = Candidate(profile(Provider.CLAUDE, "a"), available(), score=50.0)
        ranked = rank([b, a], Policy(), now=NOW)
        self.assertEqual([c.profile.account_id for c in ranked], ["a", "b"])

    def test_api_key_profile_rejected_when_gate_fully_closed(self):
        key_profile = Candidate(profile(Provider.CLAUDE, "k", is_api_key=True), available(), score=100.0)
        with self.assertRaises(NoCapacity):
            select([key_profile], Policy(), now=NOW)

    def test_api_key_gate_requires_allow_flag_not_just_budget(self):
        # Negative control: budget alone, without allow_api_key=True, must
        # still reject -- proves the gate is a genuine AND, not an OR.
        key_profile = Candidate(profile(Provider.CLAUDE, "k", is_api_key=True), available(), score=100.0)
        with self.assertRaises(NoCapacity):
            select([key_profile], Policy(allow_api_key=False, api_key_budget=5.0), now=NOW)

    def test_api_key_gate_requires_budget_not_just_allow_flag(self):
        # Negative control: allow_api_key alone, without a positive budget,
        # must still reject -- proves the gate is a genuine AND, not an OR.
        key_profile = Candidate(profile(Provider.CLAUDE, "k", is_api_key=True), available(), score=100.0)
        with self.assertRaises(NoCapacity):
            select([key_profile], Policy(allow_api_key=True, api_key_budget=None), now=NOW)
        with self.assertRaises(NoCapacity):
            select([key_profile], Policy(allow_api_key=True, api_key_budget=0.0), now=NOW)

    def test_api_key_profile_selected_when_gate_fully_open(self):
        key_profile = Candidate(profile(Provider.CLAUDE, "k", is_api_key=True), available(), score=100.0)
        winner = select([key_profile], Policy(allow_api_key=True, api_key_budget=5.0), now=NOW)
        self.assertEqual(winner.profile.account_id, "k")

    def test_disabled_profile_excluded(self):
        disabled = Candidate(profile(Provider.CLAUDE, "off", enabled=False), available(), score=100.0)
        with self.assertRaises(NoCapacity):
            select([disabled], Policy(), now=NOW)

    def test_stale_available_beyond_max_age_excluded(self):
        stale = Candidate(profile(Provider.CLAUDE, "stale"), available(observed_at=NOW - 1000), score=100.0)
        with self.assertRaises(NoCapacity):
            select([stale], Policy(max_age_seconds=300.0), now=NOW)

    def test_fresh_available_within_max_age_selected(self):
        fresh = Candidate(profile(Provider.CLAUDE, "fresh"), available(observed_at=NOW - 10), score=100.0)
        winner = select([fresh], Policy(max_age_seconds=300.0), now=NOW)
        self.assertEqual(winner.profile.account_id, "fresh")


if __name__ == "__main__":
    unittest.main()


class TieBreakersTests(unittest.TestCase):
    """Secondary preferences must never fold into the primary score.

    Design adopted from a parallel lane (ed00db90de) that reached it
    independently. The point is narrow and load-bearing: a caller often has a
    real secondary preference -- fewest jobs already running, furthest reset
    -- and the tempting shortcut is to blend it into `score`. That blend is
    exactly the fabricated single number this module refuses everywhere else,
    because once two unlike measurements are averaged nobody can say what the
    result means. So they ride alongside, and only break ties.
    """

    NOW = 1_000_000.0

    def _c(self, account_id, score, *tie_breakers):
        return Candidate(
            Profile(Provider.CLAUDE, account_id),
            Available(observed_at=self.NOW),
            score=score,
            tie_breakers=tie_breakers,
        )

    def test_tie_breakers_order_equal_scores_highest_first(self):
        ranked = rank(
            [self._c("a", 50.0, 1.0), self._c("b", 50.0, 9.0), self._c("c", 50.0, 5.0)],
            Policy(), now=self.NOW,
        )
        self.assertEqual([c.profile.account_id for c in ranked], ["b", "c", "a"])

    def test_the_primary_score_still_dominates(self):
        """Negative control: a tie-break must not become a back door."""
        ranked = rank(
            [self._c("low", 10.0, 99.0), self._c("high", 90.0, 0.0)],
            Policy(), now=self.NOW,
        )
        self.assertEqual(ranked[0].profile.account_id, "high")

    def test_absent_tie_breakers_stay_deterministic(self):
        ranked = rank(
            [self._c("z", 50.0), self._c("a", 50.0)], Policy(), now=self.NOW
        )
        self.assertEqual([c.profile.account_id for c in ranked], ["a", "z"])

    def test_a_shorter_tuple_does_not_crash_against_a_longer_one(self):
        ranked = rank(
            [self._c("one", 50.0, 5.0), self._c("two", 50.0, 5.0, 7.0)],
            Policy(), now=self.NOW,
        )
        self.assertEqual({c.profile.account_id for c in ranked}, {"one", "two"})
