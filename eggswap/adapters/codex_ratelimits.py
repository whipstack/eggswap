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
import copy
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


def _merge_snapshot(previous: dict, update: dict) -> dict:
    """Merge a sparse protocol snapshot without treating null as deletion."""
    merged = dict(previous)
    for key, value in update.items():
        if value is None:
            continue
        old = merged.get(key)
        if key in ("primary", "secondary") and isinstance(value, dict):
            # Rate windows are measurements, not nullable account metadata:
            # an explicit null reset/duration means that field is unavailable
            # in this update. Clearing it prevents an old epoch from being
            # relabelled fresh alongside a newly observed percentage.
            window = dict(old) if isinstance(old, dict) else {}
            for field in ("usedPercent", "windowDurationMins", "resetsAt"):
                if field in value:
                    window[field] = copy.deepcopy(value[field])
            for field, field_value in value.items():
                if field not in ("usedPercent", "windowDurationMins", "resetsAt") and field_value is not None:
                    window[field] = copy.deepcopy(field_value)
            merged[key] = window
            continue
        if isinstance(value, dict) and isinstance(old, dict):
            merged[key] = _merge_snapshot(old, value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def merge_rate_limit_update(
    snapshots: dict[str, dict], notification: dict
) -> dict[str, dict]:
    """Apply one ``account/rateLimits/updated`` sparse notification.

    The notification updates one ``limitId``. Other meters and fields omitted
    or null in the sparse payload stay as they were in the last full read.
    Malformed or unaddressable updates raise ``ValueError`` so callers cannot
    silently turn an update into a replacement snapshot.
    """
    params = notification.get("params") if isinstance(notification, dict) else None
    update = params.get("rateLimits") if isinstance(params, dict) else None
    if not isinstance(update, dict):
        raise ValueError("missing params.rateLimits snapshot")
    limit_id = update.get("limitId")
    if not isinstance(limit_id, str) or not limit_id:
        raise ValueError("rate-limit update has no limitId")
    merged = copy.deepcopy(snapshots)
    merged[limit_id] = _merge_snapshot(merged.get(limit_id, {}), update)
    return merged

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
        self._state_lock = threading.RLock()
        self._latest_result: Optional[dict] = None
        self._latest_snapshots: dict[str, dict] = {}
        self._latest_window_observed_at: dict[tuple[str, str], float] = {}
        self._latest_model: Optional[str] = None

    def __call__(self, profile: Profile, *, model: Optional[str] = None) -> Availability:
        codex_home = (profile.metadata or {}).get("codex_home") or str(self._codex_home)
        try:
            home_count = int((profile.metadata or {}).get("codex_home_count", 1))
        except (TypeError, ValueError):
            home_count = 0

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
                rate_id = 2
                if home_count > 1:
                    store_mode = self._effective_store_mode(proc, reader, deadline)
                    if store_mode != "file":
                        return Unknown(
                            stale_since=self._clock(),
                            reason=(
                                "multiple CODEX_HOME profiles require an explicitly "
                                f"verified effective file credential store; observed {store_mode}"
                            ),
                        )
                    rate_id = 4
                elif home_count != 1:
                    return Unknown(
                        stale_since=self._clock(),
                        reason="invalid configured CODEX_HOME count",
                    )
                self._write(
                    proc,
                    {"id": rate_id, "method": "account/rateLimits/read", "params": {}},
                )
                reply = self._await_reply(reader, expected_id=rate_id, deadline=deadline)
            except _Timeout as exc:
                return Unknown(stale_since=self._clock(), reason=str(exc))
            except _BadJSON as exc:
                return Unknown(stale_since=self._clock(), reason=str(exc))
            except _HandshakeError as exc:
                return Unknown(stale_since=self._clock(), reason=str(exc))

            observed_at = self._clock()
            result = reply.get("result")
            if isinstance(result, dict):
                with self._state_lock:
                    self._latest_result = copy.deepcopy(result)
                    self._latest_snapshots = self._read_snapshots(result) or {}
                    self._latest_window_observed_at = {
                        (limit_id, kind): observed_at
                        for limit_id, snapshot in self._latest_snapshots.items()
                        for kind in ("primary", "secondary")
                        if isinstance(snapshot.get(kind), dict)
                    }
                    self._latest_model = model
            return self._to_availability(reply, observed_at=observed_at, model=model)
        finally:
            if proc is not None:
                self._kill(proc)

    def _effective_store_mode(self, proc: Any, reader: _LineReader, deadline: float) -> str:
        """Read effective and managed store policy from this Codex app-server.

        A local config.toml or the presence of auth.json tokens cannot prove
        the effective backend: managed requirements can override local config.
        Requiring both read-only RPCs to succeed makes missing/old protocols
        fail closed for multi-home scheduling.
        """
        self._write(proc, {"id": 2, "method": "configRequirements/read", "params": {}})
        requirements_reply = self._await_reply(reader, expected_id=2, deadline=deadline)
        requirements_result = requirements_reply.get("result")
        if not isinstance(requirements_result, dict) or "requirements" not in requirements_result:
            return "unknown"
        requirements = requirements_result.get("requirements")
        if requirements is not None and not isinstance(requirements, dict):
            return "unknown"

        self._write(proc, {"id": 3, "method": "config/read", "params": {"includeLayers": True}})
        config_reply = self._await_reply(reader, expected_id=3, deadline=deadline)
        config_result = config_reply.get("result")
        config = config_result.get("config") if isinstance(config_result, dict) else None
        if not isinstance(config, dict):
            return "unknown"

        # Managed requirements are the higher-precedence source. Otherwise
        # config/read supplies the effective layered value. Missing or
        # malformed fields remain unknown; token presence is not evidence.
        if isinstance(requirements, dict):
            managed = requirements.get("cliAuthCredentialsStore")
            if managed is not None:
                modes = {"file", "keyring", "auto", "ephemeral"}
                return managed if isinstance(managed, str) and managed in modes else "unknown"
        additional = config.get("additional")
        if isinstance(additional, dict):
            value = additional.get("cli_auth_credentials_store")
            if value is None:
                value = additional.get("cliAuthCredentialsStore")
        else:
            value = config.get("cli_auth_credentials_store", config.get("cliAuthCredentialsStore"))
        modes = {"file", "keyring", "auto", "ephemeral"}
        return value if isinstance(value, str) and value in modes else "unknown"

    def apply_notification(
        self, notification: dict, *, model: Optional[str] = None
    ) -> Optional[Availability]:
        """Merge one captured sparse push into the last full read.

        Returns ``None`` for unrelated notifications. This method is for a
        caller that already owns a live app-server notification stream; the
        normal one-shot reader below does not claim that it observed future
        pushes after its read reply.
        """
        if not isinstance(notification, dict) or notification.get("method") != "account/rateLimits/updated":
            return None
        with self._state_lock:
            if self._latest_result is None or not self._latest_snapshots:
                return Unknown(
                    stale_since=self._clock(),
                    reason="rate-limit push arrived before a full account/rateLimits/read snapshot",
                )
            try:
                snapshots = merge_rate_limit_update(self._latest_snapshots, notification)
            except (TypeError, ValueError) as exc:
                return Unknown(stale_since=self._clock(), reason=f"malformed rate-limit update: {exc}")
            result = copy.deepcopy(self._latest_result)
            result["rateLimitsByLimitId"] = snapshots
            self._latest_result = result
            self._latest_snapshots = snapshots
            observed_at = self._clock()
            update_snapshot = notification["params"]["rateLimits"]
            limit_id = update_snapshot["limitId"]
            window_observed = dict(self._latest_window_observed_at)
            for kind in ("primary", "secondary"):
                payload = update_snapshot.get(kind)
                # RateLimitWindow requires usedPercent. Only a delivered
                # window value refreshes that bucket's observation age; an
                # update for another limit must not make old percentages look
                # fresh merely because the parent map changed.
                if isinstance(payload, dict) and payload.get("usedPercent") is not None:
                    window_observed[(limit_id, kind)] = observed_at
            self._latest_window_observed_at = window_observed
            chosen_model = self._latest_model if model is None else model
            reply = {"id": 2, "result": result}
        return self._to_availability(
            reply, observed_at=observed_at, model=chosen_model,
            window_observed_at=window_observed,
        )

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
    def _to_availability(
        reply: dict, *, observed_at: float, model: Optional[str] = None,
        window_observed_at: Optional[dict[tuple[str, str], float]] = None,
    ) -> Availability:
        if "error" in reply:
            return Unknown(stale_since=observed_at, reason=f"app-server error: {reply['error']}")
        result = reply.get("result")
        if not isinstance(result, dict):
            return Unknown(stale_since=observed_at, reason="missing result in account/rateLimits/read reply")
        snapshots = AppServerRateLimitReader._read_snapshots(result)
        if snapshots is None:
            return Unknown(stale_since=observed_at, reason="missing or malformed rate-limit snapshots")

        windows = []
        binding_windows = []
        scoped_reached = []
        any_spend_control = False
        for limit_id, snapshot in snapshots.items():
            if not isinstance(snapshot, dict):
                return Unknown(stale_since=observed_at, reason=f"malformed rate-limit snapshot: {limit_id}")
            model_scope = snapshot.get("normalModelSlug")
            if model_scope is not None and not isinstance(model_scope, str):
                return Unknown(stale_since=observed_at, reason=f"malformed normalModelSlug: {limit_id}")
            model_scope = model_scope.strip() if isinstance(model_scope, str) else ""
            bucket_prefix = str(limit_id)
            if model_scope:
                bucket_prefix = f"{bucket_prefix}:model:{model_scope}"

            snapshot_windows = []
            for kind in ("primary", "secondary"):
                payload = snapshot.get(kind)
                if payload is None:
                    continue
                if not isinstance(payload, dict):
                    return Unknown(stale_since=observed_at, reason=f"malformed {kind} window: {limit_id}")
                try:
                    snapshot_windows.append(
                        AppServerRateLimitReader._window(
                            f"{bucket_prefix}:{kind}", payload,
                            observed_at=(window_observed_at or {}).get(
                                (limit_id, kind), observed_at
                            ),
                            model_scope=model_scope or None,
                        )
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    return Unknown(stale_since=observed_at, reason=f"malformed {kind} window: {exc}")
            windows.extend(snapshot_windows)

            # A scoped bucket remains visible but does not determine
            # schedulability without a matching requested model. This is the
            # same rule as ClaudeCswapAdapter.availability().
            binds = not model_scope or (
                bool(model and model.strip())
                and model_scope.casefold() == model.strip().casefold()
            )
            if binds:
                binding_windows.extend(snapshot_windows)

            reached_type = snapshot.get("rateLimitReachedType")
            if reached_type is not None and not isinstance(reached_type, str):
                return Unknown(stale_since=observed_at, reason="malformed rateLimitReachedType")
            if reached_type:
                scoped_reached.append((str(reached_type), binds, snapshot_windows))
            spend_control = snapshot.get("spendControlReached")
            if spend_control is not None and not isinstance(spend_control, bool):
                return Unknown(stale_since=observed_at, reason="malformed spendControlReached")
            any_spend_control = any_spend_control or (binds and spend_control is True)

        if not windows:
            reason = (
                "missing primary window in rateLimits"
                if isinstance(result.get("rateLimits"), dict)
                and result.get("rateLimitsByLimitId") is None
                else "no rate-limit windows in reply"
            )
            return Unknown(stale_since=observed_at, reason=reason)

        if not binding_windows:
            return Unknown(
                stale_since=observed_at,
                reason=(f"no unscoped or model-matching Codex bucket for {model!r}"
                        if model else "no unscoped Codex bucket for an unspecified model"),
                windows=tuple(windows),
            )

        ordinary_allowed = result.get("ordinaryUsageAllowed")
        if ordinary_allowed is not None and not isinstance(ordinary_allowed, bool):
            return Unknown(stale_since=observed_at, reason="malformed ordinaryUsageAllowed",
                           windows=tuple(windows))
        top_reached = result.get("rateLimitReachedType")
        if top_reached is not None and not isinstance(top_reached, str):
            return Unknown(stale_since=observed_at, reason="malformed rateLimitReachedType",
                           windows=tuple(windows))
        top_spend = result.get("spendControlReached")
        if top_spend is not None and not isinstance(top_spend, bool):
            return Unknown(stale_since=observed_at, reason="malformed spendControlReached",
                           windows=tuple(windows))
        if ordinary_allowed is False or any_spend_control or top_spend is True:
            return Unknown(
                stale_since=observed_at,
                reason="app-server usage gate reported; exhaustion semantics are unverified",
                windows=tuple(windows),
            )

        exhausted_window = next((w for w in binding_windows if w.used_percent >= 100.0), None)
        reached = next((item for item in scoped_reached if item[1]), None)
        if reached is None and top_reached:
            reached = (top_reached, True, ())
        if exhausted_window is not None or reached is not None:
            reached_type = reached[0] if reached else None
            window = exhausted_window
            if reached_type:
                named = next((w for w in binding_windows if w.bucket.endswith(f":{reached_type}")), None)
                if named is not None:
                    window = named
            return Exhausted(
                reset_at=window.resets_at if window is not None else None,
                bucket=window.bucket if window is not None else reached_type,
                observed_at=observed_at,
                windows=tuple(windows),
            )

        return Available(
            windows=tuple(windows),
            # A sparse push refreshes only the bucket(s) it carried. The
            # query is as fresh as its stalest binding constraint, so a
            # one-bucket update cannot freshen an unrelated old percentage.
            observed_at=min(w.observed_at for w in binding_windows),
            binding_windows=tuple(binding_windows),
        )

    @staticmethod
    def _read_snapshots(result: dict) -> Optional[dict[str, dict]]:
        """Return every meter snapshot, preferring the multi-bucket view.

        The app-server keeps ``rateLimits`` as a backwards-compatible
        single-bucket view and adds ``rateLimitsByLimitId`` for the complete
        set. Both are merged so an older server remains readable and a newer
        one cannot silently hide a bucket that only appears in the map.
        """
        by_id = result.get("rateLimitsByLimitId")
        if by_id is not None and not isinstance(by_id, dict):
            return None
        snapshots: dict[str, dict] = {}
        # Start with the legacy view and let the complete map replace/extend
        # it. A push-updated map must win over a stale compatibility copy.
        legacy = result.get("rateLimits")
        if legacy is not None:
            if not isinstance(legacy, dict):
                return None
            limit_id = legacy.get("limitId") or "codex"
            if not isinstance(limit_id, str) or not limit_id:
                return None
            snapshots[limit_id] = _merge_snapshot({}, legacy)
        if isinstance(by_id, dict):
            for key, value in by_id.items():
                if not isinstance(key, str) or not key or not isinstance(value, dict):
                    return None
                snapshots[key] = _merge_snapshot(snapshots.get(key, {}), value)
                snapshots[key].setdefault("limitId", key)
        return snapshots or None

    @staticmethod
    def _window(
        bucket: str, payload: dict, *, observed_at: float,
        model_scope: Optional[str] = None,
    ) -> QuotaWindow:
        used_percent = float(payload["usedPercent"])
        window_minutes = payload.get("windowDurationMins")
        resets_at = payload.get("resetsAt")
        return QuotaWindow(
            bucket=bucket,
            used_percent=used_percent,
            # Measured live: windowDurationMins is MINUTES (10080 = 7 days);
            # QuotaWindow.window_seconds is SECONDS. Missing this multiplication
            # would under-report every window by 60x.
            window_seconds=int(window_minutes) * 60 if window_minutes is not None else None,
            resets_at=float(resets_at) if resets_at is not None else None,
            observed_at=observed_at,
            source=Source.OBSERVED,
            model_scope=model_scope,
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
