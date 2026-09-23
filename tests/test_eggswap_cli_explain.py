"""`--explain` must actually reach the decision record, and `--pin` must refuse.

eggswap/core/decision.py records why a profile was chosen and why each other
was refused -- and for one commit it was reachable from nothing. That is the
built-and-unwired shape this project has already been caught in twice: the
lease primitive that `eggswap run` never acquired, and the quarantine nothing
called. A record a human cannot reach audits nothing.

The first attempt at this wiring also shipped a flag that PARSED and did
nothing: argparse accepted `--explain`, the call site never forwarded it, and
the command printed its ordinary one-line answer. That failure is invisible
to anyone who only checks that the flag exists, so these tests assert the
OUTPUT, never the signature.
"""
from __future__ import annotations

import json
import unittest

from eggswap import cli
from eggswap.core.types import (
    AuthDead, Available, Profile, Provider, QuotaWindow, Unknown,
)

NOW = 1_000_000.0


class _Adapter:
    provider = Provider.CLAUDE

    def __init__(self, rows):
        self._rows = rows

    def profiles(self):
        return [p for p, _ in self._rows]

    def availability(self, profile, *, max_age_seconds=300.0, model=None):
        return dict((p.key, a) for p, a in self._rows)[profile.key]

    def launch_argv(self, profile, args):
        return ["cswap", "run", profile.account_id, "--", *args]


class _Out:
    def __init__(self):
        self.parts = []

    def write(self, s):
        self.parts.append(s)

    @property
    def text(self):
        return "".join(self.parts)


def _healthy(used):
    return Available(
        windows=(QuotaWindow(bucket="fiveHour", used_percent=used, window_seconds=18000,
                             resets_at=NOW + 18000, observed_at=NOW),),
        observed_at=NOW,
    )


class ExplainReachesTheRecordTests(unittest.TestCase):
    def setUp(self):
        self.adapter = _Adapter([
            (Profile(Provider.CLAUDE, "ok"), _healthy(5.0)),
            (Profile(Provider.CLAUDE, "dead"), AuthDead(reason="relogin_required", observed_at=NOW)),
            (Profile(Provider.CLAUDE, "stale"), Unknown(stale_since=NOW - 543, reason="stale usage")),
        ])

    def _run(self, argv):
        out = _Out()
        rc = cli.main(argv, adapters=[self.adapter], out=out, now=NOW, store=False)
        return rc, out.text

    def test_plain_select_is_unchanged_by_all_of_this(self):
        rc, text = self._run(["select"])
        self.assertEqual(rc, 0)
        self.assertEqual(text.strip(), "claude:ok")

    def test_explain_names_every_refusal_with_its_reason(self):
        rc, text = self._run(["select", "--explain"])
        self.assertEqual(rc, 0)
        self.assertIn("claude:ok", text)
        self.assertIn("claude:dead", text)
        self.assertIn("AuthDead", text)
        self.assertIn("claude:stale", text)
        self.assertIn("543", text, "an Unknown must carry its AGE, not just its name")

    def test_explain_json_carries_the_full_record(self):
        rc, text = self._run(["select", "--explain", "--json"])
        self.assertEqual(rc, 0)
        payload = json.loads(text)
        self.assertEqual(payload["profile"], "claude:ok")
        self.assertEqual(len(payload["considered"]), 3)
        self.assertEqual({r["profile"] for r in payload["refused"]},
                         {"claude:dead", "claude:stale"})
        self.assertTrue(payload["rationale"])

    def test_explain_output_DIFFERS_from_plain_select(self):
        """The control that catches a flag which parses and does nothing.

        The first version of this wiring forwarded nothing from the call
        site, so --explain printed the ordinary one-line answer and looked
        fine to anyone checking only that the flag was accepted.
        """
        _, plain = self._run(["select"])
        _, explained = self._run(["select", "--explain"])
        self.assertNotEqual(plain.strip(), explained.strip())

    def test_pin_on_an_ineligible_profile_refuses_and_does_not_fall_back(self):
        rc, text = self._run(["select", "--pin", "claude:dead"])
        self.assertEqual(rc, 3)
        self.assertNotIn("claude:ok", text, "the pin silently fell back")
        self.assertIn("claude:dead", text)

    def test_the_refusal_message_is_not_double_prefixed(self):
        rc, text = self._run(["select", "--pin", "claude:dead"])
        self.assertEqual(text.count("no schedulable profile"), 1)

    def test_pin_on_an_eligible_profile_wins(self):
        rc, text = self._run(["select", "--pin", "claude:ok"])
        self.assertEqual(rc, 0)
        self.assertIn("claude:ok", text)
