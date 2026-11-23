"""The run gate hooks: the unattended root's question prompt and its turn end.

Each case drives the command the plugin registers, over Claude-shaped JSON on stdin, against a
throwaway repository holding a run folder and a transcript written the way Claude Code writes one
(the main thread's assistant tool calls and their results, one JSON object per line). A main-thread
payload carries no agent_id; a subagent's carries agent_id and agent_type. The same file runs from the
public tree's tests/ directory.
"""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
PLUGIN = HERE.parent if (HERE.parent / "agents").is_dir() else HERE.parent / "plugins/agent-workflows"
SCRIPT = "scripts/run_gate.py"
RUN = "run-20261123-1"


def registry():
    manifest = json.loads((PLUGIN / ".claude-plugin/plugin.json").read_text())
    return json.loads((PLUGIN / manifest.get("hooks", "./hooks/hooks.json")).read_text())["hooks"]


def registered(event, matcher=None):
    commands = [h["command"] for group in registry().get(event, []) for h in group["hooks"]
                if SCRIPT in h["command"] and (matcher is None or group.get("matcher") == matcher)]
    return commands


def assistant_tool_use(name, tool_input, ts="2026-11-23T18:00:00.000Z"):
    return {"type": "assistant", "isSidechain": False, "timestamp": ts,
            "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": "toolu_1", "name": name, "input": tool_input}]}}


def tool_result(text):
    return {"type": "user", "isSidechain": False,
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1",
                                                     "content": text}]}}


def human(text):
    return {"type": "user", "isSidechain": False, "message": {"role": "user", "content": text}}


class GateCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.repo = self.tmp / "repo"
        (self.repo / ".git").mkdir(parents=True)
        self.run_dir = self.repo / "codex" / RUN
        self.run_dir.mkdir(parents=True)
        (self.run_dir / "AUTHORITY.md").write_text("# Authority: repo " + RUN + "\nprotocol: v5\n")
        self.state = self.tmp / "state"
        self.transcript = self.tmp / "root.jsonl"
        # The root wrote its run files from code mode, then carried on.
        self.say(human("/loop clear the parser issues"),
                 assistant_tool_use("REPL", {"code": "await write('" + str(self.run_dir) +
                                             "/AUTHORITY.md', text)"}),
                 tool_result("wrote 412 bytes"))

    def say(self, *entries):
        with open(self.transcript, "a") as fh:
            for entry in entries:
                fh.write(json.dumps(entry) + "\n")

    def leave(self):
        (self.run_dir / "UNATTENDED").write_text("")

    def close(self):
        (self.run_dir / "CLOSEOUT.md").write_text("# Closeout: repo " + RUN + "\nprotocol: v5\n")

    def invoke(self, command, payload, env=None, raw=None):
        result = subprocess.run(command, shell=True, input=raw if raw is not None else json.dumps(payload),
                                text=True, capture_output=True, timeout=20,
                                env={**{k: v for k, v in os.environ.items() if k != "CLAUDE_PROJECT_DIR"},
                                     "CLAUDE_PLUGIN_ROOT": str(PLUGIN), "AGENT_LOOPS_STATE_DIR": str(self.state),
                                     **(env or {})})
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout) if result.stdout.strip() else None

    def base(self, **extra):
        payload = {"session_id": "root-1", "transcript_path": str(self.transcript), "cwd": str(self.repo),
                   "permission_mode": "default"}
        payload.update(extra)
        return payload


class AskGate(GateCase):
    QUESTIONS = {"questions": [{"question": "Merge the parser change?", "header": "Merge",
                                "options": [{"label": "Yes"}, {"label": "No"}], "multiSelect": False}]}

    def command(self):
        (command,) = registered("PreToolUse", "AskUserQuestion")
        return command

    def ask(self, env=None, **extra):
        payload = self.base(hook_event_name="PreToolUse", tool_name="AskUserQuestion", tool_input=self.QUESTIONS,
                            tool_use_id="toolu_9")
        payload.update(extra)
        return self.invoke(self.command(), payload, env=env)

    def assertDenied(self, out):
        self.assertIsNotNone(out, "the question prompt was let through")
        decision = out["hookSpecificOutput"]
        self.assertEqual((decision["hookEventName"], decision["permissionDecision"]), ("PreToolUse", "deny"))
        reason = decision["permissionDecisionReason"]
        self.assertIn(str(self.run_dir / "PARKED.md"), reason)
        self.assertIn("UNATTENDED", reason)
        return reason

    def test_attended_the_prompt_is_untouched(self):
        self.assertIsNone(self.ask())

    def test_unattended_the_root_is_refused_and_told_to_park(self):
        self.leave()
        self.assertDenied(self.ask())

    def test_back_again_the_prompt_works(self):
        self.leave()
        self.assertDenied(self.ask())
        (self.run_dir / "UNATTENDED").unlink()
        self.assertIsNone(self.ask())

    def test_a_closed_run_no_longer_switches_it_off(self):
        self.leave()
        self.close()
        self.assertIsNone(self.ask())

    def test_a_subagent_is_never_affected(self):
        self.leave()
        self.assertIsNone(self.ask(agent_id="a1b2", agent_type="agent-workflows:worker"))
        self.assertIsNone(self.ask(agent_id="a1b2", agent_type="general-purpose"))

    def test_another_session_in_the_repository_is_not_the_root(self):
        self.leave()
        other = self.tmp / "other.jsonl"
        other.write_text(json.dumps(human("tidy the README")) + "\n")
        self.assertIsNone(self.ask(session_id="other", transcript_path=str(other)))

    def test_a_handover_session_that_read_the_run_files_is_the_root(self):
        self.leave()
        fresh = self.tmp / "fresh.jsonl"
        fresh.write_text(json.dumps(assistant_tool_use("Read", {"file_path": str(self.run_dir / "RESUME.md")})) + "\n")
        self.assertDenied(self.ask(session_id="fresh", transcript_path=str(fresh)))

    def test_a_longer_run_number_is_a_different_run(self):
        self.leave()
        other = self.tmp / "other.jsonl"
        other.write_text(json.dumps(assistant_tool_use("Read", {"file_path": str(self.repo / "codex" / (RUN + "2") /
                                                                                 "PLAN.md")})) + "\n")
        self.assertIsNone(self.ask(session_id="other", transcript_path=str(other)))

    def test_from_a_subdirectory_or_through_the_project_dir(self):
        self.leave()
        sub = self.repo / "src" / "pkg"
        sub.mkdir(parents=True)
        self.assertDenied(self.ask(cwd=str(sub)))
        self.assertDenied(self.ask(cwd=str(self.tmp), env={"CLAUDE_PROJECT_DIR": str(self.repo)}))

    def test_a_lane_worktree_outside_the_checkout_finds_no_run(self):
        self.leave()
        worktree = self.tmp / "wt" / "L1"
        (worktree).mkdir(parents=True)
        (worktree / ".git").write_text("gitdir: " + str(self.repo / ".git" / "worktrees" / "L1") + "\n")
        self.assertIsNone(self.ask(cwd=str(worktree)))

    def test_a_nested_checkout_stops_the_search(self):
        self.leave()
        nested = self.repo / "vendor" / "lib"
        (nested / ".git").mkdir(parents=True)
        self.assertIsNone(self.ask(cwd=str(nested)))

    def test_faults_let_the_prompt_through(self):
        self.leave()
        self.assertIsNone(self.ask(transcript_path=str(self.tmp / "missing.jsonl")))
        self.assertIsNone(self.ask(transcript_path=None))
        for raw in ("", "not json", "[]"):
            with self.subTest(raw=raw):
                self.assertIsNone(self.invoke(self.command(), None, raw=raw))


class StopGate(GateCase):
    def command(self):
        (command,) = registered("Stop")
        return command

    def stop(self, active=False, last="Lanes are running.", **extra):
        payload = self.base(hook_event_name="Stop", stop_hook_active=active, last_assistant_message=last)
        payload.update(extra)
        return self.invoke(self.command(), payload)

    def assertHeld(self, out, n):
        self.assertIsNotNone(out, "the turn was let end")
        self.assertEqual(out["decision"], "block")
        self.assertIn("CLOSEOUT.md", out["reason"])
        self.assertIn("WAITING:", out["reason"])
        self.assertIn(f"continuation {n} of 3", out["reason"])
        self.assertIn("If the owner is back, remove " + str(self.run_dir / "UNATTENDED"), out["reason"])

    def test_attended_the_turn_ends(self):
        self.assertIsNone(self.stop())

    def test_unattended_with_no_closeout_the_root_is_sent_back_at_most_three_times(self):
        self.leave()
        self.assertHeld(self.stop(), 1)
        self.assertHeld(self.stop(active=True), 2)
        self.assertHeld(self.stop(active=True), 3)
        self.assertIsNone(self.stop(active=True))
        self.assertIsNone(self.stop(active=True))
        # A new turn starts a new chain.
        self.assertHeld(self.stop(active=False), 1)

    def test_a_waiting_line_ends_the_turn(self):
        self.leave()
        self.assertIsNone(self.stop(last="Three lanes are building.\n\nWAITING: lanes L1 to L3 and PR 12 checks"))
        self.assertIsNone(self.stop(last="**WAITING:** the gate run"))
        self.assertHeld(self.stop(last="WAITING: for nothing\nmore text after it"), 1)

    def test_a_closeout_ends_the_run(self):
        self.leave()
        self.close()
        self.assertIsNone(self.stop())

    def test_a_subagent_is_never_held(self):
        self.leave()
        self.assertIsNone(self.stop(agent_id="a1b2", agent_type="agent-workflows:worker",
                                    hook_event_name="SubagentStop"))

    def test_another_session_is_never_held(self):
        self.leave()
        other = self.tmp / "other.jsonl"
        other.write_text(json.dumps(human("what time is it")) + "\n")
        self.assertIsNone(self.stop(session_id="other", transcript_path=str(other)))

    def test_with_no_place_to_count_the_turn_ends(self):
        self.leave()
        self.state.write_text("a file where the state directory should be")
        self.assertIsNone(self.stop())

    def test_faults_let_the_turn_end(self):
        self.leave()
        self.assertIsNone(self.stop(transcript_path=str(self.tmp / "missing.jsonl")))
        for raw in ("", "not json", "[]"):
            with self.subTest(raw=raw):
                self.assertIsNone(self.invoke(self.command(), None, raw=raw))


class Registration(unittest.TestCase):
    def test_the_gate_is_registered_once_per_event_and_only_for_the_question_prompt(self):
        self.assertEqual(len(registered("PreToolUse")), 1)
        self.assertEqual(len(registered("PreToolUse", "AskUserQuestion")), 1)
        self.assertEqual(len(registered("Stop")), 1)
        for event in ("SubagentStop", "PostToolUse", "UserPromptSubmit", "SessionStart"):
            self.assertEqual(registered(event), [], event)


if __name__ == "__main__":
    unittest.main()
