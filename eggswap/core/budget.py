"""Local spend accounting for a goal, kept separate from provider capacity.

WHY THIS MODULE EXISTS
-----------------------
Issue #1096 acceptance 4: "settlement decrements the root-goal budget and
prevents silent fallback. Real provider limits remain authoritative even when
the local estimate suggests capacity." This module owns exactly the first
half of that sentence -- a LOCAL spending cap on one goal -- and is built so
it structurally cannot answer the second half. Nothing here accepts or
returns an ``Availability``; a caller that wants to launch still has to ask
``eggswap.core.select`` what the provider actually reported. A ledger that
says "budget remains" is a statement about money already spent against an
approved goal, not a claim that any account is up.

This mirrors ``eggswap.core.types.UNKNOWN_IS_NOT_ZERO`` one layer up:
``ceiling=None`` means the local ceiling could not be established (no goal
budget was ever set), which is a DIFFERENT fact from "unlimited". Conflating
them would let a goal with no configured budget spend without limit --
exactly the silent-fallback failure #1096 names. So ``reserve`` against an
unknown ceiling is allowed (there is nothing to refuse against), but every
reservation records whether it was actually checked, via
``Reservation.ceiling_known``, so a caller can always tell "verified within
budget" from "no budget was known to check against".
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Mapping, Optional

__all__ = [
    "Reservation",
    "Ledger",
    "BudgetError",
    "BudgetExhausted",
    "reserve",
    "settle",
    "may_use_api_profile",
]


class BudgetError(RuntimeError):
    """Base class for budget failures."""


class BudgetExhausted(BudgetError):
    """Raised BEFORE launch -- a reservation would exceed the known ceiling.

    Carries goal_id, ceiling and the requested amount so a caller does not
    have to re-parse a message to decide what to do next.
    """

    def __init__(self, goal_id: str, ceiling: float, requested: float, *, spent: float):
        self.goal_id = goal_id
        self.ceiling = ceiling
        self.requested = requested
        self.spent = spent
        super().__init__(
            f"{goal_id}: requesting {requested} against spent={spent} would "
            f"exceed ceiling {ceiling}"
        )


@dataclass(frozen=True)
class Reservation:
    """One reserve()/settle() pair, kept so settlement can be idempotent.

    ``ceiling_known`` records whether this reservation was actually checked
    against a real ceiling (``Ledger.ceiling is not None`` at reserve time),
    never inferred later from the ledger's *current* ceiling -- the ledger a
    caller holds after settlement must not retroactively look "verified".
    """

    reservation_id: str
    profile_key: str
    amount: float
    ceiling_known: bool
    settled: bool = False
    actual: Optional[float] = None
    overrun: float = 0.0


@dataclass(frozen=True)
class Ledger:
    """Local spend accounting for one goal. Never mutated; every operation
    returns a new ``Ledger``.

    ``ceiling=None`` is UNKNOWN, not unlimited -- see module docstring.
    ``overrun_total`` accumulates every settle() where actual exceeded what
    was reserved, so a ledger that has absorbed overruns is visibly
    different from one that has not, and the next ceiling can be set from
    real data rather than from an estimate that quietly ate its own misses.
    """

    goal_id: str
    ceiling: Optional[float]
    spent: float = 0.0
    reservations: Mapping[str, Reservation] = field(default_factory=dict)
    overrun_total: float = 0.0

    @property
    def ceiling_known(self) -> bool:
        return self.ceiling is not None


def reserve(
    ledger: Ledger,
    amount: float,
    *,
    profile,
    reservation_id: Optional[str] = None,
) -> Ledger:
    """Reserve ``amount`` against ``ledger``, returning a NEW ledger.

    ``reservation_id`` defaults to a fresh uuid4 hex; a caller may pass one
    explicitly (tests do, for a deterministic id to hand to ``settle``).
    Raises ``BudgetExhausted`` BEFORE any launch when the ceiling is known
    and would be exceeded. Against an unknown ceiling (``None``) the
    reservation is allowed -- there is nothing to refuse against -- but is
    recorded with ``ceiling_known=False`` so the distinction survives.
    """
    if amount < 0:
        raise ValueError(f"reserve amount must be >= 0, got {amount!r}")

    new_spent = ledger.spent + amount
    if ledger.ceiling is not None and new_spent > ledger.ceiling:
        raise BudgetExhausted(ledger.goal_id, ledger.ceiling, amount, spent=ledger.spent)

    profile_key = getattr(profile, "key", None) or str(profile)
    rid = reservation_id if reservation_id is not None else uuid.uuid4().hex
    if rid in ledger.reservations:
        raise BudgetError(f"{ledger.goal_id}: reservation id {rid!r} already in use")

    res = Reservation(
        reservation_id=rid,
        profile_key=profile_key,
        amount=amount,
        ceiling_known=ledger.ceiling is not None,
    )
    new_reservations = dict(ledger.reservations)
    new_reservations[rid] = res
    return Ledger(
        goal_id=ledger.goal_id,
        ceiling=ledger.ceiling,
        spent=new_spent,
        reservations=new_reservations,
        overrun_total=ledger.overrun_total,
    )


def settle(ledger: Ledger, actual: float, *, reserved: str) -> Ledger:
    """Reconcile a reservation against what was actually spent.

    ``reserved`` is the ``reservation_id`` returned via the ``Ledger`` from
    ``reserve``. ``actual`` may exceed the reserved amount -- providers
    charge what they charge -- but the excess is RECORDED as an overrun
    (``Reservation.overrun`` and ``Ledger.overrun_total``), never silently
    absorbed into ``spent`` as if it had been predicted.

    Idempotent per reservation id: settling the same id twice returns the
    ledger produced by the FIRST settlement unchanged, so a retried
    settlement call cannot double-decrement the goal's budget.
    """
    if actual < 0:
        raise ValueError(f"settle actual must be >= 0, got {actual!r}")

    res = ledger.reservations.get(reserved)
    if res is None:
        raise BudgetError(f"{ledger.goal_id}: unknown reservation id {reserved!r}")
    if res.settled:
        return ledger

    overrun = max(0.0, actual - res.amount)
    new_spent = ledger.spent - res.amount + actual
    settled_res = Reservation(
        reservation_id=res.reservation_id,
        profile_key=res.profile_key,
        amount=res.amount,
        ceiling_known=res.ceiling_known,
        settled=True,
        actual=actual,
        overrun=overrun,
    )
    new_reservations = dict(ledger.reservations)
    new_reservations[reserved] = settled_res
    return Ledger(
        goal_id=ledger.goal_id,
        ceiling=ledger.ceiling,
        spent=new_spent,
        reservations=new_reservations,
        overrun_total=ledger.overrun_total + overrun,
    )


def may_use_api_profile(profile, policy) -> bool:
    """True only when the profile is not an API key, or the caller opted in
    with BOTH ``allow_api_key`` and a positive ``api_key_budget`` (#1037
    acceptance criterion 9 -- API-key profiles are OFF by default, and
    neither flag alone is sufficient).
    """
    if not profile.is_api_key:
        return True
    return bool(getattr(policy, "allow_api_key", False)) and (
        getattr(policy, "api_key_budget", None) is not None
        and policy.api_key_budget > 0
    )
