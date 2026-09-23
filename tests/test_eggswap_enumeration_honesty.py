"""A provider whose account list cannot be READ must not simply go missing.

Found by an acceptance check rather than by a test, which is the interesting
part. `eggswap list` with cswap removed from PATH printed the Codex account
and nothing else: six Claude accounts silently vanished. Nothing UNSAFE
followed -- an account that is not enumerated is never selected -- but
answering "3 profiles" when the truthful answer is "3, and I could not see
the rest" is the frozen-cache lie one level up. An absence was rendered as a
fact.

The adapter already knew the difference: `_list_accounts` returns None for
"could not ask cswap" and [] for "cswap says there are no accounts".
`profiles()` collapsed both to [] at the last possible step.
"""
from __future__ import annotations

import subprocess
import unittest

from eggswap import cli
from eggswap.adapters.claude_cswap import ClaudeCswapAdapter

GOOD = (
    '{"schemaVersion":1,"activeAccountNumber":1,"accounts":[{"number":1,'
    '"email":"alpha@example.com","usageStatus":"ok","usageAgeSeconds":1.0,'
    '"usageFetchedAt":"2026-09-23T13:21:42Z","usage":{"fiveHour":{"pct":0.0,'
    '"resetsAt":"2026-09-23T18:09:59+00:00"}}}]}'
)


class _R:
    def __init__(self, code=0, out=""):
        self.returncode = code
        self.stdout = out
        self.stderr = ""


class _Out:
    def __init__(self):
        self.parts = []

    def write(self, s):
        self.parts.append(s)

    @property
    def text(self):
        return "".join(self.parts)


class EnumerationStatusTests(unittest.TestCase):
    def test_successful_enumeration_reports_no_failure(self):
        a = ClaudeCswapAdapter(runner=lambda *x, **k: _R(0, GOOD))
        self.assertEqual(len(a.profiles()), 1)
        self.assertIsNone(a.enumeration_status())

    def test_cswap_missing_is_reported_not_swallowed(self):
        def boom(*a, **k):
            raise FileNotFoundError("cswap")

        a = ClaudeCswapAdapter(runner=boom)
        self.assertEqual(a.profiles(), [])
        self.assertIn("could not ask cswap", a.enumeration_status() or "")

    def test_timeout_is_reported(self):
        def slow(*a, **k):
            raise subprocess.TimeoutExpired(cmd="cswap", timeout=30)

        a = ClaudeCswapAdapter(runner=slow)
        self.assertEqual(a.profiles(), [])
        self.assertIsNotNone(a.enumeration_status())

    def test_garbage_output_is_reported(self):
        a = ClaudeCswapAdapter(runner=lambda *x, **k: _R(0, "not json"))
        self.assertEqual(a.profiles(), [])
        self.assertIsNotNone(a.enumeration_status())

    def test_genuinely_zero_accounts_is_NOT_a_failure(self):
        """The distinction this whole file exists to keep.

        cswap answering "I have no accounts" is a successful read of an empty
        estate. Reporting it as unreadable would be the opposite lie.
        """
        a = ClaudeCswapAdapter(
            runner=lambda *x, **k: _R(0, '{"schemaVersion":1,"accounts":[]}')
        )
        self.assertEqual(a.profiles(), [])
        self.assertIsNone(a.enumeration_status())

    def test_recovery_clears_a_previous_failure(self):
        state = {"fail": True}

        def flaky(*a, **k):
            if state["fail"]:
                raise FileNotFoundError("cswap")
            return _R(0, GOOD)

        a = ClaudeCswapAdapter(runner=flaky)
        a.profiles()
        self.assertIsNotNone(a.enumeration_status())
        state["fail"] = False
        self.assertEqual(len(a.profiles()), 1)
        self.assertIsNone(a.enumeration_status(),
                          "a recovered provider must stop being reported as unreadable")


class CliReportsUnreadableProvidersTests(unittest.TestCase):
    def _adapter(self):
        def boom(*a, **k):
            raise FileNotFoundError("cswap")

        return ClaudeCswapAdapter(runner=boom)

    def test_list_says_the_account_list_is_unreadable(self):
        out = _Out()
        cli.main(["list"], adapters=[self._adapter()], out=out, now=1000.0, store=False)
        self.assertIn("UNKNOWN", out.text)
        self.assertIn("account list unreadable", out.text)

    def test_status_says_it_too(self):
        out = _Out()
        rc = cli.main(["status"], adapters=[self._adapter()], out=out, now=1000.0, store=False)
        self.assertIn("account list unreadable", out.text)
        self.assertEqual(rc, 3, "nothing schedulable must still exit 3")

    def test_a_healthy_provider_produces_no_warning(self):
        good = ClaudeCswapAdapter(runner=lambda *x, **k: _R(0, GOOD))
        out = _Out()
        cli.main(["list"], adapters=[good], out=out, now=1_790_000_000.0, store=False)
        self.assertNotIn("unreadable", out.text,
                         "a working provider must not be warned about")


if __name__ == "__main__":
    unittest.main()
