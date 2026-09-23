"""Codex account adapter -- CODEX_HOME is the account boundary.

WHY THIS FILE EXISTS
---------------------
docs/research/eggswap/codex-account-model.md (measured on this machine,
2026-09-23): a running Codex process binds to exactly one account through the
CODEX_HOME directory for the life of that process -- CODEX_HOME points at a
directory holding one flat `auth.json`, and `auth.json` is a single
`(auth_mode, account_id, token set)` tuple, never an array of accounts. This
is the structural twin of CLAUDE_CONFIG_DIR on the Claude side
(eggswap/adapters/claude_cswap.py), so rotation works the same way: one
account per directory, never in-place credential swapping.

THE LIMIT OF THAT CLAIM, and it is a real one. Codex can be configured with
`cli_auth_credentials_store` set to file, keyring, auto or ephemeral, and an
admin policy can override the config. The binary carries a keyring code path
(`failed to write OAuth tokens to keyring` is in the shipped 0.156.1 binary),
so on a keyring-backed install the credential does NOT live under CODEX_HOME
and two CODEX_HOME directories are not, by themselves, two isolated accounts.
Separate directories are proof of isolation only where the effective store is
file-backed, which is what `store_mode()` below reports per profile.

Measured on the machine this was built against: no `cli_auth_credentials_store`
in config.toml, and auth.json carries all four token fields non-empty, so this
profile appears file-backed. UNKNOWN, and recorded as such rather than assumed:
whether a keyring entry is namespaced per CODEX_HOME. The probe that settles
it is two logged-in homes on a keyring-backed install, checking whether a
logout in one revokes the other. Until someone runs it, eggswap labels the
store and does not promise isolation it has not measured.

This adapter never reads `access_token`, `refresh_token` or `id_token`. It
parses only `tokens.account_id` (an address, not a secret), `auth_mode` and
`OPENAI_API_KEY` (to label a profile, never to use it).

WHY availability() DEFAULTS TO Unknown, NOT Available
-----------------------------------------------------
The reader must return a fresh observed quota window before this adapter can
report capacity. Missing readers, auth files or provider responses remain
Unknown; this module never invents a percentage or reset time.
"""
from __future__ import annotations

import base64
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, List, Mapping, Optional

from eggswap.core.types import (
    Availability,
    Profile,
    Provider,
    Unknown,
)

#: auth_mode value meaning the profile authenticates with a bare API key
#: rather than a ChatGPT OAuth token set (codex-account-model.md section 1).
_APIKEY_MODE = "apikey"
_STORE_MODES = {"file", "keyring", "auto", "ephemeral"}
_PREFERENCES_UNSET = object()


def _toml_store_mode(text: str) -> Optional[str]:
    """Read only a top-level credential-store value without a TOML dependency."""
    section = ""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("["):
            section = stripped
            continue
        if section or not stripped.startswith("cli_auth_credentials_store"):
            continue
        match = re.fullmatch(
            r"cli_auth_credentials_store\s*=\s*(['\"])([^'\"]+)\1\s*(?:#.*)?",
            stripped,
        )
        if match:
            value = match.group(2)
            return value if value in _STORE_MODES else "unknown"
        return "unknown"
    return None


def _managed_preferences() -> Optional[dict[str, str]]:
    """Read the two relevant macOS MDM TOML payloads without logging them.

    None means the preference probe failed; an empty mapping means no
    relevant managed preference was set. Values are decoded in memory only.
    """
    if sys.platform != "darwin":
        return {}
    result: dict[str, str] = {}
    for key in ("requirements_toml_base64", "config_toml_base64"):
        try:
            probe = subprocess.run(
                ["/usr/bin/defaults", "read", "com.openai.codex", key],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if probe.returncode != 0:
            continue
        try:
            raw = probe.stdout.strip().strip('"')
            result[key] = base64.b64decode(raw, validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return None
    return result


def _read_policy_file(path: Path) -> Optional[str]:
    """Read a config layer's store setting; unreadable policy is UNKNOWN."""
    if not path.exists():
        return None
    try:
        return _toml_store_mode(path.read_text(encoding="utf-8"))
    except OSError:
        return "unknown"


def _read_auth_json(path: Path) -> Optional[dict]:
    """Return auth.json's parsed contents, or None on any read/parse failure.

    None on failure (never {}), so callers can tell "the file said nothing"
    from "we could not read the file" -- the latter must become Unknown.
    """
    try:
        text = path.read_text()
    except OSError:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _store_mode(
    home: Path,
    *,
    system_policy_dir: Path = Path("/etc/codex"),
    managed_preferences: Any = _PREFERENCES_UNSET,
) -> str:
    """Where this home's credential actually lives, as far as we can tell.

    "file" only when auth.json really carries the token fields: a config that
    SAYS file while auth.json holds no tokens is not a file-backed store, and
    the observable state wins over the declared one. Anything else is reported
    verbatim or as "unknown" -- never silently treated as isolated.
    """
    if managed_preferences is _PREFERENCES_UNSET:
        managed_preferences = _managed_preferences()
    if managed_preferences is None:
        return "unknown"

    sources = (
        _toml_store_mode(managed_preferences["requirements_toml_base64"])
        if "requirements_toml_base64" in managed_preferences
        else None,
        _read_policy_file(system_policy_dir / "requirements.toml"),
        _read_policy_file(system_policy_dir / "managed_config.toml"),
        _toml_store_mode(managed_preferences["config_toml_base64"])
        if "config_toml_base64" in managed_preferences
        else None,
        _read_policy_file(home / "config.toml"),
    )
    declared_sources = [source for source in sources if source is not None]
    if "unknown" in declared_sources or len(set(declared_sources)) > 1:
        # Do not guess which policy layer wins when this adapter cannot prove
        # that the runtime resolved it the same way.
        return "unknown"
    declared = declared_sources[0] if declared_sources else None
    if declared is not None and declared != "file":
        return declared
    auth = home / "auth.json"
    try:
        tokens = json.loads(auth.read_text()).get("tokens") or {}
    except Exception:
        return "unknown"
    if isinstance(tokens, dict) and tokens.get("access_token") and tokens.get("refresh_token"):
        return "file"
    return "unknown"


class CodexHomeAdapter:
    """Reads Codex account identity/capacity through CODEX_HOME/auth.json.

    `clock` and `rate_limit_reader` are injected so tests never sleep, never
    touch the network and never read a real account.
    """

    provider = Provider.CODEX

    def __init__(
        self,
        homes: List[Path],
        *,
        clock: Callable[[], float] = time.time,
        rate_limit_reader: Optional[Callable[[Profile], Any]] = None,
        store_mode_reader: Optional[Callable[[Path], str]] = None,
    ) -> None:
        self._homes = list(homes)
        self._clock = clock
        self._rate_limit_reader = rate_limit_reader
        self._store_mode_reader = store_mode_reader
        self._managed_preferences: Any = _PREFERENCES_UNSET

    def _credential_store_mode(self, home: Path) -> str:
        if self._store_mode_reader is not None:
            return self._store_mode_reader(home)
        if self._managed_preferences is _PREFERENCES_UNSET:
            self._managed_preferences = _managed_preferences()
        return _store_mode(home, managed_preferences=self._managed_preferences)

    @staticmethod
    def _auth_path(home: Path) -> Path:
        return home / "auth.json"

    def _home_for_profile(self, profile: Profile) -> Optional[Path]:
        meta_home = (profile.metadata or {}).get("codex_home")
        if meta_home:
            return Path(meta_home)
        for home in self._homes:
            data = _read_auth_json(self._auth_path(home))
            tokens = data.get("tokens") if isinstance(data, dict) else None
            account_id = tokens.get("account_id") if isinstance(tokens, dict) else None
            if account_id and str(account_id) == profile.account_id:
                return home
        return None

    def profiles(self) -> List[Profile]:
        result = []
        for home in self._homes:
            if not home.is_dir():
                continue
            data = _read_auth_json(self._auth_path(home))
            if data is None:
                continue
            tokens = data.get("tokens")
            account_id = tokens.get("account_id") if isinstance(tokens, dict) else None
            if not account_id:
                continue
            auth_mode = data.get("auth_mode")
            is_api_key = auth_mode == _APIKEY_MODE or bool(data.get("OPENAI_API_KEY"))
            result.append(
                Profile(
                    provider=Provider.CODEX,
                    account_id=str(account_id),
                    label=str(account_id),
                    is_api_key=is_api_key,
                    metadata={
                        "codex_home": str(home),
                        "credentials_store": self._credential_store_mode(home),
                    },
                )
            )
        return result

    def availability(
        self, profile: Profile, *, max_age_seconds: float = 300.0
    ) -> Availability:
        now = self._clock()
        home = self._home_for_profile(profile)
        if home is None:
            return Unknown(stale_since=now, reason=f"no configured CODEX_HOME for {profile.key}")

        auth_path = self._auth_path(home)
        if not auth_path.exists():
            return Unknown(stale_since=now, reason="auth.json missing")
        try:
            mtime = auth_path.stat().st_mtime
        except OSError:
            mtime = now
        if _read_auth_json(auth_path) is None:
            return Unknown(stale_since=mtime, reason="auth.json unreadable or not valid JSON")

        credential_store = self._credential_store_mode(home)
        if credential_store != "file" and len(self.profiles()) > 1:
            return Unknown(
                stale_since=now,
                reason=(
                    f"multiple CODEX_HOME profiles use an unverified {credential_store} "
                    "credential store; account isolation is not established"
                ),
            )

        if self._rate_limit_reader is None:
            return Unknown(
                stale_since=now, reason="no capacity signal for codex on this install"
            )
        return self._rate_limit_reader(profile)

    def launch_env(
        self, profile: Profile, base_env: Optional[Mapping[str, str]] = None
    ) -> dict:
        home = self._home_for_profile(profile)
        if home is None:
            raise ValueError(f"no configured CODEX_HOME for {profile.key}")
        env = dict(base_env) if base_env else {}
        env["CODEX_HOME"] = str(home)
        return env
