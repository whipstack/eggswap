"""Tests for eggswap.core.decision -- recording WHY a provider was selected.

Issue #1096 acceptance criterion 1: a scheduler that cannot say why it chose
an account cannot be audited after a bad choice.
"""
from __future__ import annotations

import unittest

from eggswap.core.decision import Refusal, SelectionDecision, decide
from eggswap.core.select import Policy
from eggswap.core.types import (
    Available,
    AuthDead,
    Candidate,
    Exhausted,
    Profile,
    Provider,
    Unknown,
)

NOW = 1_000_000.0


def profile(provider, account_id, *, is_api_key=False, enabled=True):
    return Profile(provider=provider, account_id=account_id, is_api_key=is_api_key, enabled=enabled)


def available(observed_at=NOW):
    return Available(windows=(), observed_at=observed_at)


class DecideAccountingTests(unittest.TestCase):
    """The arithmetic invariant: nobody handed in vanishes from the record."""

    def test_considered_and_refused_account_for_every_candidate(self):
        claude = Candidate(profile(Provider.CLAUDE, "a"), available(), score=10.0)
        codex = Candidate(profile(Provider.CODEX, "b"), Unknown(stale_since=NOW - 5), score=0.0)
        dead = Candidate(profile(Provider.CLAUDE, "c"), AuthDead(observed_at=NOW), score=0.0)
        decision = decide([claude, codex, dead], Policy(), now=NOW)

        self.assertEqual(len(decision.considered), 3)
        others_eligible_not_chosen = 0  # only one Available candidate here
        self.assertEqual(
            len(decision.considered),
            len(decision.refused) + (1 if decision.chosen else 0) + others_eligible_not_chosen,
        )
        self.assertEqual({r.profile_key for r in decision.refused}, {"codex:b", "claude:c"})
        self.assertEqual(decision.chosen.profile.key, "claude:a")

    def test_accounting_holds_with_multiple_eligible_candidates(self):
        low = Candidate(profile(Provider.CLAUDE, "low"), available(), score=10.0)
        high = Candidate(profile(Provider.CLAUDE, "high"), available(), score=90.0)
        unknown = Candidate(profile(Provider.CODEX, "x"), Unknown(stale_since=NOW - 5), score=0.0)
        decision = decide([low, high, unknown], Policy(), now=NOW)

        others_eligible_not_chosen = 1  # "low" is eligible but lost to "high"
        self.assertEqual(
            len(decision.considered),
            len(decision.refused) + (1 if decision.chosen else 0) + others_eligible_not_chosen,
        )
        self.assertEqual(decision.chosen.profile.key, "claude:high")
        self.assertEqual({r.profile_key for r in decision.refused}, {"codex:x"})

    def test_no_candidate_vanishes_when_none_are_schedulable(self):
        unknown = Candidate(profile(Provider.CLAUDE, "a"), Unknown(stale_since=NOW - 5), score=0.0)
        exhausted = Candidate(profile(Provider.CODEX, "b"), Exhausted(observed_at=NOW), score=0.0)
        decision = decide([unknown, exhausted], Policy(), now=NOW)

        self.assertIsNone(decision.chosen)
        self.assertEqual(len(decision.considered), 2)
        self.assertEqual(len(decision.refused), 2)


class DecideReasonTests(unittest.TestCase):
    def test_unknown_refusal_names_type_and_age(self):
        unknown = Candidate(profile(Provider.CLAUDE, "a"), Unknown(stale_since=NOW - 543), score=0.0)
        decision = decide([unknown], Policy(), now=NOW)
        self.assertEqual(len(decision.refused), 1)
        reason = decision.refused[0].reason
        self.assertIn("Unknown", reason)
        self.assertIn("543", reason)

    def test_auth_dead_refusal_names_type(self):
        dead = Candidate(profile(Provider.CLAUDE, "a"), AuthDead(observed_at=NOW), score=0.0)
        decision = decide([dead], Policy(), now=NOW)
        self.assertIn("AuthDead", decision.refused[0].reason)

    def test_exhausted_refusal_names_type(self):
        exhausted = Candidate(profile(Provider.CLAUDE, "a"), Exhausted(observed_at=NOW), score=0.0)
        decision = decide([exhausted], Policy(), now=NOW)
        self.assertIn("Exhausted", decision.refused[0].reason)

    def test_api_key_refusal_names_the_gate_not_generic_ineligible(self):
        key_profile = Candidate(profile(Provider.CLAUDE, "k", is_api_key=True), available(), score=100.0)
        decision = decide([key_profile], Policy(), now=NOW)
        self.assertEqual(len(decision.refused), 1)
        self.assertIn("api-key", decision.refused[0].reason)
        self.assertNotEqual(decision.refused[0].reason, "not eligible")

    def test_disabled_profile_refusal_names_it(self):
        disabled = Candidate(profile(Provider.CLAUDE, "off", enabled=False), available(), score=100.0)
        decision = decide([disabled], Policy(), now=NOW)
        self.assertIn("disabled", decision.refused[0].reason)

    def test_provider_not_allowed_refusal_names_it(self):
        codex = Candidate(profile(Provider.CODEX, "only"), available(), score=100.0)
        decision = decide([codex], Policy(allow_providers=(Provider.CLAUDE,)), now=NOW)
        self.assertIn("provider", decision.refused[0].reason)

    def test_stale_available_refusal_names_age(self):
        stale = Candidate(profile(Provider.CLAUDE, "stale"), available(observed_at=NOW - 1000), score=100.0)
        decision = decide([stale], Policy(max_age_seconds=300.0), now=NOW)
        self.assertIn("stale", decision.refused[0].reason)

    def test_no_reason_is_the_bare_word_not_eligible(self):
        """Negative control: every refusal must be more specific than the ban phrase."""
        claude = Candidate(profile(Provider.CLAUDE, "a"), available(), score=10.0)
        codex = Candidate(profile(Provider.CODEX, "b"), Unknown(stale_since=NOW - 5), score=0.0)
        dead = Candidate(profile(Provider.CLAUDE, "c"), AuthDead(observed_at=NOW), score=0.0)
        decision = decide([claude, codex, dead], Policy(), now=NOW)
        for refusal in decision.refused:
            self.assertNotEqual(refusal.reason, "not eligible")


class DecidePolicySummaryTests(unittest.TestCase):
    def test_policy_summary_records_inputs_not_budget_value(self):
        candidate = Candidate(
            profile(Provider.CLAUDE, "k", is_api_key=True), available(), score=1.0
        )
        decision = decide([candidate], Policy(allow_api_key=True, api_key_budget=42.0), now=NOW)
        self.assertEqual(decision.policy_summary["allow_api_key"], "True")
        self.assertEqual(decision.policy_summary["api_key_budget"], "set")
        self.assertNotIn("42", decision.policy_summary["api_key_budget"])
        for value in decision.policy_summary.values():
            self.assertNotIn("42", str(value))

    def test_policy_summary_records_allow_providers_max_age_and_prefer(self):
        decision = decide(
            [],
            Policy(allow_providers=(Provider.CODEX, Provider.CLAUDE), max_age_seconds=60.0, prefer="least_headroom"),
            now=NOW,
        )
        self.assertEqual(decision.policy_summary["allow_providers"], "codex,claude")
        self.assertEqual(decision.policy_summary["max_age_seconds"], "60")
        self.assertEqual(decision.policy_summary["prefer"], "least_headroom")


class DecideRationaleTests(unittest.TestCase):
    def test_rationale_names_winner_and_decisive_score(self):
        low = Candidate(profile(Provider.CLAUDE, "low"), available(), score=10.0)
        high = Candidate(profile(Provider.CLAUDE, "high"), available(), score=90.0)
        decision = decide([low, high], Policy(), now=NOW)
        self.assertIn("claude:high", decision.rationale)

    def test_rationale_names_provider_order_when_that_is_decisive(self):
        claude = Candidate(profile(Provider.CLAUDE, "a"), available(), score=10.0)
        codex = Candidate(profile(Provider.CODEX, "a"), available(), score=99.0)
        decision = decide([claude, codex], Policy(allow_providers=(Provider.CLAUDE, Provider.CODEX)), now=NOW)
        self.assertIn("claude:a", decision.rationale)
        self.assertIn("allow_providers", decision.rationale)

    def test_rationale_is_a_single_sentence(self):
        low = Candidate(profile(Provider.CLAUDE, "low"), available(), score=10.0)
        decision = decide([low], Policy(), now=NOW)
        self.assertNotIn("\n", decision.rationale)

    def test_rationale_on_no_capacity_still_names_a_reason(self):
        unknown = Candidate(profile(Provider.CLAUDE, "a"), Unknown(stale_since=NOW - 5), score=0.0)
        decision = decide([unknown], Policy(), now=NOW)
        self.assertIsNone(decision.chosen)
        self.assertIn("refused", decision.rationale)


class DecideDeterminismTests(unittest.TestCase):
    def test_same_inputs_same_decision(self):
        low = Candidate(profile(Provider.CLAUDE, "low"), available(), score=10.0)
        high = Candidate(profile(Provider.CLAUDE, "high"), available(), score=90.0)
        unknown = Candidate(profile(Provider.CODEX, "x"), Unknown(stale_since=NOW - 5), score=0.0)
        candidates = [low, high, unknown]
        policy = Policy()

        first = decide(candidates, policy, now=NOW)
        second = decide(candidates, policy, now=NOW)

        self.assertEqual(first.considered, second.considered)
        self.assertEqual(first.refused, second.refused)
        self.assertEqual(first.rationale, second.rationale)
        self.assertEqual(first.chosen.profile.key, second.chosen.profile.key)
        self.assertEqual(first.policy_summary, second.policy_summary)

    def test_decide_does_not_reimplement_selection_ordering(self):
        # Same set, reversed input order -- the winner must not depend on
        # input order, matching rank()'s own deterministic tie-break.
        a = Candidate(profile(Provider.CLAUDE, "a"), available(), score=50.0)
        b = Candidate(profile(Provider.CLAUDE, "b"), available(), score=50.0)
        forward = decide([a, b], Policy(), now=NOW)
        backward = decide([b, a], Policy(), now=NOW)
        self.assertEqual(forward.chosen.profile.key, backward.chosen.profile.key)
        self.assertEqual(forward.chosen.profile.key, "claude:a")


class DecideEmptyInputTests(unittest.TestCase):
    def test_no_candidates_at_all(self):
        decision = decide([], Policy(), now=NOW)
        self.assertIsNone(decision.chosen)
        self.assertEqual(decision.considered, ())
        self.assertEqual(decision.refused, ())
        self.assertIn("no candidates", decision.rationale)


if __name__ == "__main__":
    unittest.main()
