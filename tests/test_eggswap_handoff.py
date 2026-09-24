"""Tests for eggswap.core.handoff -- #1037 acceptance criterion 5.

Criterion 5: "A cross-provider handoff preserves the exact goal/contract
version and verified artifact hashes, resumes only remaining authorized
work, and passes an independent continuity check. Naming the goal in the
first turn is diagnostic, not sufficient acceptance."

Each rule below has a test that FAILS if the rule is removed from
continuity_violations -- that is the property that makes this an audit
rather than a vibe check.
"""
from __future__ import annotations

import unittest

from eggswap.core.handoff import checkpoint, continuity_violations, resume
from eggswap.core.types import Lease, Profile, Provider, StaleFence, WorkSpec

CLAUDE = Profile(provider=Provider.CLAUDE, account_id="acct-1", label="c1")
CODEX = Profile(provider=Provider.CODEX, account_id="acct-2", label="x2")

WS = WorkSpec(
    goal_id="goal-1037",
    contract_version="2026-09-20-goal-delegation-v1",
    spec_hash="sha256:abc123",
    model="claude-sonnet-5",
    description="rotate accounts across providers",
)


def make_lease(profile, fence=1, workspec=WS, lease_id="lease-a"):
    return Lease(
        lease_id=lease_id,
        profile=profile,
        fence=fence,
        acquired_at=1000.0,
        expires_at=2000.0,
        workspec=workspec,
        holder="worker-1",
    )


class CheckpointResumeTests(unittest.TestCase):
    def test_checkpoint_captures_lease_workspec_profile_and_fence(self):
        lease = make_lease(CLAUDE, fence=3)
        cp = checkpoint(
            lease,
            completed=("art-1",),
            remaining=("art-2", "art-3"),
            clock=lambda: 1234.5,
        )
        self.assertEqual(cp.workspec, WS)
        self.assertEqual(cp.from_profile, CLAUDE.key)
        self.assertEqual(cp.fence, 3)
        self.assertEqual(cp.completed, ("art-1",))
        self.assertEqual(cp.remaining, ("art-2", "art-3"))
        self.assertEqual(cp.created_at, 1234.5)

    def test_resume_binds_to_new_lease_profile_and_fence_preserving_ledger(self):
        old_lease = make_lease(CLAUDE, fence=1)
        cp = checkpoint(old_lease, completed=("art-1",), remaining=("art-2",))
        new_lease = make_lease(CODEX, fence=1, lease_id="lease-b")

        after = resume(cp, new_lease)

        self.assertEqual(after.from_profile, CODEX.key)
        self.assertEqual(after.fence, 1)
        self.assertEqual(after.workspec, WS)
        self.assertEqual(after.completed, ("art-1",))
        self.assertEqual(after.remaining, ("art-2",))
        self.assertEqual(after.created_at, new_lease.acquired_at)
        self.assertEqual(continuity_violations(cp, after), [])

    def test_resume_refuses_a_superseded_fence_on_the_same_profile(self):
        fresh_lease = make_lease(CLAUDE, fence=5)
        cp = checkpoint(fresh_lease, completed=(), remaining=("art-1",))

        late_lease = make_lease(CLAUDE, fence=2, lease_id="lease-late")
        with self.assertRaises(StaleFence):
            resume(cp, late_lease)

    def test_resume_allows_equal_or_higher_fence_on_same_profile(self):
        lease1 = make_lease(CLAUDE, fence=5)
        cp = checkpoint(lease1, completed=(), remaining=("art-1",))
        lease2 = make_lease(CLAUDE, fence=5, lease_id="lease-same")
        after = resume(cp, lease2)
        self.assertEqual(after.fence, 5)


class ContinuityViolationsTests(unittest.TestCase):
    def _cp_raw(self, workspec, completed, remaining, profile):
        from eggswap.core.handoff import Checkpoint

        return Checkpoint(
            workspec=workspec,
            completed=tuple(completed),
            remaining=tuple(remaining),
            from_profile=profile,
            created_at=1.0,
            fence=1,
        )

    def test_empty_for_a_genuine_handoff(self):
        before = self._cp_raw(WS, ("art-1",), ("art-2", "art-3"), CLAUDE.key)
        after = self._cp_raw(WS, ("art-1", "art-2"), ("art-3",), CODEX.key)
        self.assertEqual(continuity_violations(before, after), [])

    def test_changed_goal_id_is_a_named_violation(self):
        before = self._cp_raw(WS, (), ("art-1",), CLAUDE.key)
        after_ws = WorkSpec(
            goal_id="a-different-goal",
            contract_version=WS.contract_version,
            spec_hash=WS.spec_hash,
        )
        after = self._cp_raw(after_ws, (), ("art-1",), CODEX.key)
        violations = continuity_violations(before, after)
        self.assertTrue(any("goal_id" in v for v in violations), violations)

    def test_changed_contract_version_is_a_named_violation(self):
        before = self._cp_raw(WS, (), ("art-1",), CLAUDE.key)
        after_ws = WorkSpec(
            goal_id=WS.goal_id,
            contract_version="some-other-version",
            spec_hash=WS.spec_hash,
        )
        after = self._cp_raw(after_ws, (), ("art-1",), CODEX.key)
        violations = continuity_violations(before, after)
        self.assertTrue(any("contract_version" in v for v in violations), violations)

    def test_changed_spec_hash_is_a_named_violation(self):
        before = self._cp_raw(WS, (), ("art-1",), CLAUDE.key)
        after_ws = WorkSpec(
            goal_id=WS.goal_id,
            contract_version=WS.contract_version,
            spec_hash="sha256:different",
        )
        after = self._cp_raw(after_ws, (), ("art-1",), CODEX.key)
        violations = continuity_violations(before, after)
        self.assertTrue(any("spec_hash" in v for v in violations), violations)

    def test_dropped_completed_artifact_is_a_violation(self):
        before = self._cp_raw(WS, ("art-1", "art-2"), (), CLAUDE.key)
        after = self._cp_raw(WS, ("art-1",), (), CODEX.key)
        violations = continuity_violations(before, after)
        self.assertTrue(any("art-2" in v for v in violations), violations)

    def test_new_item_appearing_in_remaining_is_a_violation(self):
        before = self._cp_raw(WS, (), ("art-1",), CLAUDE.key)
        after = self._cp_raw(WS, (), ("art-1", "art-99-never-authorized"), CODEX.key)
        violations = continuity_violations(before, after)
        self.assertTrue(
            any("art-99-never-authorized" in v for v in violations), violations
        )

    def test_redoing_completed_work_is_a_violation(self):
        before = self._cp_raw(WS, ("art-1",), (), CLAUDE.key)
        after = self._cp_raw(WS, ("art-1",), ("art-1",), CODEX.key)
        violations = continuity_violations(before, after)
        self.assertTrue(
            any("art-1" in v and "re-added" in v.lower() for v in violations), violations
        )

    def test_shrinking_remaining_by_completing_work_is_not_a_violation(self):
        before = self._cp_raw(WS, (), ("art-1", "art-2"), CLAUDE.key)
        after = self._cp_raw(WS, ("art-1",), ("art-2",), CODEX.key)
        self.assertEqual(continuity_violations(before, after), [])

    def test_naming_the_goal_correctly_does_not_excuse_a_silently_dropped_artifact(self):
        """#1037 criterion 5's own trap, encoded directly.

        Every human-readable field (goal_id, contract_version, spec_hash,
        description, from_profile) agrees between before/after -- a checker
        that only read those would call this a pass. The ledger says
        otherwise: 'art-verified-2' was completed before and is silently
        gone after. continuity_violations must catch it anyway.
        """
        before = self._cp_raw(
            WS, ("art-verified-1", "art-verified-2"), ("art-3",), CLAUDE.key
        )
        after = self._cp_raw(
            WS, ("art-verified-1",), ("art-3",), CLAUDE.key
        )
        self.assertEqual(before.workspec, after.workspec)
        self.assertEqual(before.from_profile, after.from_profile)

        violations = continuity_violations(before, after)
        self.assertTrue(violations, "dropped artifact must not pass silently")
        self.assertTrue(any("art-verified-2" in v for v in violations), violations)


if __name__ == "__main__":
    unittest.main()
