"""Checks on the published plugin: manifests, the loop skill, the lane agents and the bin/ programs.

The hooks have their own tests beside this file (test_run_gate.py and test_lane_return.py).

Every test works in a temporary directory; nothing touches a Claude Code home or ~/.local/state.
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN = os.path.join(ROOT, "plugins", "agent-workflows")

BIN = os.path.join(PLUGIN, "bin")
LOOP_LINT = os.path.join(BIN, "loop-lint")


def read(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as fh:
        return fh.read()


class Manifests(unittest.TestCase):
    def test_marketplace_lists_the_plugin_by_its_manifest_name(self):
        market = json.loads(read(ROOT, ".claude-plugin", "marketplace.json"))
        self.assertRegex(market["name"], r"^[a-z0-9][a-z0-9-]*$")
        self.assertTrue(market["owner"]["name"])
        (entry,) = market["plugins"]
        self.assertTrue(entry["source"].startswith("./"))
        self.assertNotIn("..", entry["source"])
        plugin_dir = os.path.normpath(os.path.join(ROOT, entry["source"]))
        manifest = json.loads(read(plugin_dir, ".claude-plugin", "plugin.json"))
        self.assertEqual(entry["name"], manifest["name"])
        self.assertEqual(manifest["license"], "Apache-2.0")

    def test_hook_commands_name_scripts_that_exist(self):
        hooks = json.loads(read(PLUGIN, "hooks", "hooks.json"))["hooks"]
        commands = [h["command"] for event in hooks.values() for group in event for h in group["hooks"]]
        # The run gate, the lane-return record and the tmux session lifecycle hook must all ship.
        self.assertEqual({re.search(r'scripts/[^"]+', c).group(0) for c in commands},
                         {"scripts/run_gate.py", "scripts/lane_return.py", "scripts/tmux_title_state.sh"})
        for command in commands:
            script = re.search(r'"\$\{CLAUDE_PLUGIN_ROOT\}/([^"]+)"', command).group(1)
            self.assertTrue(os.path.isfile(os.path.join(PLUGIN, script)), script)

    def test_the_bin_programs_are_executable(self):
        for name in ("loop-lint", "loop-land", "loop-teardown", "run-check", "wait-until"):
            with self.subTest(program=name):
                self.assertTrue(os.access(os.path.join(BIN, name), os.X_OK))


class Programs(unittest.TestCase):
    """The protocol tells the root to land with loop-land and every agent to wait with wait-until."""

    def run_program(self, name, *args):
        with tempfile.TemporaryDirectory() as elsewhere:
            return subprocess.run([sys.executable, os.path.join(BIN, name), *args], cwd=elsewhere,
                                  capture_output=True, text=True, timeout=60)

    def test_loop_land_runs(self):
        result = self.run_program("loop-land", "--help")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("ready", result.stdout)

    def test_wait_until_returns_when_the_test_passes(self):
        result = self.run_program("wait-until", "--timeout", "5s", "--what", "x", "--", "true")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_wait_until_times_out_with_124(self):
        result = self.run_program("wait-until", "--timeout", "1s", "--every", "0.2", "--what", "x", "--", "false")
        self.assertEqual(result.returncode, 124, result.stdout + result.stderr)
        self.assertIn("TIMED OUT", result.stderr)

    def test_wait_until_has_a_portable_interpreter_line(self):
        self.assertEqual(read(BIN, "wait-until").splitlines()[0], "#!/usr/bin/env python3")


class LoopSkill(unittest.TestCase):
    def test_every_plugin_path_in_the_skill_exists(self):
        skill = read(PLUGIN, "skills", "loop", "SKILL.md")
        paths = re.findall(r"\$\{CLAUDE_PLUGIN_ROOT\}/([\w./<>-]+)", skill)
        self.assertEqual(sorted(paths), ["protocol/loop/run-claude.md", "protocol/loop/run.md"])
        for rel in paths:
            self.assertTrue(os.path.isfile(os.path.join(PLUGIN, rel)), rel)

    def test_the_lane_agents_return_plain_text_with_a_status_line(self):
        for name in ("worker", "composer", "senior", "senior-escalation", "reviewer", "scout"):
            with self.subTest(agent=name):
                text = read(PLUGIN, "agents", name + ".md")
                self.assertIn("Its first line is `Status: complete`", " ".join(text.split()))
                self.assertNotIn("lane-return", text)


class LoopLint(unittest.TestCase):
    def lint(self, *args):
        return subprocess.run([sys.executable, LOOP_LINT, *args], capture_output=True, text=True, timeout=60)

    def test_a_charter_with_no_standing_grants_lints_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            codex = os.path.join(tmp, "codex")
            os.makedirs(codex)
            with open(os.path.join(tmp, "LOOP.md"), "w") as fh:
                fh.write("# Loop: repo\n\nci-required: none\n")  # main's CI is not read: no remote here
            standing = os.path.join(codex, "standing.md")
            with open(standing, "w") as fh:
                fh.write("No standing grants.\n")
            digest = hashlib.sha256(b"No standing grants.\n").hexdigest()
            charter = os.path.join(codex, "charter-2026-01-05-loop1.md")
            with open(charter, "w") as fh:
                fh.write(
                    "# Charter: repo loop1\n\nprotocol:   v4\nharness:    claude\nroot:       claude-opus-5-5 / medium\n"
                    "aim:        Clear the parser backlog.\npriorities: label:parser > status:\"To Do\"\n"
                    "exclude:    label:needs-owner\nlanes:      4\ngate:       just check\n"
                    "gate-route: local\nstanding:   " + standing + " sha256=" + digest + "\npreflight:  none\n"
                    "overnight:  keep building waves from the board in priority order\n\n"
                    "## First wave\n\n| Task | Scope | Route | Note |\n|---|---|---|---|\n"
                    "| ABC-1 (fix the parser) | src/parser.py | worker | none |\n"
                )
            result = self.lint("charter", charter)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            with open(standing, "a") as fh:
                fh.write("Deploy anything.\n")
            self.assertEqual(self.lint("charter", charter).returncode, 1)

    def test_the_readme_loop_md_examples_lint_clean(self):
        examples = re.findall(r"```markdown\n( *# Loop: .*?)\n *```", read(ROOT, "README.md"), re.S)
        self.assertEqual(len(examples), 2)
        for example in examples:
            with self.subTest(example=example.splitlines()[-1]), tempfile.TemporaryDirectory() as tmp:
                path = os.path.join(tmp, "LOOP.md")
                with open(path, "w") as fh:
                    fh.write(textwrap.dedent(example) + "\n")
                result = self.lint("loopmd", path, "--offline")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
