"""Claude account adapter -- a thin wrapper over the already-installed `cswap` CLI.

WHY THIS FILE IS THIN
----------------------
cswap already owns OAuth, the keychain and refresh-token rotation for Claude
accounts (see claude_swap/session.py:490). This module reads `cswap list
--json` and maps its fields onto the provider-neutral contract in
`eggswap/core/types.py`; it never touches a credential, never shells out to
anything that could mutate account state, and binds a launch through
`cswap run <account_id> -- <claude_args>` rather than re-deriving
CLAUDE_CONFIG_DIR itself.

THE relogin_required FIELD (not the human-readable string)
------------------------------------------------------------
`cswap list --json` distinguishes a dead refresh token with a typed field:
``"usageStatus": "relogin_required"`` (accounts pair with ``"usage": null``
and a separate ``lastGoodUsage`` block of frozen numbers). The human-readable
`cswap list` text additionally prints "re-login needed - refresh token dead"
for the same accounts, but that string is presentation, not data; classifying
off `usageStatus` is the only field the JSON schema actually commits to.
Measured on this machine 2026-09-23: accounts 2 and 3 are in this state, and
their `lastGoodUsage.sevenDay.pct` values (96.0 and 100.0) are exactly the
kind of frozen number the negative-cache incident (see types.py) warns about
-- they must never be read as current, which is why this adapter does not
look at `lastGoodUsage` at all once `usageStatus == "relogin_required"`.
"""
from __future__ import annotations

import json
import subprocess
import time
from datetime import datetime
from typing import Any, Callable, List, Optional

from eggswap.core.types import (
    Availability,
    Available,
    AuthDead,
    Exhausted,
    Profile,
    Provider,
    QuotaWindow,
    Unknown,
)

#: usageStatus values this adapter knows map to a live, readable usage block.
_OK_STATUS = "ok"
#: usageStatus value cswap uses for a dead refresh token -- see module docstring.
_RELOGIN_STATUS = "relogin_required"


def _parse_iso8601(value: Optional[str]) -> Optional[float]:
    """Parse cswap's ISO-8601 timestamps to epoch seconds, or None if unreadable.

    cswap emits two shapes for the same kind of field: `usageFetchedAt` uses a
    trailing "Z" (e.g. "2026-09-23T13:21:42Z"), while `resetsAt` uses an
    explicit "+00:00" offset. `datetime.fromisoformat` on Python 3.10 accepts
    the offset form but not "Z", so "Z" is normalized before parsing.
    """
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


class ClaudeCswapAdapter:
    """Reads Claude account state through `cswap` and reports it provider-neutrally.

    Never executes `claude` itself and never reads a credential; `runner` and
    `clock` are injected so tests never touch the network, a real account or
    the wall clock.
    """

    provider = Provider.CLAUDE

    def __init__(
        self,
        *,
        runner: Callable[..., Any] = subprocess.run,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._runner = runner
        self._clock = clock
        self._enumeration_error: Optional[str] = None

    def _list_accounts(self) -> Optional[List[dict]]:
        """Return cswap's `accounts` list, or None on any transport/parse failure.

        None (never []) on failure, so callers can tell "cswap said there are
        no accounts" from "we could not ask cswap" -- the latter must become
        Unknown, never an empty-but-successful read.
        """
        try:
            result = self._runner(
                ["cswap", "list", "--json"],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0:
            return None
        try:
            data = json.loads(result.stdout)
        except (json.JSONDecodeError, TypeError):
            return None
        accounts = data.get("accounts") if isinstance(data, dict) else None
        if not isinstance(accounts, list):
            return None
        return accounts

    def enumeration_status(self) -> Optional[str]:
        """Why the last profiles() call returned nothing, or None if it was fine.

        _list_accounts already distinguishes "cswap said there are no accounts"
        (an empty list) from "we could not ask cswap" (None) -- and profiles()
        used to collapse both to []. That threw the distinction away at the
        last possible step, and the result was that removing cswap from PATH
        made six accounts silently VANISH from `eggswap list` rather than
        appear as unreadable. Nothing unsafe followed -- an absent account is
        never selected -- but "there are 3 profiles" when there are 7 is the
        frozen-cache lie wearing a different hat: an absence rendered as a
        fact. Found by an acceptance check, not by a test, which is its own
        lesson about where these checks belong.
        """
        return self._enumeration_error

    def profiles(self) -> List[Profile]:
        accounts = self._list_accounts()
        if accounts is None:
            self._enumeration_error = (
                "could not ask cswap for the account list "
                "(not on PATH, failed, timed out, or unparseable output)"
            )
            return []
        self._enumeration_error = None
        if not accounts:
            return []
        result = []
        for account in accounts:
            number = account.get("number")
            if number is None:
                continue
            email = account.get("email", "")
            result.append(
                Profile(
                    provider=Provider.CLAUDE,
                    account_id=str(number),
                    label=email,
                    metadata={"email": email},
                )
            )
        return result

    def availability(
        self,
        profile: Profile,
        *,
        max_age_seconds: float = 300.0,
        model: Optional[str] = None,
    ) -> Availability:
        """Availability of one account, optionally FOR A SPECIFIC MODEL.

        cswap reports account-wide windows (fiveHour, sevenDay) plus per-model
        `scoped` buckets. Those are not interchangeable and a code review
        caught this adapter treating them as if they were: every scoped bucket
        was folded into one profile-wide verdict, so an account with
        Fable at 100% and healthy 5h/7d came back Exhausted -- and would have
        been refused for `--model Sonnet`, which it can serve perfectly well.
        The inverse was as bad: a high-but-not-exhausted bucket for an
        unrelated model depressed that account's ranking for every model.

        So the rule is: account-wide windows always bind. A scoped bucket
        binds only when it is the REQUESTED model's bucket. Every bucket is
        still reported in `windows` -- a human running `eggswap list` wants to
        see them -- but visibility and bindingness are different things, and
        conflating them is what caused the defect.

        ONE DELIBERATE DIVERGENCE, recorded because a parallel lane
        (fix/eggswap-release-ci-latest, ed00db90de) reached the same design
        independently and chose the opposite on this single case. With NO
        model requested, that lane lets every scoped bucket bind, "to keep the
        complete view for diagnostics". Here no scoped bucket binds.

        The reason is what Exhausted MEANS: it sets `.schedulable = False`, so
        it is a scheduling verdict, not a display. An account whose Fable
        bucket is walled can still serve Sonnet, and calling it unschedulable
        would refuse work it can do -- the same class of error as the defect
        above, merely pointed at the no-model case. The complete view is not
        lost either way, because every bucket is still in `windows` and
        `eggswap list` prints all of them.

        The cost of this choice, stated so nobody is surprised: `select()`
        with no model named can return an account whose unnamed model is
        walled. That is the caller's omission, and naming the model fixes it.
        """
        now = self._clock()
        accounts = self._list_accounts()
        if accounts is None:
            return Unknown(stale_since=now, reason="cswap list --json failed or was unparseable")

        account = next(
            (a for a in accounts if str(a.get("number")) == profile.account_id), None
        )
        if account is None:
            return Unknown(
                stale_since=now,
                reason=f"account {profile.account_id} not present in cswap list output",
            )

        status = account.get("usageStatus")
        if status == _RELOGIN_STATUS:
            return AuthDead(
                reason="cswap usageStatus=relogin_required",
                observed_at=now,
                needs_human_login=True,
            )

        usage = account.get("usage")
        fetched_at = _parse_iso8601(account.get("usageFetchedAt"))
        if status != _OK_STATUS or not isinstance(usage, dict) or fetched_at is None:
            return Unknown(
                stale_since=now,
                reason=f"unrecognized usageStatus={status!r} or missing/unparseable usage",
            )

        age_seconds = account.get("usageAgeSeconds")
        if isinstance(age_seconds, (int, float)) and age_seconds > max_age_seconds:
            return Unknown(stale_since=now - float(age_seconds), reason="stale usage")

        windows = []
        binding_windows = []
        exhausted_bucket = None
        exhausted_reset_at = None

        def _consider(bucket_name: str, bucket: Optional[dict], *, binding: bool) -> None:
            nonlocal exhausted_bucket, exhausted_reset_at
            if not isinstance(bucket, dict) or "pct" not in bucket:
                return
            pct = float(bucket["pct"])
            reset_at = _parse_iso8601(bucket.get("resetsAt"))
            windows.append(
                QuotaWindow(
                    bucket=bucket_name,
                    used_percent=min(max(pct, 0.0), 100.0),
                    window_seconds=None,
                    resets_at=reset_at,
                    observed_at=fetched_at,
                    model_scope=None if binding else bucket_name,
                )
            )
            if binding:
                binding_windows.append(windows[-1])
            if pct >= 100.0 and binding and exhausted_bucket is None:
                exhausted_bucket = bucket_name
                exhausted_reset_at = reset_at

        _consider("fiveHour", usage.get("fiveHour"), binding=True)
        _consider("sevenDay", usage.get("sevenDay"), binding=True)
        wanted = (model or "").strip().casefold()
        for scoped in usage.get("scoped") or []:
            name = scoped.get("name") if isinstance(scoped, dict) else None
            if not name:
                continue
            # Binding only when this IS the requested model's bucket. With no
            # model asked for, no scoped bucket binds: the account is usable
            # for whatever model the caller has not named yet.
            _consider(name, scoped, binding=bool(wanted) and name.strip().casefold() == wanted)

        if exhausted_bucket is not None:
            return Exhausted(
                reset_at=exhausted_reset_at, bucket=exhausted_bucket, observed_at=fetched_at
            )

        if not binding_windows:
            return Unknown(
                stale_since=fetched_at,
                reason="no account-wide or model-matching capacity bucket",
                windows=tuple(windows),
            )

        return Available(
            windows=tuple(windows), observed_at=fetched_at,
            binding_windows=tuple(binding_windows),
        )

    def launch_argv(self, profile: Profile, claude_args: List[str]) -> List[str]:
        return ["cswap", "run", profile.account_id, "--", *claude_args]
