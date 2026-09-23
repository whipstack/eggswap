"""Provider-neutral selection -- "which account, of ANY provider, should take
this unit of work".

WHY THIS MODULE EXISTS
-----------------------
Every prior selector in this estate baked "Claude" or "Codex" into the call
site, so a scheduler that ran out of one provider's capacity had no code path
to even consider the other. ``rank``/``select`` operate on ``Candidate``
sequences that already mix providers; provider is read off
``Profile.provider``, never assumed.

A Claude 5h-window percentage and a Codex bucket percentage are NOT the same
unit -- they come from different vendors with different windows and different
meaning at the same numeric value. This module does not pretend otherwise: it
ranks WITHIN a provider by headroom (``Candidate.score``, higher score means
more headroom -- the score is computed upstream by whoever built the
Candidate, this module does not re-derive it from quota windows). ACROSS
providers it falls back to the order the caller listed in
``Policy.allow_providers``. That fallback is a POLICY CHOICE the caller made,
not a measurement, and is documented as such rather than hidden behind an
average of incomparable units.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional, Sequence

from eggswap.core.types import (
    Available,
    Candidate,
    NoCapacity,
    Provider,
)

__all__ = ["Policy", "rank", "select"]


@dataclass(frozen=True)
class Policy:
    """Caller-stated selection preferences. Never widened by a child on its own."""

    allow_providers: tuple[Provider, ...] = (Provider.CLAUDE, Provider.CODEX)
    #: API-key profiles are OFF by default.
    #: Both this AND api_key_budget must be set; each alone is insufficient.
    allow_api_key: bool = False
    api_key_budget: Optional[float] = None
    max_age_seconds: float = 300.0
    #: "most_headroom" | "least_headroom" | "round_robin"
    prefer: str = "most_headroom"


def _api_key_eligible(profile, policy: Policy) -> bool:
    if not profile.is_api_key:
        return True
    return policy.allow_api_key and policy.api_key_budget is not None and policy.api_key_budget > 0


def _is_eligible(candidate: Candidate, policy: Policy, *, now: float) -> bool:
    profile = candidate.profile
    if profile.provider not in policy.allow_providers:
        return False
    if not profile.enabled:
        return False
    # Only a READ Available is schedulable. Unknown/Exhausted/AuthDead are
    # never selected, no matter how good their .score looks -- UNKNOWN_IS_NOT_ZERO.
    availability = candidate.availability
    if not isinstance(availability, Available):
        return False
    if not _api_key_eligible(profile, policy):
        return False
    age = now - availability.observed_at
    if age > policy.max_age_seconds:
        return False
    return True


def rank(candidates: Sequence[Candidate], policy: Policy, *, now: Optional[float] = None) -> list[Candidate]:
    """Eligible candidates, best first. Ineligible candidates are dropped, not scored.

    ``now`` is injectable so tests never depend on wall-clock time.
    """
    if policy.prefer not in ("most_headroom", "least_headroom", "round_robin"):
        raise ValueError(f"unknown policy.prefer: {policy.prefer!r}")

    resolved_now = time.time() if now is None else now
    eligible = [c for c in candidates if _is_eligible(c, policy, now=resolved_now)]

    provider_order = {provider: index for index, provider in enumerate(policy.allow_providers)}

    if policy.prefer == "most_headroom":
        headroom_key = lambda c: -c.score
    elif policy.prefer == "least_headroom":
        headroom_key = lambda c: c.score
    else:
        # No persisted state crosses calls (rank/select are pure functions of
        # their arguments), so round-robin degrades to a stable, documented
        # order -- Profile.key -- rather than faking rotation with a counter
        # nothing keeps between invocations.
        headroom_key = lambda c: 0.0

    eligible.sort(
        key=lambda c: (
            provider_order.get(c.profile.provider, len(provider_order)),
            headroom_key(c),
            tuple(-value for value in c.tie_breakers),
            c.profile.key,
        )
    )
    return eligible


def select(candidates: Sequence[Candidate], policy: Policy, *, now: Optional[float] = None) -> Candidate:
    """The single best candidate, or raise NoCapacity carrying every profile's
    actual availability so the caller can tell "all exhausted until 19:59"
    from "we could not read anything" -- different facts, different recoveries.
    """
    ranked = rank(candidates, policy, now=now)
    if ranked:
        return ranked[0]
    reasons = {c.profile.key: c.availability for c in candidates}
    raise NoCapacity(reasons)
