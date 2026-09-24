"""eggswap lease ledger -- the primitive cswap Issue #581 lacks.

WHY THIS MODULE EXISTS
-----------------------
Measured incident (Issue #581): two Claude CLIs sharing one HOME concurrently
rotate a single-use refresh token and destroy it (``invalid_grant`` / "Not
logged in"). Holding an account must be an EXCLUSIVE, crash-recoverable act,
not a hint two processes both believe.

A second incident, found and fixed once already in this estate: a stale
holder that woke up late (after its lease expired, was reaped and
re-granted) called ``release()`` unconditionally and dropped the NEW
holder's lease. The fix is the fence: every successful acquire bumps a
per-profile integer that NEVER resets, even across release/reap -- the fence
lives in the record independently of whether a lease is currently held, so a
late caller comparing its old fence against the current one is always
refused, not obeyed. This is why the on-disk record is
``{fence, lease-or-null}`` rather than deleting the whole file on release:
deleting the fence along with the lease would let a stale holder's old fence
of 1 match a freshly-reset fence of 1 after a second acquire.

Storage is one JSON file per profile, written atomically (tmp + os.replace)
under an ``fcntl.flock`` exclusive lock so two PROCESSES -- not just two
threads -- cannot both observe "free" and both acquire.
"""
from __future__ import annotations

import contextlib
import json
import math
import os
import time
from dataclasses import replace
from pathlib import Path
from typing import Optional

from eggswap.core.types import (
    Lease,
    LeaseError,
    Profile,
    ProfileDisabled,
    Provider,
    StaleFence,
    WorkSpec,
)

try:
    import fcntl
except ImportError:  # pragma: no cover - POSIX only, matches this estate
    fcntl = None

__all__ = ["LeaseStore"]


class LeaseStore:
    """Filesystem-backed lease ledger, one JSON record per profile.

    ``clock`` is injected so tests can advance time without sleeping and
    without depending on wall-clock scheduling jitter.
    """

    def __init__(self, root: Path, *, clock=time.time):
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._clock = clock

    def _path(self, profile: Profile) -> Path:
        safe = profile.key.replace("/", "_")
        return self._root / f"{safe}.json"

    @contextlib.contextmanager
    def _locked(self, profile: Profile):
        """Hold an exclusive OS-level lock across a profile's read-modify-write.

        The lock file is separate from the data file so a reader can never
        observe a lock-fd's own flock state as data corruption.
        """
        lock_path = self._path(profile).with_suffix(".lock")
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _read_raw(self, path: Path) -> Optional[dict]:
        try:
            with open(path, "r") as f:
                text = f.read()
        except FileNotFoundError:
            return None
        if not text:
            return None
        return json.loads(text)

    def _write_raw(self, path: Path, record: dict) -> None:
        """Atomic commit: write to a tmp file, fsync, then os.replace.

        A crash mid-write leaves either the old file (replace never
        happened) or the fully-written new one (replace is atomic on
        POSIX); it can never leave a torn/truncated file in the committed
        path.
        """
        tmp_path = path.with_suffix(f".tmp.{os.getpid()}")
        with open(tmp_path, "w") as f:
            json.dump(record, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)

    @staticmethod
    def _profile_to_dict(profile: Profile) -> dict:
        return {
            "provider": profile.provider.value,
            "account_id": profile.account_id,
            "label": profile.label,
            "is_api_key": profile.is_api_key,
            "enabled": profile.enabled,
            "metadata": dict(profile.metadata),
        }

    @staticmethod
    def _profile_from_dict(data: dict) -> Profile:
        return Profile(
            provider=Provider(data["provider"]),
            account_id=data["account_id"],
            label=data.get("label", ""),
            is_api_key=data.get("is_api_key", False),
            enabled=data.get("enabled", True),
            metadata=data.get("metadata", {}),
        )

    @staticmethod
    def _lease_from_record(profile: Profile, fence: int, lease_data: dict) -> Lease:
        return Lease(
            lease_id=lease_data["lease_id"],
            profile=profile,
            fence=fence,
            acquired_at=lease_data["acquired_at"],
            expires_at=lease_data["expires_at"],
            workspec=WorkSpec(**lease_data["workspec"]),
            holder=lease_data["holder"],
        )

    @staticmethod
    def _lease_to_dict(lease: Lease) -> dict:
        return {
            "lease_id": lease.lease_id,
            "acquired_at": lease.acquired_at,
            "expires_at": lease.expires_at,
            "workspec": {
                "goal_id": lease.workspec.goal_id,
                "contract_version": lease.workspec.contract_version,
                "spec_hash": lease.workspec.spec_hash,
                "model": lease.workspec.model,
                "description": lease.workspec.description,
            },
            "holder": lease.holder,
        }

    def _now(self) -> float:
        return self._clock()

    @staticmethod
    def _operator_enabled(record: Optional[dict], profile_key: str) -> bool:
        if record is None:
            return True
        saved_profile = record.get("profile")
        if not isinstance(saved_profile, dict):
            raise ValueError(f"invalid profile state for {profile_key}")
        enabled = saved_profile.get("operator_enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError(f"invalid operator-enabled state for {profile_key}")
        return enabled

    def apply_profile_state(self, profile: Profile) -> Profile:
        """Apply the durable operator-enabled bit to a freshly discovered profile.

        Adapter disablement remains authoritative too; this setting only adds
        an operator veto and never promotes an adapter-disabled profile.
        """
        with self._locked(profile):
            record = self._read_raw(self._path(profile))
        operator_enabled = self._operator_enabled(record, profile.key)
        return replace(profile, enabled=bool(profile.enabled and operator_enabled))

    def set_enabled(self, profile: Profile, enabled: bool) -> None:
        """Persist an operator enable/disable without disturbing a live lease."""
        if not isinstance(enabled, bool):
            raise TypeError("enabled must be a bool")
        with self._locked(profile):
            path = self._path(profile)
            record = self._read_raw(path)
            if record is None:
                record = {"profile": self._profile_to_dict(profile), "fence": 0, "lease": None}
            saved_profile = dict(record.get("profile") or self._profile_to_dict(profile))
            saved_profile.update(self._profile_to_dict(profile))
            saved_profile["operator_enabled"] = enabled
            record["profile"] = saved_profile
            record.setdefault("fence", 0)
            record.setdefault("lease", None)
            self._write_raw(path, record)

    def acquire(
        self,
        profile: Profile,
        *,
        ttl_seconds: float,
        holder: str,
        workspec: WorkSpec = WorkSpec(),
    ) -> Lease:
        if not math.isfinite(ttl_seconds) or ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be a finite positive number")
        with self._locked(profile):
            path = self._path(profile)
            record = self._read_raw(path)
            if not profile.enabled:
                raise ProfileDisabled(f"{profile.key}: profile disabled")
            if not self._operator_enabled(record, profile.key):
                raise ProfileDisabled(f"{profile.key}: profile disabled by operator")
            now = self._now()
            current_fence = record["fence"] if record is not None else 0
            live_lease = record.get("lease") if record is not None else None
            if live_lease is not None and live_lease["expires_at"] > now:
                raise LeaseError(
                    f"{profile.key}: held by {live_lease['holder']!r} until "
                    f"{live_lease['expires_at']!r}"
                )
            new_fence = current_fence + 1
            lease = Lease(
                lease_id=Lease.new_id(),
                profile=profile,
                fence=new_fence,
                acquired_at=now,
                expires_at=now + ttl_seconds,
                workspec=workspec,
                holder=holder,
            )
            new_record = {
                "profile": self._profile_to_dict(profile),
                "fence": new_fence,
                "lease": self._lease_to_dict(lease),
            }
            self._write_raw(path, new_record)
            return lease

    def revalidate(self, lease: Lease) -> Lease:
        with self._locked(lease.profile):
            record = self._read_raw(self._path(lease.profile))
            current_fence = record["fence"] if record is not None else 0
            if lease.fence != current_fence:
                raise StaleFence(lease.profile.key, lease.fence, current_fence)
            live_lease = record.get("lease") if record is not None else None
            now = self._now()
            if live_lease is None or live_lease["expires_at"] <= now:
                raise LeaseError(f"{lease.profile.key}: lease {lease.lease_id} expired")
            return self._lease_from_record(lease.profile, current_fence, live_lease)

    def renew(self, lease: Lease, *, ttl_seconds: float) -> Lease:
        """Extend a live lease without changing its identity or fence.

        Renewal is fenced and atomic: an expired or superseded holder can
        never resurrect its lease or overwrite the replacement's record.
        """
        if not math.isfinite(ttl_seconds) or ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be a finite positive number")
        with self._locked(lease.profile):
            path = self._path(lease.profile)
            record = self._read_raw(path)
            current_fence = record["fence"] if record is not None else 0
            if lease.fence != current_fence:
                raise StaleFence(lease.profile.key, lease.fence, current_fence)
            live_lease = record.get("lease") if record is not None else None
            now = self._now()
            if live_lease is None or live_lease["expires_at"] <= now:
                raise LeaseError(f"{lease.profile.key}: lease {lease.lease_id} expired")
            if live_lease["lease_id"] != lease.lease_id:
                raise StaleFence(lease.profile.key, lease.fence, current_fence)
            renewed = dict(live_lease)
            renewed["expires_at"] = now + ttl_seconds
            record["lease"] = renewed
            self._write_raw(path, record)
            return self._lease_from_record(lease.profile, current_fence, renewed)

    def release(self, lease: Lease) -> None:
        with self._locked(lease.profile):
            path = self._path(lease.profile)
            record = self._read_raw(path)
            current_fence = record["fence"] if record is not None else 0
            if lease.fence != current_fence:
                # The whole point: a late holder waking up after its lease
                # was reaped and reacquired must not delete the NEW
                # holder's record. Refuse instead of releasing
                # unconditionally -- this is the exact bug this estate
                # already found and fixed once.
                raise StaleFence(lease.profile.key, lease.fence, current_fence)
            new_record = {
                "profile": record["profile"],
                "fence": current_fence,
                "lease": None,
            }
            self._write_raw(path, new_record)

    def holder_of(self, profile: Profile) -> Optional[Lease]:
        with self._locked(profile):
            record = self._read_raw(self._path(profile))
            if record is None or record.get("lease") is None:
                return None
            return self._lease_from_record(profile, record["fence"], record["lease"])

    def reap_expired(self) -> list[Lease]:
        reaped: list[Lease] = []
        if not self._root.is_dir():
            return reaped
        for path in sorted(self._root.glob("*.json")):
            record = self._read_raw(path)
            if record is None or record.get("lease") is None:
                continue
            profile = self._profile_from_dict(record["profile"])
            with self._locked(profile):
                current = self._read_raw(path)
                if current is None or current.get("lease") is None:
                    continue
                now = self._now()
                if current["lease"]["expires_at"] > now:
                    continue
                lease = self._lease_from_record(
                    profile, current["fence"], current["lease"]
                )
                reaped.append(lease)
                new_record = {
                    "profile": current["profile"],
                    "fence": current["fence"] + 1,
                    "lease": None,
                }
                self._write_raw(path, new_record)
        return reaped

    def fence_of(self, profile: Profile) -> int:
        with self._locked(profile):
            record = self._read_raw(self._path(profile))
            return record["fence"] if record is not None else 0
