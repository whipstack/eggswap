"""Cross-provider ranking strategies -- named, explicit, and honest about units.

WHY THIS MODULE EXISTS
-----------------------
``eggswap.core.select`` ranks WITHIN a provider by headroom score and, ACROSS
providers, falls back to the order the caller listed in
``Policy.allow_providers``. That fallback is documented there as a POLICY
CHOICE, not a measurement -- a Claude 5h-window percentage and a Codex 7d
bucket percentage are different units, and averaging them would be a lie
(#773 slice 4, shipped PARTIAL).

This module makes the richer choices available WITHOUT hiding an
apples-to-oranges comparison behind a single number. Each
``CrossProviderStrategy`` states in its own docstring what it optimises and
what it structurally cannot know. None of them may compare two numbers whose
units differ without saying so.

This module does not itself change ``select.py``'s behaviour -- it is exposed
so a later, separate commit can adopt ``rank_cross_provider``/
``cross_provider_key`` there. ``select.py`` may be mid-edit elsewhere; this
file does not touch it.

NON-GOALS. Never touches a credential. Only consumes ``Candidate`` objects
whose ``availability`` is already ``Available`` -- callers are responsible for
filtering out ``Unknown``/``Exhausted``/``AuthDead`` upstream (see
``eggswap.core.select._is_eligible``); this module refuses to resurrect one if
handed one by mistake.
"""
from __future__ import annotations

import enum
from typing import Mapping, Optional, Sequence

from eggswap.core.types import Available, Candidate, Provider

__all__ = [
    "CrossProviderStrategy",
    "IncomparableWindows",
    "cross_provider_key",
    "rank_cross_provider",
]


class IncomparableWindows(ValueError):
    """Raised when MOST_ABSOLUTE_HEADROOM is asked to compare unlike units.

    A 5-hour Claude session window and a 7-day Codex weekly window are not
    the same measurement, even when both are expressed as "percent used".
    Rather than average them into a number that looks precise and means
    nothing, ranking refuses. The caller's documented recovery is to catch
    this and re-rank with ``CrossProviderStrategy.PROVIDER_ORDER``.
    """


class CrossProviderStrategy(enum.Enum):
    """How to break a tie BETWEEN candidates from different providers.

    Ranking within one provider is a headroom comparison (same vendor, same
    unit). Ranking across providers is a judgement call about incomparable
    things, so each strategy below says plainly what it optimises for and
    what it cannot see.
    """

    #: Today's behaviour: the order the caller listed in
    #: ``Policy.allow_providers`` decides, full stop. Optimises for whatever
    #: the operator said mattered most; knows nothing about actual headroom.
    #: The default, and the only strategy that never raises or degrades.
    PROVIDER_ORDER = "provider_order"

    #: Compares headroom (100 - used_percent) only across windows that share
    #: the same ``window_seconds`` -- e.g. two 5-hour windows. Optimises for
    #: "use whichever account has the most room left", but only where "room"
    #: means the same thing on both sides. Cannot rank a set that mixes
    #: window lengths; see ``IncomparableWindows``.
    MOST_ABSOLUTE_HEADROOM = "most_absolute_headroom"

    #: Prefers the candidate whose window resets furthest in the future.
    #: Optimises for runway visibility, not current headroom. Cannot know
    #: anything about a candidate with no ``resets_at`` at all -- such a
    #: candidate is ranked last, never treated as resetting soon or never.
    LONGEST_UNTIL_RESET = "longest_until_reset"

    #: Prefers the provider with fewer currently-held leases, to avoid
    #: piling every unit of work onto one vendor. Optimises for load
    #: spreading; knows nothing about headroom or reset time. Requires the
    #: caller to supply ``held_counts`` -- with none supplied it degrades to
    #: PROVIDER_ORDER rather than pretending every provider is unheld.
    SPREAD = "spread"


def _require_available(candidate: Candidate) -> Available:
    availability = candidate.availability
    if not isinstance(availability, Available):
        raise TypeError(
            f"{candidate.profile.key}: cross_provider_key only accepts Available "
            f"candidates, got {type(availability).__name__}; Unknown/Exhausted/"
            "AuthDead must be filtered out before ranking (UNKNOWN_IS_NOT_ZERO)"
        )
    return availability


def _binding_window_headroom(availability: Available, *, profile_key: str):
    """The (window_seconds, headroom_percent) of the window that actually BINDS.

    Binding means the window with the LEAST headroom, not the one with the
    shortest duration. The first version of this function used the shortest
    duration and a code review supplied the counterexample that kills it:

        account A: 5h = 0% used, 7d = 99% used
        account B: 5h = 50% used, 7d = 0% used

    A is one percentage point from its weekly wall. Collapsing it to its
    shortest bucket reported "5h, 100% headroom" and ranked it ABOVE B, i.e.
    the scheduler would pick the account about to hit a wall over the one with
    room everywhere. Shortest duration is not the same thing as binding
    capacity, and a helper that discards a 99%-used bucket cannot honestly
    call what remains "absolute headroom".

    Every decision-bearing window is preserved: the minimum is taken across
    all of them. The returned window_seconds is the BINDING window's duration,
    which keeps the caller's grouping honest -- comparing "1% of a weekly
    window left" against "50% of a five-hour window left" is still comparing
    unlike units, and the grouping is what refuses to do it.
    """
    numbered = [w for w in availability.windows if w.window_seconds is not None]
    if not numbered:
        raise IncomparableWindows(
            f"{profile_key}: no window carries a window_seconds, so its "
            "headroom cannot be compared to any other account's"
        )
    binding = min(numbered, key=lambda w: (100.0 - w.used_percent, w.window_seconds))
    return binding.window_seconds, 100.0 - binding.used_percent


def _longest_until_reset_key(candidate: Candidate):
    availability = _require_available(candidate)
    resets = [w.resets_at for w in availability.windows if w.resets_at is not None]
    if not resets:
        # No resets_at anywhere: ranked LAST, never mistaken for "resets now"
        # or "never resets" -- both would be inventing a fact we don't have.
        return (1, 0.0, candidate.profile.key)
    furthest = max(resets)
    return (0, -furthest, candidate.profile.key)


def _spread_key(candidate: Candidate, held_counts: Optional[Mapping[Provider, int]]):
    if not held_counts:
        # No counts supplied: treating every provider as "0 held" would be a
        # fabricated fact, not a measurement, so degrade to PROVIDER_ORDER.
        return (0.0, candidate.profile.key)
    return (held_counts.get(candidate.profile.provider, 0), candidate.profile.key)


def cross_provider_key(
    candidate: Candidate,
    strategy: CrossProviderStrategy,
    *,
    now: float,
    held_counts: Optional[Mapping[Provider, int]] = None,
) -> tuple:
    """Sort key for ONE candidate under ``strategy``. Ascending sort wins.

    ``now`` is accepted for interface symmetry with the rest of this estate
    (every ranking function here takes an injectable clock) even though no
    current strategy needs it; a future strategy (e.g. one that decays
    preference over time) can use it without changing the signature again.

    Every returned tuple ends in ``candidate.profile.key`` so two candidates
    that are otherwise tied still sort deterministically.
    """
    _require_available(candidate)
    if strategy is CrossProviderStrategy.PROVIDER_ORDER:
        # No opinion: the caller's own allow_providers order, applied before
        # this key, is what actually decides. This is a neutral tie.
        return (0.0, candidate.profile.key)
    if strategy is CrossProviderStrategy.MOST_ABSOLUTE_HEADROOM:
        window_seconds, headroom = _binding_window_headroom(
            candidate.availability, profile_key=candidate.profile.key
        )
        return (window_seconds, -headroom, candidate.profile.key)
    if strategy is CrossProviderStrategy.LONGEST_UNTIL_RESET:
        return _longest_until_reset_key(candidate)
    if strategy is CrossProviderStrategy.SPREAD:
        return _spread_key(candidate, held_counts)
    raise ValueError(f"unknown CrossProviderStrategy: {strategy!r}")


def rank_cross_provider(
    candidates: Sequence[Candidate],
    strategy: CrossProviderStrategy,
    *,
    now: float,
    held_counts: Optional[Mapping[Provider, int]] = None,
) -> list[Candidate]:
    """All ``candidates``, best first, under ``strategy``.

    For MOST_ABSOLUTE_HEADROOM this checks comparability across the WHOLE
    set, not just pairwise: if the candidates' binding windows do not all
    share one ``window_seconds``, ranking by headroom would silently compare
    a 5h percentage to a 7d percentage, so this raises ``IncomparableWindows``
    instead. Callers that must produce an order regardless should catch that
    and re-call with ``CrossProviderStrategy.PROVIDER_ORDER``.
    """
    if strategy is CrossProviderStrategy.MOST_ABSOLUTE_HEADROOM:
        keys = [
            (c, _binding_window_headroom(_require_available(c), profile_key=c.profile.key))
            for c in candidates
        ]
        window_lengths = {window_seconds for _, (window_seconds, _) in keys}
        if len(window_lengths) > 1:
            raise IncomparableWindows(
                "candidate set mixes window lengths "
                f"{sorted(window_lengths)!r}; MOST_ABSOLUTE_HEADROOM cannot "
                "compare them without averaging incomparable units"
            )
        return [
            c
            for c, _ in sorted(
                keys, key=lambda pair: (pair[1][0], -pair[1][1], pair[0].profile.key)
            )
        ]
    return sorted(
        candidates,
        key=lambda c: cross_provider_key(c, strategy, now=now, held_counts=held_counts),
    )
