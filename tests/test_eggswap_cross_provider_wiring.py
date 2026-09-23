"""Policy.cross_provider must actually change the ORDER ACROSS providers.

eggswap/core/policy.py defined four cross-provider strategies and had zero
callers; this is the wiring. The tests exist because my first attempt at it
was wrong in a way that looked right: I led the sort key with the provider
index, so Claude (index 0) beat Codex (index 1) whatever the strategy said,
and the strategy only ever broke ties WITHIN one provider -- which is the one
thing a CROSS-provider ordering is not for. All four strategies returned
identical output and nothing failed.

That is the third time this session a knob parsed and did nothing (the CLI's
--explain flag, and before it the exhaustion gates reading the wrong nesting
level). So the load-bearing test here is not "the option is accepted" but
"the option CHANGES THE ANSWER".
"""
from __future__ import annotations

import unittest

from eggswap.core.select import Policy, rank
from eggswap.core.types import (
    Available, Candidate, Profile, Provider, QuotaWindow,
)

NOW = 1_000_000.0
FIVE_HOURS = 18_000
SEVEN_DAYS = 604_800


def candidate(provider, account_id, used_percent, window_seconds):
    window = QuotaWindow(
        bucket="b", used_percent=used_percent, window_seconds=window_seconds,
        resets_at=NOW + window_seconds, observed_at=NOW,
    )
    return Candidate(
        Profile(provider, account_id),
        Available(windows=(window,), observed_at=NOW),
        score=100.0 - used_percent,
    )


class CrossProviderWiringTests(unittest.TestCase):
    def setUp(self):
        # Codex is barely used but on a weekly window; Claude is over half
        # spent on a five-hour one. Which is "better" genuinely depends on the
        # policy, which is the whole reason this is a choice and not a metric.
        self.codex = candidate(Provider.CODEX, "cx", 10.0, SEVEN_DAYS)
        self.claude = candidate(Provider.CLAUDE, "cl", 60.0, FIVE_HOURS)
        self.pool = [self.codex, self.claude]

    def _order(self, strategy=None):
        policy = Policy() if strategy is None else Policy(cross_provider=strategy)
        return [c.profile.key for c in rank(self.pool, policy, now=NOW)]

    def test_the_default_is_unchanged(self):
        self.assertEqual(self._order(), ["claude:cl", "codex:cx"])

    def test_default_equals_explicit_provider_order(self):
        self.assertEqual(self._order(), self._order("provider_order"))

    def test_a_strategy_CHANGES_the_cross_provider_answer(self):
        """The control my first attempt failed.

        longest_until_reset must prefer the weekly window, which means the
        Codex account overtakes the Claude one -- across providers, not
        merely within one.
        """
        self.assertEqual(self._order("longest_until_reset"), ["codex:cx", "claude:cl"])

    def test_not_every_strategy_reorders_and_that_is_correct(self):
        """most_absolute_headroom refuses to compare unlike windows.

        A 5h bucket and a 7d bucket are different units, so this strategy
        groups by window length rather than interleaving them. Asserted so
        nobody later "fixes" it into averaging incomparable measurements.
        """
        self.assertEqual(self._order("most_absolute_headroom"), ["claude:cl", "codex:cx"])

    def test_spread_without_counts_degrades_instead_of_pretending(self):
        self.assertEqual(self._order("spread"), ["claude:cl", "codex:cx"])

    def test_an_unknown_strategy_falls_back_and_does_not_raise(self):
        """An ordering preference must never turn a schedulable pool into no
        answer at all."""
        self.assertEqual(self._order("no-such-strategy"), ["claude:cl", "codex:cx"])

    def test_case_is_not_load_bearing(self):
        self.assertEqual(self._order("LONGEST_UNTIL_RESET"), ["codex:cx", "claude:cl"])


class CliFlagReachesTheStrategyTests(unittest.TestCase):
    """`eggswap select --cross-provider` must reach Policy.cross_provider.

    The strategies were wired into rank() and then reachable from no command
    line at all -- the same built-and-unwired shape, one layer up, and the
    fourth instance of it in this project.

    Checking it against the REAL accounts on a developer's machine would have
    proved nothing: all three strategies returned the same profile there,
    because today's account state does not happen to differentiate them. A
    null result on uncontrolled data cannot tell "wired but undifferentiated"
    from "not wired", so these fixtures are built so the strategies MUST
    disagree.
    """

    class _Adapter:
        provider = Provider.CLAUDE

        def __init__(self, rows):
            self._rows = rows

        def profiles(self):
            return [p for p, _ in self._rows]

        def availability(self, profile, *, max_age_seconds=300.0, model=None):
            return dict((p.key, a) for p, a in self._rows)[profile.key]

    class _Out:
        def __init__(self):
            self.parts = []

        def write(self, s):
            self.parts.append(s)

        @property
        def text(self):
            return "".join(self.parts)

    def setUp(self):
        self.rows = [
            (Profile(Provider.CODEX, "cx"),
             Available(windows=(QuotaWindow(bucket="b", used_percent=10.0,
                                            window_seconds=SEVEN_DAYS,
                                            resets_at=NOW + SEVEN_DAYS,
                                            observed_at=NOW),), observed_at=NOW)),
            (Profile(Provider.CLAUDE, "cl"),
             Available(windows=(QuotaWindow(bucket="b", used_percent=60.0,
                                            window_seconds=FIVE_HOURS,
                                            resets_at=NOW + FIVE_HOURS,
                                            observed_at=NOW),), observed_at=NOW)),
        ]

    def _select(self, argv):
        from eggswap import cli

        out = self._Out()
        cli.main(argv, adapters=[self._Adapter(self.rows)], out=out, now=NOW, store=False)
        return out.text.strip()

    def test_the_flag_changes_the_answer(self):
        self.assertEqual(self._select(["select", "--cross-provider", "longest_until_reset"]),
                         "codex:cx")

    def test_the_default_and_provider_order_agree_and_differ_from_it(self):
        default = self._select(["select"])
        explicit = self._select(["select", "--cross-provider", "provider_order"])
        self.assertEqual(default, "claude:cl")
        self.assertEqual(default, explicit)
        self.assertNotEqual(default,
                            self._select(["select", "--cross-provider", "longest_until_reset"]))

    def test_an_invalid_strategy_is_refused_by_the_parser(self):
        from eggswap import cli

        with self.assertRaises(SystemExit):
            cli.main(["select", "--cross-provider", "nonsense"],
                     adapters=[self._Adapter(self.rows)], out=self._Out(),
                     now=NOW, store=False)
