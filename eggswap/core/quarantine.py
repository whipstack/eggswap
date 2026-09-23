"""Memory of which ROUTE just failed, and why -- so selection re-selects
instead of replaying the same broken route.

THE OBSERVED FAILURE. A session repeatedly relaunched the identical
``cswap run <account> ... --resume`` route after that exact route had already
failed, with no new registered PID, while a replacement PID was logged as
VERIFIED and the receiver reported idle. Nothing in the estate remembered
"this route just failed" between attempts, so the same dead route was handed
back out on the very next selection. eggswap's account plane
(``eggswap/core/select.py``) already knows which profiles exist and what
their capacity looks like; it has no memory of recent FAILURE, which is a
different fact from current capacity -- a profile can be ``Available`` and
still be the profile that just failed to launch three times in a row.

A PID rebound is not a verification. Re-selection, not replay of persisted
argv, is the only correct response to a failed route: this module is the
primitive that lets a selector ask "did this profile just fail, and for how
long should I leave it alone" before handing it out again.

Quarantine is per PROFILE for scheduling purposes (``is_quarantined``,
``until``, ``reason``, ``filter``), but the exponential backoff that decides
HOW LONG is tracked per ``(profile_key, kind)`` pair on purpose: an account
that is alternately ``EXHAUSTED`` and ``LAUNCH_FAILED`` must not have those
two unrelated failure modes compound into one runaway backoff for either
kind. ``AUTH_DEAD`` is deliberately exempt from any timer -- the negative
cache incident documented in ``eggswap/core/types.py`` is exactly what
happens when a dead credential is allowed to look fresh again on a clock
alone, so it stays quarantined until a human fixes the login and something
calls ``clear()`` to say so explicitly.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

__all__ = ["Failure", "Quarantine", "KINDS"]

AUTH_DEAD = "AUTH_DEAD"
EXHAUSTED = "EXHAUSTED"
LAUNCH_FAILED = "LAUNCH_FAILED"
NO_RECEIPT = "NO_RECEIPT"
UNKNOWN = "UNKNOWN"

KINDS = (AUTH_DEAD, EXHAUSTED, LAUNCH_FAILED, NO_RECEIPT, UNKNOWN)

#: Sentinel meaning "quarantined until explicitly cleared" -- see AUTH_DEAD
#: handling below. Never compared as a real deadline; is_quarantined() short
#: circuits on it before any time arithmetic happens.
INDEFINITE = float("inf")


@dataclass(frozen=True)
class Failure:
    profile_key: str
    kind: str
    at: float
    detail: str = ""


class Quarantine:
    """Per-profile memory of the most recent failed route.

    All timing is driven by the injected ``clock`` -- never ``time.time()``
    read internally at check-time -- so tests can move time forward without
    a real sleep.
    """

    def __init__(
        self,
        *,
        clock=time.time,
        base_backoff: float = 30.0,
        max_backoff: float = 3600.0,
    ) -> None:
        self._clock = clock
        self._base_backoff = base_backoff
        self._max_backoff = max_backoff
        self._failures: Dict[str, Failure] = {}
        self._until: Dict[str, float] = {}
        #: Escalation count per (profile_key, kind), independent of every
        #: other kind on the same profile -- the "do not compound" rule.
        self._counts: Dict[Tuple[str, str], int] = {}

    def record(
        self,
        profile_key: str,
        kind: str,
        detail: str = "",
        *,
        reset_at: Optional[float] = None,
    ) -> None:
        now = self._clock()
        count_key = (profile_key, kind)
        count = self._counts.get(count_key, 0) + 1
        self._counts[count_key] = count

        self._failures[profile_key] = Failure(
            profile_key=profile_key, kind=kind, at=now, detail=detail
        )

        if kind == AUTH_DEAD:
            self._until[profile_key] = INDEFINITE
            return

        backoff = min(self._base_backoff * (2 ** (count - 1)), self._max_backoff)
        if kind == EXHAUSTED and reset_at is not None:
            # The provider TOLD us when capacity returns; that is strictly
            # better information than a guessed backoff, and the quarantine
            # must never outlast it.
            self._until[profile_key] = reset_at
        else:
            self._until[profile_key] = now + backoff

    def is_quarantined(self, profile_key: str) -> bool:
        until = self._until.get(profile_key)
        if until is None:
            return False
        if until == INDEFINITE:
            return True
        return self._clock() < until

    def until(self, profile_key: str) -> Optional[float]:
        if not self.is_quarantined(profile_key):
            return None
        return self._until[profile_key]

    def reason(self, profile_key: str) -> Optional[Failure]:
        if not self.is_quarantined(profile_key):
            return None
        return self._failures.get(profile_key)

    def clear(self, profile_key: str) -> None:
        self._failures.pop(profile_key, None)
        self._until.pop(profile_key, None)
        for count_key in [k for k in self._counts if k[0] == profile_key]:
            del self._counts[count_key]

    def filter(self, candidates: Sequence) -> List:
        return [c for c in candidates if not self.is_quarantined(c.profile.key)]
