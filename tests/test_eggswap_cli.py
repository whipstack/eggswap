"""Tests for eggswap.cli -- the acceptance surface a human actually reads.

Covers #1037 acceptance criterion 7 (a frozen percentage must never render as
fresh), AuthDead vs Exhausted distinguishability, the exact --dry-run cswap
argv, and exit code 3 when nothing is schedulable. All adapters here are
fakes: no network, no subprocess, no real account, no wall-clock dependence
(``now`` is injected throughout).
"""
from __future__ import annotations

import io
import json
import unittest
from unittest import mock

from eggswap.cli import main
from eggswap.core.types import (
    Available,
    AuthDead,
    Exhausted,
    Profile,
    Provider,
    QuotaWindow,
    Unknown,
)

NOW = 1_800_000_000.0


class FakeAdapter:
    """In-memory stand-in for ClaudeCswapAdapter / CodexHomeAdapter."""

    def __init__(self, provider, entries):
        self.provider = provider
        self._entries = list(entries)

    def profiles(self):
        return [profile for profile, _ in self._entries]

    def availability(self, profile, *, max_age_seconds=300.0):
        for candidate_profile, availability in self._entries:
            if candidate_profile.key == profile.key:
                return availability
        raise AssertionError(f"no fake availability for {profile.key}")

    def launch_argv(self, profile, claude_args):
        return ["cswap", "run", profile.account_id, "--", *claude_args]

    def launch_env(self, profile, base_env):
        env = dict(base_env)
        env["CODEX_HOME"] = f"/fake/codex-home/{profile.account_id}"
        return env


def _run(argv, adapters, *, now=NOW):
    out = io.StringIO()
    code = main(argv, adapters=adapters, out=out, now=now, store=False, quarantine=False)
    return code, out.getvalue()


class ListRenderingTests(unittest.TestCase):
    def test_stale_window_renders_unknown_not_a_number(self):
        profile = Profile(provider=Provider.CLAUDE, account_id="1", label="a@example.com")
        stale_window = QuotaWindow(
            bucket="fiveHour",
            used_percent=97.0,
            window_seconds=None,
            resets_at=None,
            observed_at=NOW - 100_000,  # far older than the 300s default max age
        )
        adapter = FakeAdapter(
            Provider.CLAUDE,
            [(profile, Available(windows=(stale_window,), observed_at=NOW - 100_000))],
        )

        code, output = _run(["list"], [adapter])

        self.assertEqual(code, 0)
        self.assertIn("UNKNOWN", output)
        self.assertNotIn("97%", output)

    def test_fresh_window_renders_the_number(self):
        profile = Profile(provider=Provider.CLAUDE, account_id="1", label="a@example.com")
        fresh_window = QuotaWindow(
            bucket="fiveHour",
            used_percent=42.0,
            window_seconds=None,
            resets_at=None,
            observed_at=NOW - 5,
        )
        adapter = FakeAdapter(
            Provider.CLAUDE,
            [(profile, Available(windows=(fresh_window,), observed_at=NOW - 5))],
        )

        code, output = _run(["list"], [adapter])

        self.assertEqual(code, 0)
        self.assertIn("42%", output)


class AuthDeadVsExhaustedTests(unittest.TestCase):
    def test_authdead_and_exhausted_are_visually_distinct(self):
        dead_profile = Profile(provider=Provider.CLAUDE, account_id="2", label="dead@example.com")
        exhausted_profile = Profile(provider=Provider.CLAUDE, account_id="3", label="tapped@example.com")
        adapter = FakeAdapter(
            Provider.CLAUDE,
            [
                (dead_profile, AuthDead(reason="refresh token dead", observed_at=NOW)),
                (exhausted_profile, Exhausted(reset_at=NOW + 3600, bucket="fiveHour", observed_at=NOW)),
            ],
        )

        code, output = _run(["list"], [adapter])
        lines = {line.split("\t", 1)[0]: line for line in output.splitlines()}

        self.assertEqual(code, 0)
        self.assertIn("re-login needed", lines[dead_profile.key])
        self.assertIn("EXHAUSTED", lines[exhausted_profile.key])
        self.assertNotIn("re-login needed", lines[exhausted_profile.key])
        self.assertNotIn("EXHAUSTED", lines[dead_profile.key])


class DryRunTests(unittest.TestCase):
    def test_run_help_explains_provider_command_without_loading_adapters(self):
        output = io.StringIO()
        with mock.patch("eggswap.cli._default_adapters", side_effect=AssertionError("loaded")):
            code = main(["run", "--help"], out=output)
        self.assertEqual(code, 0)
        self.assertIn("usage: eggswap run", output.getvalue())
        self.assertIn("Codex", output.getvalue())

    def test_dry_run_emits_exact_cswap_argv(self):
        profile = Profile(provider=Provider.CLAUDE, account_id="2", label="a@example.com")
        adapter = FakeAdapter(
            Provider.CLAUDE,
            [(profile, Available(windows=(), observed_at=NOW))],
        )

        code, output = _run(
            ["run", "claude:2", "--dry-run", "--", "echo", "hi"], [adapter]
        )

        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertEqual(payload["argv"], ["cswap", "run", "2", "--", "echo", "hi"])

    def test_child_dry_run_flag_is_forwarded_after_separator(self):
        profile = Profile(provider=Provider.CLAUDE, account_id="2", label="a@example.com")
        adapter = FakeAdapter(
            Provider.CLAUDE,
            [(profile, Available(windows=(), observed_at=NOW))],
        )
        code, output = _run(
            ["run", "claude:2", "--dry-run", "--", "--dry-run"], [adapter]
        )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["argv"],
                         ["cswap", "run", "2", "--", "--dry-run"])

    def test_dry_run_sets_codex_home_in_env_not_argv(self):
        profile = Profile(provider=Provider.CODEX, account_id="acct-9", label="acct-9")
        adapter = FakeAdapter(
            Provider.CODEX,
            [(profile, Available(windows=(), observed_at=NOW))],
        )

        code, output = _run(
            ["run", "codex:acct-9", "--dry-run", "--", "codex", "exec", "hi"], [adapter]
        )

        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertEqual(payload["argv"], ["codex", "exec", "hi"])
        self.assertEqual(payload["env"]["CODEX_HOME"], "/fake/codex-home/acct-9")


class ExitCodeTests(unittest.TestCase):
    def test_status_does_not_count_api_key_without_budget(self):
        profile = Profile(provider=Provider.CODEX, account_id="paid", label="paid", is_api_key=True)
        adapter = FakeAdapter(
            Provider.CODEX,
            [(profile, Available(windows=(), observed_at=NOW))],
        )
        status_code, status_output = _run(["status"], [adapter])
        select_code, _ = _run(["select"], [adapter])
        self.assertEqual(status_code, 3)
        self.assertIn("0 schedulable", status_output)
        self.assertEqual(select_code, 3)

    def test_exit_code_3_when_nothing_schedulable(self):
        dead_profile = Profile(provider=Provider.CLAUDE, account_id="2", label="dead@example.com")
        unknown_profile = Profile(provider=Provider.CODEX, account_id="acct-1", label="acct-1")
        adapter_claude = FakeAdapter(
            Provider.CLAUDE,
            [(dead_profile, AuthDead(reason="refresh token dead", observed_at=NOW))],
        )
        adapter_codex = FakeAdapter(
            Provider.CODEX,
            [(unknown_profile, Unknown(stale_since=NOW - 10, reason="no capacity signal"))],
        )

        status_code, status_output = _run(["status"], [adapter_claude, adapter_codex])
        select_code, select_output = _run(["select"], [adapter_claude, adapter_codex])

        self.assertEqual(status_code, 3)
        self.assertIn("0 schedulable", status_output)
        self.assertEqual(select_code, 3)
        self.assertIn("no schedulable profile", select_output)

    def test_exhausted_codex_is_not_schedulable_in_status_or_select(self):
        profile = Profile(provider=Provider.CODEX, account_id="acct-1", label="acct-1")
        adapter = FakeAdapter(
            Provider.CODEX,
            [(profile, Exhausted(reset_at=NOW + 3600, bucket="codex:primary", observed_at=NOW))],
        )

        status_code, status_output = _run(["status"], [adapter])
        select_code, select_output = _run(["select"], [adapter])

        self.assertEqual(status_code, 3)
        self.assertIn("0 schedulable", status_output)
        self.assertEqual(select_code, 3)
        self.assertIn("no schedulable profile", select_output)

    def test_exit_code_0_when_one_profile_schedulable(self):
        profile = Profile(provider=Provider.CLAUDE, account_id="1", label="a@example.com")
        adapter = FakeAdapter(
            Provider.CLAUDE,
            [(profile, Available(windows=(), observed_at=NOW))],
        )

        status_code, _ = _run(["status"], [adapter])
        select_code, select_output = _run(["select"], [adapter])

        self.assertEqual(status_code, 0)
        self.assertEqual(select_code, 0)
        self.assertEqual(select_output.strip(), "claude:1")


if __name__ == "__main__":
    unittest.main()
