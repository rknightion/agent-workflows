"""Resolve the actual plugin manifest and hook registry, then invoke the lane-return hook over Claude JSON on stdin.

The hook never blocks a lane over the shape of its return. It reads the status line, a branch and a commit
from the text and appends them to a record. The same file runs from the public tree's tests/ directory;
no provider or live session is used.
"""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
PLUGIN = HERE.parent if (HERE.parent / "agents").is_dir() else HERE.parent / "plugins/agent-workflows"
ROLES = ("worker", "composer", "senior", "senior-escalation", "reviewer", "scout")
SCRIPT = "scripts/lane_return.py"
SHA = "0123456789abcdef0123456789abcdef01234567"

# A v5 return as a worker writes it.
GOOD = (
    "Status: complete\n\n"
    "Outcome: the parser accepts empty input. Branch v5/parser-empty, commit " + SHA + ".\n"
    "Evidence: python3 -m unittest tests.test_parser: failed before (1 failure), OK after.\n"
    "Blockers and left out: none.\n"
    "Decisions: an empty file parses to an empty list, because the caller already handles one.\n"
)
# Returns that the v4 validator sent back. None of them may be blocked now.
OLD_REJECTS = {
    "no status line": "I fixed the parser on branch fix/parser and committed abc1234.",
    "v4 fenced block, bad hash": '```lane-return\n{"v":3,"status":"complete","patch_sha256":"zz"}\n```\n',
    "two status lines": "Status: partial\nStatus: complete\n",
    "relative paths everywhere": "Status: blocked\nSee ./return.md and ../x.patch.\n",
    "empty": "",
    "huge": "Status: complete\n" + "x" * 200000,
}


def registry():
    manifest = json.loads((PLUGIN / ".claude-plugin/plugin.json").read_text())
    return (PLUGIN / manifest.get("hooks", "./hooks/hooks.json")).resolve()


class LaneReturnHook(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "state"

    def registered_command(self):
        path = registry()
        self.assertTrue(path.is_relative_to(PLUGIN.resolve()))
        entries = json.loads(path.read_text())["hooks"].get("SubagentStop", [])
        commands = [h for entry in entries for h in entry["hooks"] if SCRIPT in h["command"]]
        self.assertEqual(len(commands), 1)
        hook = commands[0]
        self.assertEqual(hook["type"], "command")
        return hook["command"]

    def run_raw(self, stdin):
        return subprocess.run(self.registered_command(), shell=True, input=stdin, text=True,
                              capture_output=True, timeout=10,
                              env={**os.environ, "CLAUDE_PLUGIN_ROOT": str(PLUGIN),
                                   "AGENT_LOOPS_STATE_DIR": str(self.state)})

    def run_hook(self, payload):
        result = self.run_raw(json.dumps(payload))
        self.assertEqual(result.returncode, 0, result.stderr)
        # Nothing on stdout: no decision, so the lane's stop always goes through.
        self.assertEqual(result.stdout.strip(), "", result.stdout)
        return result

    def lane(self, message, role="worker", **extra):
        payload = {"hook_event_name": "SubagentStop", "session_id": "root-1", "agent_id": "lane-1",
                   "agent_type": "agent-workflows:" + role, "stop_hook_active": False,
                   "transcript_path": os.path.join(self.tmp.name, "root.jsonl"),
                   "agent_transcript_path": os.path.join(self.tmp.name, "lane.jsonl"),
                   "last_assistant_message": message}
        payload.update(extra)
        return self.run_hook(payload)

    def records(self, session="root-1"):
        path = self.state / "lane-returns" / (session + ".jsonl")
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def test_registered_for_exactly_the_six_roles(self):
        entries = json.loads(registry().read_text())["hooks"]["SubagentStop"]
        (entry,) = [e for e in entries if any(SCRIPT in h["command"] for h in e["hooks"])]
        self.assertEqual(entry["matcher"], "^agent-workflows:(" + "|".join(ROLES) + ")$")

    def test_a_v5_return_is_recorded_with_status_branch_and_commit(self):
        self.lane(GOOD)
        (record,) = self.records()
        self.assertEqual((record["status"], record["branch"], record["commit"]),
                         ("complete", "v5/parser-empty", SHA))
        self.assertEqual((record["agent_id"], record["agent_type"]), ("lane-1", "agent-workflows:worker"))
        self.assertIn("at", record)

    def test_status_line_forms(self):
        for message, status in (("**Status:** partial\nrest", "partial"), ("status: BLOCKED on creds", "blocked"),
                                ("Outcome first\nStatus: failed\n", "failed"),
                                ("  Status:   complete  ", "complete")):
            with self.subTest(message=message):
                self.lane(message)
                self.assertEqual(self.records()[-1]["status"], status)

    def test_short_commit_and_branch_forms(self):
        self.lane("Status: complete\nCommitted abc1234 on branch `fix/one`.")
        record = self.records()[-1]
        self.assertEqual((record["branch"], record["commit"]), ("fix/one", "abc1234"))

    def test_no_status_line_goes_through_and_records_none(self):
        self.lane("I could not finish; the gate needs credentials.")
        self.assertIsNone(self.records()[-1]["status"])

    def test_returns_the_v4_validator_rejected_are_never_blocked(self):
        for role in ROLES:
            for name, message in OLD_REJECTS.items():
                with self.subTest(role=role, case=name):
                    self.lane(message, role=role)
        self.assertEqual(len(self.records()), len(ROLES) * len(OLD_REJECTS))

    def test_a_repeated_stop_is_never_blocked(self):
        self.lane("no status", stop_hook_active=True)

    def test_a_root_or_an_unrelated_agent_is_a_no_op(self):
        self.run_hook({"hook_event_name": "Stop", "session_id": "root-1", "last_assistant_message": GOOD})
        self.run_hook({"hook_event_name": "SubagentStop", "session_id": "root-1", "agent_id": "x",
                       "agent_type": "general-purpose", "last_assistant_message": GOOD})
        self.run_hook({"hook_event_name": "SubagentStop", "session_id": "root-1", "agent_id": "",
                       "agent_type": "agent-workflows:worker", "last_assistant_message": GOOD})
        self.assertEqual(self.records(), [])

    def test_malformed_input_never_blocks(self):
        for stdin in ("", "not json", "[]", '{"agent_type": 5}', '{"last_assistant_message": null}'):
            with self.subTest(stdin=stdin):
                result = self.run_raw(stdin)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), "")

    def test_an_unwritable_record_never_blocks(self):
        self.state.parent.mkdir(parents=True, exist_ok=True)
        self.state.write_text("a file where the state directory should be")
        self.lane(GOOD)

    def test_the_session_id_cannot_escape_the_record_directory(self):
        self.lane(GOOD, session_id="../../escape")
        self.assertFalse((Path(self.tmp.name) / "escape.jsonl").exists())
        self.assertEqual(len(list((self.state / "lane-returns").iterdir())), 1)


if __name__ == "__main__":
    unittest.main()
