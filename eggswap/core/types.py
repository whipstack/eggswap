"""eggswap core contract -- what an account IS, provider-neutrally.

WHY THIS MODULE EXISTS
----------------------
Two measured incidents in this estate define every honesty rule below.

1. A frozen negative cache.
   A dead account's quota percentages froze at their last good values and kept
   rendering as if fresh. Four workers were dispatched onto an account that had
   been dead for hours and died on ``authentication_failed``. The lesson is not
   "cache less". It is that a NUMBER and a NUMBER'S AGE are one indivisible
   fact: a percentage without its observation time is not data, and a read that
   FAILED is not a read that returned zero.

2. Shared-credential corruption. Two Claude CLIs on one
   HOME concurrently rotate a single-use refresh token and destroy it
   (``invalid_grant`` / "Not logged in"). So holding an account is an
   EXCLUSIVE act with a fence, not a hint.

Hence the three type-level commitments here:

* ``Availability`` is a sealed union in which UNKNOWN is a FIRST-CLASS state
  carrying ``stale_since``. There is no code path that turns an unreadable
  account into an available one or into a zero.
* ``QuotaWindow`` cannot be constructed without ``observed_at``, and
  ``Observed`` vs ``Estimated`` is recorded, not inferred.
* ``Lease`` carries a fence generation, so a stale holder that wakes up late
  cannot settle over its replacement.

NON-GOALS, deliberately. This module never touches a credential. Claude
account binding is delegated to ``cswap run`` (which sets CLAUDE_CONFIG_DIR;
see claude_swap/session.py:490) and Codex binding to its own adapter. eggswap
does not reimplement OAuth, a keychain, or a token refresh, and no value in
this module is ever a secret: an account is named by an OPAQUE id.
"""
from __future__ import annotations

import enum
import time
import uuid
from dataclasses import dataclass, field
from typing import Mapping, Optional, Sequence, Union

__all__ = [
    "Provider",
    "Source",
    "QuotaWindow",
    "Available",
    "Exhausted",
    "AuthDead",
    "Unknown",
    "Availability",
    "Profile",
    "Candidate",
    "Lease",
    "WorkSpec",
    "SelectionError",
    "NoCapacity",
    "LeaseError",
    "StaleFence",
    "UNKNOWN_IS_NOT_ZERO",
]

#: Stated once, so a reviewer can grep for the rule rather than re-derive it.
UNKNOWN_IS_NOT_ZERO = (
    "A quota that could not be read is UNKNOWN with an age. It is never 0%, "
    "never 100%, never 'probably fine'. A transport failure while asking about "
    "an account says nothing about that account."
)


class Provider(enum.Enum):
    """Which vendor an account belongs to.

    Provider is an ATTRIBUTE of a profile, never a branch baked into a call
   site. Consumers must be able to ask "which account, of
    any provider, should take this unit of work".
    """

    CLAUDE = "claude"
    CODEX = "codex"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


class Source(enum.Enum):
    """Where a number came from. Never inferred, always recorded."""

    #: Read from the provider in this observation.
    OBSERVED = "observed"
    #: Derived locally (e.g. counted our own spend since the last read).
    ESTIMATED = "estimated"


@dataclass(frozen=True)
class QuotaWindow:
    """One capacity bucket of one account, with its provenance attached.

    ``observed_at`` is REQUIRED. There is deliberately no default: a caller
    that does not know when a number was read does not get to construct one.
    """

    bucket: str
    used_percent: float
    window_seconds: Optional[int]
    resets_at: Optional[float]
    observed_at: float
    source: Source = Source.OBSERVED

    def __post_init__(self) -> None:
        if not self.bucket:
            raise ValueError("bucket id is required")
        if not (0.0 <= float(self.used_percent) <= 100.0):
            raise ValueError(
                f"used_percent out of range: {self.used_percent!r}; an "
                "unreadable bucket is Unknown, not an out-of-range number"
            )
        if self.observed_at <= 0:
            raise ValueError("observed_at is required and must be a real time")

    def age_seconds(self, *, now: Optional[float] = None) -> float:
        return max(0.0, (time.time() if now is None else now) - self.observed_at)

    def is_stale(self, max_age_seconds: float, *, now: Optional[float] = None) -> bool:
        return self.age_seconds(now=now) > max_age_seconds

    def render(self, max_age_seconds: float, *, now: Optional[float] = None) -> str:
        """Human rendering that CANNOT show a stale number as a fresh one.

        This encodes the negative-cache lesson: rendering a frozen percentage
        as fresh is a failing test.
        """
        age = self.age_seconds(now=now)
        if self.is_stale(max_age_seconds, now=now):
            return f"{self.bucket}: UNKNOWN (last read {age:.0f}s ago, stale)"
        suffix = "" if self.source is Source.OBSERVED else " (estimated)"
        return f"{self.bucket}: {self.used_percent:.0f}% used, {age:.0f}s ago{suffix}"


@dataclass(frozen=True)
class Available:
    """Capacity was READ and there is room."""

    windows: Sequence[QuotaWindow] = ()
    observed_at: float = 0.0

    @property
    def schedulable(self) -> bool:
        return True


@dataclass(frozen=True)
class Exhausted:
    """Capacity was READ and there is none. ``reset_at`` may still be unknown."""

    reset_at: Optional[float] = None
    bucket: Optional[str] = None
    observed_at: float = 0.0

    @property
    def schedulable(self) -> bool:
        return False


@dataclass(frozen=True)
class AuthDead:
    """The credential is gone and only the human can restore it.

    Distinct from Exhausted on purpose: exhaustion heals with time, a revoked
    refresh token heals only through the vendor's own login flow. The
    scheduler must not promise recovery it cannot perform.
    """

    reason: str = ""
    observed_at: float = 0.0
    #: True when the fix is a human re-login; nothing we retry will help.
    needs_human_login: bool = True

    @property
    def schedulable(self) -> bool:
        return False


@dataclass(frozen=True)
class Unknown:
    """The read did not happen or did not answer. NOT zero, NOT unlimited.

    ``stale_since`` is the last time this profile's state was actually known,
    or the time of the failed probe. It is required precisely so that no
    consumer can print an unknown as a fresh fact.
    """

    stale_since: float
    reason: str = ""

    def __post_init__(self) -> None:
        if self.stale_since <= 0:
            raise ValueError(
                "Unknown requires stale_since: " + UNKNOWN_IS_NOT_ZERO
            )

    def age_seconds(self, *, now: Optional[float] = None) -> float:
        return max(0.0, (time.time() if now is None else now) - self.stale_since)

    @property
    def schedulable(self) -> bool:
        # An account whose capacity cannot be read is not the same as an
        # account with capacity. Conflating them schedules work onto a wall
        # The account cannot be scheduled without a fresh capacity reading.
        return False


Availability = Union[Available, Exhausted, AuthDead, Unknown]


@dataclass(frozen=True)
class Profile:
    """One account, provider-neutral, named only by an opaque id.

    ``account_id`` is whatever the adapter uses to address the account through
    its own tool (for the Claude adapter, the cswap slot number or email). It
    is an ADDRESS, never a credential: nothing in eggswap ever carries a token.
    """

    provider: Provider
    account_id: str
    label: str = ""
    #: API-key profiles are OFF by default and never auto-selected without an
    #: explicit opt-in AND a budget.
    is_api_key: bool = False
    enabled: bool = True
    metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.account_id:
            raise ValueError("account_id is required")

    @property
    def key(self) -> str:
        return f"{self.provider.value}:{self.account_id}"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.key


@dataclass(frozen=True)
class Candidate:
    """A selection RESULT. Selecting does not reserve anything.

    Kept separate from Lease so the gap between "this looked best" and "this is
    mine" stays visible in the type system: two planners can produce the same
    Candidate, and exactly one of them can turn it into a Lease.
    """

    profile: Profile
    availability: Availability
    score: float = 0.0
    rationale: str = ""
    #: Optional secondary scores, ordered from most to least important.
    #: Higher values win after `score` ties; provider-neutral callers can
    #: preserve a documented tie-break without folding unlike measurements
    #: into one fabricated quota number.
    tie_breakers: tuple[float, ...] = ()


@dataclass(frozen=True)
class WorkSpec:
    """What a lease is being taken FOR, bound to its goal lineage."""

    goal_id: str = ""
    contract_version: str = ""
    spec_hash: str = ""
    model: str = ""
    description: str = ""


@dataclass(frozen=True)
class Lease:
    """Exclusive hold on one profile, fenced.

    ``fence`` is a monotonically increasing generation per profile. A holder
    that wakes up after its lease expired and was re-granted carries an older
    fence and MUST be refused at settle time, or it could release its
    replacement's hold after waking late.
    """

    lease_id: str
    profile: Profile
    fence: int
    acquired_at: float
    expires_at: float
    workspec: WorkSpec = field(default_factory=WorkSpec)
    holder: str = ""

    @staticmethod
    def new_id() -> str:
        return uuid.uuid4().hex

    def is_expired(self, *, now: Optional[float] = None) -> bool:
        return (time.time() if now is None else now) >= self.expires_at

    def remaining_seconds(self, *, now: Optional[float] = None) -> float:
        return max(0.0, self.expires_at - (time.time() if now is None else now))


class SelectionError(RuntimeError):
    """Base class for selection failures."""


class NoCapacity(SelectionError):
    """No profile is schedulable. The caller QUEUES; it does not spawn.

    Carries the per-profile reason so the caller can tell "everything is
    exhausted until 19:59" from "we could not read anything", which are
    different facts with different recoveries.
    """

    def __init__(self, reasons: Mapping[str, Availability]):
        self.reasons = dict(reasons)
        super().__init__(
            "no schedulable profile: "
            + ", ".join(f"{k}={type(v).__name__}" for k, v in sorted(self.reasons.items()))
        )


class LeaseError(RuntimeError):
    """Base class for lease failures."""


class StaleFence(LeaseError):
    """A late holder tried to act with a superseded generation."""

    def __init__(self, profile_key: str, presented: int, current: int):
        self.profile_key = profile_key
        self.presented = presented
        self.current = current
        super().__init__(
            f"{profile_key}: fence {presented} is stale, current is {current}"
        )
