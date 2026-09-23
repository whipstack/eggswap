"""The handoff demo's live Codex adapter is testable without provider calls."""
from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
import json
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from eggswap.core.handoff import Checkpoint, SessionRef, continuity_violations
from eggswap.core.types import Lease, Profile, Provider, WorkSpec


DEMO_PATH = Path(__file__).resolve().parents[1] / "bin" / "eggswap-handoff-demo"
LOADER = importlib.machinery.SourceFileLoader("eggswap_handoff_demo", str(DEMO_PATH))
SPEC = importlib.util.spec_from_loader("eggswap_handoff_demo", LOADER)
demo = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(demo)


def event_stream(thread_id="native-codex-thread", text="PART_TWO_DONE"):
    return "\n".join(
        [
            json.dumps({"type": "thread.started", "thread_id": thread_id}),
            json.dumps({
                "type": "item.completed",
                "item": {"id": "item-1", "type": "agent_message", "text": text},
            }),
            json.dumps({"type": "turn.completed", "usage": {}}),
        ]
    )


class ParseCodexEventsTests(unittest.TestCase):
    def test_captures_native_thread_and_completed_message(self):
        session, output = demo._parse_codex_events(event_stream())
        self.assertEqual(session.provider, Provider.CODEX)
        self.assertEqual(session.native_id, "native-codex-thread")
        self.assertEqual(output, "PART_TWO_DONE")

    def test_missing_native_thread_is_unknown_not_a_fake_session(self):
        with self.assertRaisesRegex(ValueError, "no native thread id"):
            demo._parse_codex_events(
                json.dumps({"type": "turn.completed", "usage": {}})
            )

    def test_no_completed_turn_is_refused(self):
        stream = "\n".join(
            [
                json.dumps({"type": "thread.started", "thread_id": "t-1"}),
                json.dumps({
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "PART_TWO_DONE"},
                }),
            ]
        )
        with self.assertRaisesRegex(ValueError, "no turn.completed"):
            demo._parse_codex_events(stream)

    def test_failed_turn_is_refused(self):
        stream = "\n".join(
            [
                json.dumps({"type": "thread.started", "thread_id": "t-1"}),
                json.dumps({"type": "turn.failed", "error": {"message": "failed"}}),
            ]
        )
        with self.assertRaisesRegex(ValueError, "did not complete"):
            demo._parse_codex_events(stream)

    def test_malformed_jsonl_is_refused(self):
        with self.assertRaisesRegex(ValueError, "invalid Codex JSONL"):
            demo._parse_codex_events("not json")


class RunCodexCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.profile = Profile(Provider.CODEX, "acct-test", metadata={"codex_home": "/tmp/codex-home"})
        self.adapter = SimpleNamespace(
            launch_env=lambda _profile, base: {**dict(base), "CODEX_HOME": "/tmp/codex-home"}
        )
        self.checkpoint = Checkpoint(
            workspec=WorkSpec(
                goal_id="goal-test", contract_version="v1", spec_hash="sha256:test",
                model="sonnet", description="two part task",
            ),
            completed=(hashlib.sha256(b"PART_ONE_DONE").hexdigest(),),
            remaining=("part-two",),
            from_profile="claude:acct-source",
            created_at=1.0,
            fence=4,
        )

    def test_invocation_is_bound_read_only_and_receives_exact_checkpoint(self):
        captured = {}

        def runner(argv, **kwargs):
            captured["argv"] = argv
            captured.update(kwargs)
            captured["cwd_was_directory"] = Path(kwargs["cwd"]).is_dir()
            return SimpleNamespace(returncode=0, stdout=event_stream(), stderr="")

        session, output = demo._run_codex_checkpoint(
            self.adapter, self.profile, self.checkpoint, "PART_ONE_DONE",
            timeout_seconds=90, runner=runner, which=lambda _name: "/usr/bin/codex",
        )
        self.assertEqual(session.native_id, "native-codex-thread")
        self.assertEqual(output, "PART_TWO_DONE")
        self.assertIn("exec", captured["argv"])
        self.assertIn("--ephemeral", captured["argv"])
        self.assertIn("--json", captured["argv"])
        self.assertIn("--sandbox", captured["argv"])
        self.assertIn("read-only", captured["argv"])
        self.assertEqual(captured["env"]["CODEX_HOME"], "/tmp/codex-home")
        self.assertEqual(captured["timeout"], 90)
        self.assertTrue(captured["cwd_was_directory"])
        self.assertEqual(captured["input"][-1], "}")
        payload = json.loads(captured["input"].splitlines()[-1])
        artifact = payload["verified_artifact"]
        self.assertEqual(artifact["text"], "PART_ONE_DONE")
        self.assertEqual(artifact["sha256"], hashlib.sha256(b"PART_ONE_DONE").hexdigest())
        self.assertEqual(payload["checkpoint"]["spec_hash"], "sha256:test")
        self.assertEqual(payload["checkpoint"]["remaining"], ["part-two"])

    def test_nonzero_codex_exit_is_refused(self):
        runner = lambda *_a, **_kw: SimpleNamespace(returncode=10, stdout="", stderr="")
        with self.assertRaisesRegex(RuntimeError, "exit code 10"):
            demo._run_codex_checkpoint(
                self.adapter, self.profile, self.checkpoint, "PART_ONE_DONE",
                timeout_seconds=90, runner=runner, which=lambda _name: "/usr/bin/codex",
            )

    def test_checkpoint_cannot_claim_an_unrecorded_artifact(self):
        with self.assertRaisesRegex(ValueError, "does not match"):
            demo._checkpoint_prompt(self.checkpoint, "different content")

    def test_completion_checkpoint_records_codex_session_and_finishes_remaining_work(self):
        lease = Lease(
            lease_id="lease-2", profile=self.profile, fence=5, acquired_at=2.0,
            expires_at=100.0, workspec=self.checkpoint.workspec, holder="demo",
        )
        session = SessionRef(Provider.CODEX, "native-codex-thread")
        after = demo._complete_checkpoint(
            lease, self.checkpoint, "PART_TWO_DONE", session
        )
        self.assertEqual(after.session, session)
        self.assertEqual(after.remaining, ())
        self.assertEqual(after.generation, self.checkpoint.generation + 1)
        self.assertEqual(len(after.completed), len(self.checkpoint.completed) + 1)
        self.assertEqual(continuity_violations(self.checkpoint, after), [])

    @patch.dict(os.environ, {"OPENAI_API_KEY": "test-key", "CODEX_API_KEY": "test-key"})
    def test_subscription_launch_strips_api_key_environment_fallback(self):
        captured = {}

        def runner(argv, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(returncode=0, stdout=event_stream(), stderr="")

        demo._run_codex_checkpoint(
            self.adapter, self.profile, self.checkpoint, "PART_ONE_DONE",
            timeout_seconds=90, runner=runner, which=lambda _name: "/usr/bin/codex",
        )
        self.assertNotIn("OPENAI_API_KEY", captured["env"])
        self.assertNotIn("CODEX_API_KEY", captured["env"])

    def test_missing_cli_is_refused_without_running(self):
        def should_not_run(*_args, **_kwargs):
            self.fail("runner must not execute without a Codex binary")

        with self.assertRaisesRegex(RuntimeError, "binary not found"):
            demo._run_codex_checkpoint(
                self.adapter, self.profile, self.checkpoint, "PART_ONE_DONE",
                timeout_seconds=90, runner=should_not_run, which=lambda _name: None,
            )


if __name__ == "__main__":
    unittest.main()
