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

from eggswap.adapters.codex_ratelimits import AppServerRateLimitReader, merge_rate_limit_update
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

# Verbatim (redacted) shape from docs/research/eggswap/codex-ratelimits-live.md #2.
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


def _multi_home_profile() -> Profile:
    return Profile(provider=Provider.CODEX, account_id="acct-1", metadata={
        "codex_home": "/tmp/codex-home", "codex_home_count": 2,
    })


def _store_reply_lines(*, effective="file", managed=None):
    requirements = {
        "requirements": None if managed is None else {
            "cliAuthCredentialsStore": managed,
        }
    }
    config = {"config": {"cli_auth_credentials_store": effective}, "origins": {}}
    rate = json.loads(_RATE_LIMITS_REPLY)
    rate["id"] = 4
    return [
        _INIT_REPLY,
        json.dumps({"id": 2, "result": requirements}),
        json.dumps({"id": 3, "result": config}),
        json.dumps(rate),
    ]


class HappyPathTest(unittest.TestCase):
    def test_multiple_homes_require_effective_file_store_and_then_read_capacity(self):
        proc = _FakeProcess(_store_reply_lines(effective="file"))
        reader = AppServerRateLimitReader(
            codex_home=Path("/unused"), spawn=lambda *a, **k: proc, clock=lambda: 42.0
        )

        result = reader(_multi_home_profile())

        self.assertIsInstance(result, Available)
        requests = [json.loads(line) for line in proc.stdin.writes]
        self.assertEqual(
            [request.get("method") for request in requests],
            [
                "initialize", "initialized", "configRequirements/read", "config/read",
                "account/rateLimits/read",
            ],
        )
        self.assertTrue(proc.kill_called)

    def test_managed_store_override_beats_local_effective_config(self):
        proc = _FakeProcess(_store_reply_lines(effective="file", managed="keyring"))
        reader = AppServerRateLimitReader(
            codex_home=Path("/unused"), spawn=lambda *a, **k: proc, clock=lambda: 42.0
        )

        result = reader(_multi_home_profile())

        self.assertIsInstance(result, Unknown)
        self.assertIn("observed keyring", result.reason)
        self.assertEqual(len(proc.stdin.writes), 4)  # no quota request was made

    def test_unknown_or_missing_effective_store_blocks_multiple_homes(self):
        for effective in (None, "unknown", "auto", "ephemeral", "keyring", {"bad": "shape"}):
            with self.subTest(effective=effective):
                proc = _FakeProcess(_store_reply_lines(effective=effective))
                reader = AppServerRateLimitReader(
                    codex_home=Path("/unused"), spawn=lambda *a, **k: proc, clock=lambda: 42.0
                )
                result = reader(_multi_home_profile())
                self.assertIsInstance(result, Unknown)
                self.assertNotIn("account/rateLimits/read", [
                    json.loads(line).get("method") for line in proc.stdin.writes
                ])

    def test_single_home_does_not_need_store_policy_rpc(self):
        proc = _FakeProcess([_INIT_REPLY, _RATE_LIMITS_REPLY])
        reader = AppServerRateLimitReader(
            codex_home=Path("/unused"), spawn=lambda *a, **k: proc, clock=lambda: 42.0
        )

        result = reader(_profile())

        self.assertIsInstance(result, Available)
        self.assertEqual([json.loads(line).get("method") for line in proc.stdin.writes], [
            "initialize", "initialized", "account/rateLimits/read",
        ])

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

    def test_multi_bucket_read_keeps_scoped_windows_visible_without_binding_them(self) -> None:
        reply = json.loads(_RATE_LIMITS_REPLY)
        reply["result"]["rateLimitsByLimitId"] = {
            "codex": reply["result"]["rateLimits"],
            "fable": {
                "limitId": "fable",
                "normalModelSlug": "fable",
                "primary": {
                    "usedPercent": 100,
                    "windowDurationMins": 300,
                    "resetsAt": 1790000000,
                },
            },
        }
        proc = _FakeProcess([_INIT_REPLY, json.dumps(reply)])
        reader = AppServerRateLimitReader(
            codex_home=Path("/unused"), spawn=lambda *a, **k: proc, clock=lambda: 42.0
        )

        result = reader(_profile())

        self.assertIsInstance(result, Available)
        self.assertEqual({w.bucket for w in result.windows}, {
            "codex:primary", "fable:model:fable:primary",
        })
        self.assertEqual([w.bucket for w in result.scheduling_windows], ["codex:primary"])
        self.assertEqual(result.windows[1].model_scope, "fable")

    def test_model_scoped_exhaustion_binds_only_for_that_requested_model(self) -> None:
        reply = json.loads(_RATE_LIMITS_REPLY)
        reply["result"]["rateLimitsByLimitId"] = {
            "codex": reply["result"]["rateLimits"],
            "fable": {
                "limitId": "fable",
                "normalModelSlug": "fable",
                "primary": {"usedPercent": 100, "windowDurationMins": 300},
            },
        }
        proc = _FakeProcess([_INIT_REPLY, json.dumps(reply)])
        reader = AppServerRateLimitReader(
            codex_home=Path("/unused"), spawn=lambda *a, **k: proc, clock=lambda: 42.0
        )

        result = reader(_profile(), model="Fable")

        self.assertIsInstance(result, Exhausted)
        self.assertEqual(result.bucket, "fable:model:fable:primary")
        self.assertEqual(len(result.windows), 2)

    def test_scoped_only_buckets_without_a_model_remain_unknown_and_visible(self) -> None:
        reply = {
            "id": 2,
            "result": {
                "rateLimitsByLimitId": {
                    "fable": {
                        "limitId": "fable",
                        "normalModelSlug": "fable",
                        "primary": {"usedPercent": 20},
                    }
                }
            },
        }
        proc = _FakeProcess([_INIT_REPLY, json.dumps(reply)])
        reader = AppServerRateLimitReader(
            codex_home=Path("/unused"), spawn=lambda *a, **k: proc, clock=lambda: 42.0
        )

        result = reader(_profile())

        self.assertIsInstance(result, Unknown)
        self.assertIn("no unscoped", result.reason)
        self.assertEqual([w.bucket for w in result.windows], ["fable:model:fable:primary"])

    def test_sparse_update_changes_one_bucket_and_preserves_unmentioned_values(self) -> None:
        initial = {
            "codex": {
                "limitId": "codex",
                "primary": {"usedPercent": 31, "windowDurationMins": 10080,
                            "resetsAt": 1790580677},
            },
            "fable": {
                "limitId": "fable", "normalModelSlug": "fable",
                "primary": {"usedPercent": 8, "windowDurationMins": 300,
                            "resetsAt": 1790000000},
            },
        }
        update = {
            "method": "account/rateLimits/updated",
            "params": {"rateLimits": {
                "limitId": "fable",
                "normalModelSlug": None,
                "primary": {"usedPercent": 44, "resetsAt": None},
            }},
        }

        result = merge_rate_limit_update(initial, update)

        self.assertEqual(initial["fable"]["primary"]["usedPercent"], 8)
        self.assertEqual(result["fable"]["primary"], {
            "usedPercent": 44, "windowDurationMins": 300,
            "resetsAt": None,
        })
        self.assertEqual(result["fable"]["normalModelSlug"], "fable")
        self.assertEqual(result["codex"], initial["codex"])

    def test_reader_applies_sparse_push_to_its_last_full_read(self) -> None:
        reply = json.loads(_RATE_LIMITS_REPLY)
        reply["result"]["rateLimitsByLimitId"] = {
            "codex": reply["result"]["rateLimits"],
            "fable": {
                "limitId": "fable", "normalModelSlug": "fable",
                "primary": {"usedPercent": 8, "windowDurationMins": 300,
                            "resetsAt": 1790000000},
            },
        }
        proc = _FakeProcess([_INIT_REPLY, json.dumps(reply)])
        clock_values = iter([42.0, 99.0])
        reader = AppServerRateLimitReader(
            codex_home=Path("/unused"), spawn=lambda *a, **k: proc,
            clock=lambda: next(clock_values),
        )
        self.assertIsInstance(reader(_profile()), Available)

        updated = reader.apply_notification({
            "method": "account/rateLimits/updated",
            "params": {"rateLimits": {
                "limitId": "fable",
                "primary": {"usedPercent": 44, "resetsAt": None},
            }},
        })

        self.assertIsInstance(updated, Available)
        by_bucket = {w.bucket: w for w in updated.windows}
        self.assertEqual(by_bucket["fable:model:fable:primary"].used_percent, 44)
        self.assertIsNone(by_bucket["fable:model:fable:primary"].resets_at,
                          "an explicit null reset must not make the old reset look freshly observed")
        self.assertEqual(by_bucket["fable:model:fable:primary"].observed_at, 99.0)
        self.assertIn("codex:primary", by_bucket)
        self.assertEqual(by_bucket["codex:primary"].observed_at, 42.0)
        self.assertEqual(updated.observed_at, 42.0,
                         "the untouched binding bucket must not become fresh from another bucket's push")


class FailureModeTest(unittest.TestCase):
    def test_sparse_update_before_a_full_snapshot_stays_unknown(self) -> None:
        reader = AppServerRateLimitReader(codex_home=Path("/unused"), clock=lambda: 7.0)

        result = reader.apply_notification({
            "method": "account/rateLimits/updated",
            "params": {"rateLimits": {"limitId": "codex", "primary": {"usedPercent": 1}}},
        })

        self.assertIsInstance(result, Unknown)
        self.assertFalse(result.schedulable)
        self.assertIn("before a full", result.reason)

    def test_unaddressable_sparse_update_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "no limitId"):
            merge_rate_limit_update({}, {
                "method": "account/rateLimits/updated",
                "params": {"rateLimits": {"primary": {"usedPercent": 1}}},
            })

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
        self.assertIn("missing primary window", result.reason)


if __name__ == "__main__":
    unittest.main()
