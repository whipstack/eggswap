"""Cross-provider handoff -- #1037 acceptance criterion 5.

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

``Checkpoint.fence`` (beyond the four narrative fields the issue names) exists
because "resume must refuse a superseded lease" (#581's lesson, replayed here)
needs something to compare against: the fence recorded when this checkpoint
was cut. A ``resume()`` presented with an older fence for the SAME profile is
exactly the late-holder-wakes-up case StaleFence exists to catch.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Iterable, List, Optional

from eggswap.core.types import Lease, Provider, StaleFence, WorkSpec


@dataclass(frozen=True)
class SessionRef:
    """A NATIVE provider session/thread id, never comparable across vendors.

    Issue #1096: Claude session uuids and Codex thread ids share a shape
    (both are opaque strings) but mean nothing to each other -- a Claude
    session cannot be resumed by handing its uuid to Codex. ``native_id``
    equality alone is therefore never sufficient; ``provider`` must also
    match, which is why equality/``same_session_as`` checks it FIRST.
    """

    provider: Provider
    native_id: str

    def same_session_as(self, other: "SessionRef") -> bool:
        if not isinstance(other, SessionRef):
            return False
        return self.provider == other.provider and self.native_id == other.native_id


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
    #: Which NATIVE session this checkpoint's work ran under, if any. A
    #: cross-provider handoff must mint a NEW SessionRef (#1096); it may not
    #: carry the source provider's session forward unchanged.
    session: Optional[SessionRef] = None
    #: Monotonic per-lineage counter. 0 means "not yet using generation
    #: tracking" (the value every pre-#1096 checkpoint has by construction),
    #: so continuity_violations only audits generation ordering once a
    #: caller has actually started stamping non-zero generations.
    generation: int = 0
    #: Cumulative spend charged to this lineage's budget so far.
    budget_spent: float = 0.0
    #: Cap on budget_spent, if the goal set one.
    budget_ceiling: Optional[float] = None


def checkpoint(
    lease: Lease,
    *,
    completed: Iterable[str],
    remaining: Iterable[str],
    clock=time.time,
    session: Optional[SessionRef] = None,
    generation: int = 0,
    budget_spent: float = 0.0,
    budget_ceiling: Optional[float] = None,
) -> Checkpoint:
    """Cut a checkpoint from the lease currently doing the work."""
    return Checkpoint(
        workspec=lease.workspec,
        completed=tuple(completed),
        remaining=tuple(remaining),
        from_profile=lease.profile.key,
        created_at=clock(),
        fence=lease.fence,
        session=session,
        generation=generation,
        budget_spent=budget_spent,
        budget_ceiling=budget_ceiling,
    )


def resume(cp: Checkpoint, lease: Lease, *, session: Optional[SessionRef] = None) -> Checkpoint:
    """Bind a checkpoint to a NEW lease, preserving its goal/ledger exactly.

    The WorkSpec, completed and remaining tuples are carried forward
    UNCHANGED -- resume() only rebinds ``from_profile``/``fence`` to the new
    lease. This is what makes the primary path structurally unable to rebase
    the work onto a different contract; ``continuity_violations`` remains as
    the independent check for checkpoints built any other way.

    ``session`` lets a cross-provider caller supply the NEW native session it
    started for the target provider; when omitted, the prior session carries
    forward as-is (the correct behavior for a same-provider resume).
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
        session=cp.session if session is None else session,
        generation=cp.generation,
        budget_spent=cp.budget_spent,
        budget_ceiling=cp.budget_ceiling,
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

    before_provider = before.from_profile.split(":", 1)[0]
    after_provider = after.from_profile.split(":", 1)[0]
    if (
        before_provider != after_provider
        and before.session is not None
        and after.session is not None
        and after.session.same_session_as(before.session)
    ):
        violations.append(
            "cross-provider handoff carried the source SessionRef unchanged: "
            f"{before.session!r} reused for a {after_provider} handoff instead "
            "of a new native session"
        )

    if after.generation < before.generation:
        violations.append(
            f"generation went backwards: {before.generation!r} -> {after.generation!r}"
        )
    elif (
        (before.generation or after.generation)
        and after.generation == before.generation
        and after.completed != before.completed
    ):
        violations.append(
            f"generation {after.generation!r} repeated while completed work "
            f"advanced ({before.completed!r} -> {after.completed!r}); a "
            "replayed generation is a stale worker committing after its "
            "replacement"
        )

    if after.budget_spent < before.budget_spent:
        violations.append(
            f"budget_spent decreased: {before.budget_spent!r} -> {after.budget_spent!r}"
        )
    if after.budget_ceiling is not None and after.budget_spent > after.budget_ceiling:
        violations.append(
            f"budget_spent {after.budget_spent!r} exceeds budget_ceiling "
            f"{after.budget_ceiling!r}"
        )

    return violations
