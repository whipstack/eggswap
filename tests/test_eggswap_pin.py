"""Tests for Policy.pin / Policy.capability (Issue #1096 acceptance 1).

A pin that silently degrades into a preference is worse than no pin, because
the caller believes it was honoured. These tests exist to pin (no pun
intended) that failure mode shut: an eligible-but-lower-scoring pin must win,
an ineligible pin must raise rather than fall back, and a not-found pin must
say so distinctly from "nothing was available".
"""
from __future__ import annotations

import unittest

from eggswap.core.select import Policy, select
from eggswap.core.types import (
    Available,
    Candidate as EggCandidate,
    Exhausted,
    NoCapacity,
    Profile,
    Provider,
    Unknown,
)

NOW = 1_000_000.0


def profile(account_id, *, provider=Provider.CLAUDE, metadata=None):
    return Profile(provider=provider, account_id=account_id, metadata=metadata or {})


def available(observed_at=NOW):
    return Available(windows=(), observed_at=observed_at)


def candidate(account_id, availability, score=0.0, *, provider=Provider.CLAUDE, metadata=None):
    return EggCandidate(profile(account_id, provider=provider, metadata=metadata), availability, score=score)


class PinTests(unittest.TestCase):
    def test_pinned_eligible_wins_over_better_scoring_rival(self):
        pinned = candidate("pinned", available(), score=1.0)
        rival = candidate("rival", available(), score=99.0)
        winner = select(
            [pinned, rival], Policy(pin="claude:pinned"), now=NOW
        )
        self.assertEqual(winner.profile.account_id, "pinned")

    def test_pinned_but_exhausted_raises_rather_than_falling_back(self):
        pinned = candidate("pinned", Exhausted(reset_at=NOW + 3600, observed_at=NOW), score=0.0)
        rival = candidate("rival", available(), score=99.0)
        with self.assertRaises(NoCapacity) as ctx:
            select([pinned, rival], Policy(pin="claude:pinned"), now=NOW)
        # No other profile is ever returned in place of an ineligible pin.
        self.assertIn("claude:pinned", ctx.exception.reasons)
        self.assertIsInstance(ctx.exception.reasons["claude:pinned"], Exhausted)
        self.assertNotIn("claude:rival", ctx.exception.reasons)

    def test_pin_not_found_says_so_distinctly(self):
        rival = candidate("rival", available(), score=99.0)
        with self.assertRaises(NoCapacity) as ctx:
            select([rival], Policy(pin="claude:ghost"), now=NOW)
        reasons = ctx.exception.reasons
        (key, value) = next(iter(reasons.items()))
        self.assertIn("not_found", key)
        self.assertIsInstance(value, Unknown)
        self.assertIn("not found", value.reason)
        self.assertIn("ghost", value.reason)

    def test_capability_filters_ineligible_profiles(self):
        vision = candidate("vision-acct", available(), score=10.0, metadata={"capabilities": "vision,long-context"})
        plain = candidate("plain-acct", available(), score=99.0)
        winner = select([vision, plain], Policy(capability="vision"), now=NOW)
        self.assertEqual(winner.profile.account_id, "vision-acct")

    def test_profile_with_no_declared_capabilities_never_matches(self):
        plain = candidate("plain-acct", available(), score=99.0)
        with self.assertRaises(NoCapacity):
            select([plain], Policy(capability="vision"), now=NOW)

    def test_pin_and_capability_conflict_refuses(self):
        pinned = candidate("pinned", available(), score=50.0)  # no capabilities declared
        with self.assertRaises(NoCapacity) as ctx:
            select([pinned], Policy(pin="claude:pinned", capability="vision"), now=NOW)
        self.assertIn("claude:pinned", ctx.exception.reasons)

    def test_negative_control_neither_set_behaviour_unchanged(self):
        low = candidate("low", available(), score=10.0)
        high = candidate("high", available(), score=90.0)
        winner = select([low, high], Policy(), now=NOW)
        self.assertEqual(winner.profile.account_id, "high")


if __name__ == "__main__":
    unittest.main()
