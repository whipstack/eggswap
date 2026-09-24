"""Record WHY a provider was selected -- Issue #1096 acceptance criterion 1.

WHY THIS MODULE EXISTS
-----------------------
``select()`` returns a winning ``Candidate`` and throws every other candidate's
fate away. A scheduler that cannot say why it picked one account over another
cannot be audited after a bad choice, and "it had the best score" is not a
reason when the score itself was produced by a policy decision (allow list,
max age, api-key gate) upstream of the ranking.

This module does not re-rank anything. It calls ``eggswap.core.select.rank``
-- the one selection algorithm -- and narrates its result: which candidates
were even considered, which of those were ineligible and why, which one won,
and which policy inputs actually decided the outcome. ``NoCapacity`` becomes
data (``chosen=None`` with every refusal recorded) rather than an exception,
because a refusal is the most interesting case to audit, not an error to
unwind past.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Mapping, Optional

from eggswap.core.select import Policy, _api_key_eligible, rank
from eggswap.core.types import (
    Available,
    AuthDead,
    Candidate,
    Exhausted,
    Unknown,
)

__all__ = ["Refusal", "SelectionDecision", "decide"]


@dataclass(frozen=True)
class Refusal:
    """One candidate that could not win, and the fact that says why.

    ``reason`` names the ``Availability`` subtype, not a generic verdict --
    "not eligible" tells an auditor nothing they didn't already know.
    """

    profile_key: str
    reason: str


@dataclass(frozen=True)
class SelectionDecision:
    """The audit record of one ``decide()`` call.

    ``considered`` and ``refused`` must jointly account for every candidate
    handed in: a profile that vanishes from both is the bug this record
    exists to prevent.
    """

    chosen: Optional[Candidate]
    policy_summary: Mapping[str, str]
    considered: tuple[str, ...]
    refused: tuple[Refusal, ...]
    rationale: str
    decided_at: float


def _refusal_reason(candidate: Candidate, policy: Policy, *, now: float) -> str:
    profile = candidate.profile
    availability = candidate.availability

    if profile.provider not in policy.allow_providers:
        return f"provider {profile.provider.value} not in allow_providers"
    if not profile.enabled:
        return "profile disabled"
    if isinstance(availability, Unknown):
        age = availability.age_seconds(now=now)
        suffix = f", {availability.reason}" if availability.reason else ""
        return f"Unknown(stale {age:.0f}s{suffix})"
    if isinstance(availability, Exhausted):
        return "Exhausted" + (
            f"(reset_at={availability.reset_at:.0f})" if availability.reset_at else ""
        )
    if isinstance(availability, AuthDead):
        return "AuthDead" + (f": {availability.reason}" if availability.reason else "")
    if isinstance(availability, Available):
        if not _api_key_eligible(profile, policy):
            return "api-key profile without opt-in+budget"
        age = now - availability.observed_at
        if age > policy.max_age_seconds:
            return f"Available but stale (age {age:.0f}s > max_age {policy.max_age_seconds:.0f}s)"
    # Unreachable if rank()'s eligibility check and this narration stay in
    # sync; kept as an honest fallback rather than a silent misattribution.
    return "not eligible"


def _rationale(chosen: Optional[Candidate], ranked: list, refused: tuple[Refusal, ...], policy: Policy) -> str:
    if chosen is None:
        if refused:
            return f"no candidate schedulable: {len(refused)} refused, first reason {refused[0].reason}"
        return "no candidates were considered"
    if len(ranked) == 1:
        return f"{chosen.profile.key} chosen as the sole eligible candidate"
    runner_up = ranked[1]
    if chosen.profile.provider != runner_up.profile.provider:
        return (
            f"{chosen.profile.key} chosen: provider {chosen.profile.provider.value} "
            f"precedes {runner_up.profile.provider.value} in allow_providers"
        )
    if chosen.score != runner_up.score:
        return f"{chosen.profile.key} chosen: {policy.prefer} score {chosen.score:g} beats {runner_up.score:g}"
    if chosen.tie_breakers != runner_up.tie_breakers:
        return f"{chosen.profile.key} chosen: tie-breaker preference over {runner_up.profile.key}"
    return f"{chosen.profile.key} chosen: deterministic key order among equal scores"


def decide(candidates, policy: Policy, *, now: Optional[float] = None) -> SelectionDecision:
    """Wrap ``rank()`` with the reasoning it discards.

    Calls ``rank`` exactly once -- the single selection algorithm -- and never
    re-derives eligibility on its own; the per-candidate reason strings are
    narration of that one result, not a second gate.
    """
    resolved_now = time.time() if now is None else now
    candidates = list(candidates)
    considered = tuple(c.profile.key for c in candidates)

    ranked = rank(candidates, policy, now=resolved_now)
    eligible_ids = {id(c) for c in ranked}
    chosen = ranked[0] if ranked else None

    refused = tuple(
        Refusal(profile_key=c.profile.key, reason=_refusal_reason(c, policy, now=resolved_now))
        for c in candidates
        if id(c) not in eligible_ids
    )

    policy_summary = {
        "allow_providers": ",".join(p.value for p in policy.allow_providers),
        "allow_api_key": str(policy.allow_api_key),
        "api_key_budget": "set" if policy.api_key_budget is not None else "unset",
        "max_age_seconds": f"{policy.max_age_seconds:g}",
        "prefer": policy.prefer,
    }

    return SelectionDecision(
        chosen=chosen,
        policy_summary=policy_summary,
        considered=considered,
        refused=refused,
        rationale=_rationale(chosen, ranked, refused, policy),
        decided_at=resolved_now,
    )
