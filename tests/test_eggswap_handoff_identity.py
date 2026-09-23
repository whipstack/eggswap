"""Tests for eggswap.core.handoff -- #1096 acceptance criteria 2 and 3.

Criterion 2: "If a different provider is chosen, start its NATIVE session
from an explicit checkpoint; do not treat Claude and Codex session IDs as
interchangeable."
Criterion 3: a handoff carries budget lineage, with a negative control that
changing one hash or generation REFUSES continuation.

Each rule below has a test that FAILS if the rule is removed from
SessionRef/continuity_violations.
"""
from __future__ import annotations

import unittest

from eggswap.core.handoff import Checkpoint, SessionRef, continuity_violations
from eggswap.core.types import Profile, Provider, WorkSpec

CLAUDE = Profile(provider=Provider.CLAUDE, account_id="acct-1", label="c1")
CODEX = Profile(provider=Provider.CODEX, account_id="acct-2", label="x2")

WS = WorkSpec(
    goal_id="goal-1096",
    contract_version="2026-09-20-goal-delegation-v1",
    spec_hash="sha256:abc123",
    model="claude-sonnet-5",
    description="native session handoff carries identity and budget",
)


def _cp(
    completed=(),
    remaining=(),
    profile=CLAUDE.key,
    session=None,
    generation=0,
    budget_spent=0.0,
    budget_ceiling=None,
):
    return Checkpoint(
        workspec=WS,
        completed=tuple(completed),
        remaining=tuple(remaining),
        from_profile=profile,
        created_at=1.0,
        fence=1,
        session=session,
        generation=generation,
        budget_spent=budget_spent,
        budget_ceiling=budget_ceiling,
    )


class SessionRefIdentityTests(unittest.TestCase):
    def test_same_native_id_different_provider_is_not_same_session(self):
        claude_sess = SessionRef(Provider.CLAUDE, "shared-shape-id-123")
        codex_sess = SessionRef(Provider.CODEX, "shared-shape-id-123")
        self.assertFalse(claude_sess.same_session_as(codex_sess))
        self.assertFalse(codex_sess.same_session_as(claude_sess))

    def test_same_provider_and_id_is_the_same_session(self):
        a = SessionRef(Provider.CLAUDE, "abc")
        b = SessionRef(Provider.CLAUDE, "abc")
        self.assertTrue(a.same_session_as(b))


class ContinuityIdentityAndBudgetTests(unittest.TestCase):
    def test_cross_provider_handoff_carrying_old_sessionref_unchanged_is_a_violation(self):
        claude_sess = SessionRef(Provider.CLAUDE, "claude-sess-1")
        before = _cp(completed=("art-1",), profile=CLAUDE.key, session=claude_sess)
        after = _cp(
            completed=("art-1",),
            profile=CODEX.key,
            session=claude_sess,  # bug: source session reused unchanged
        )
        violations = continuity_violations(before, after)
        self.assertTrue(
            any("SessionRef unchanged" in v for v in violations), violations
        )

    def test_generation_going_backwards_is_a_violation(self):
        before = _cp(completed=("art-1",), generation=5)
        after = _cp(completed=("art-1",), generation=3)
        violations = continuity_violations(before, after)
        self.assertTrue(any("generation" in v and "backwards" in v for v in violations), violations)

    def test_generation_repeating_while_work_advanced_is_a_violation(self):
        before = _cp(completed=("art-1",), generation=2)
        after = _cp(completed=("art-1", "art-2"), generation=2)
        violations = continuity_violations(before, after)
        self.assertTrue(
            any("replayed generation" in v for v in violations), violations
        )

    def test_budget_spent_decreasing_is_a_violation(self):
        before = _cp(budget_spent=50.0)
        after = _cp(budget_spent=10.0)
        violations = continuity_violations(before, after)
        self.assertTrue(
            any("budget_spent decreased" in v for v in violations), violations
        )

    def test_budget_spent_exceeding_ceiling_is_a_violation(self):
        before = _cp(budget_spent=10.0, budget_ceiling=100.0)
        after = _cp(budget_spent=150.0, budget_ceiling=100.0)
        violations = continuity_violations(before, after)
        self.assertTrue(
            any("exceeds budget_ceiling" in v for v in violations), violations
        )

    def test_genuine_cross_provider_handoff_returns_empty(self):
        """The empty list must be reachable, or every rule above is decoration."""
        claude_sess = SessionRef(Provider.CLAUDE, "claude-sess-1")
        codex_sess = SessionRef(Provider.CODEX, "codex-thread-9")
        before = _cp(
            completed=("art-1",),
            remaining=("art-2",),
            profile=CLAUDE.key,
            session=claude_sess,
            generation=1,
            budget_spent=10.0,
            budget_ceiling=100.0,
        )
        after = _cp(
            completed=("art-1",),
            remaining=("art-2",),
            profile=CODEX.key,
            session=codex_sess,
            generation=1,
            budget_spent=10.0,
            budget_ceiling=100.0,
        )
        self.assertEqual(continuity_violations(before, after), [])


if __name__ == "__main__":
    unittest.main()
