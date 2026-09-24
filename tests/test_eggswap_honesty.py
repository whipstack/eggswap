"""Adversarial tests against eggswap's honesty claims (whipstack #1037 / #773).

Each test class attacks ONE claim eggswap's own docstrings make about itself
and would FAIL if a future change silently weakened that claim. This file is
not the implementation's test suite; it exists to catch a regression back
into the specific incidents eggswap/core/types.py documents (the frozen
negative cache, and the #581 stale-fence-releases-the-new-holder bug).
"""
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from eggswap.core.select import Policy, rank, select
from eggswap.core.types import (
    Available,
    AuthDead,
    Candidate,
    NoCapacity,
    Profile,
    Provider,
    QuotaWindow,
    Source,
    StaleFence,
    Unknown,
    WorkSpec,
)
from eggswap.core.store import LeaseStore

try:
    from eggswap.adapters.claude_cswap import ClaudeCswapAdapter
except ImportError:  # pragma: no cover
    ClaudeCswapAdapter = None

try:
    from eggswap.adapters.codex_home import CodexHomeAdapter
except ImportError:  # pragma: no cover
    CodexHomeAdapter = None

try:
    import eggswap.cli as cli_mod
except ImportError:  # pragma: no cover
    cli_mod = None


class FrozenPercentageNeverRendersFreshTest(unittest.TestCase):
    """Attack 1: "a frozen percentage never renders as fresh".

    A QuotaWindow read 6 days ago at 44% must never render as a bare "44%"
    anywhere -- neither from QuotaWindow.render() itself nor from the CLI's
    own rendering path, which is the one place a human actually reads this.
    """

    def _stale_window(self, now: float) -> QuotaWindow:
        return QuotaWindow(
            bucket="sevenDay",
            used_percent=44.0,
            window_seconds=None,
            resets_at=None,
            observed_at=now - 6 * 86400,
            source=Source.OBSERVED,
        )

    def test_quota_window_render_marks_staleness(self):
        now = 2_000_000.0
        text = self._stale_window(now).render(max_age_seconds=300.0, now=now)
        self.assertNotIn("44% used", text)
        self.assertIn("UNKNOWN", text)
        self.assertIn("stale", text)

    def test_cli_render_availability_marks_staleness(self):
        if cli_mod is None:
            self.skipTest("eggswap.cli not present yet")
        now = 2_000_000.0
        availability = Available(windows=(self._stale_window(now),), observed_at=now - 6 * 86400)
        text = cli_mod._render_availability(availability, max_age_seconds=300.0, now=now)
        self.assertNotIn("44% used", text)
        self.assertIn("UNKNOWN", text)


class UnknownNeverScheduledTest(unittest.TestCase):
    """Attack 2: "Unknown is never scheduled" -- select() must never return
    an Unknown candidate, regardless of score or what else is available.
    """

    def _policy(self) -> Policy:
        return Policy(allow_providers=(Provider.CLAUDE, Provider.CODEX))

    def test_unknown_as_only_candidate_raises_no_capacity(self):
        profile = Profile(provider=Provider.CLAUDE, account_id="1")
        candidates = [Candidate(profile=profile, availability=Unknown(stale_since=1.0), score=999.0)]
        with self.assertRaises(NoCapacity):
            select(candidates, self._policy(), now=10.0)

    def test_unknown_with_best_score_is_not_chosen_over_available(self):
        p_unknown = Profile(provider=Provider.CLAUDE, account_id="1")
        p_available = Profile(provider=Provider.CLAUDE, account_id="2")
        candidates = [
            Candidate(profile=p_unknown, availability=Unknown(stale_since=1.0), score=9999.0),
            Candidate(
                profile=p_available,
                availability=Available(windows=(), observed_at=9.0),
                score=1.0,
            ),
        ]
        chosen = select(candidates, self._policy(), now=10.0)
        self.assertEqual(chosen.profile.account_id, "2")

    def test_unknown_when_every_other_candidate_is_auth_dead_raises(self):
        p1 = Profile(provider=Provider.CLAUDE, account_id="1")
        p2 = Profile(provider=Provider.CLAUDE, account_id="2")
        candidates = [
            Candidate(profile=p1, availability=Unknown(stale_since=1.0), score=100.0),
            Candidate(profile=p2, availability=AuthDead(reason="dead", observed_at=1.0), score=0.0),
        ]
        with self.assertRaises(NoCapacity) as ctx:
            select(candidates, self._policy(), now=10.0)
        self.assertEqual(rank(candidates, self._policy(), now=10.0), [])
        self.assertEqual(len(ctx.exception.reasons), 2)


class TransportFailureIsNotAnAnswerTest(unittest.TestCase):
    """Attack 3: "a transport failure is not an answer about the account" --
    a raising, timing-out, or garbage-returning runner must all become
    Unknown, never Available or Exhausted.
    """

    def setUp(self):
        if ClaudeCswapAdapter is None:
            self.skipTest("eggswap.adapters.claude_cswap not present yet")
        self.profile = Profile(provider=Provider.CLAUDE, account_id="1", label="a@example.com")

    def test_runner_raises_oserror_becomes_unknown(self):
        def runner(*a, **kw):
            raise OSError("cswap binary not found")

        adapter = ClaudeCswapAdapter(runner=runner, clock=lambda: 10.0)
        result = adapter.availability(self.profile)
        self.assertIsInstance(result, Unknown)

    def test_runner_times_out_becomes_unknown(self):
        def runner(*a, **kw):
            raise subprocess.TimeoutExpired(cmd=["cswap"], timeout=30)

        adapter = ClaudeCswapAdapter(runner=runner, clock=lambda: 10.0)
        result = adapter.availability(self.profile)
        self.assertIsInstance(result, Unknown)

    def test_runner_returns_garbage_becomes_unknown(self):
        class FakeResult:
            returncode = 0
            stdout = "{ this is not json ]]]"

        adapter = ClaudeCswapAdapter(runner=lambda *a, **kw: FakeResult(), clock=lambda: 10.0)
        result = adapter.availability(self.profile)
        self.assertIsInstance(result, Unknown)

    def test_runner_nonzero_exit_becomes_unknown(self):
        class FakeResult:
            returncode = 1
            stdout = ""

        adapter = ClaudeCswapAdapter(runner=lambda *a, **kw: FakeResult(), clock=lambda: 10.0)
        result = adapter.availability(self.profile)
        self.assertIsInstance(result, Unknown)


class ApiKeyProfilesOffByDefaultTest(unittest.TestCase):
    """Attack 4: "API profiles are OFF by default" -- allow_api_key alone,
    or api_key_budget alone, must each still refuse an API-key profile.
    """

    def _candidate(self) -> Candidate:
        profile = Profile(provider=Provider.CLAUDE, account_id="1", is_api_key=True)
        return Candidate(profile=profile, availability=Available(windows=(), observed_at=9.0), score=1.0)

    def test_allow_api_key_true_without_budget_refuses(self):
        policy = Policy(allow_api_key=True, api_key_budget=None)
        self.assertEqual(rank([self._candidate()], policy, now=10.0), [])
        with self.assertRaises(NoCapacity):
            select([self._candidate()], policy, now=10.0)

    def test_budget_set_without_allow_api_key_refuses(self):
        policy = Policy(allow_api_key=False, api_key_budget=5.0)
        self.assertEqual(rank([self._candidate()], policy, now=10.0), [])
        with self.assertRaises(NoCapacity):
            select([self._candidate()], policy, now=10.0)

    def test_both_set_is_required_to_admit(self):
        policy = Policy(allow_api_key=True, api_key_budget=5.0)
        ranked = rank([self._candidate()], policy, now=10.0)
        self.assertEqual(len(ranked), 1)


class StaleHolderCannotReleaseReplacementTest(unittest.TestCase):
    """Attack 5: "a stale holder cannot release its replacement's lease" --
    reproduces the #581-adjacent bug this estate already found and fixed
    once: acquire, expire, reap, re-acquire, then release the ORIGINAL
    lease must raise StaleFence and must NOT drop the new holder's lease.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._now = [1000.0]
        self.store = LeaseStore(Path(self._tmp.name), clock=lambda: self._now[0])
        self.profile = Profile(provider=Provider.CLAUDE, account_id="1")

    def test_stale_release_is_refused_and_new_holder_survives(self):
        original = self.store.acquire(self.profile, ttl_seconds=10.0, holder="worker-A")

        self._now[0] += 20.0  # original lease now expired
        reaped = self.store.reap_expired()
        self.assertEqual(len(reaped), 1)
        self.assertEqual(reaped[0].lease_id, original.lease_id)

        new_lease = self.store.acquire(self.profile, ttl_seconds=10.0, holder="worker-B")
        self.assertNotEqual(new_lease.fence, original.fence)

        with self.assertRaises(StaleFence):
            self.store.release(original)

        holder = self.store.holder_of(self.profile)
        self.assertIsNotNone(holder)
        self.assertEqual(holder.lease_id, new_lease.lease_id)
        self.assertEqual(holder.holder, "worker-B")


class NoTokenLeavesTheAdapterTest(unittest.TestCase):
    """Attack 6: "no token ever leaves the adapter" -- a canary string placed
    in auth.json's token fields must not appear in any repr/str/mapping the
    Codex adapter returns from profiles() or availability().
    """

    CANARY = "CANARY-TOKEN-VALUE"

    def setUp(self):
        if CodexHomeAdapter is None:
            self.skipTest("eggswap.adapters.codex_home not present yet")
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        home = Path(self._tmp.name)
        (home / "auth.json").write_text(
            '{"tokens": {"account_id": "acct-1", '
            '"access_token": "%s", "refresh_token": "%s-2", '
            '"id_token": "%s-3"}, "auth_mode": "chatgpt"}' % (self.CANARY, self.CANARY, self.CANARY)
        )
        self.home = home

    def test_canary_absent_from_profiles_and_availability(self):
        adapter = CodexHomeAdapter([self.home], clock=lambda: 10.0)
        profiles = adapter.profiles()
        self.assertEqual(len(profiles), 1)
        profile = profiles[0]

        self.assertNotIn(self.CANARY, repr(profile))
        self.assertNotIn(self.CANARY, str(profile))
        self.assertNotIn(self.CANARY, repr(profiles))
        for value in profile.metadata.values():
            self.assertNotIn(self.CANARY, str(value))

        availability = adapter.availability(profile)
        self.assertNotIn(self.CANARY, repr(availability))
        self.assertNotIn(self.CANARY, str(availability))


if __name__ == "__main__":
    unittest.main()
