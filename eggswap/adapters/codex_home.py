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
Separate directories are proof of isolation only where the store is
file-backed, which is what `store_mode()` below reports per profile.

Measured on the machine this was built against: no `cli_auth_credentials_store`
in config.toml, and auth.json carries all four token fields non-empty, so the
effective store is file.

The keyring case has since been NARROWED, and it got worse rather than better
(docs/research/eggswap/codex-keyring-namespacing.md). The keyring SERVICE name
in the 0.156.1 string table is the constant literal "codex" -- one global value
with no visible per-CODEX_HOME component. The ACCOUNT half of the
(service, account) pair is built at runtime and is not a literal in the binary,
so it cannot be read statically. If that half is also constant, then two
CODEX_HOME directories on a keyring-backed install COLLIDE ON ONE SECRET, and
a tool rotating between them would hammer a single account's quota believing
it had two.

So this is not a neutral unknown any more: the one half that can be read looks
global. eggswap therefore labels the store per profile, promises isolation
only for the file-backed case it has measured, and the README tells a reader
with multiple Codex accounts on a keyring-backed install to verify isolation
before trusting rotation. The experiment that would settle it needs two
disposable test logins and is written out in that document.

This adapter never reads `access_token`, `refresh_token` or `id_token`. It
parses only `tokens.account_id` (an address, not a secret), `auth_mode` and
`OPENAI_API_KEY` (to label a profile, never to use it).

WHY availability() DEFAULTS TO Unknown, NOT Available
-------------------------------------------------------
docs/research/eggswap/codex-ratelimits-probe.md found the `account/
rateLimits/read` JSON-RPC method and its response struct names in the
installed binary via `strings`, but made no live call -- there is no observed
percentage, window or reset time on this install, only evidence the method
exists. Wiring a reader interface for it while defaulting to None keeps that
distinction: a future reader can supply real numbers, but this module never
invents one. Returning Available with no real reading here would be exactly
the frozen-number failure eggswap/core/types.py exists to prevent.
"""
from __future__ import annotations

import json
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


def _declared_store_mode(config: Path) -> Optional[str]:
    """Read the credential-store setting on Python versions without tomllib.

    The package supports Python 3.10, which predates stdlib tomllib. Falling
    through to token presence there mislabeled keyring, auto and ephemeral
    profiles as file-backed, hiding the account-isolation uncertainty.
    """
    try:
        import tomllib
    except ModuleNotFoundError:
        try:
            lines = config.read_text(encoding="utf-8").splitlines()
        except OSError:
            return None
        in_table = False
        for raw in lines:
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            if line.startswith("["):
                in_table = True
                continue
            if in_table or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.strip() != "cli_auth_credentials_store":
                continue
            value = value.strip()
            if len(value) >= 2 and value[0] in ("'", '"') and value[-1] == value[0]:
                return value[1:-1]
            return None
        return None
    try:
        with config.open("rb") as handle:
            return tomllib.load(handle).get("cli_auth_credentials_store")
    except Exception:
        return None


def _store_mode(home: Path) -> str:
    """Where this home's credential actually lives, as far as we can tell.

    "file" only when auth.json really carries the token fields: a config that
    SAYS file while auth.json holds no tokens is not a file-backed store, and
    the observable state wins over the declared one. Anything else is reported
    verbatim or as "unknown" -- never silently treated as isolated.
    """
    declared = None
    config = home / "config.toml"
    if config.is_file():
        declared = _declared_store_mode(config)
    if declared and str(declared) != "file":
        return str(declared)
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
    ) -> None:
        self._homes = list(homes)
        self._clock = clock
        self._rate_limit_reader = rate_limit_reader

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
                        "credentials_store": _store_mode(home),
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
