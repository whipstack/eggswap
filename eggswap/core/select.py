"""Provider-neutral selection -- "which account, of ANY provider, should take
this unit of work" (Issue #773 part 3).

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

import os
import time
from dataclasses import dataclass
from typing import Optional, Sequence

from eggswap.core.types import (
    Available,
    Candidate,
    NoCapacity,
    Provider,
    Unknown,
)

__all__ = ["Policy", "rank", "select"]


@dataclass(frozen=True)
class Policy:
    """Caller-stated selection preferences. Never widened by a child on its own."""

    allow_providers: tuple[Provider, ...] = (Provider.CLAUDE, Provider.CODEX)
    #: API-key profiles are OFF by default (#1037 acceptance criterion 9).
    #: Both this AND api_key_budget must be set; each alone is insufficient.
    allow_api_key: bool = False
    api_key_budget: Optional[float] = None
    max_age_seconds: float = 300.0
    #: "most_headroom" | "least_headroom" | "round_robin"
    prefer: str = "most_headroom"
    #: A required task capability (e.g. "vision", "long-context"). None means
    #: no requirement -- every profile is eligible on this axis, unchanged
    #: from prior behaviour (Issue #1096 acceptance 1).
    #: Which cross-provider ordering to apply. The default reproduces today's
    #: behaviour exactly -- allow_providers order decides, and that is a policy
    #: CHOICE rather than a measurement, because a Claude 5h percentage and a
    #: Codex 7d bucket are different units. The alternatives live in
    #: eggswap/core/policy.py, which had zero callers until this wired it in.
    cross_provider: str = "provider_order"
    capability: Optional[str] = None
    #: A Profile.key the caller DEMANDS. Set, it overrides scoring entirely:
    #: that profile wins if eligible, or selection raises NoCapacity naming
    #: the pin -- it never silently falls back to a different account, because
    #: a pin that quietly degrades into a preference is worse than no pin at
    #: all (the caller believes it was honoured when it was not).
    pin: Optional[str] = None


def _api_key_eligible(profile, policy: Policy) -> bool:
    if not profile.is_api_key:
        return True
    return policy.allow_api_key and policy.api_key_budget is not None and policy.api_key_budget > 0


def _declared_capabilities(profile) -> set:
    """Profile.metadata["capabilities"], os.pathsep- or comma-separated.

    Both separators are accepted (documented choice, not a guess): callers
    that build metadata from an OS-style PATH-like list and callers that just
    write a plain comma list both work without a second code path. Matching
    is case-insensitive. A profile that declares nothing has an EMPTY set --
    absence is not permission, so it never matches any required capability.
    """
    raw = (profile.metadata or {}).get("capabilities", "")
    if not raw:
        return set()
    normalized = raw.replace(os.pathsep, ",")
    return {part.strip().lower() for part in normalized.split(",") if part.strip()}


def _meets_capability(candidate: Candidate, policy: Policy) -> bool:
    if policy.capability is None:
        return True
    return policy.capability.lower() in _declared_capabilities(candidate.profile)


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
    if not _meets_capability(candidate, policy):
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

    # Cross-provider ordering. PROVIDER_ORDER is the default and is literally
    # the old expression, so the untouched path stays byte-identical. Any other
    # strategy is imported lazily and, if it refuses (IncomparableWindows) or
    # is unknown, falls back to provider order rather than raising: an ordering
    # preference must never turn a schedulable pool into no answer at all.
    def _cross_key(candidate):
        index = provider_order.get(candidate.profile.provider, len(provider_order))
        wanted = (policy.cross_provider or "provider_order").strip().lower()
        if wanted == "provider_order":
            return (index,)
        try:
            from eggswap.core.policy import CrossProviderStrategy, cross_provider_key

            strategy = CrossProviderStrategy[wanted.upper()]
            # The provider index must NOT lead here. Leading with it was my
            # first attempt and it made every strategy identical to
            # PROVIDER_ORDER: Claude (index 0) beat Codex (index 1) whatever
            # the strategy said, so the strategy only ever broke ties WITHIN
            # one provider -- which is the one thing a CROSS-provider ordering
            # is not for. The index survives only as a final tie-break, so the
            # result stays deterministic when the strategy genuinely ties.
            return tuple(cross_provider_key(candidate, strategy, now=resolved_now)[:-1]) + (index,)
        except Exception:
            return (index,)

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
            _cross_key(c),
            headroom_key(c),
            # Secondary preferences, highest first, applied only after the
            # primary score ties -- never averaged into it.
            tuple(-value for value in (c.tie_breakers or ())),
            c.profile.key,
        )
    )
    return eligible


def select(candidates: Sequence[Candidate], policy: Policy, *, now: Optional[float] = None) -> Candidate:
    """The single best candidate, or raise NoCapacity carrying every profile's
    actual availability so the caller can tell "all exhausted until 19:59"
    from "we could not read anything" -- different facts, different recoveries.

    ``policy.pin`` short-circuits scoring entirely: the pinned profile wins if
    it is eligible (same eligibility gates as anything else -- provider
    allow-list, enabled, api-key gate, capability, freshness), and NoCapacity
    is raised naming that exact pin otherwise. A pin is never treated as a
    mere preference that falls back to a different account (Issue #1096).
    """
    resolved_now = time.time() if now is None else now
    if policy.pin is not None:
        pinned = next((c for c in candidates if c.profile.key == policy.pin), None)
        if pinned is None:
            # Distinct from "nothing was available": the pin does not even
            # name a candidate in this call, which is a different fact for
            # whoever is debugging than "it was there but exhausted".
            raise NoCapacity({
                f"pin:{policy.pin}:not_found": Unknown(
                    stale_since=resolved_now,
                    reason=f"pin {policy.pin!r} not found among candidate profiles",
                )
            })
        if not _is_eligible(pinned, policy, now=resolved_now):
            raise NoCapacity({pinned.profile.key: pinned.availability})
        return pinned

    ranked = rank(candidates, policy, now=now)
    if ranked:
        return ranked[0]
    reasons = {c.profile.key: c.availability for c in candidates}
    raise NoCapacity(reasons)
