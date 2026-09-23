"""Codex capacity reader -- a private `codex app-server` child over stdio.

WHY A NEW CHILD PROCESS, NOT THE OPERATOR'S DAEMON
----------------------------------------------------
docs/research/eggswap/codex-ratelimits-live.md measured the handshake by
spawning a bare `codex app-server` (no subcommand) as an independent,
stdio-scoped process -- never touching
``~/.codex/app-server-control/app-server-control.sock``, never the live
shared daemon those terminals use. This reader repeats exactly that: one
throwaway child per call, killed in a ``finally`` block, never the daemon.

WHY observed_at IS STAMPED HERE, NOT PARSED FROM THE REPLY
------------------------------------------------------------
Measured live (same doc, section 3): `account/rateLimits/read` carries no
capture-time field of its own -- every timestamp in the payload (resetsAt,
grantedAt, expiresAt) is a window/credit epoch, not a "this snapshot was
computed at T" marker. ``QuotaWindow.observed_at`` is a required constructor
argument for exactly this reason; this reader supplies it as the injected
clock's reading at the moment the id=2 reply line is fully parsed.

WHY EVERY FAILURE BECOMES Unknown, NEVER Available OR Exhausted
-------------------------------------------------------------------
eggswap/core/types.py UNKNOWN_IS_NOT_ZERO: a transport failure while asking
about an account says nothing about that account's real capacity. Spawn
failure, a bad handshake, a timeout, a non-JSON line and a malformed/error
reply are five distinct facts (kept as five distinct ``reason`` strings) but
one identical type-level outcome.

SIGNATURE NOTE re eggswap/adapters/codex_home.py
---------------------------------------------------
``CodexHomeAdapter`` takes ``rate_limit_reader: Optional[Callable[[Profile],
Any]]`` -- a single reader shared across every profile the adapter manages,
each of which can have its own CODEX_HOME. So ``codex_home`` given at this
reader's construction is only a fallback; per call,
``profile.metadata["codex_home"]`` (set by ``CodexHomeAdapter.profiles()``)
wins when present. This matches the sibling's signature as specified.
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from eggswap.core.types import (
    Availability,
    Available,
    Exhausted,
    Profile,
    QuotaWindow,
    Source,
    Unknown,
)

#: Identifies this reader to the app-server; harmless, never a secret.
_CLIENT_INFO = {"name": "eggswap", "title": "eggswap", "version": "0.0.1"}


class _Timeout(Exception):
    """No reply arrived within the reader's timeout budget."""


class _BadJSON(Exception):
    """A line from the app-server's stdout did not parse as JSON."""


class _HandshakeError(Exception):
    """The app-server's stdio protocol did not behave as measured."""


class _LineReader:
    """Pumps a stream's lines onto a queue on a background thread.

    A plain blocking ``readline()`` cannot be bounded by a timeout on its
    own; pairing it with ``queue.Queue.get(timeout=...)`` is what lets a
    hung or silent child be detected without the caller blocking forever.
    """

    def __init__(self, stream: Any) -> None:
        self._queue: "queue.Queue[tuple[str, Any]]" = queue.Queue()
        thread = threading.Thread(target=self._pump, args=(stream,), daemon=True)
        thread.start()

    def _pump(self, stream: Any) -> None:
        try:
            for line in iter(stream.readline, ""):
                self._queue.put(("line", line))
            self._queue.put(("eof", None))
        except Exception as exc:  # noqa: BLE001 - surfaced to the caller, not swallowed
            self._queue.put(("error", exc))

    def get_line(self, timeout_seconds: float) -> str:
        try:
            kind, payload = self._queue.get(timeout=max(0.0, timeout_seconds))
        except queue.Empty:
            raise _Timeout("timed out waiting for a line from the app-server")
        if kind == "eof":
            raise _HandshakeError("app-server closed stdout unexpectedly")
        if kind == "error":
            raise _HandshakeError(f"error reading app-server stdout: {payload}")
        return payload


class AppServerRateLimitReader:
    """Reads one Codex account's capacity via a private `codex app-server` child.

    `spawn` and `clock` are injected so tests never launch a real app-server,
    never touch the network, and never depend on wall-clock time.
    """

    def __init__(
        self,
        *,
        codex_home: Path,
        spawn: Callable[..., Any] = subprocess.Popen,
        clock: Callable[[], float] = time.time,
        timeout_seconds: float = 20.0,
    ) -> None:
        self._codex_home = codex_home
        self._spawn = spawn
        self._clock = clock
        self._timeout_seconds = timeout_seconds

    def __call__(self, profile: Profile) -> Availability:
        codex_home = (profile.metadata or {}).get("codex_home") or str(self._codex_home)

        proc = None
        try:
            env = dict(os.environ)
            env["CODEX_HOME"] = str(codex_home)
            try:
                proc = self._spawn(
                    ["codex", "app-server"],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    bufsize=1,
                    env=env,
                )
            except OSError as exc:
                return Unknown(stale_since=self._clock(), reason=f"spawn failed: {exc}")

            deadline = time.monotonic() + self._timeout_seconds
            reader = _LineReader(proc.stdout)
            try:
                self._write(proc, {"id": 1, "method": "initialize", "params": {"clientInfo": _CLIENT_INFO}})
                init_reply = self._await_reply(reader, expected_id=1, deadline=deadline)
                if "result" not in init_reply:
                    raise _HandshakeError(
                        f"initialize failed: {init_reply.get('error', init_reply)}"
                    )
                self._write(proc, {"method": "initialized", "params": {}})
                self._write(proc, {"id": 2, "method": "account/rateLimits/read", "params": {}})
                reply = self._await_reply(reader, expected_id=2, deadline=deadline)
            except _Timeout as exc:
                return Unknown(stale_since=self._clock(), reason=str(exc))
            except _BadJSON as exc:
                return Unknown(stale_since=self._clock(), reason=str(exc))
            except _HandshakeError as exc:
                return Unknown(stale_since=self._clock(), reason=str(exc))

            observed_at = self._clock()
            return self._to_availability(reply, observed_at=observed_at)
        finally:
            if proc is not None:
                self._kill(proc)

    @staticmethod
    def _write(proc: Any, obj: Any) -> None:
        try:
            proc.stdin.write(json.dumps(obj) + "\n")
            proc.stdin.flush()
        except Exception as exc:  # noqa: BLE001 - a write failure is a handshake failure
            raise _HandshakeError(f"failed writing to app-server stdin: {exc}") from exc

    def _await_reply(self, reader: _LineReader, *, expected_id: int, deadline: float) -> dict:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _Timeout(f"timed out waiting for id={expected_id} reply from app-server")
            line = reader.get_line(remaining).strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise _BadJSON(f"invalid JSON from app-server: {exc}") from exc
            if isinstance(obj, dict) and obj.get("id") == expected_id:
                return obj
            # A notification (no "id") or a reply to some other id -- keep waiting.

    @staticmethod
    def _to_availability(reply: dict, *, observed_at: float) -> Availability:
        if "error" in reply:
            return Unknown(stale_since=observed_at, reason=f"app-server error: {reply['error']}")
        result = reply.get("result")
        if not isinstance(result, dict):
            return Unknown(stale_since=observed_at, reason="missing result in account/rateLimits/read reply")
        rate_limits = result.get("rateLimits")
        if not isinstance(rate_limits, dict):
            return Unknown(stale_since=observed_at, reason="missing rateLimits in reply")
        limit_id = rate_limits.get("limitId") or "codex"

        primary = rate_limits.get("primary")
        if not isinstance(primary, dict):
            return Unknown(stale_since=observed_at, reason="missing primary window in rateLimits")
        try:
            windows = [
                AppServerRateLimitReader._window(f"{limit_id}:primary", primary, observed_at=observed_at)
            ]
        except (KeyError, TypeError, ValueError) as exc:
            return Unknown(stale_since=observed_at, reason=f"malformed primary window: {exc}")

        # `secondary` unpopulated means that account has one window, not a
        # zero-usage second bucket -- an absent bucket and a bucket at 0%
        # used are different facts (docs/research/eggswap/codex-ratelimits-live.md #5).
        secondary = rate_limits.get("secondary")
        if isinstance(secondary, dict):
            try:
                windows.append(
                    AppServerRateLimitReader._window(f"{limit_id}:secondary", secondary, observed_at=observed_at)
                )
            except (KeyError, TypeError, ValueError) as exc:
                return Unknown(stale_since=observed_at, reason=f"malformed secondary window: {exc}")

        # The App Server contract says rateLimitReachedType is populated when
        # the server has classified a reached limit. Its enum is not public,
        # so retain the provider's value only as evidence and associate it
        # with a bucket when it names one we observed. A 100% window is also
        # direct quota evidence; never leave either case schedulable.
        # NESTING MATTERS AND IT IS NOT UNIFORM. The live capture
        # (docs/research/eggswap/codex-ratelimits-live.md section 2) puts
        # ordinaryUsageAllowed at the TOP of `result`, but nests
        # spendControlReached and rateLimitReachedType INSIDE
        # `result.rateLimits`. Reading all three from `result` made two of
        # these three gates unable to fire against any real reply -- present
        # in the code, inert in production, which is the worst shape a guard
        # can take: it reads as a defence in review and defends nothing.
        # Both levels are consulted, rateLimits first, so a future top-level
        # move does not silently re-break it.
        def _gate(name):
            # Prefer a MEANINGFUL value from either level rather than the
            # first level that merely contains the key. The live reply carries
            # rateLimitReachedType: null inside rateLimits, so a presence
            # check there shadows a real value at the top of result -- the
            # gate would read None and pass. Absent and null are the same
            # "nothing said" here; a set value at either level is the signal.
            nested = rate_limits.get(name)
            if nested is not None:
                return nested
            return result.get(name)

        reached_type = _gate("rateLimitReachedType")
        if reached_type is not None and not isinstance(reached_type, str):
            return Unknown(stale_since=observed_at, reason="malformed rateLimitReachedType")
        ordinary_allowed = _gate("ordinaryUsageAllowed")
        if ordinary_allowed is not None and not isinstance(ordinary_allowed, bool):
            return Unknown(stale_since=observed_at, reason="malformed ordinaryUsageAllowed")
        spend_control = _gate("spendControlReached")
        if spend_control is not None and not isinstance(spend_control, bool):
            return Unknown(stale_since=observed_at, reason="malformed spendControlReached")

        exhausted_window = next((window for window in windows if window.used_percent >= 100.0), None)
        # The published contract does not define ordinaryUsageAllowed or
        # spendControlReached semantics. A negative/positive value there is
        # a measurement we cannot safely translate into quota exhaustion.
        reached = bool(reached_type)
        if exhausted_window is not None or reached:
            window = exhausted_window
            if reached_type:
                named = next((w for w in windows if w.bucket.endswith(f":{reached_type}")), None)
                if named is not None:
                    window = named
            return Exhausted(
                reset_at=window.resets_at if window is not None else None,
                bucket=window.bucket if window is not None else (str(reached_type) if reached_type else None),
                observed_at=observed_at,
            )

        if ordinary_allowed is False or spend_control is True:
            return Unknown(
                stale_since=observed_at,
                reason="app-server usage gate reported; exhaustion semantics are unverified",
            )

        return Available(windows=windows, observed_at=observed_at)

    @staticmethod
    def _window(bucket: str, payload: dict, *, observed_at: float) -> QuotaWindow:
        used_percent = float(payload["usedPercent"])
        window_minutes = payload["windowDurationMins"]
        resets_at = payload.get("resetsAt")
        return QuotaWindow(
            bucket=bucket,
            used_percent=used_percent,
            # Measured live: windowDurationMins is MINUTES (10080 = 7 days);
            # QuotaWindow.window_seconds is SECONDS. Missing this multiplication
            # would under-report every window by 60x.
            window_seconds=int(window_minutes) * 60,
            resets_at=float(resets_at) if resets_at is not None else None,
            observed_at=observed_at,
            source=Source.OBSERVED,
        )

    @staticmethod
    def _kill(proc: Any) -> None:
        try:
            proc.kill()
        except Exception:  # noqa: BLE001 - best-effort cleanup, never raise from finally
            pass
        try:
            proc.wait(timeout=1)
        except Exception:  # noqa: BLE001
            pass
