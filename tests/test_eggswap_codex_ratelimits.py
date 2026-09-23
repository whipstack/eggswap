"""Tests for eggswap.adapters.codex_ratelimits.AppServerRateLimitReader.

All tests use a FAKE spawn -- none launch a real `codex app-server` or touch
the network. The happy-path fixture is the redacted live reply captured in
docs/research/eggswap/codex-ratelimits-live.md section 2.
"""
from __future__ import annotations

import json
import queue
import threading
import unittest
from pathlib import Path

from eggswap.adapters.codex_ratelimits import AppServerRateLimitReader
from eggswap.core.select import Policy, select
from eggswap.core.types import Available, Candidate, Exhausted, NoCapacity, Profile, Provider, Unknown

TEST_SIZE = "medium"
TEST_SIZE_REASON = (
    "AppServerRateLimitReader reads its child's stdout on a background "
    "thread so a hung app-server can be timed out instead of blocking "
    "forever; exercising that (including the timeout test) requires threads "
    "even with a fake, in-process subprocess."
)

_INIT_REPLY = json.dumps(
    {
        "id": 1,
        "result": {
            "userAgent": "eggswap-probe/0.156.1",
            "codexHome": "/home/testuser/.codex",
            "platformFamily": "unix",
            "platformOs": "macos",
        },
    }
)

_UNSOLICITED_NOTIFICATION = json.dumps(
    {"method": "account/updated", "params": {"authMode": "chatgpt", "planType": "pro"}}
)

# Verbatim (redacted) shape from docs/research/eggswap/codex-ratelimits-live.md, section 2.
_RATE_LIMITS_REPLY = json.dumps(
    {
        "id": 2,
        "result": {
            "ordinaryUsageAllowed": True,
            "rateLimits": {
                "limitId": "codex",
                "limitName": None,
                "primary": {
                    "usedPercent": 37,
                    "windowDurationMins": 10080,
                    "resetsAt": 1790580677,
                },
                "secondary": None,
                "credits": {"hasCredits": False, "unlimited": False, "balance": "0"},
            },
            "accountId": "REDACTED",
        },
    }
)


class _FakeStdin:
    def __init__(self) -> None:
        self.writes: list[str] = []

    def write(self, data: str) -> None:
        self.writes.append(data)

    def flush(self) -> None:
        pass


class _FakeStdout:
    """Serves canned lines, then blocks forever (simulating a hang)."""

    def __init__(self, lines: list[str], *, hang_after: bool = False) -> None:
        self._lines = list(lines)
        self._hang_after = hang_after
        self._never = threading.Event()

    def readline(self) -> str:
        if self._lines:
            return self._lines.pop(0) + "\n"
        if self._hang_after:
            self._never.wait()  # blocks until process exit; thread is a daemon
        return ""  # EOF


class _FakeProcess:
    def __init__(self, lines: list[str], *, hang_after: bool = False) -> None:
        self.stdin = _FakeStdin()
        self.stdout = _FakeStdout(lines, hang_after=hang_after)
        self.kill_called = False
        self.wait_called = False

    def kill(self) -> None:
        self.kill_called = True

    def wait(self, timeout=None) -> None:
        self.wait_called = True


def _profile() -> Profile:
    return Profile(provider=Provider.CODEX, account_id="acct-1", metadata={"codex_home": "/tmp/codex-home"})


class HappyPathTest(unittest.TestCase):
    def test_produces_available_with_converted_window_and_stamped_observed_at(self) -> None:
        proc = _FakeProcess([_INIT_REPLY, _UNSOLICITED_NOTIFICATION, _RATE_LIMITS_REPLY])
        clock_values = iter([100.0, 200.0, 300.0])
        reader = AppServerRateLimitReader(
            codex_home=Path("/unused"),
            spawn=lambda *a, **k: proc,
            clock=lambda: next(clock_values),
        )

        result = reader(_profile())

        self.assertIsInstance(result, Available)
        self.assertEqual(len(result.windows), 1)
        window = result.windows[0]
        self.assertEqual(window.bucket, "codex:primary")
        self.assertEqual(window.used_percent, 37.0)
        # 10080 minutes -> seconds; a test that would catch a missing *60.
        self.assertEqual(window.window_seconds, 10080 * 60)
        self.assertEqual(window.resets_at, 1790580677.0)
        # observed_at is whatever the clock read when the reply arrived, not
        # a field parsed out of the reply itself.
        self.assertEqual(window.observed_at, result.observed_at)
        self.assertTrue(proc.kill_called)

    def test_100_percent_window_is_exhausted_and_selector_refuses_it(self) -> None:
        reply = json.loads(_RATE_LIMITS_REPLY)
        reply["result"]["rateLimits"]["primary"]["usedPercent"] = 100
        proc = _FakeProcess([_INIT_REPLY, json.dumps(reply)])
        reader = AppServerRateLimitReader(
            codex_home=Path("/unused"), spawn=lambda *a, **k: proc, clock=lambda: 42.0
        )

        result = reader(_profile())

        self.assertIsInstance(result, Exhausted)
        self.assertEqual(result.bucket, "codex:primary")
        self.assertEqual(result.reset_at, 1790580677.0)
        with self.assertRaises(NoCapacity):
            select(
                [Candidate(profile=_profile(), availability=result, score=100.0)],
                Policy(allow_providers=(Provider.CODEX,)),
                now=42.0,
            )

    def test_provider_reached_limit_signal_is_exhausted(self) -> None:
        reply = json.loads(_RATE_LIMITS_REPLY)
        reply["result"]["rateLimitReachedType"] = "primary"
        proc = _FakeProcess([_INIT_REPLY, json.dumps(reply)])
        reader = AppServerRateLimitReader(
            codex_home=Path("/unused"), spawn=lambda *a, **k: proc, clock=lambda: 42.0
        )

        result = reader(_profile())

        self.assertIsInstance(result, Exhausted)
        self.assertEqual(result.bucket, "codex:primary")
        self.assertEqual(result.reset_at, 1790580677.0)

    def test_undocumented_usage_gate_is_unknown_without_quota_evidence(self) -> None:
        reply = json.loads(_RATE_LIMITS_REPLY)
        reply["result"]["ordinaryUsageAllowed"] = False
        proc = _FakeProcess([_INIT_REPLY, json.dumps(reply)])
        reader = AppServerRateLimitReader(
            codex_home=Path("/unused"), spawn=lambda *a, **k: proc, clock=lambda: 42.0
        )

        result = reader(_profile())

        self.assertIsInstance(result, Unknown)
        self.assertIn("semantics are unverified", result.reason)

    def test_absent_secondary_produces_no_phantom_bucket(self) -> None:
        proc = _FakeProcess([_INIT_REPLY, _RATE_LIMITS_REPLY])
        reader = AppServerRateLimitReader(
            codex_home=Path("/unused"), spawn=lambda *a, **k: proc, clock=lambda: 42.0
        )

        result = reader(_profile())

        self.assertIsInstance(result, Available)
        buckets = [w.bucket for w in result.windows]
        self.assertNotIn("codex:secondary", buckets)
        self.assertEqual(buckets, ["codex:primary"])

    def test_populated_secondary_is_included(self) -> None:
        reply = json.loads(_RATE_LIMITS_REPLY)
        reply["result"]["rateLimits"]["secondary"] = {
            "usedPercent": 12,
            "windowDurationMins": 300,
            "resetsAt": 1790000000,
        }
        proc = _FakeProcess([_INIT_REPLY, json.dumps(reply)])
        reader = AppServerRateLimitReader(
            codex_home=Path("/unused"), spawn=lambda *a, **k: proc, clock=lambda: 42.0
        )

        result = reader(_profile())

        buckets = {w.bucket: w for w in result.windows}
        self.assertIn("codex:secondary", buckets)
        self.assertEqual(buckets["codex:secondary"].window_seconds, 300 * 60)

    def test_multi_bucket_map_preserves_every_bucket_and_window(self) -> None:
        reply = json.loads(_RATE_LIMITS_REPLY)
        codex = reply["result"]["rateLimits"]
        reply["result"]["rateLimitsByLimitId"] = {
            "codex": codex,
            "codex_other": {
                "limitId": "codex_other",
                "limitName": "other",
                "primary": {
                    "usedPercent": 88,
                    "windowDurationMins": 30,
                    "resetsAt": 1790000100,
                },
                "secondary": {
                    "usedPercent": 12,
                    "windowDurationMins": 1440,
                    "resetsAt": 1790000200,
                },
                "rateLimitReachedType": None,
                "spendControlReached": False,
            },
        }
        proc = _FakeProcess([_INIT_REPLY, json.dumps(reply)])
        reader = AppServerRateLimitReader(
            codex_home=Path("/unused"), spawn=lambda *a, **k: proc, clock=lambda: 42.0
        )

        result = reader(_profile())

        self.assertIsInstance(result, Available)
        windows = {window.bucket: window for window in result.windows}
        self.assertEqual(
            set(windows),
            {"codex:primary", "codex_other:primary", "codex_other:secondary"},
        )
        self.assertEqual(windows["codex_other:primary"].used_percent, 88)
        self.assertEqual(windows["codex_other:primary"].window_seconds, 30 * 60)
        self.assertEqual(windows["codex_other:primary"].resets_at, 1790000100)
        self.assertEqual(windows["codex_other:secondary"].used_percent, 12)

    def test_exhausted_additional_bucket_is_not_hidden_by_healthy_legacy_view(self) -> None:
        reply = json.loads(_RATE_LIMITS_REPLY)
        reply["result"]["rateLimitsByLimitId"] = {
            "codex": reply["result"]["rateLimits"],
            "codex_other": {
                "limitId": "codex_other",
                "primary": {
                    "usedPercent": 100,
                    "windowDurationMins": 30,
                    "resetsAt": 1790000100,
                },
                "secondary": None,
            },
        }
        proc = _FakeProcess([_INIT_REPLY, json.dumps(reply)])
        reader = AppServerRateLimitReader(
            codex_home=Path("/unused"), spawn=lambda *a, **k: proc, clock=lambda: 42.0
        )

        result = reader(_profile())

        self.assertIsInstance(result, Exhausted)
        self.assertEqual(result.bucket, "codex_other:primary")
        self.assertEqual(result.reset_at, 1790000100)
        with self.assertRaises(NoCapacity):
            select(
                [Candidate(profile=_profile(), availability=result, score=100.0)],
                Policy(allow_providers=(Provider.CODEX,)),
                now=42.0,
            )

        import io
        from eggswap.cli import main as cli_main

        class Adapter:
            provider = Provider.CODEX

            def profiles(self):
                return [_profile()]

            def availability(self, profile, *, max_age_seconds=300.0):
                return result

            def launch_env(self, profile, base_env=None):
                return {**(base_env or {}), "CODEX_HOME": "/tmp/fake-codex"}

        spawned = []
        output = io.StringIO()
        rc = cli_main(
            ["run", _profile().key, "--", "codex", "exec"],
            adapters=[Adapter()],
            out=output,
            now=42.0,
            runner=lambda argv, **kwargs: spawned.append(argv),
            store=False,
            quarantine=False,
        )
        self.assertEqual(rc, 3, output.getvalue())
        self.assertEqual(spawned, [], "an exhausted additional bucket must not spawn")

    def test_malformed_multi_bucket_entry_degrades_to_unknown(self) -> None:
        reply = json.loads(_RATE_LIMITS_REPLY)
        reply["result"]["rateLimitsByLimitId"] = {"codex_other": "not-a-snapshot"}
        proc = _FakeProcess([_INIT_REPLY, json.dumps(reply)])
        reader = AppServerRateLimitReader(
            codex_home=Path("/unused"), spawn=lambda *a, **k: proc, clock=lambda: 42.0
        )

        result = reader(_profile())

        self.assertIsInstance(result, Unknown)
        self.assertIn("malformed rateLimitsByLimitId entry", result.reason)

    def test_boolean_percentage_is_not_coerced_into_a_quota_number(self) -> None:
        reply = json.loads(_RATE_LIMITS_REPLY)
        reply["result"]["rateLimits"]["primary"]["usedPercent"] = True
        proc = _FakeProcess([_INIT_REPLY, json.dumps(reply)])
        reader = AppServerRateLimitReader(
            codex_home=Path("/unused"), spawn=lambda *a, **k: proc, clock=lambda: 42.0
        )

        result = reader(_profile())

        self.assertIsInstance(result, Unknown)
        self.assertIn("usedPercent is missing or not numeric", result.reason)


class FailureModeTest(unittest.TestCase):
    def test_spawn_failure_is_unknown(self) -> None:
        def _spawn(*a, **k):
            raise OSError("no such file: codex")

        reader = AppServerRateLimitReader(codex_home=Path("/unused"), spawn=_spawn, clock=lambda: 7.0)

        result = reader(_profile())

        self.assertIsInstance(result, Unknown)
        self.assertEqual(result.stale_since, 7.0)
        self.assertIn("spawn failed", result.reason)

    def test_handshake_failure_is_unknown(self) -> None:
        bad_init = json.dumps({"id": 1, "error": {"code": -1, "message": "nope"}})
        proc = _FakeProcess([bad_init])
        reader = AppServerRateLimitReader(
            codex_home=Path("/unused"), spawn=lambda *a, **k: proc, clock=lambda: 7.0
        )

        result = reader(_profile())

        self.assertIsInstance(result, Unknown)
        self.assertIn("initialize failed", result.reason)
        self.assertTrue(proc.kill_called)

    def test_timeout_is_unknown_and_kills_child(self) -> None:
        proc = _FakeProcess([_INIT_REPLY], hang_after=True)
        reader = AppServerRateLimitReader(
            codex_home=Path("/unused"),
            spawn=lambda *a, **k: proc,
            clock=lambda: 7.0,
            timeout_seconds=0.05,
        )

        result = reader(_profile())

        self.assertIsInstance(result, Unknown)
        self.assertIn("timed out", result.reason)
        self.assertTrue(proc.kill_called)

    def test_non_json_line_is_unknown(self) -> None:
        proc = _FakeProcess([_INIT_REPLY, "not json at all {{{"])
        reader = AppServerRateLimitReader(
            codex_home=Path("/unused"), spawn=lambda *a, **k: proc, clock=lambda: 7.0
        )

        result = reader(_profile())

        self.assertIsInstance(result, Unknown)
        self.assertIn("invalid JSON", result.reason)

    def test_error_response_is_unknown(self) -> None:
        error_reply = json.dumps({"id": 2, "error": {"code": -32000, "message": "boom"}})
        proc = _FakeProcess([_INIT_REPLY, error_reply])
        reader = AppServerRateLimitReader(
            codex_home=Path("/unused"), spawn=lambda *a, **k: proc, clock=lambda: 7.0
        )

        result = reader(_profile())

        self.assertIsInstance(result, Unknown)
        self.assertIn("app-server error", result.reason)

    def test_missing_fields_is_unknown(self) -> None:
        malformed = json.dumps({"id": 2, "result": {"rateLimits": {"limitId": "codex"}}})
        proc = _FakeProcess([_INIT_REPLY, malformed])
        reader = AppServerRateLimitReader(
            codex_home=Path("/unused"), spawn=lambda *a, **k: proc, clock=lambda: 7.0
        )

        result = reader(_profile())

        self.assertIsInstance(result, Unknown)
        self.assertIn("no populated quota windows", result.reason)


if __name__ == "__main__":
    unittest.main()
