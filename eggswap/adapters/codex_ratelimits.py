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
        by_limit_id = result.get("rateLimitsByLimitId")
        snapshots: list[tuple[str, dict]] = []
        if by_limit_id is not None:
            if not isinstance(by_limit_id, dict):
                return Unknown(stale_since=observed_at, reason="malformed rateLimitsByLimitId")
            for key, snapshot in by_limit_id.items():
                if not isinstance(key, str) or not key or not isinstance(snapshot, dict):
                    return Unknown(stale_since=observed_at, reason="malformed rateLimitsByLimitId entry")
                snapshot_id = snapshot.get("limitId")
                if snapshot_id is not None and snapshot_id != key:
                    return Unknown(
                        stale_since=observed_at,
                        reason="rateLimitsByLimitId key does not match snapshot limitId",
                    )
                snapshots.append((key, snapshot))

        # Older app-server versions and empty maps retain the backward-
        # compatible single-snapshot response. No synthetic limit ID: if the
        # provider gives neither a keyed map nor an actual limitId, capacity
        # is unknown.
        if not snapshots:
            legacy = result.get("rateLimits")
            if not isinstance(legacy, dict):
                return Unknown(stale_since=observed_at, reason="missing rateLimits in reply")
            limit_id = legacy.get("limitId")
            if not isinstance(limit_id, str) or not limit_id:
                return Unknown(stale_since=observed_at, reason="missing limitId in rateLimits")
            snapshots = [(limit_id, legacy)]

        windows: list[QuotaWindow] = []
        reached_types: list[tuple[str, str]] = []
        for limit_id, rate_limits in snapshots:
            for window_name in ("primary", "secondary"):
                payload = rate_limits.get(window_name)
                # Null means no populated bucket, not a fabricated zero.
                if payload is None:
                    continue
                if not isinstance(payload, dict):
                    return Unknown(
                        stale_since=observed_at,
                        reason=f"malformed {window_name} window in {limit_id}",
                    )
                try:
                    windows.append(
                        AppServerRateLimitReader._window(
                            f"{limit_id}:{window_name}", payload, observed_at=observed_at
                        )
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    return Unknown(
                        stale_since=observed_at,
                        reason=f"malformed {window_name} window in {limit_id}: {exc}",
                    )

            # rateLimitReachedType and spendControlReached are scoped to each
            # snapshot; ordinaryUsageAllowed is a top-level response field.
            reached_type = rate_limits.get("rateLimitReachedType")
            if reached_type is None:
                reached_type = result.get("rateLimitReachedType")
            if reached_type is not None:
                if not isinstance(reached_type, str):
                    return Unknown(stale_since=observed_at, reason="malformed rateLimitReachedType")
                reached_types.append((limit_id, reached_type))
            spend_control = rate_limits.get("spendControlReached")
            if spend_control is None:
                spend_control = result.get("spendControlReached")
            if spend_control is not None and not isinstance(spend_control, bool):
                return Unknown(stale_since=observed_at, reason="malformed spendControlReached")
            if spend_control is True:
                return Unknown(
                    stale_since=observed_at,
                    reason="app-server usage gate reported; exhaustion semantics are unverified",
                )

        if not windows:
            return Unknown(stale_since=observed_at, reason="no populated quota windows in reply")
        ordinary_allowed = result.get("ordinaryUsageAllowed")
        if ordinary_allowed is not None and not isinstance(ordinary_allowed, bool):
            return Unknown(stale_since=observed_at, reason="malformed ordinaryUsageAllowed")
        if ordinary_allowed is False:
            return Unknown(
                stale_since=observed_at,
                reason="app-server usage gate reported; exhaustion semantics are unverified",
            )

        # This adapter represents a provider profile without model-to-bucket
        # applicability. Treat any observed exhausted bucket conservatively;
        # task-specific selection needs an explicit mapping before it can
        # safely ignore a full bucket.
        exhausted_window = next((window for window in windows if window.used_percent >= 100.0), None)
        if exhausted_window is not None:
            return Exhausted(
                reset_at=exhausted_window.resets_at,
                bucket=exhausted_window.bucket,
                observed_at=observed_at,
            )
        if reached_types:
            limit_id, reached_type = reached_types[0]
            named = next((w for w in windows if w.bucket.startswith(f"{limit_id}:")), None)
            return Exhausted(
                reset_at=named.resets_at if named is not None else None,
                bucket=named.bucket if named is not None else f"{limit_id}:{reached_type}",
                observed_at=observed_at,
            )

        return Available(windows=windows, observed_at=observed_at)

    @staticmethod
    def _window(bucket: str, payload: dict, *, observed_at: float) -> QuotaWindow:
        raw_used_percent = payload.get("usedPercent")
        if isinstance(raw_used_percent, bool) or not isinstance(raw_used_percent, (int, float)):
            raise ValueError("usedPercent is missing or not numeric")
        raw_window_minutes = payload.get("windowDurationMins")
        if isinstance(raw_window_minutes, bool) or not isinstance(raw_window_minutes, int):
            raise ValueError("windowDurationMins is missing or not an integer")
        resets_at = payload.get("resetsAt")
        if resets_at is not None and (
            isinstance(resets_at, bool) or not isinstance(resets_at, (int, float))
        ):
            raise ValueError("resetsAt is not numeric")
        return QuotaWindow(
            bucket=bucket,
            used_percent=float(raw_used_percent),
            # Measured live: windowDurationMins is MINUTES (10080 = 7 days);
            # QuotaWindow.window_seconds is SECONDS. Missing this multiplication
            # would under-report every window by 60x.
            window_seconds=raw_window_minutes * 60,
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
