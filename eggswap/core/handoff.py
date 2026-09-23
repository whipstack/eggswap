"""Cross-provider handoff across fenced account sessions.

WHY THIS MODULE EXISTS
-----------------------
Criterion 5 names its own trap: "Naming the goal in the first turn is
diagnostic, not sufficient acceptance." A handoff whose human-readable fields
(goal_id, description, from_profile) all say the right thing can still have
silently dropped a verified artifact or grown its remaining work -- and a
check that only reads those fields would call that a pass. So this module
keeps two things separate: ``checkpoint``/``resume`` are the MECHANISM that
carries a WorkSpec and its ledger of completed/remaining work across a lease
boundary; ``continuity_violations`` is an INDEPENDENT auditor that compares
two Checkpoints structurally and does not trust either one's narrative.

``Checkpoint.fence`` exists because resuming must refuse a superseded lease.
It needs something to compare against: the fence recorded when this checkpoint
was cut. A ``resume()`` presented with an older fence for the SAME profile is
exactly the late-holder-wakes-up case StaleFence exists to catch.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Iterable, List

from eggswap.core.types import Lease, StaleFence, WorkSpec


@dataclass(frozen=True)
class Checkpoint:
    """A handoff point: what a lease's work has proven and what is left.

    ``completed`` holds only VERIFIED artifact ids/hashes -- not "attempted",
    not "the model said so". ``remaining`` is authorized work not yet done;
    a handoff may shrink it, never grow it (a new child gets a bounded
    attempt, not a new root budget -- AGENTS.md section 3).
    """

    workspec: WorkSpec
    completed: tuple[str, ...]
    remaining: tuple[str, ...]
    from_profile: str
    created_at: float
    #: Fence of the lease this checkpoint was cut from; enables resume() to
    #: detect a superseded (late/stale) lease for the same profile.
    fence: int = 0


def checkpoint(
    lease: Lease,
    *,
    completed: Iterable[str],
    remaining: Iterable[str],
    clock=time.time,
) -> Checkpoint:
    """Cut a checkpoint from the lease currently doing the work."""
    return Checkpoint(
        workspec=lease.workspec,
        completed=tuple(completed),
        remaining=tuple(remaining),
        from_profile=lease.profile.key,
        created_at=clock(),
        fence=lease.fence,
    )


def resume(cp: Checkpoint, lease: Lease) -> Checkpoint:
    """Bind a checkpoint to a NEW lease, preserving its goal/ledger exactly.

    The WorkSpec, completed and remaining tuples are carried forward
    UNCHANGED -- resume() only rebinds ``from_profile``/``fence`` to the new
    lease. This is what makes the primary path structurally unable to rebase
    the work onto a different contract; ``continuity_violations`` remains as
    the independent check for checkpoints built any other way.
    """
    if lease.profile.key == cp.from_profile and lease.fence < cp.fence:
        raise StaleFence(lease.profile.key, lease.fence, cp.fence)
    return Checkpoint(
        workspec=cp.workspec,
        completed=cp.completed,
        remaining=cp.remaining,
        from_profile=lease.profile.key,
        created_at=lease.acquired_at,
        fence=lease.fence,
    )


def continuity_violations(before: Checkpoint, after: Checkpoint) -> List[str]:
    """Structural audit of a handoff. Returns [] only for a genuine one.

    Deliberately reads none of the narrative/label fields (from_profile,
    description) -- those can all say the right thing while the ledger
    underneath is broken. See the module docstring: naming the goal is
    diagnostic, not sufficient.
    """
    violations: List[str] = []

    if before.workspec.goal_id != after.workspec.goal_id:
        violations.append(
            f"goal_id changed: {before.workspec.goal_id!r} -> {after.workspec.goal_id!r}"
        )
    if before.workspec.contract_version != after.workspec.contract_version:
        violations.append(
            "contract_version changed: "
            f"{before.workspec.contract_version!r} -> {after.workspec.contract_version!r}"
        )
    if before.workspec.spec_hash != after.workspec.spec_hash:
        violations.append(
            f"spec_hash changed: {before.workspec.spec_hash!r} -> {after.workspec.spec_hash!r}"
        )

    after_completed = set(after.completed)
    for artifact in before.completed:
        if artifact not in after_completed:
            violations.append(
                f"verified artifact dropped: {artifact!r} was completed before "
                "the handoff but is absent after it"
            )

    before_completed = set(before.completed)
    before_remaining = set(before.remaining)
    for item in after.remaining:
        if item in before_completed:
            violations.append(
                f"completed work re-added to remaining: {item!r} was already "
                "verified before the handoff"
            )
        elif item not in before_remaining:
            violations.append(
                f"remaining work grew: {item!r} was not authorized before "
                "the handoff"
            )

    return violations
