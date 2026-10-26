"""Resolve the actual plugin manifest/hook registry, then invoke the lane-return hook over Claude JSON/stdin.

The same file runs from the public tree's tests/ directory; no provider or live session is used.
"""

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
PLUGIN = HERE.parent if (HERE.parent / "agents").is_dir() else HERE.parent / "plugins/agent-workflows"
ROLES = ("worker", "senior", "senior-escalation", "reviewer", "scout")
SCRIPT = "scripts/lane_return.py"


def registry():
    manifest = json.loads((PLUGIN / ".claude-plugin/plugin.json").read_text())
    return (PLUGIN / manifest.get("hooks", "./hooks/hooks.json")).resolve()


class LaneReturnHook(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.patch = Path(self.tmp.name) / "candidate.patch"
        self.patch.write_bytes(b"fixture candidate\n")
        self.good = {
            "v": 3, "lane": "L1", "task": "none", "status": "complete",
            "base": "a" * 40, "worktree": self.tmp.name,
            "patch": str(self.patch), "patch_sha256": hashlib.sha256(self.patch.read_bytes()).hexdigest(),
            "check": "python3 -m unittest", "exit": 0, "tail": "OK",
            "coderabbit": None, "dsreview": None, "questions": [],
        }

    def registered_command(self):
        path = registry()
        self.assertTrue(path.is_relative_to(PLUGIN.resolve()))
        entries = json.loads(path.read_text())["hooks"].get("SubagentStop", [])
        commands = [h for entry in entries for h in entry["hooks"] if SCRIPT in h["command"]]
        self.assertEqual(len(commands), 1)
        hook = commands[0]
        self.assertEqual(hook["type"], "command")
        self.assertNotIn("async", hook)
        return hook["command"]

    def run_hook(self, payload):
        result = subprocess.run(self.registered_command(), shell=True, input=json.dumps(payload), text=True,
                                capture_output=True, timeout=10,
                                env={**os.environ, "CLAUDE_PLUGIN_ROOT": str(PLUGIN)})
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout) if result.stdout.strip() else {}

    def hook(self, role, event, payload):
        return self.run_hook({"hook_event_name": event, "agent_id": "fixture-lane-1",
                              "agent_type": "agent-workflows:" + role, **payload})

    def stop(self, value, role="worker", event="SubagentStop", **extra):
        message = value if isinstance(value, str) else "```lane-return\n" + json.dumps(value) + "\n```"
        return self.hook(role, event, {"last_assistant_message": message, **extra})

    def assertBlocked(self, result, reason):
        self.assertEqual(result.get("decision"), "block", result)
        self.assertIn(reason, result["reason"])

    def test_every_role_rejects_absent_malformed_and_missing_fields(self):
        for role in ROLES:
            for message, reason in (("Done.", "lane-return"),
                                    ("```lane-return\n{broken}\n```", "JSON"),
                                    ({k: v for k, v in self.good.items() if k != "check"}, "check")):
                with self.subTest(role=role, reason=reason):
                    self.assertBlocked(self.stop(message, role), reason)

    def test_hash_and_complete_exit_are_checked_even_on_repeated_stops(self):
        actual = hashlib.sha256(self.patch.read_bytes()).hexdigest()
        for change, reasons in (({"patch_sha256": "0" * 64}, ("patch_sha256 mismatch", "0" * 64, actual)),
                                ({"exit": 7}, ("complete conflicts with nonzero check exit 7",)),
                                ({"patch": str(self.patch) + ".missing"}, ("patch",))):
            with self.subTest(change=change):
                result = self.stop({**self.good, **change}, stop_hook_active=True)
                for reason in reasons:
                    self.assertBlocked(result, reason)

    def test_unreadable_patch_reasons_name_the_path(self):
        missing = str(self.patch) + ".missing"
        for patch in (self.tmp.name, missing):
            with self.subTest(patch=patch):
                self.assertBlocked(self.stop({**self.good, "patch": patch}), patch)

    def test_valid_candidate_partial_blocked_read_only_and_committed_returns_pass(self):
        for role in ROLES:
            with self.subTest(role=role):
                self.assertEqual(self.stop(self.good, role), {})
                for status in ("partial", "blocked", "failed"):
                    self.assertEqual(self.stop({**self.good, "status": status, "exit": None,
                                                "patch": None, "patch_sha256": None}, role), {})
        self.assertEqual(self.stop({**self.good, "patch": None, "patch_sha256": None}, "reviewer"), {})
        committed = {k: v for k, v in self.good.items() if k not in ("patch", "patch_sha256")}
        self.assertEqual(self.stop({**committed, "sha": "b" * 40}, "senior"), {})
        crlf = "```lane-return\r\n" + json.dumps(self.good) + "\r\n```\r\nDone.\r\n"
        self.assertEqual(self.stop(crlf), {})

    def test_wrong_types_duplicate_keys_and_ambiguous_blocks_are_not_returns(self):
        for change in ({"exit": False}, {"v": True}, {"questions": "none"}, {"coderabbit": {}},
                       {"patch_sha256": None}, {"status": "done"}):
            with self.subTest(change=change):
                self.assertBlocked(self.stop({**self.good, **change}), "lane return")
        text = "```lane-return\n" + json.dumps(self.good) + "\n```"
        self.assertBlocked(self.stop(text + "\n" + text), "one")
        self.assertBlocked(self.stop(text.replace('"v": 3', '"v": 3, "v": 3')), "duplicate")

    def test_oversized_deep_and_invalid_returns_block_instead_of_hook_error(self):
        for contents, reason in (("[" * 20000 + "0" + "]" * 20000, "size"),
                                 ('{"extra":' + "[" * 100 + "0" + "]" * 100 + "}", "depth"),
                                 ('{"extra":"' + "x" * 2000 + '"}', "size"), ("{bad json}", "JSON")):
            with self.subTest(length=len(contents)):
                self.assertBlocked(self.stop("```lane-return\n" + contents + "\n```"), reason)
        self.assertEqual(self.stop({**self.good, "tail": "[" * 100 + '"\\\\'}), {})
        unicode_return = json.dumps({**self.good, "tail": "\U0001f600" * 700}, ensure_ascii=False)
        self.assertBlocked(self.stop("```lane-return\n" + unicode_return + "\n```"), "size")

    def test_decode_and_envelope_failures_use_blocking_exit_two(self):
        for raw in (b"\xff", b"[" * 20000 + b"0" + b"]" * 20000):
            result = subprocess.run(self.registered_command(), shell=True, input=raw,
                                    capture_output=True, timeout=10,
                                    env={**os.environ, "CLAUDE_PLUGIN_ROOT": str(PLUGIN)})
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn(b"lane return:", result.stderr)

    def test_plugin_registration_is_supported_and_agent_frontmatter_is_not_used(self):
        hooks = json.loads(registry().read_text())["hooks"]
        entries = [e for e in hooks.get("SubagentStop", []) if any(SCRIPT in h["command"] for h in e["hooks"])]
        self.assertEqual(len(entries), 1)
        self.assertTrue(all(re.fullmatch(entries[0]["matcher"], "agent-workflows:" + r) for r in ROLES))
        self.assertFalse(re.fullmatch(entries[0]["matcher"], "agent-workflows:worker-extra"))
        self.assertFalse(any(SCRIPT in h["command"] for e in hooks.get("Stop", []) for h in e["hooks"]))
        for role in ROLES:
            self.assertNotRegex((PLUGIN / "agents" / (role + ".md")).read_text().split("---", 2)[1], r"(?m)^hooks:")

    def test_no_hook_denies_a_lane_command(self):
        # Owner ruling: the lane no-push rule stays prose; nothing in the plugin inspects lane commands.
        hooks = json.loads(registry().read_text())["hooks"]
        for entry in hooks.get("PreToolUse", []):
            matcher = entry.get("matcher", "")
            with self.subTest(matcher=matcher):
                self.assertNotIn(matcher, ("", "*"))
                self.assertFalse(any(re.fullmatch(matcher, tool) for tool in ("Bash", "Monitor")))
        self.assertNotIn("lane_guard", registry().read_text())
        for role in ROLES:
            self.assertNotRegex((PLUGIN / "agents" / (role + ".md")).read_text().split("---", 2)[1], r"(?m)^hooks:")
        for tool in ("Bash", "Monitor"):
            with self.subTest(tool=tool):
                self.assertEqual(self.hook("worker", "PreToolUse", {"tool_name": tool, "tool_input": {
                    "command": "git push", "description": "fixture", "timeout_ms": 1000}}), {})

    def test_script_itself_excludes_roots_unrelated_agents_and_empty_type_forks(self):
        identities = [{"agent_id": ""}, {"agent_id": None}, {"agent_id": True},
                      {"agent_id": "  "}, {"agent_type": ""}, {"agent_type": None},
                      {"agent_type": "worker"}, {"agent_type": "other:worker"},
                      {"agent_type": "agent-workflows:worker-extra"},
                      {"agent_type": "agent-workflows:worker\\n"}]
        for identity in identities:
            with self.subTest(identity=identity):
                self.assertEqual(self.hook("worker", "SubagentStop", {"last_assistant_message": "Done.", **identity}), {})


if __name__ == "__main__":
    unittest.main()
