"""Tests for eggswap.adapters.codex_home.CodexHomeAdapter.

Run: python3 -m unittest tests.test_eggswap_codex_adapter -v
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from eggswap.adapters.codex_home import CodexHomeAdapter
from eggswap.core.types import Available, Profile, Provider, Unknown

_FAKE_TOKEN = "REDACTED-NOT-A-TOKEN"


def _write_auth(home: Path, **fields) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "auth.json").write_text(json.dumps(fields))


def _chatgpt_auth(account_id: str) -> dict:
    return {
        "auth_mode": "chatgpt",
        "last_refresh": "2026-09-23T00:00:00Z",
        "tokens": {
            "account_id": account_id,
            "access_token": _FAKE_TOKEN,
            "refresh_token": _FAKE_TOKEN,
            "id_token": _FAKE_TOKEN,
        },
    }


class CodexHomeAdapterTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_two_homes_two_distinct_profiles(self):
        home_a = self.root / "a"
        home_b = self.root / "b"
        _write_auth(home_a, **_chatgpt_auth("acct-a"))
        _write_auth(home_b, **_chatgpt_auth("acct-b"))

        adapter = CodexHomeAdapter([home_a, home_b])
        profiles = adapter.profiles()

        self.assertEqual(len(profiles), 2)
        ids = {p.account_id for p in profiles}
        self.assertEqual(ids, {"acct-a", "acct-b"})
        for p in profiles:
            self.assertEqual(p.provider, Provider.CODEX)
            self.assertFalse(p.is_api_key)

    def test_apikey_mode_labelled(self):
        home = self.root / "apikey"
        _write_auth(
            home,
            auth_mode="apikey",
            OPENAI_API_KEY="sk-FAKE-NOT-REAL",
            tokens={"account_id": "acct-key"},
        )

        adapter = CodexHomeAdapter([home])
        profiles = adapter.profiles()

        self.assertEqual(len(profiles), 1)
        self.assertTrue(profiles[0].is_api_key)

    def test_missing_auth_json_is_unknown(self):
        home = self.root / "empty"
        home.mkdir()

        adapter = CodexHomeAdapter([home])
        profile = Profile(
            provider=Provider.CODEX,
            account_id="acct-missing",
            metadata={"codex_home": str(home)},
        )
        availability = adapter.availability(profile)

        self.assertIsInstance(availability, Unknown)
        self.assertFalse(availability.schedulable)

    def test_missing_home_directory_yields_no_profile(self):
        home = self.root / "does-not-exist"
        adapter = CodexHomeAdapter([home])
        self.assertEqual(adapter.profiles(), [])

    def test_no_rate_limit_reader_is_unknown_not_available(self):
        home = self.root / "a"
        _write_auth(home, **_chatgpt_auth("acct-a"))

        adapter = CodexHomeAdapter([home], rate_limit_reader=None)
        profile = adapter.profiles()[0]
        availability = adapter.availability(profile)

        self.assertIsInstance(availability, Unknown)
        self.assertNotIsInstance(availability, Available)

    def test_rate_limit_reader_is_used_when_provided(self):
        home = self.root / "a"
        _write_auth(home, **_chatgpt_auth("acct-a"))
        sentinel = Available(windows=(), observed_at=1.0)

        adapter = CodexHomeAdapter([home], rate_limit_reader=lambda profile: sentinel)
        profile = adapter.profiles()[0]
        availability = adapter.availability(profile)

        self.assertIs(availability, sentinel)

    def test_model_is_forwarded_to_reader_without_model_fallback(self):
        home = self.root / "a"
        _write_auth(home, **_chatgpt_auth("acct-a"))
        calls = []

        def reader(profile, *, model=None):
            calls.append((profile.account_id, model))
            return Unknown(stale_since=12.0, reason="no matching model bucket")

        adapter = CodexHomeAdapter([home], rate_limit_reader=reader)
        profile = adapter.profiles()[0]

        result = adapter.availability(profile, model="fable")

        self.assertIsInstance(result, Unknown)
        self.assertEqual(calls, [("acct-a", "fable")])

    def test_launch_env_sets_codex_home_and_does_not_mutate_base(self):
        home = self.root / "a"
        _write_auth(home, **_chatgpt_auth("acct-a"))

        adapter = CodexHomeAdapter([home])
        profile = adapter.profiles()[0]
        base_env = {"PATH": "/usr/bin"}
        env = adapter.launch_env(profile, base_env)

        self.assertEqual(env["CODEX_HOME"], str(home))
        self.assertEqual(env["PATH"], "/usr/bin")
        self.assertEqual(base_env, {"PATH": "/usr/bin"})
        self.assertNotIn("CODEX_HOME", base_env)

    def test_no_token_leak_in_reprs(self):
        home = self.root / "a"
        _write_auth(home, **_chatgpt_auth("acct-a"))

        adapter = CodexHomeAdapter([home])
        profile = adapter.profiles()[0]
        availability = adapter.availability(profile)
        env = adapter.launch_env(profile, {})

        for blob in (repr(profile), repr(availability), repr(env)):
            self.assertNotIn(_FAKE_TOKEN, blob)


if __name__ == "__main__":
    unittest.main()
