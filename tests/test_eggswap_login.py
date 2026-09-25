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

    def _main(self, argv, runner):
        output = io.StringIO()
        rc = cli.main(argv, out=output, runner=runner)
        return rc, output.getvalue()

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

    def test_codex_add_skips_incompatible_existing_home(self):
        base = self.root / ".local/share/eggswap"
        occupied = base / "codex-2"
        occupied.mkdir(parents=True)
        (occupied / "config.toml").write_text('cli_auth_credentials_store = "keyring"\n')
        with mock.patch("eggswap.cli.Path.home", return_value=self.root):
            self.assertEqual(cli._next_codex_home(), base / "codex-3")

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
        with mock.patch("eggswap.cli.Path.home", return_value=self.root / "no-default"):
            self.assertIn(home.resolve(), cli._default_codex_homes())

    def test_failed_codex_login_does_not_enroll(self):
        home = self.root / "codex-2"

        def runner(argv, *, env):
            return SimpleNamespace(returncode=1)

        with mock.patch("eggswap.cli._default_codex_homes", return_value=[]):
            rc, _ = self._main(["add", "--codex", "--home", str(home)], runner)
        self.assertEqual(rc, 1)
        self.assertEqual(enrolled_codex_homes(), [])

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

        def runner(argv, *, env):
            calls.append(argv)
            return SimpleNamespace(returncode=0)

        rc, output = self._main(["add", "--claude"], runner)
        self.assertEqual(rc, 0, output)
        self.assertEqual(calls, [["claude", "auth", "login"], ["cswap", "add"]])

    def test_claude_login_refuses_session_home_before_mutation(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.root / "session")}):
            runner = mock.Mock()
            rc, output = self._main(["add", "--claude"], runner)
        self.assertEqual(rc, 2)
        self.assertIn("unset CLAUDE_CONFIG_DIR", output)
        runner.assert_not_called()

    def test_claude_login_refuses_secure_storage_override(self):
        with mock.patch.dict(os.environ, {"CLAUDE_SECURESTORAGE_CONFIG_DIR": str(self.root / "other") }):
            runner = mock.Mock()
            rc, output = self._main(["add", "--claude"], runner)
        self.assertEqual(rc, 2)
        self.assertIn("CLAUDE_SECURESTORAGE_CONFIG_DIR", output)
        runner.assert_not_called()

    def test_claude_login_ignores_ambient_api_credentials(self):
        calls = []

        def runner(argv, *, env):
            calls.append((argv, env))
            return SimpleNamespace(returncode=0)

        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "unused", "ANTHROPIC_AUTH_TOKEN": "unused"}):
            rc, output = self._main(["add", "--claude"], runner)
        self.assertEqual(rc, 0, output)
        self.assertEqual(len(calls), 2)
        for _, env in calls:
            self.assertNotIn("ANTHROPIC_API_KEY", env)
            self.assertNotIn("ANTHROPIC_AUTH_TOKEN", env)

    def test_codex_login_ignores_ambient_api_credentials(self):
        home = self.root / "codex-2"
        calls = []

        def runner(argv, *, env):
            calls.append((argv, env))
            if argv == ["codex", "login"]:
                _fake_auth(home, "second-account")
            return SimpleNamespace(returncode=0)

        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "unused", "CODEX_API_KEY": "unused", "CODEX_ACCESS_TOKEN": "unused"}), \
             mock.patch("eggswap.cli._default_codex_homes", return_value=[]):
            rc, output = self._main(["add", "--codex", "--home", str(home)], runner)
        self.assertEqual(rc, 0, output)
        self.assertEqual(len(calls), 2)
        for _, env in calls:
            self.assertNotIn("OPENAI_API_KEY", env)
            self.assertNotIn("CODEX_API_KEY", env)
            self.assertNotIn("CODEX_ACCESS_TOKEN", env)
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
