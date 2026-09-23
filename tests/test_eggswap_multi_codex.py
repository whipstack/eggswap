"""Proves eggswap rotates MULTIPLE Codex accounts -- synthetically.

WHY THIS TEST EXISTS
--------------------
This machine has exactly one real Codex account, so the multi-account path
(docs/research/eggswap/codex-account-model.md: a Codex account IS a
CODEX_HOME directory) is otherwise asserted by construction and never
executed. This test drives three synthetic CODEX_HOME directories -- each
with its own fake auth.json, never a real credential -- through
CodexHomeAdapter, ``eggswap.cli._default_codex_homes`` and
``eggswap.core.select`` so the rotation claim is actually exercised.

Run: python3 -m unittest tests.test_eggswap_multi_codex -v
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from eggswap.adapters.codex_home import CodexHomeAdapter
from eggswap.cli import _default_codex_homes
from eggswap.core.select import Policy, rank, select
from eggswap.core.types import Available, Candidate, NoCapacity, Provider, Unknown

_FAKE_TOKEN_A = "NOT-A-REAL-TOKEN-a"
_FAKE_TOKEN_B = "NOT-A-REAL-TOKEN-b"
_FAKE_TOKEN_C = "NOT-A-REAL-TOKEN-c"


def _write_auth(home: Path, **fields) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "auth.json").write_text(json.dumps(fields))


def _chatgpt_auth(account_id: str, token: str) -> dict:
    return {
        "auth_mode": "chatgpt",
        "tokens": {
            "account_id": account_id,
            "access_token": token,
            "refresh_token": token,
            "id_token": token,
        },
    }


def _score(availability) -> float:
    if not isinstance(availability, Available) or not availability.windows:
        return 100.0
    return 100.0 - max(w.used_percent for w in availability.windows)


class ThreeFakeCodexHomesTest(unittest.TestCase):
    """Three synthetic CODEX_HOME directories: chatgpt, apikey, broken."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        preferences = mock.patch(
            "eggswap.adapters.codex_home._managed_preferences", return_value={}
        )
        preferences.start()
        self.addCleanup(preferences.stop)

        self.home_a = self.root / "acct-a"
        self.home_b = self.root / "acct-b"
        self.home_apikey = self.root / "acct-apikey"
        self.home_broken = self.root / "acct-broken"

        _write_auth(self.home_a, **_chatgpt_auth("acct-a", _FAKE_TOKEN_A))
        _write_auth(self.home_b, **_chatgpt_auth("acct-b", _FAKE_TOKEN_B))
        _write_auth(
            self.home_apikey,
            auth_mode="apikey",
            OPENAI_API_KEY="sk-FAKE-NOT-REAL",
            tokens={"account_id": "acct-apikey", "access_token": _FAKE_TOKEN_C},
        )
        # Broken: directory exists, auth.json absent -- distinct from unparseable.
        self.home_broken.mkdir(parents=True)

    def test_three_homes_three_distinct_profiles_no_key_collisions(self):
        adapter = CodexHomeAdapter([self.home_a, self.home_b, self.home_apikey])
        profiles = adapter.profiles()

        self.assertEqual(len(profiles), 3)
        account_ids = {p.account_id for p in profiles}
        self.assertEqual(account_ids, {"acct-a", "acct-b", "acct-apikey"})
        keys = {p.key for p in profiles}
        self.assertEqual(len(keys), 3, "Profile.key must not collide across accounts")
        for p in profiles:
            self.assertEqual(p.provider, Provider.CODEX)

    def test_default_codex_homes_reads_env_pathsep_separated_dedup_and_skips_bad(self):
        # A fourth, non-existent path in the list must be silently skipped,
        # and a duplicate of home_a must not appear twice.
        import os

        raw = os.pathsep.join(
            [
                str(self.home_a),
                str(self.home_b),
                str(self.root / "does-not-exist"),
                str(self.home_a),  # duplicate
            ]
        )
        with mock.patch.dict(
            "os.environ",
            {"EGGSWAP_CODEX_HOMES": raw, "CODEX_HOME": ""},
            clear=False,
        ):
            with mock.patch("eggswap.cli.Path.home", return_value=self.root / "no-default-here"):
                homes = _default_codex_homes()

        self.assertEqual(homes, [self.home_a, self.home_b])

    def test_broken_home_missing_auth_json_is_unknown_not_absent_not_available(self):
        adapter = CodexHomeAdapter([self.home_broken])
        # The broken directory itself yields no profile -- it never became an
        # account (no account_id was ever readable from it).
        self.assertEqual(adapter.profiles(), [])

        # But asked about directly (as e.g. a previously-known profile would
        # be), it must read as Unknown -- "this account's state could not be
        # read" -- never as an absent profile silently reinterpreted as
        # Available.
        from eggswap.core.types import Profile

        ghost = Profile(
            provider=Provider.CODEX,
            account_id="acct-broken",
            metadata={"codex_home": str(self.home_broken)},
        )
        availability = adapter.availability(ghost)
        self.assertIsInstance(availability, Unknown)
        self.assertFalse(availability.schedulable)

    def test_unparseable_auth_json_is_also_unknown(self):
        home = self.root / "acct-corrupt"
        home.mkdir()
        (home / "auth.json").write_text("{not valid json")

        adapter = CodexHomeAdapter([home])
        # Unparseable JSON means no account_id could be read -- no profile,
        # exactly like the missing-file case, not a crash.
        self.assertEqual(adapter.profiles(), [])

    def test_apikey_home_labelled_and_not_auto_selected_under_default_policy(self):
        adapter = CodexHomeAdapter([self.home_a, self.home_b, self.home_apikey])
        profiles = {p.account_id: p for p in adapter.profiles()}

        self.assertTrue(profiles["acct-apikey"].is_api_key)
        self.assertFalse(profiles["acct-a"].is_api_key)
        self.assertFalse(profiles["acct-b"].is_api_key)

        now = 1_000_000.0
        candidates = [
            Candidate(
                profile=profiles["acct-a"],
                availability=Available(windows=(), observed_at=now),
                score=90.0,
            ),
            Candidate(
                profile=profiles["acct-b"],
                availability=Available(windows=(), observed_at=now),
                score=80.0,
            ),
            Candidate(
                profile=profiles["acct-apikey"],
                availability=Available(windows=(), observed_at=now),
                score=100.0,  # best score, but must still be excluded
            ),
        ]

        ranked = rank(candidates, Policy(), now=now)

        selected_ids = [c.profile.account_id for c in ranked]
        self.assertNotIn("acct-apikey", selected_ids)
        self.assertEqual(set(selected_ids), {"acct-a", "acct-b"})

    def test_rotation_exhausts_best_then_falls_to_second(self):
        """The actual rotation claim: most-headroom wins, then its sibling
        wins once the first is exhausted. Not a smoke check."""
        now = 2_000_000.0
        # used_percent, not headroom: acct-a is used LESS (more headroom).
        headroom = {"acct-a": 20.0, "acct-b": 70.0}

        def rate_limit_reader(profile):
            used = headroom[profile.account_id]
            from eggswap.core.types import QuotaWindow

            return Available(
                windows=(
                    QuotaWindow(
                        bucket="primary",
                        used_percent=used,
                        window_seconds=3600,
                        resets_at=now + 3600,
                        observed_at=now,
                    ),
                ),
                observed_at=now,
            )

        adapter = CodexHomeAdapter(
            [self.home_a, self.home_b], rate_limit_reader=rate_limit_reader
        )
        profiles = adapter.profiles()

        def build_candidates():
            return [
                Candidate(
                    profile=p,
                    availability=adapter.availability(p),
                    score=_score(adapter.availability(p)),
                )
                for p in profiles
            ]

        policy = Policy()
        first = select(build_candidates(), policy, now=now)
        self.assertEqual(first.profile.account_id, "acct-a")

        # Exhaust acct-a: its own siblings become ineligible once its
        # availability reads Exhausted, so the second call must land on b.
        from eggswap.core.types import Exhausted

        def rate_limit_reader_after_exhaustion(profile):
            if profile.account_id == "acct-a":
                return Exhausted(reset_at=now + 3600, bucket="primary", observed_at=now)
            return rate_limit_reader(profile)

        adapter2 = CodexHomeAdapter(
            [self.home_a, self.home_b],
            rate_limit_reader=rate_limit_reader_after_exhaustion,
        )
        profiles2 = adapter2.profiles()
        candidates2 = [
            Candidate(
                profile=p,
                availability=adapter2.availability(p),
                score=_score(adapter2.availability(p)),
            )
            for p in profiles2
        ]
        second = select(candidates2, policy, now=now)
        self.assertEqual(second.profile.account_id, "acct-b")

    def test_no_token_leak_in_any_repr(self):
        adapter = CodexHomeAdapter([self.home_a, self.home_b, self.home_apikey])
        profiles = adapter.profiles()
        blobs = [repr(p) for p in profiles]
        for p in profiles:
            blobs.append(repr(adapter.availability(p)))
        blobs.append(repr(adapter.launch_env(profiles[0], {})))

        joined = "\n".join(blobs)
        for token in (_FAKE_TOKEN_A, _FAKE_TOKEN_B, _FAKE_TOKEN_C, "sk-FAKE-NOT-REAL"):
            self.assertNotIn(token, joined)

    def test_no_capacity_when_all_exhausted(self):
        # Sanity: the selector's failure path is exercised too, not only its
        # success path.
        from eggswap.core.types import Exhausted

        now = 3_000_000.0
        profiles = CodexHomeAdapter([self.home_a, self.home_b]).profiles()
        candidates = [
            Candidate(
                profile=p,
                availability=Exhausted(reset_at=now + 60, bucket="primary", observed_at=now),
                score=0.0,
            )
            for p in profiles
        ]
        with self.assertRaises(NoCapacity):
            select(candidates, Policy(), now=now)


if __name__ == "__main__":
    unittest.main()


class CredentialStoreModeTests(unittest.TestCase):
    """CODEX_HOME is the account boundary ONLY where the store is file-backed.

    Raised by a peer session reviewing the same provider: Codex supports
    `cli_auth_credentials_store` = file | keyring | auto | ephemeral, and an
    admin policy can override the config. The shipped 0.156.1 binary really
    does carry a keyring path. On a keyring-backed install the credential does
    not live under CODEX_HOME, so two directories are not two isolated
    accounts, and a tool that assumes otherwise will cheerfully "rotate"
    between two views of ONE account.

    eggswap cannot prove an unmeasured keyring mapping, but it must not hide
    that limit: managed settings are inspected, conflicts become "unknown",
    and multi-home selection refuses an unverified store.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        preferences = mock.patch(
            "eggswap.adapters.codex_home._managed_preferences", return_value={}
        )
        preferences.start()
        self.addCleanup(preferences.stop)

    def _home(self, name, *, auth=None, config=None):
        home = self.root / name
        home.mkdir()
        if auth is not None:
            (home / "auth.json").write_text(json.dumps(auth))
        if config is not None:
            (home / "config.toml").write_text(config)
        return home

    def _full_auth(self, acct):
        return {
            "auth_mode": "chatgpt",
            "tokens": {
                "account_id": acct,
                "access_token": "NOT-A-REAL-TOKEN",
                "refresh_token": "NOT-A-REAL-TOKEN",
                "id_token": "NOT-A-REAL-TOKEN",
            },
        }

    def _store_of(self, home):
        adapter = CodexHomeAdapter([home])
        profiles = adapter.profiles()
        self.assertEqual(len(profiles), 1)
        return profiles[0].metadata["credentials_store"]

    def test_tokens_present_and_no_config_reads_as_file(self):
        home = self._home("a", auth=self._full_auth("acct-file"))
        self.assertEqual(self._store_of(home), "file")

    def test_declared_keyring_is_reported_not_swallowed(self):
        home = self._home(
            "b",
            auth=self._full_auth("acct-keyring"),
            config='cli_auth_credentials_store = "keyring"\n',
        )
        self.assertEqual(self._store_of(home), "keyring")

    def test_declared_file_but_no_tokens_is_unknown_not_file(self):
        """Observable state beats the declared setting.

        A config that SAYS file while auth.json carries no tokens is not a
        file-backed store; calling it one would manufacture exactly the
        isolation guarantee this test exists to withhold.
        """
        home = self._home(
            "c",
            auth={"auth_mode": "chatgpt", "tokens": {"account_id": "acct-empty"}},
            config='cli_auth_credentials_store = "file"\n',
        )
        self.assertEqual(self._store_of(home), "unknown")

    def test_ephemeral_and_auto_are_reported_verbatim(self):
        for mode in ("ephemeral", "auto"):
            with self.subTest(mode=mode):
                home = self._home(
                    f"h-{mode}",
                    auth=self._full_auth(f"acct-{mode}"),
                    config=f'cli_auth_credentials_store = "{mode}"\n',
                )
                self.assertEqual(self._store_of(home), mode)
