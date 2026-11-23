"""Checks on the published plugin: manifests, the loop skill, the lane agents and the bin/ programs.

The hooks have their own tests beside this file (test_run_gate.py and test_lane_return.py).

Every test works in a temporary directory; nothing touches a Claude Code home or ~/.local/state.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN = os.path.join(ROOT, "plugins", "agent-workflows")

BIN = os.path.join(PLUGIN, "bin")


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
        # The Python hooks name the launcher first, then their script.
        self.assertEqual({s for c in commands for s in re.findall(r'scripts/[^"]+', c)},
                         {"scripts/py3", "scripts/run_gate.py", "scripts/lane_return.py",
                          "scripts/tmux_title_state.sh"})
        for command in commands:
            for script in re.findall(r'"\$\{CLAUDE_PLUGIN_ROOT\}/([^"]+)"', command):
                self.assertTrue(os.path.isfile(os.path.join(PLUGIN, script)), script)

    def test_the_bin_programs_are_executable(self):
        for name in ("run-check", "run-drive", "wait-until"):
            with self.subTest(program=name):
                self.assertTrue(os.access(os.path.join(BIN, name), os.X_OK))


class Programs(unittest.TestCase):
    """The protocol tells the root to land with run-check and run-drive and every agent to wait with wait-until."""

    def run_program(self, name, *args):
        with tempfile.TemporaryDirectory() as elsewhere:
            return subprocess.run([sys.executable, os.path.join(BIN, name), *args], cwd=elsewhere,
                                  capture_output=True, text=True, timeout=60)

    def test_run_check_and_run_drive_start(self):
        for name, verb in (("run-check", "land"), ("run-drive", "sweep")):
            with self.subTest(program=name):
                result = self.run_program(name, "--help")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn(verb, result.stdout)

    def test_wait_until_returns_when_the_test_passes(self):
        result = self.run_program("wait-until", "--timeout", "5s", "--what", "x", "--", "true")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_wait_until_times_out_with_124(self):
        result = self.run_program("wait-until", "--timeout", "1s", "--every", "0.2", "--what", "x", "--", "false")
        self.assertEqual(result.returncode, 124, result.stdout + result.stderr)
        self.assertIn("TIMED OUT", result.stderr)

    def test_wait_until_starts_from_sh(self):
        self.assertEqual(read(BIN, "wait-until").splitlines()[0], "#!/bin/sh")


class WaitUntilStart(unittest.TestCase):
    """wait-until run directly on a host whose PATH has no python3.15, in a private copy of the plugin layout."""

    HOMEBREW = ("/opt/homebrew/bin/python3.15", "/home/linuxbrew/.linuxbrew/bin/python3.15")

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name
        self.path = os.path.join(self.root, "path")
        os.makedirs(self.path)
        self.brew = os.path.join(self.root, "brew", "python3.15")
        launcher = read(PLUGIN, "scripts", "py3")
        for candidate in self.HOMEBREW:
            self.assertIn(candidate, launcher)
        launcher = launcher.replace(self.HOMEBREW[0], self.brew).replace(self.HOMEBREW[1], self.brew + "-absent")
        self.write("plugin/scripts/py3", launcher)
        self.program = self.write("plugin/bin/wait-until", read(BIN, "wait-until"))

    def write(self, rel, text):
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(path, 0o755)
        return path

    def python_at(self, rel):
        return self.write(rel, f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')

    def run_it(self, *test_command, program=None, path=None):
        return subprocess.run([program or self.program, "--timeout", "2s", "--every", "0.2", "--what", "a b", "--",
                               *test_command], cwd=self.root, capture_output=True, text=True, timeout=30,
                              env={"PATH": path or self.path})

    def assert_exit_codes_are_exact(self):
        for script, code in (("exit 0", 0), ("exit 2", 3)):
            with self.subTest(script=script):
                result = self.run_it("/bin/sh", "-c", script)
                self.assertEqual(result.returncode, code, result.stdout + result.stderr)

    def test_python_reachable_only_through_homebrew(self):
        self.python_at("brew/python3.15")
        self.assert_exit_codes_are_exact()

    def test_python_reachable_only_through_uv(self):
        managed = self.python_at("managed/bin/python3.15")
        self.write("path/uv", f"#!/bin/sh\nprintf '%s\\n' '{managed}'\n")
        self.assert_exit_codes_are_exact()

    def test_a_timeout_still_exits_124(self):
        self.python_at("brew/python3.15")
        result = subprocess.run([self.program, "--timeout", "1s", "--every", "0.2", "--what", "x", "--", "/bin/sh", "-c",
                                 "exit 1"], cwd=self.root, capture_output=True, text=True, timeout=30,
                                env={"PATH": self.path})
        self.assertEqual(result.returncode, 124, result.stdout + result.stderr)

    def test_a_symlink_to_it_still_finds_the_launcher(self):
        self.python_at("brew/python3.15")
        link = os.path.join(self.root, "elsewhere", "wait-until")
        os.makedirs(os.path.dirname(link))
        os.symlink(self.program, link)
        result = self.run_it("/bin/sh", "-c", "exit 0", program=link)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_a_dot_in_path_never_runs_a_program_from_the_callers_directory(self):
        self.python_at("brew/python3.15")
        marker = os.path.join(self.root, "marker")
        for name in ("sh", "readlink", "python3.15", "uv"):
            self.write(name, f'#!/bin/sh\n: > "{marker}"\nexit 9\n')
        link = os.path.join(self.root, "elsewhere", "wait-until")
        os.makedirs(os.path.dirname(link))
        os.symlink(self.program, link)
        for program in (self.program, link):
            with self.subTest(program=program):
                result = self.run_it("/bin/sh", "-c", "exit 0", program=program, path=f".:{self.path}")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertFalse(os.path.exists(marker))

    def test_no_python_fails_with_how_to_get_it(self):
        result = self.run_it("/bin/sh", "-c", "exit 0")
        self.assertEqual(result.returncode, 127, result.stdout + result.stderr)
        self.assertIn("python@3.15", result.stderr)


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


if __name__ == "__main__":
    unittest.main()
