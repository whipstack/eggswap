"""The exhaustion gates must fire against the reply shape the provider sends.

A code review (PR #1092, andy-zen-dev, REQUEST_CHANGES) found that the Codex
reader returned ``Available`` for every well-formed reply and never
``Exhausted``, so a fully exhausted Codex profile could be selected. A fix
landed for the 100%-used case. Verifying it, the other half was still open in
a way the fix's own tests could not see: ``ordinaryUsageAllowed``,
``spendControlReached`` and ``rateLimitReachedType`` were all read from
``result``, but the live capture
(docs/research/eggswap/codex-ratelimits-live.md section 2) puts only the
FIRST of those at the top of ``result`` and nests the other two inside
``result.rateLimits``.

Two of three gates therefore could not fire against any real reply. Present
in the code, inert in production -- the worst shape a guard can take, because
it reads as a defence in review and defends nothing.

Every test here uses the LIVE nesting, not a convenient flat one. That is the
whole point: a fixture that puts the fields where the code happens to look
proves only that the code agrees with itself.
"""
from __future__ import annotations

import unittest

from eggswap.adapters.codex_ratelimits import AppServerRateLimitReader
from eggswap.core.select import Policy, select
from eggswap.core.types import (
    Available,
    Candidate,
    Exhausted,
    NoCapacity,
    Profile,
    Provider,
    Unknown,
)

NOW = 1_000_000.0


def live_reply(used_percent=37, *, rate_limits_extra=None, result_extra=None):
    """The exact shape of the captured live reply, minus redactions."""
    rate_limits = {
        "limitId": "codex",
        "limitName": None,
        "normalModelSlug": None,
        "primary": {
            "usedPercent": used_percent,
            "windowDurationMins": 10080,
            "resetsAt": 1790580677,
        },
        "secondary": None,
        "credits": {"hasCredits": False, "unlimited": False, "balance": "0"},
        "individualLimit": None,
        "spendControlReached": False,
        "planType": "pro",
        "rateLimitReachedType": None,
    }
    if rate_limits_extra:
        rate_limits.update(rate_limits_extra)
    result = {
        "ordinaryUsageAllowed": True,
        "rateLimits": rate_limits,
        "accountId": "REDACTED",
    }
    if result_extra:
        result.update(result_extra)
    return {"id": 2, "result": result}


def availability(**kwargs):
    return AppServerRateLimitReader._to_availability(live_reply(**kwargs), observed_at=NOW)


class HappyPathStillWorksTests(unittest.TestCase):
    """The gates must not be so eager that nothing is ever schedulable."""

    def test_the_captured_live_reply_is_available(self):
        self.assertIsInstance(availability(), Available)

    def test_high_but_not_full_is_still_available(self):
        self.assertIsInstance(availability(used_percent=99.9), Available)


class GatesFireAtTheLiveNestingTests(unittest.TestCase):
    def test_full_window_is_exhausted(self):
        self.assertIsInstance(availability(used_percent=100), Exhausted)

    def test_reached_type_nested_in_rateLimits_is_exhausted(self):
        """The regression: this field lives in result.rateLimits, not result."""
        got = availability(rate_limits_extra={"rateLimitReachedType": "weekly"})
        self.assertIsInstance(got, Exhausted)

    def test_spend_control_nested_in_rateLimits_is_not_available(self):
        got = availability(rate_limits_extra={"spendControlReached": True})
        self.assertNotIsInstance(got, Available)
        self.assertIsInstance(got, Unknown)

    def test_ordinary_usage_disallowed_at_result_top_level_is_not_available(self):
        got = availability(result_extra={"ordinaryUsageAllowed": False})
        self.assertNotIsInstance(got, Available)
        self.assertIsInstance(got, Unknown)

    def test_a_gate_at_the_WRONG_level_is_still_caught(self):
        """Both levels are consulted, so a future move does not re-break it."""
        got = availability(result_extra={"rateLimitReachedType": "weekly"})
        self.assertNotIsInstance(got, Available)


class NothingBlockedIsSchedulableTests(unittest.TestCase):
    """The consequence the review actually cared about: no spawn."""

    def _select(self, avail):
        # `now=NOW` matters: these fixtures are stamped at NOW, and without it
        # select() measures their age against the wall clock, finds them
        # decades stale and refuses everything -- which would make the three
        # refusal tests below pass for entirely the wrong reason.
        candidate = Candidate(Profile(Provider.CODEX, "a"), avail, score=100.0)
        return select([candidate], Policy(allow_providers=(Provider.CODEX,)), now=NOW)

    def test_exhausted_codex_is_never_selected_even_as_the_only_candidate(self):
        with self.assertRaises(NoCapacity):
            self._select(availability(used_percent=100))

    def test_reached_limit_codex_is_never_selected(self):
        with self.assertRaises(NoCapacity):
            self._select(availability(rate_limits_extra={"rateLimitReachedType": "weekly"}))

    def test_spend_controlled_codex_is_never_selected(self):
        with self.assertRaises(NoCapacity):
            self._select(availability(rate_limits_extra={"spendControlReached": True}))

    def test_a_healthy_codex_account_IS_selected(self):
        """Negative control: the three tests above must not pass vacuously."""
        self.assertEqual(self._select(availability()).profile.key, "codex:a")


if __name__ == "__main__":
    unittest.main()
