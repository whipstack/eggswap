"""Tests for eggswap.adapters.codex_home.CodexHomeAdapter.

Run: python3 -m unittest tests.test_eggswap_codex_adapter -v
"""
from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from eggswap.adapters.codex_home import CodexHomeAdapter, _managed_preferences, _store_mode
from eggswap.core.select import Policy, select
from eggswap.core.types import Available, Candidate, NoCapacity, Profile, Provider, Unknown

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
        preferences = mock.patch(
            "eggswap.adapters.codex_home._managed_preferences", return_value={}
        )
        preferences.start()
        self.addCleanup(preferences.stop)

    def _store_probe(self, home):
        return _store_mode(
            home,
            system_policy_dir=self.root / "no-system-policy",
            managed_preferences={},
        )

    def test_conflicting_system_and_user_store_settings_are_unknown(self):
        home = self.root / "home"
        _write_auth(home, **_chatgpt_auth("acct-a"))
        (home / "config.toml").write_text('cli_auth_credentials_store = "file"\n')
        policy_dir = self.root / "system"
        policy_dir.mkdir()
        (policy_dir / "requirements.toml").write_text(
            'cli_auth_credentials_store = "keyring"\n'
        )

        mode = _store_mode(home, system_policy_dir=policy_dir, managed_preferences={})

        self.assertEqual(mode, "unknown")

    def test_conflicting_mdm_and_system_requirements_are_unknown(self):
        home = self.root / "home"
        _write_auth(home, **_chatgpt_auth("acct-a"))
        policy_dir = self.root / "system"
        policy_dir.mkdir()
        (policy_dir / "requirements.toml").write_text(
            'cli_auth_credentials_store = "keyring"\n'
        )

        mode = _store_mode(
            home,
            system_policy_dir=policy_dir,
            managed_preferences={
                "requirements_toml_base64": 'cli_auth_credentials_store = "ephemeral"\n'
            },
        )

        self.assertEqual(mode, "unknown")

    def test_mdm_requirement_reports_non_file_store(self):
        home = self.root / "home"
        _write_auth(home, **_chatgpt_auth("acct-a"))

        mode = _store_mode(
            home,
            system_policy_dir=self.root / "no-system-policy",
            managed_preferences={
                "requirements_toml_base64": 'cli_auth_credentials_store = "keyring"\n'
            },
        )

        self.assertEqual(mode, "keyring")

    def test_mdm_payload_is_decoded_without_emitting_its_contents(self):
        payload = 'cli_auth_credentials_store = "ephemeral"\n'
        encoded = base64.b64encode(payload.encode()).decode()
        with mock.patch("eggswap.adapters.codex_home.sys.platform", "darwin"):
            with mock.patch("eggswap.adapters.codex_home.subprocess.run") as run:
                run.side_effect = [
                    mock.Mock(returncode=0, stdout=encoded),
                    mock.Mock(returncode=1, stdout=""),
                ]
                preferences = _managed_preferences()

        self.assertEqual(preferences, {"requirements_toml_base64": payload})

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

    def test_unverified_keyring_home_is_unknown_in_a_multi_home_set(self):
        file_home = self.root / "file-home"
        keyring_home = self.root / "keyring-home"
        _write_auth(file_home, **_chatgpt_auth("acct-file"))
        _write_auth(keyring_home, **_chatgpt_auth("acct-keyring"))
        (keyring_home / "config.toml").write_text(
            'cli_auth_credentials_store = "keyring"\n'
        )
        reader_calls = []
        sentinel = Available(windows=(), observed_at=1.0)

        def reader(profile):
            reader_calls.append(profile.account_id)
            return sentinel

        adapter = CodexHomeAdapter(
            [file_home, keyring_home],
            rate_limit_reader=reader,
            store_mode_reader=self._store_probe,
        )
        profiles = {profile.account_id: profile for profile in adapter.profiles()}

        keyring_availability = adapter.availability(profiles["acct-keyring"])
        file_availability = adapter.availability(profiles["acct-file"])

        self.assertIsInstance(keyring_availability, Unknown)
        self.assertIn("account isolation is not established", keyring_availability.reason)
        self.assertIs(file_availability, sentinel)
        self.assertEqual(reader_calls, ["acct-file"])
        chosen = select(
            [
                Candidate(profiles["acct-keyring"], keyring_availability, score=100.0),
                Candidate(profiles["acct-file"], file_availability, score=1.0),
            ],
            Policy(allow_providers=(Provider.CODEX,)),
            now=1.0,
        )
        self.assertEqual(chosen.profile.account_id, "acct-file")
        with self.assertRaises(NoCapacity):
            select(
                [Candidate(profiles["acct-keyring"], keyring_availability, score=100.0)],
                Policy(allow_providers=(Provider.CODEX,)),
                now=1.0,
            )

    def test_single_keyring_home_can_use_its_capacity_reader(self):
        home = self.root / "keyring-home"
        _write_auth(home, **_chatgpt_auth("acct-keyring"))
        (home / "config.toml").write_text('cli_auth_credentials_store = "keyring"\n')
        sentinel = Available(windows=(), observed_at=1.0)
        adapter = CodexHomeAdapter(
            [home],
            rate_limit_reader=lambda profile: sentinel,
            store_mode_reader=self._store_probe,
        )

        availability = adapter.availability(adapter.profiles()[0])

        self.assertIs(availability, sentinel)

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
