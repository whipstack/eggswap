"""Tests for eggswap.adapters.claude_cswap.ClaudeCswapAdapter.

FIXTURE_JSON below is a real (redacted only by trimming to 3 accounts),
directly-captured `cswap list --json` sample from this machine, taken
2026-09-23. Accounts 2 and 3 are genuine relogin_required accounts -- the
negative-cache incident in eggswap/core/types.py is about exactly this shape
of frozen `lastGoodUsage`, so the fixture keeps it instead of a synthesized
stand-in.
"""
from __future__ import annotations

import json
import subprocess
import unittest

from eggswap.adapters.claude_cswap import ClaudeCswapAdapter
from eggswap.core.types import Available, AuthDead, Exhausted, Profile, Provider, Unknown

FIXTURE_JSON = json.dumps(
    {
        "schemaVersion": 1,
        "activeAccountNumber": 4,
        "accounts": [
            {
                "number": 1,
                "email": "alpha@example.com",
                "organizationName": "alpha@example.com's Organization",
                "isOrganization": True,
                "active": False,
                "usageStatus": "ok",
                "usage": {
                    "fiveHour": {
                        "pct": 0.0,
                        "resetsAt": "2026-09-23T18:09:59.898912+00:00",
                        "countdown": "4h 41m",
                        "clock": "20:09",
                    },
                    "sevenDay": {
                        "pct": 0.0,
                        "resetsAt": "2026-09-29T15:59:59.898938+00:00",
                        "countdown": "6d 2h",
                        "clock": "Sep 29 17:59",
                    },
                    "scoped": [
                        {
                            "pct": 0.0,
                            "resetsAt": "2026-09-29T16:00:00+00:00",
                            "countdown": "6d 2h",
                            "clock": "Sep 29 18:00",
                            "name": "Fable",
                        }
                    ],
                },
                "usageFetchedAt": "2026-09-23T13:21:42Z",
                "usageAgeSeconds": 386.9,
            },
            {
                "number": 2,
                "email": "bravo@example.com",
                "organizationName": "bravo@example.com's Organization",
                "isOrganization": True,
                "active": False,
                "usageStatus": "relogin_required",
                "usage": None,
                "lastGoodUsage": {
                    "fiveHour": {"pct": 0.0},
                    "sevenDay": {
                        "pct": 96.0,
                        "resetsAt": "2026-09-19T05:59:59.754120+00:00",
                        "countdown": "0m",
                        "clock": "Sep 19 07:59",
                    },
                },
                "lastGoodFetchedAt": "2026-09-16T19:38:48Z",
                "lastGoodAgeSeconds": 582560.1,
            },
            {
                "number": 3,
                "email": "worker@example.com",
                "organizationName": "Example Org",
                "isOrganization": True,
                "active": False,
                "usageStatus": "relogin_required",
                "usage": None,
                "lastGoodUsage": {
                    "fiveHour": {"pct": 0.0},
                    "sevenDay": {
                        "pct": 100.0,
                        "resetsAt": "2026-09-23T05:59:59.578378+00:00",
                        "countdown": "0m",
                        "clock": "07:59",
                    },
                    "spend": {"used": 21.27, "limit": 20.0, "pct": 100.0, "currency": "EUR"},
                },
                "lastGoodFetchedAt": "2026-09-22T20:32:44Z",
                "lastGoodAgeSeconds": 60924.2,
            },
            {
                "number": 4,
                "email": "charlie@example.com",
                "organizationName": "charlie@example.com's Organization",
                "isOrganization": True,
                "active": True,
                "usageStatus": "ok",
                "usage": {
                    "fiveHour": {
                        "pct": 4.0,
                        "resetsAt": "2026-09-23T17:59:59.842562+00:00",
                        "countdown": "4h 31m",
                        "clock": "19:59",
                    },
                    "sevenDay": {
                        "pct": 2.0,
                        "resetsAt": "2026-09-30T07:59:59.842587+00:00",
                        "countdown": "6d 18h",
                        "clock": "Sep 30 09:59",
                    },
                    "scoped": [
                        {
                            "pct": 0.0,
                            "resetsAt": "2026-09-30T08:00:00+00:00",
                            "countdown": "6d 18h",
                            "clock": "Sep 30 10:00",
                            "name": "Fable",
                        }
                    ],
                },
                "usageFetchedAt": "2026-09-23T13:27:25Z",
                "usageAgeSeconds": 43.0,
            },
            {
                "number": 5,
                "email": "delta@example.com",
                "organizationName": "delta@example.com's Organization",
                "isOrganization": True,
                "active": False,
                "usageStatus": "ok",
                "usage": {
                    "fiveHour": {
                        "pct": 13.0,
                        "resetsAt": "2026-09-23T13:50:00.143888+00:00",
                        "countdown": "21m",
                        "clock": "15:50",
                    },
                    "sevenDay": {
                        "pct": 44.0,
                        "resetsAt": "2026-09-28T14:00:00.143908+00:00",
                        "countdown": "5d 0h",
                        "clock": "Sep 28 16:00",
                    },
                    "scoped": [
                        {
                            "pct": 59.0,
                            "resetsAt": "2026-09-28T14:00:00.144067+00:00",
                            "countdown": "5d 0h",
                            "clock": "Sep 28 16:00",
                            "name": "Fable",
                        }
                    ],
                },
                # 6 days old on purpose: this is the frozen-reading regression case.
                "usageFetchedAt": "2026-09-17T13:25:22Z",
                "usageAgeSeconds": 6 * 86400.0,
            },
        ],
    }
)

NOW = 1790170200.0  # fixed instant, 2026-09-23T13:30:00Z; tests never touch the wall clock


def _fake_result(stdout: str = FIXTURE_JSON, returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["cswap", "list", "--json"], returncode=returncode, stdout=stdout, stderr="")


def _runner_returning(result: subprocess.CompletedProcess):
    def _runner(*args, **kwargs):
        return result
    return _runner


def _raising_runner(exc: Exception):
    def _runner(*args, **kwargs):
        raise exc
    return _runner


class ProfilesTests(unittest.TestCase):
    def test_profiles_lists_every_account_including_auth_dead_ones(self):
        adapter = ClaudeCswapAdapter(runner=_runner_returning(_fake_result()), clock=lambda: NOW)
        profiles = adapter.profiles()
        self.assertEqual([p.account_id for p in profiles], ["1", "2", "3", "4", "5"])
        self.assertEqual(profiles[0].provider, Provider.CLAUDE)
        self.assertEqual(profiles[3].label, "charlie@example.com")

    def test_profiles_empty_on_transport_failure(self):
        adapter = ClaudeCswapAdapter(runner=_raising_runner(OSError("no such file")), clock=lambda: NOW)
        self.assertEqual(adapter.profiles(), [])


class AvailabilityTests(unittest.TestCase):
    def _adapter(self, result=None, runner=None):
        if runner is not None:
            return ClaudeCswapAdapter(runner=runner, clock=lambda: NOW)
        return ClaudeCswapAdapter(runner=_runner_returning(result or _fake_result()), clock=lambda: NOW)

    def test_ok_status_maps_to_available_with_all_windows(self):
        adapter = self._adapter()
        profile = Profile(provider=Provider.CLAUDE, account_id="4")
        availability = adapter.availability(profile)
        self.assertIsInstance(availability, Available)
        self.assertTrue(availability.schedulable)
        buckets = {w.bucket: w.used_percent for w in availability.windows}
        self.assertEqual(buckets, {"fiveHour": 4.0, "sevenDay": 2.0, "Fable": 0.0})
        self.assertEqual({w.bucket for w in availability.scheduling_windows},
                         {"fiveHour", "sevenDay"})
        self.assertEqual(availability.windows[2].model_scope, "Fable")
        for w in availability.windows:
            self.assertEqual(w.observed_at, availability.observed_at)

    def test_requested_model_adds_only_its_scoped_window_to_binding(self):
        adapter = self._adapter()
        profile = Profile(provider=Provider.CLAUDE, account_id="4")
        availability = adapter.availability(profile, model="Fable")

        self.assertIsInstance(availability, Available)
        self.assertEqual({w.bucket for w in availability.scheduling_windows},
                         {"fiveHour", "sevenDay", "Fable"})

    def test_relogin_required_maps_to_auth_dead_not_the_frozen_numbers(self):
        adapter = self._adapter()
        for account_id in ("2", "3"):
            profile = Profile(provider=Provider.CLAUDE, account_id=account_id)
            availability = adapter.availability(profile)
            self.assertIsInstance(availability, AuthDead)
            self.assertFalse(availability.schedulable)
            self.assertTrue(availability.needs_human_login)

    def test_stale_usage_is_unknown_not_available(self):
        """The 6-day-old 44% sevenDay reading (account 5) must never read as Available."""
        adapter = self._adapter()
        profile = Profile(provider=Provider.CLAUDE, account_id="5")
        availability = adapter.availability(profile, max_age_seconds=300.0)
        self.assertIsInstance(availability, Unknown)
        self.assertFalse(availability.schedulable)
        self.assertNotIsInstance(availability, Available)

    def test_fresh_usage_within_max_age_is_available(self):
        adapter = self._adapter()
        profile = Profile(provider=Provider.CLAUDE, account_id="1")
        availability = adapter.availability(profile, max_age_seconds=600.0)
        self.assertIsInstance(availability, Available)

    def test_bucket_at_100_percent_is_exhausted(self):
        result = _fake_result(
            json.dumps(
                {
                    "accounts": [
                        {
                            "number": 9,
                            "email": "x@example.com",
                            "usageStatus": "ok",
                            "usage": {
                                "fiveHour": {
                                    "pct": 100.0,
                                    "resetsAt": "2026-09-24T00:00:00+00:00",
                                },
                                "sevenDay": {"pct": 10.0, "resetsAt": "2026-09-30T00:00:00+00:00"},
                            },
                            "usageFetchedAt": "2026-09-23T13:27:25Z",
                            "usageAgeSeconds": 5.0,
                        }
                    ]
                }
            )
        )
        adapter = self._adapter(result=result)
        profile = Profile(provider=Provider.CLAUDE, account_id="9")
        availability = adapter.availability(profile)
        self.assertIsInstance(availability, Exhausted)
        self.assertFalse(availability.schedulable)
        self.assertEqual(availability.bucket, "fiveHour")
        self.assertIsNotNone(availability.reset_at)

    def _custom_usage_result(
        self, *, scoped=None, five_hour_pct=4,
        fetched_at="2026-09-23T13:27:25Z", age_seconds=5,
    ):
        account = {
            "number": 9,
            "usageStatus": "ok",
            "usage": {
                "fiveHour": {"pct": five_hour_pct},
                "sevenDay": {"pct": 10},
                "scoped": scoped if scoped is not None else [],
            },
            "usageFetchedAt": fetched_at,
            "usageAgeSeconds": age_seconds,
        }
        return _fake_result(json.dumps({"accounts": [account]}))

    def test_malformed_percentage_data_maps_to_unknown(self):
        values = ("not-a-number", float("nan"), float("inf"), 10**4000, 101, -1, True)
        for index, value in enumerate(values):
            with self.subTest(case=index):
                availability = self._adapter(result=self._custom_usage_result(five_hour_pct=value)).availability(
                    Profile(provider=Provider.CLAUDE, account_id="9")
                )
                self.assertIsInstance(availability, Unknown)
                self.assertFalse(availability.schedulable)

    def test_malformed_scoped_name_maps_to_unknown(self):
        for index, name in enumerate((None, 42, {"name": "nested"}, "  ")):
            with self.subTest(case=index):
                availability = self._adapter(
                    result=self._custom_usage_result(scoped=[{"name": name, "pct": 5}])
                ).availability(Profile(provider=Provider.CLAUDE, account_id="9"))
                self.assertIsInstance(availability, Unknown)
                self.assertFalse(availability.schedulable)

    def test_malformed_scoped_percentage_maps_to_unknown(self):
        for index, pct in enumerate(("not-a-number", float("nan"), float("inf"), 101, -1, True)):
            with self.subTest(case=index):
                availability = self._adapter(
                    result=self._custom_usage_result(scoped=[{"name": "Fable", "pct": pct}])
                ).availability(Profile(provider=Provider.CLAUDE, account_id="9"))
                self.assertIsInstance(availability, Unknown)
                self.assertFalse(availability.schedulable)

    def test_malformed_or_pre_epoch_observation_time_maps_to_unknown(self):
        values = (None, 42, {}, "not-a-timestamp", "0001-01-01T00:00:00Z")
        for index, fetched_at in enumerate(values):
            with self.subTest(case=index):
                availability = self._adapter(
                    result=self._custom_usage_result(fetched_at=fetched_at)
                ).availability(Profile(provider=Provider.CLAUDE, account_id="9"))
                self.assertIsInstance(availability, Unknown)
                self.assertFalse(availability.schedulable)

    def test_malformed_age_maps_to_unknown_and_timestamp_still_enforces_freshness(self):
        values = ("not-a-number", float("nan"), float("inf"), 10**4000, -1, True)
        for index, age in enumerate(values):
            with self.subTest(case=index):
                availability = self._adapter(
                    result=self._custom_usage_result(age_seconds=age)
                ).availability(Profile(provider=Provider.CLAUDE, account_id="9"))
                self.assertIsInstance(availability, Unknown)
                self.assertFalse(availability.schedulable)

        stale = self._adapter(
            result=self._custom_usage_result(
                fetched_at="2026-09-17T13:25:22Z", age_seconds=0,
            )
        ).availability(Profile(provider=Provider.CLAUDE, account_id="9"))
        self.assertIsInstance(stale, Unknown)
        self.assertIn("stale", stale.reason)

    def test_subprocess_failure_is_unknown(self):
        adapter = self._adapter(runner=_raising_runner(OSError("cswap not found")))
        profile = Profile(provider=Provider.CLAUDE, account_id="4")
        availability = adapter.availability(profile)
        self.assertIsInstance(availability, Unknown)
        self.assertFalse(availability.schedulable)

    def test_subprocess_timeout_is_unknown(self):
        adapter = self._adapter(
            runner=_raising_runner(subprocess.TimeoutExpired(cmd="cswap", timeout=30))
        )
        profile = Profile(provider=Provider.CLAUDE, account_id="4")
        availability = adapter.availability(profile)
        self.assertIsInstance(availability, Unknown)

    def test_unparseable_output_is_unknown(self):
        adapter = self._adapter(result=_fake_result(stdout="not json"))
        profile = Profile(provider=Provider.CLAUDE, account_id="4")
        availability = adapter.availability(profile)
        self.assertIsInstance(availability, Unknown)

    def test_nonzero_returncode_is_unknown(self):
        adapter = self._adapter(result=_fake_result(returncode=1))
        profile = Profile(provider=Provider.CLAUDE, account_id="4")
        availability = adapter.availability(profile)
        self.assertIsInstance(availability, Unknown)

    def test_unknown_account_id_is_unknown(self):
        adapter = self._adapter()
        profile = Profile(provider=Provider.CLAUDE, account_id="999")
        availability = adapter.availability(profile)
        self.assertIsInstance(availability, Unknown)


class LaunchArgvTests(unittest.TestCase):
    def test_launch_argv_shape(self):
        adapter = ClaudeCswapAdapter()
        profile = Profile(provider=Provider.CLAUDE, account_id="4")
        argv = adapter.launch_argv(profile, ["--print", "hello"])
        self.assertEqual(argv, ["cswap", "run", "4", "--", "--print", "hello"])


if __name__ == "__main__":
    unittest.main()
