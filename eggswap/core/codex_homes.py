"""Non-secret enrollment list for Codex homes added through Eggswap.

Credentials remain in Codex's own store. This file records only directory
paths, with a process lock so concurrent logins cannot lose an enrollment.
"""
from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path

import fcntl


def state_root() -> Path:
    raw = os.environ.get("EGGSWAP_STATE_DIR")
    return Path(raw).expanduser() if raw else Path.home() / ".local/state/eggswap"


@contextlib.contextmanager
def _lock(root: Path):
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(str(root / "codex_homes.lock"), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


@contextlib.contextmanager
def exclusive_login(home: Path, root: Path | None = None):
    """Serialize account login and duplicate-identity checking across homes."""
    root = state_root() if root is None else Path(root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(str(root / "codex-login.lock"), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("another Eggswap Codex login is already active") from exc
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _read(path: Path) -> list[Path]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Codex home enrollment unreadable: {path}") from exc
    if not isinstance(raw, list) or any(
        not isinstance(item, str) or not Path(item).is_absolute() for item in raw
    ):
        raise ValueError(f"Codex home enrollment malformed: {path}")
    return [Path(item) for item in raw]


def enrolled_codex_homes(root: Path | None = None) -> list[Path]:
    root = state_root() if root is None else Path(root)
    return _read(root / "codex_homes.json")


def enroll_codex_home(home: Path, root: Path | None = None) -> None:
    root = state_root() if root is None else Path(root)
    home = Path(home).resolve(strict=True)
    if not home.is_dir():
        raise ValueError(f"CODEX_HOME is not a directory: {home}")
    with _lock(root):
        path = root / "codex_homes.json"
        homes = _read(path)
        if home in homes:
            return
        homes.append(home)
        fd, temporary = tempfile.mkstemp(prefix="codex_homes.", suffix=".tmp", dir=root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump([str(item) for item in homes], handle)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
