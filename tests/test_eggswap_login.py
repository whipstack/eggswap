"""Interactive-login wiring, with fake provider CLIs and disposable homes."""
from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from eggswap import cli
from eggswap.core.codex_homes import enroll_codex_home, enrolled_codex_homes, exclusive_login
from eggswap.core.quarantine import AUTH_DEAD, Quarantine


def _fake_auth(home: Path, account_id: str) -> None:
    (home / "auth.json").write_text(json.dumps({
        "auth_mode": "chatgpt", "tokens": {"account_id": account_id},
    }), encoding="utf-8")


class LoginTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.state = self.root / "state"
        self.env = mock.patch.dict(os.environ, {
            "EGGSWAP_STATE_DIR": str(self.state),
            "EGGSWAP_CODEX_HOMES": "",
            "CODEX_HOME": "",
            "CLAUDE_CONFIG_DIR": "",
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    def _main(self, argv, runner, **kwargs):
        output = io.StringIO()
        rc = cli.main(argv, out=output, runner=runner, **kwargs)
        return rc, output.getvalue()

    def test_help_and_default_output_show_the_four_account_recipe(self):
        rc, output = self._main([], mock.Mock())
        self.assertEqual(rc, 0)
        self.assertIn("eggswap add --claude twice", output)
        self.assertIn("eggswap add --codex twice", output)
        help_text = cli._build_parser()._subparsers._group_actions[0].choices["add"].format_help()
        self.assertEqual(help_text.count("  eggswap add --claude\n"), 2)
        self.assertEqual(help_text.count("  eggswap add --codex\n"), 2)
        self.assertIn("eggswap add --codex --device-auth", help_text)

    def test_codex_add_chooses_next_free_home(self):
        base = self.root / ".local/share/eggswap"
        occupied = base / "codex-2"
        occupied.mkdir(parents=True)
        _fake_auth(occupied, "already-here")
        chosen = base / "codex-3"
        calls = []

        def runner(argv, *, env):
            calls.append((argv, env["CODEX_HOME"]))
            if argv == ["codex", "login"]:
                _fake_auth(chosen, "new-account")
            return SimpleNamespace(returncode=0)

        with mock.patch("eggswap.cli.Path.home", return_value=self.root), \
             mock.patch("eggswap.cli._default_codex_homes", return_value=[occupied]):
            rc, output = self._main(["add", "--codex"], runner)
        self.assertEqual(rc, 0, output)
        self.assertEqual(calls[0], (["codex", "login"], str(chosen.resolve())))
        self.assertEqual(enrolled_codex_homes(), [chosen.resolve()])
        self.assertEqual((occupied / "auth.json").exists(), True)
        self.assertIn("Added Codex account as codex:new-account", output)

    def test_codex_add_skips_incompatible_existing_home(self):
        base = self.root / ".local/share/eggswap"
        occupied = base / "codex-2"
        occupied.mkdir(parents=True)
        (occupied / "config.toml").write_text('cli_auth_credentials_store = "keyring"\n')
        with mock.patch("eggswap.cli.Path.home", return_value=self.root):
            self.assertEqual(cli._next_codex_home(), base / "codex-3")

    def test_codex_only_home_option_skips_provider_prompt(self):
        home = self.root / "codex-2"

        def runner(argv, *, env):
            if argv == ["codex", "login"]:
                _fake_auth(home, "second-account")
            return SimpleNamespace(returncode=0)

        with mock.patch("eggswap.cli._default_codex_homes", return_value=[]), \
             mock.patch("builtins.input", side_effect=AssertionError("unexpected prompt")):
            rc, output = self._main(["add", "--home", str(home)], runner)
        self.assertEqual(rc, 0, output)
        self.assertIn("codex:second-account", output)

    def test_claude_only_flags_are_rejected_before_login(self):
        runner = mock.Mock()
        rc, output = self._main(["add", "--claude", "--device-auth"], runner)
        self.assertEqual(rc, 2)
        self.assertIn("Codex options", output)
        runner.assert_not_called()

    def test_codex_login_uses_selected_home_and_enrolls_only_after_success(self):
        home = self.root / "codex-2"
        calls = []

        def runner(argv, *, env):
            calls.append((argv, env["CODEX_HOME"]))
            if argv == ["codex", "login"]:
                _fake_auth(home, "second-account")
            return SimpleNamespace(returncode=0)

        with mock.patch("eggswap.cli._default_codex_homes", return_value=[]):
            rc, output = self._main(["add", "--codex", "--home", str(home)], runner)
        self.assertEqual(rc, 0, output)
        self.assertEqual(calls, [
            (["codex", "login"], str(home.resolve())),
            (["codex", "login", "status"], str(home.resolve())),
        ])
        self.assertEqual((home / "config.toml").read_text(),
                         'cli_auth_credentials_store = "file"\n')
        self.assertEqual(enrolled_codex_homes(), [home.resolve()])
        self.assertIn("codex:second-account", output)
        self.assertIn("Opening Codex sign-in", output)
        with mock.patch("eggswap.cli.Path.home", return_value=self.root / "no-default"):
            self.assertIn(home.resolve(), cli._default_codex_homes())

    def test_failed_codex_login_does_not_enroll(self):
        home = self.root / "codex-2"

        def runner(argv, *, env):
            return SimpleNamespace(returncode=1)

        with mock.patch("eggswap.cli._default_codex_homes", return_value=[]):
            rc, output = self._main(["add", "--codex", "--home", str(home)], runner)
        self.assertEqual(rc, 1)
        self.assertEqual(enrolled_codex_homes(), [])
        self.assertIn("was not enrolled", output)

    def test_duplicate_codex_identity_is_refused(self):
        first = self.root / "codex-1"
        first.mkdir()
        _fake_auth(first, "same-account")
        home = self.root / "codex-2"

        def runner(argv, *, env):
            if argv == ["codex", "login"]:
                _fake_auth(home, "same-account")
            return SimpleNamespace(returncode=0)

        with mock.patch("eggswap.cli._default_codex_homes", return_value=[first]):
            rc, output = self._main(["add", "--codex", "--home", str(home)], runner)
        self.assertEqual(rc, 3)
        self.assertIn("already present", output)
        self.assertIn(str(home.resolve()), output)
        self.assertEqual(enrolled_codex_homes(), [])

    def test_existing_non_file_store_is_refused_before_login(self):
        home = self.root / "codex-2"
        home.mkdir(mode=0o700)
        (home / "config.toml").write_text('cli_auth_credentials_store = "keyring"\n')
        runner = mock.Mock()
        rc, output = self._main(["add", "--codex", "--home", str(home)], runner)
        self.assertEqual(rc, 2)
        self.assertIn("file", output)
        runner.assert_not_called()

    def test_existing_auth_is_not_overwritten(self):
        home = self.root / "codex-2"
        home.mkdir(mode=0o700)
        _fake_auth(home, "existing")
        before = (home / "auth.json").read_bytes()
        runner = mock.Mock()
        rc, output = self._main(["add", "--codex", "--home", str(home)], runner)
        self.assertEqual(rc, 2)
        self.assertIn("fresh home", output)
        self.assertEqual((home / "auth.json").read_bytes(), before)
        runner.assert_not_called()

    def test_second_concurrent_login_is_refused(self):
        home = self.root / "codex-2"
        home.mkdir(mode=0o700)
        with exclusive_login(home):
            runner = mock.Mock()
            rc, output = self._main(["add", "--codex", "--home", str(home)], runner)
        self.assertEqual(rc, 2)
        self.assertIn("already active", output)
        runner.assert_not_called()

    def test_claude_login_delegates_to_native_cli_then_cswap(self):
        calls = []

        def runner(argv, *, env, **kwargs):
            calls.append(argv)
            if argv == ["claude", "auth", "status", "--json"]:
                return SimpleNamespace(returncode=0, stdout=json.dumps({
                    "loggedIn": True, "email": "one@example.test", "orgId": "org-one",
                }))
            if argv == ["cswap", "status", "--json"]:
                return SimpleNamespace(returncode=0, stdout=json.dumps({
                    "active": {"managed": True, "number": 1, "usageStatus": "ok",
                               "email": "one@example.test", "organizationUuid": "org-one"},
                }))
            return SimpleNamespace(returncode=0)

        rc, output = self._main(["add", "--claude"], runner)
        self.assertEqual(rc, 0, output)
        self.assertEqual(calls, [["claude", "auth", "login"],
                                 ["claude", "auth", "status", "--json"],
                                 ["cswap", "add"],
                                 ["cswap", "status", "--json"]])
        self.assertIn("claude:1", output)
        self.assertIn("Opening Claude sign-in", output)

    def test_claude_refresh_names_quarantined_slot_without_clearing(self):
        quarantine = Quarantine()
        quarantine.record("claude:1", AUTH_DEAD)
        quarantine.record("claude:2", AUTH_DEAD)

        def runner(argv, *, env, **kwargs):
            if argv == ["claude", "auth", "status", "--json"]:
                return SimpleNamespace(returncode=0, stdout=json.dumps({
                    "loggedIn": True, "email": "one@example.test", "orgId": "org-one",
                }))
            if argv == ["cswap", "status", "--json"]:
                return SimpleNamespace(returncode=0, stdout=json.dumps({
                    "active": {"managed": True, "number": 1, "usageStatus": "ok",
                               "email": "one@example.test", "organizationUuid": "org-one"},
                }))
            return SimpleNamespace(returncode=0)

        rc, output = self._main(["add", "--claude"], runner, quarantine=quarantine)
        self.assertEqual(rc, 0, output)
        self.assertTrue(quarantine.is_quarantined("claude:1"))
        self.assertTrue(quarantine.is_quarantined("claude:2"))
        self.assertIn("eggswap clear claude:1", output)

    def test_failed_capture_preserves_auth_dead_quarantine(self):
        quarantine = Quarantine()
        quarantine.record("claude:1", AUTH_DEAD)

        def runner(argv, *, env, **kwargs):
            return SimpleNamespace(returncode=1 if argv == ["cswap", "add"] else 0)

        rc, _ = self._main(["add", "--claude"], runner, quarantine=quarantine)
        self.assertEqual(rc, 1)
        self.assertTrue(quarantine.is_quarantined("claude:1"))

    def test_unconfirmed_claude_slot_preserves_quarantine(self):
        quarantine = Quarantine()
        quarantine.record("claude:1", AUTH_DEAD)

        def runner(argv, *, env, **kwargs):
            if argv == ["cswap", "status", "--json"]:
                return SimpleNamespace(returncode=0, stdout='{"active":{"managed":false}}')
            return SimpleNamespace(returncode=0)

        rc, output = self._main(["add", "--claude"], runner, quarantine=quarantine)
        self.assertEqual(rc, 3, output)
        self.assertTrue(quarantine.is_quarantined("claude:1"))

    def test_claude_refresh_preserves_persisted_quarantine(self):
        path = self.state / "quarantine.json"
        quarantine = Quarantine()
        quarantine.record("claude:1", AUTH_DEAD)
        cli.save_quarantine(quarantine, path)

        def runner(argv, *, env, **kwargs):
            if argv == ["claude", "auth", "status", "--json"]:
                return SimpleNamespace(returncode=0, stdout=json.dumps({
                    "loggedIn": True, "email": "one@example.test", "orgId": "org-one",
                }))
            if argv == ["cswap", "status", "--json"]:
                return SimpleNamespace(returncode=0, stdout=json.dumps({
                    "active": {"managed": True, "number": 1, "usageStatus": "ok",
                               "email": "one@example.test", "organizationUuid": "org-one"},
                }))
            return SimpleNamespace(returncode=0)

        rc, output = self._main(["add", "--claude"], runner)
        self.assertEqual(rc, 0, output)
        self.assertTrue(cli.load_quarantine(path).is_quarantined("claude:1"))
        self.assertIn("eggswap clear claude:1", output)

    def test_still_dead_claude_slot_preserves_quarantine(self):
        quarantine = Quarantine()
        quarantine.record("claude:1", AUTH_DEAD)

        def runner(argv, *, env, **kwargs):
            if argv == ["claude", "auth", "status", "--json"]:
                return SimpleNamespace(returncode=0, stdout=json.dumps({
                    "loggedIn": True, "email": "one@example.test", "orgId": "org-one",
                }))
            if argv == ["cswap", "status", "--json"]:
                return SimpleNamespace(returncode=0, stdout=json.dumps({
                    "active": {"managed": True, "number": 1,
                               "usageStatus": "relogin_required",
                               "email": "one@example.test", "organizationUuid": "org-one"},
                }))
            return SimpleNamespace(returncode=0)

        rc, output = self._main(["add", "--claude"], runner, quarantine=quarantine)
        self.assertEqual(rc, 0, output)
        self.assertTrue(quarantine.is_quarantined("claude:1"))
        self.assertNotIn("eggswap clear", output)

    def test_concurrent_active_switch_cannot_clear_another_slot(self):
        quarantine = Quarantine()
        quarantine.record("claude:2", AUTH_DEAD)

        def runner(argv, *, env, **kwargs):
            if argv == ["claude", "auth", "status", "--json"]:
                return SimpleNamespace(returncode=0, stdout=json.dumps({
                    "loggedIn": True, "email": "one@example.test", "orgId": "org-one",
                }))
            if argv == ["cswap", "status", "--json"]:
                return SimpleNamespace(returncode=0, stdout=json.dumps({
                    "active": {"managed": True, "number": 2, "usageStatus": "ok",
                               "email": "other@example.test", "organizationUuid": "org-two"},
                }))
            return SimpleNamespace(returncode=0)

        rc, output = self._main(["add", "--claude"], runner, quarantine=quarantine)
        self.assertEqual(rc, 3, output)
        self.assertTrue(quarantine.is_quarantined("claude:2"))
        self.assertNotIn("registered claude:2", output)

    def test_missing_usage_status_does_not_clear_quarantine(self):
        quarantine = Quarantine()
        quarantine.record("claude:1", AUTH_DEAD)

        def runner(argv, *, env, **kwargs):
            if argv == ["claude", "auth", "status", "--json"]:
                return SimpleNamespace(returncode=0, stdout=json.dumps({
                    "loggedIn": True, "email": "one@example.test", "orgId": "org-one",
                }))
            if argv == ["cswap", "status", "--json"]:
                return SimpleNamespace(returncode=0, stdout=json.dumps({
                    "active": {"managed": True, "number": 1,
                               "email": "one@example.test", "organizationUuid": "org-one"},
                }))
            return SimpleNamespace(returncode=0)

        rc, output = self._main(["add", "--claude"], runner, quarantine=quarantine)
        self.assertEqual(rc, 0, output)
        self.assertTrue(quarantine.is_quarantined("claude:1"))
        self.assertNotIn("eggswap clear", output)

    def test_bare_add_prompts_for_claude(self):
        calls = []

        def runner(argv, *, env, **kwargs):
            calls.append(argv)
            if argv == ["claude", "auth", "status", "--json"]:
                return SimpleNamespace(returncode=0, stdout=json.dumps({
                    "loggedIn": True, "email": "one@example.test", "orgId": "org-one",
                }))
            if argv == ["cswap", "status", "--json"]:
                return SimpleNamespace(returncode=0, stdout=json.dumps({
                    "active": {"managed": True, "number": 1,
                               "email": "one@example.test", "organizationUuid": "org-one"},
                }))
            return SimpleNamespace(returncode=0)

        with mock.patch("builtins.input", return_value="1"):
            rc, output = self._main(["add"], runner)
        self.assertEqual(rc, 0, output)
        self.assertIn("[1] Claude", output)
        self.assertIn("Added Claude account one@example.test as claude:1", output)
        self.assertEqual(calls, [["claude", "auth", "login"],
                                 ["claude", "auth", "status", "--json"],
                                 ["cswap", "add"], ["cswap", "status", "--json"]])

    def test_bare_add_can_cancel_before_provider_login(self):
        with mock.patch("builtins.input", return_value="q"):
            runner = mock.Mock()
            rc, _ = self._main(["add"], runner)
            self.assertEqual(rc, 0)
            runner.assert_not_called()
        with mock.patch("builtins.input", side_effect=["unknown", "q"]) as prompt:
            runner = mock.Mock()
            rc, output = self._main(["add"], runner)
            self.assertEqual(rc, 0)
            self.assertIn("choose 1 for Claude, 2 for Codex", output)
            self.assertEqual(prompt.call_count, 2)
            runner.assert_not_called()
        with mock.patch("builtins.input", side_effect=KeyboardInterrupt):
            runner = mock.Mock()
            rc, _ = self._main(["add"], runner)
            self.assertEqual(rc, 130)
            runner.assert_not_called()

    def test_interrupt_during_provider_login_is_reported_without_success(self):
        def runner(argv, *, env, **kwargs):
            raise KeyboardInterrupt

        rc, output = self._main(["add", "--claude"], runner)
        self.assertEqual(rc, 130)
        self.assertIn("interrupted", output)
        self.assertNotIn("Added Claude account", output)

    def test_claude_login_refuses_session_home_before_mutation(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.root / "session")}):
            runner = mock.Mock()
            rc, output = self._main(["add", "--claude"], runner)
        self.assertEqual(rc, 2)
        self.assertIn("unset CLAUDE_CONFIG_DIR", output)
        self.assertIn("outside cswap run", output)
        runner.assert_not_called()

    def test_claude_login_refuses_secure_storage_override(self):
        with mock.patch.dict(os.environ, {"CLAUDE_SECURESTORAGE_CONFIG_DIR": str(self.root / "other") }):
            runner = mock.Mock()
            rc, output = self._main(["add", "--claude"], runner)
        self.assertEqual(rc, 2)
        self.assertIn("CLAUDE_SECURESTORAGE_CONFIG_DIR", output)
        runner.assert_not_called()

    def test_claude_login_ignores_ambient_credentials(self):
        calls = []

        def runner(argv, *, env, **kwargs):
            calls.append((argv, env))
            if argv == ["claude", "auth", "status", "--json"]:
                return SimpleNamespace(returncode=0, stdout=json.dumps({
                    "loggedIn": True, "email": "one@example.test", "orgId": "org-one",
                }))
            if argv == ["cswap", "status", "--json"]:
                return SimpleNamespace(returncode=0, stdout=json.dumps({
                    "active": {"managed": True, "number": 1,
                               "email": "one@example.test", "organizationUuid": "org-one"},
                }))
            return SimpleNamespace(returncode=0)

        with mock.patch.dict(os.environ, {
            "ANTHROPIC_API_KEY": "unused",
            "ANTHROPIC_AUTH_TOKEN": "unused",
            "CLAUDE_CODE_OAUTH_TOKEN": "unused",
            "CLAUDE_CODE_OAUTH_REFRESH_TOKEN": "unused",
            "CLAUDE_CODE_OAUTH_SCOPES": "unused",
            "CLAUDE_CODE_USE_BEDROCK": "1",
            "CLAUDE_CODE_USE_ANTHROPIC_AWS": "1",
            "ANTHROPIC_AWS_API_KEY": "unused",
            "ANTHROPIC_BASE_URL": "https://example.invalid",
            "ANTHROPIC_CUSTOM_HEADERS": "Authorization: unused",
            "ANTHROPIC_PROFILE": "other",
            "ANTHROPIC_FEDERATION_RULE_ID": "unused",
        }):
            rc, output = self._main(["add", "--claude"], runner)
        self.assertEqual(rc, 0, output)
        self.assertEqual(len(calls), 4)
        for _, env in calls:
            self.assertNotIn("ANTHROPIC_API_KEY", env)
            self.assertNotIn("ANTHROPIC_AUTH_TOKEN", env)
            self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", env)
            self.assertNotIn("CLAUDE_CODE_OAUTH_REFRESH_TOKEN", env)
            self.assertNotIn("CLAUDE_CODE_OAUTH_SCOPES", env)
            self.assertNotIn("CLAUDE_CODE_USE_BEDROCK", env)
            self.assertNotIn("CLAUDE_CODE_USE_ANTHROPIC_AWS", env)
            self.assertNotIn("ANTHROPIC_AWS_API_KEY", env)
            self.assertNotIn("ANTHROPIC_BASE_URL", env)
            self.assertNotIn("ANTHROPIC_CUSTOM_HEADERS", env)
            self.assertNotIn("ANTHROPIC_PROFILE", env)
            self.assertNotIn("ANTHROPIC_FEDERATION_RULE_ID", env)

    def test_codex_login_ignores_ambient_api_credentials(self):
        home = self.root / "codex-2"
        calls = []

        def runner(argv, *, env):
            calls.append((argv, env))
            if argv == ["codex", "login"]:
                _fake_auth(home, "second-account")
            return SimpleNamespace(returncode=0)

        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "unused", "CODEX_API_KEY": "unused", "CODEX_ACCESS_TOKEN": "unused", "OPENAI_BASE_URL": "https://example.invalid", "CODEX_CA_CERTIFICATE": str(self.root / "corporate-ca.pem")}), \
             mock.patch("eggswap.cli._default_codex_homes", return_value=[]):
            rc, output = self._main(["add", "--codex", "--home", str(home)], runner)
        self.assertEqual(rc, 0, output)
        self.assertEqual(len(calls), 2)
        for _, env in calls:
            self.assertNotIn("OPENAI_API_KEY", env)
            self.assertNotIn("CODEX_API_KEY", env)
            self.assertNotIn("CODEX_ACCESS_TOKEN", env)
            self.assertNotIn("OPENAI_BASE_URL", env)
            self.assertEqual(env["CODEX_CA_CERTIFICATE"], str(self.root / "corporate-ca.pem"))
            self.assertEqual(env["CODEX_HOME"], str(home.resolve()))

    def test_add_requires_terminal_but_help_does_not(self):
        with mock.patch.object(cli.sys, "argv", ["eggswap", "add", "--codex"]), \
             mock.patch.object(cli.sys, "stdin") as stdin, \
             mock.patch.object(cli.sys, "stderr", new_callable=io.StringIO) as stderr, \
             mock.patch.object(cli, "main") as main:
            stdin.isatty.return_value = False
            with self.assertRaises(SystemExit) as stopped:
                cli.main_entry()
            self.assertEqual(stopped.exception.code, 2)
            self.assertIn("terminal", stderr.getvalue())
            self.assertIn("eggswap add --claude", stderr.getvalue())
            self.assertIn("eggswap add --codex", stderr.getvalue())
            main.assert_not_called()
        with mock.patch.object(cli.sys, "argv", ["eggswap", "add", "--help"]), \
             mock.patch.object(cli.sys, "stdin") as stdin, \
             mock.patch.object(cli, "main", return_value=0) as main:
            stdin.isatty.return_value = False
            with self.assertRaises(SystemExit) as stopped:
                cli.main_entry()
            self.assertEqual(stopped.exception.code, 0)
            main.assert_called_once_with(["add", "--help"])

    def test_unreadable_catalog_refuses_instead_of_hiding_enrollment(self):
        self.state.mkdir()
        (self.state / "codex_homes.json").write_text("{broken")
        with self.assertRaises(ValueError):
            enrolled_codex_homes()

    def test_catalog_add_is_idempotent(self):
        home = self.root / "codex-2"
        home.mkdir()
        enroll_codex_home(home)
        enroll_codex_home(home)
        self.assertEqual(enrolled_codex_homes(), [home.resolve()])
