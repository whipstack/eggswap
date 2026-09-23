"""Tests for eggswap.core.budget (Issue #1096 acceptance 4).

Each test below fails if the rule it names is removed -- see the module
docstring in eggswap/core/budget.py for why each rule exists.
"""
from __future__ import annotations

import unittest

from eggswap.core.budget import (
    BudgetExhausted,
    Ledger,
    may_use_api_profile,
    reserve,
    settle,
)
from eggswap.core.select import Policy
from eggswap.core.types import Exhausted, Profile, Provider, Unknown


def profile(account_id="acct-1", *, is_api_key=False):
    return Profile(provider=Provider.CLAUDE, account_id=account_id, is_api_key=is_api_key)


class ReserveCeilingKnownTests(unittest.TestCase):
    def test_reserve_within_known_ceiling_marks_ceiling_known(self):
        ledger = Ledger(goal_id="g1", ceiling=10.0)
        after = reserve(ledger, 4.0, profile=profile(), reservation_id="r1")
        self.assertEqual(after.spent, 4.0)
        self.assertTrue(after.reservations["r1"].ceiling_known)

    def test_reserve_against_unknown_ceiling_is_allowed_but_marked_unknown(self):
        ledger = Ledger(goal_id="g1", ceiling=None)
        after = reserve(ledger, 1_000_000.0, profile=profile(), reservation_id="r1")
        # Allowed -- there is nothing to refuse against.
        self.assertEqual(after.spent, 1_000_000.0)
        # But NOT conflated with "verified within a real ceiling".
        self.assertFalse(after.reservations["r1"].ceiling_known)
        self.assertFalse(after.ceiling_known)

    def test_unknown_ceiling_is_distinct_from_a_zero_or_huge_ceiling(self):
        unknown = Ledger(goal_id="g1", ceiling=None)
        huge = Ledger(goal_id="g1", ceiling=1e18)
        self.assertFalse(unknown.ceiling_known)
        self.assertTrue(huge.ceiling_known)
        self.assertNotEqual(unknown.ceiling, huge.ceiling)


class ReserveOverspendTests(unittest.TestCase):
    def test_overspend_refuses_before_launch_naming_goal_ceiling_and_request(self):
        ledger = Ledger(goal_id="goal-42", ceiling=5.0, spent=4.0)
        with self.assertRaises(BudgetExhausted) as ctx:
            reserve(ledger, 2.0, profile=profile())
        err = ctx.exception
        self.assertEqual(err.goal_id, "goal-42")
        self.assertEqual(err.ceiling, 5.0)
        self.assertEqual(err.requested, 2.0)

    def test_overspend_does_not_mutate_the_ledger(self):
        ledger = Ledger(goal_id="g1", ceiling=5.0, spent=4.0)
        with self.assertRaises(BudgetExhausted):
            reserve(ledger, 2.0, profile=profile())
        self.assertEqual(ledger.spent, 4.0)
        self.assertEqual(ledger.reservations, {})

    def test_reserve_never_mutates_its_input_ledger(self):
        ledger = Ledger(goal_id="g1", ceiling=10.0)
        reserve(ledger, 3.0, profile=profile(), reservation_id="r1")
        self.assertEqual(ledger.spent, 0.0)
        self.assertEqual(ledger.reservations, {})


class SettleOverrunTests(unittest.TestCase):
    def test_settle_actual_over_reserved_is_recorded_as_overrun(self):
        ledger = Ledger(goal_id="g1", ceiling=100.0)
        reserved = reserve(ledger, 5.0, profile=profile(), reservation_id="r1")
        settled = settle(reserved, 8.0, reserved="r1")
        self.assertEqual(settled.reservations["r1"].overrun, 3.0)
        self.assertEqual(settled.overrun_total, 3.0)
        # actual charge, not the stale estimate, ends up in spent
        self.assertEqual(settled.spent, 8.0)

    def test_settle_actual_under_reserved_records_no_overrun(self):
        ledger = Ledger(goal_id="g1", ceiling=100.0)
        reserved = reserve(ledger, 5.0, profile=profile(), reservation_id="r1")
        settled = settle(reserved, 2.0, reserved="r1")
        self.assertEqual(settled.reservations["r1"].overrun, 0.0)
        self.assertEqual(settled.overrun_total, 0.0)
        self.assertEqual(settled.spent, 2.0)

    def test_settle_is_idempotent_per_reservation_id(self):
        ledger = Ledger(goal_id="g1", ceiling=100.0)
        reserved = reserve(ledger, 5.0, profile=profile(), reservation_id="r1")
        once = settle(reserved, 8.0, reserved="r1")
        twice = settle(once, 8.0, reserved="r1")
        self.assertEqual(once.spent, twice.spent)
        self.assertEqual(twice.overrun_total, 3.0)  # not double-counted to 6.0

    def test_settle_unknown_reservation_id_raises(self):
        ledger = Ledger(goal_id="g1", ceiling=100.0)
        with self.assertRaises(Exception):
            settle(ledger, 1.0, reserved="does-not-exist")


class MayUseApiProfileTests(unittest.TestCase):
    def test_non_api_key_profile_is_always_allowed(self):
        p = profile(is_api_key=False)
        policy = Policy(allow_api_key=False, api_key_budget=None)
        self.assertTrue(may_use_api_profile(p, policy))

    def test_api_key_profile_refused_when_policy_does_not_allow_it(self):
        p = profile(is_api_key=True)
        policy = Policy(allow_api_key=False, api_key_budget=100.0)
        self.assertFalse(may_use_api_profile(p, policy))

    def test_api_key_profile_refused_when_allowed_but_budget_is_none(self):
        p = profile(is_api_key=True)
        policy = Policy(allow_api_key=True, api_key_budget=None)
        self.assertFalse(may_use_api_profile(p, policy))

    def test_api_key_profile_refused_when_budget_is_zero(self):
        p = profile(is_api_key=True)
        policy = Policy(allow_api_key=True, api_key_budget=0.0)
        self.assertFalse(may_use_api_profile(p, policy))

    def test_api_key_profile_allowed_when_both_flags_set(self):
        p = profile(is_api_key=True)
        policy = Policy(allow_api_key=True, api_key_budget=50.0)
        self.assertTrue(may_use_api_profile(p, policy))


class AuthoritativeLimitTests(unittest.TestCase):
    """The subtle rule: a healthy local ledger must never promote a provider
    read that came back Exhausted or Unknown into something schedulable.
    Budget is a spending cap on OUR side; it is not evidence about the
    provider, which only eggswap.core.select's Availability speaks for.
    """

    def test_healthy_ledger_does_not_make_an_exhausted_profile_schedulable(self):
        ledger = Ledger(goal_id="g1", ceiling=1_000.0)
        after = reserve(ledger, 1.0, profile=profile(), reservation_id="r1")
        self.assertLess(after.spent, after.ceiling)  # plenty of local budget

        availability = Exhausted(reset_at=None, bucket="5h", observed_at=1.0)
        # Nothing in budget.py's return value carries or overrides this.
        self.assertFalse(availability.schedulable)
        self.assertNotIn("schedulable", vars(after))

    def test_healthy_ledger_does_not_make_an_unknown_profile_schedulable(self):
        ledger = Ledger(goal_id="g1", ceiling=1_000.0)
        after = reserve(ledger, 1.0, profile=profile(), reservation_id="r1")
        self.assertLess(after.spent, after.ceiling)

        availability = Unknown(stale_since=1.0, reason="probe failed")
        self.assertFalse(availability.schedulable)


if __name__ == "__main__":
    unittest.main()
