"""Checks on the published plugin: manifests, the loop skill's launch template, both hooks and loop-lint.

Every test works in a temporary directory; nothing touches a Claude Code home or ~/.local/state.
"""

import datetime
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN = os.path.join(ROOT, "plugins", "agent-workflows")
sys.path.insert(0, os.path.join(PLUGIN, "scripts"))

import brief_header  # noqa: E402
import fanout_continue  # noqa: E402
import fanout_session  # noqa: E402
import loop_launch  # noqa: E402

LOOP_LINT = os.path.join(PLUGIN, "bin", "loop-lint")
PLUGIN_ROOT_VAR = "${CLAUDE_PLUGIN_ROOT}"


def read(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as fh:
        return fh.read()


def launch_template():
    skill = read(PLUGIN, "skills", "loop", "SKILL.md")
    return re.search(r"```text\n(.*?)\n```", skill, re.S).group(1)


def render_launch(repo, date="2026-01-05", loop=1):
    """The skill's launch template as Claude Code would show it, filled in for one run."""
    text = launch_template().replace(PLUGIN_ROOT_VAR, PLUGIN)
    return (text.replace("<abs repo>", repo).replace("<date>", date).replace("<N>", str(loop))
            .replace("<name>", "claude"))


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
        # Two loop hooks (brief header, fan-out continue) plus the tmux title-state hook on each
        # session lifecycle event; every command must name a script shipped in this tree.
        self.assertEqual(len({re.search(r'scripts/[^"]+', c).group(0) for c in commands}), 3)
        for command in commands:
            script = re.search(r'"\$\{CLAUDE_PLUGIN_ROOT\}/([^"]+)"', command).group(1)
            self.assertTrue(os.path.isfile(os.path.join(PLUGIN, script)), script)

    def test_loop_lint_is_executable(self):
        self.assertTrue(os.access(LOOP_LINT, os.X_OK))


class LaunchTemplate(unittest.TestCase):
    def test_every_plugin_path_in_the_skill_exists(self):
        skill = read(PLUGIN, "skills", "loop", "SKILL.md")
        paths = re.findall(r"\$\{CLAUDE_PLUGIN_ROOT\}/([\w./<>-]+)", skill)
        self.assertTrue(paths)
        for rel in paths:
            rel = rel.rstrip(".")
            if "<name>" in rel:
                for name in ("claude", "codex", "pi"):
                    self.assertTrue(os.path.isfile(os.path.join(PLUGIN, rel.replace("<name>", name))), rel)
            else:
                self.assertTrue(os.path.exists(os.path.join(PLUGIN, rel)), rel)

    def test_the_rendered_launch_arms_the_stop_hook(self):
        launch = loop_launch.parse_launch(render_launch("/r"), "/r", "2026-01-05T10:00:00Z", None)
        self.assertIsNotNone(launch)
        self.assertEqual(launch["report"], "/r/codex/closeout-2026-01-05-loop1.md")
        self.assertEqual(launch["loop"], 1)

    def test_the_rendered_launch_names_protocol_files_that_exist(self):
        rendered = render_launch("/r")
        named = re.findall(r"(/\S+/protocol/loop/[\w-]+\.md)", rendered)
        self.assertEqual(len(named), 2)
        for path in named:
            self.assertTrue(os.path.isfile(path), path)


class HookCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        for patcher in (
            mock.patch.object(fanout_session, "cache_dir", return_value=os.path.join(self.tmp, "cache")),
            mock.patch.dict(os.environ, {fanout_session.STATE_DIR_ENV: os.path.join(self.tmp, "state")}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(os.path.join(self.repo, ".git"))
        os.makedirs(os.path.join(self.repo, "codex"))
        self.transcript = os.path.join(self.tmp, "t.jsonl")
        open(self.transcript, "w").close()
        self.closeout = os.path.join(self.repo, "codex", "closeout-2026-01-05-loop1.md")

    def say(self, text):
        ts = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=5)).strftime(
            "%Y-%m-%dT%H:%M:%S.000Z")
        entry = {"type": "user", "origin": {"kind": "human"}, "timestamp": ts, "cwd": self.repo,
                 "message": {"content": text}}
        with open(self.transcript, "a") as fh:
            fh.write(json.dumps(entry) + "\n")

    def payload(self, **extra):
        return dict({"session_id": "s1", "transcript_path": self.transcript, "cwd": self.repo}, **extra)


class StopHook(HookCase):
    def stop(self, last="Done for now."):
        return fanout_continue.decide(self.payload(last_assistant_message=last, stop_hook_active=False))

    def test_an_ordinary_session_is_never_held(self):
        self.say("Tidy the README please.")
        self.assertIsNone(self.stop())

    def test_an_armed_root_is_held_until_its_closeout_counts(self):
        self.say(render_launch(self.repo))
        reason = self.stop()
        self.assertIn(self.closeout, reason)
        self.assertIn("does not exist yet", reason)
        with open(self.closeout, "w") as fh:
            fh.write("# Closeout: repo loop1 · Charter: " + "0" * 64 + "\n")
            fh.write("".join(f"{h}\nNone\n" for h in ("## Landed", "## Parked", "## For ratification", "## Notes")))
        self.assertIsNone(self.stop())

    def test_a_paused_line_releases_the_turn(self):
        self.say(render_launch(self.repo))
        self.assertIsNone(self.stop("Blocked on the owner.\nPAUSED: need a deploy decision"))


class BriefHeaderHook(HookCase):
    def dispatch(self, prompt):
        return brief_header.decide(self.payload(tool_name="Agent", tool_input={"prompt": prompt}))

    def test_an_armed_root_needs_the_header(self):
        self.say(render_launch(self.repo))
        self.assertIn("loop brief header", self.dispatch("Please fix the parser."))
        self.assertIsNone(self.dispatch("Lane: w1 · Task: ABC-1 (fix the parser) · Tier: routine\nScope: src/"))

    def test_an_ordinary_session_dispatches_freely(self):
        self.say("Map the parser for me.")
        self.assertIsNone(self.dispatch("Please map the parser."))


class LoopLint(unittest.TestCase):
    def lint(self, *args):
        return subprocess.run([sys.executable, LOOP_LINT, *args], capture_output=True, text=True, timeout=60)

    def test_a_charter_with_no_standing_grants_lints_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            codex = os.path.join(tmp, "codex")
            os.makedirs(codex)
            standing = os.path.join(codex, "standing.md")
            with open(standing, "w") as fh:
                fh.write("No standing grants.\n")
            digest = hashlib.sha256(b"No standing grants.\n").hexdigest()
            charter = os.path.join(codex, "charter-2026-01-05-loop1.md")
            with open(charter, "w") as fh:
                fh.write(
                    "# Charter: repo loop1\n\nharness:    claude\nroot:       claude-opus-5-5 / medium\n"
                    "aim:        Clear the parser backlog.\npriorities: label:parser > status:\"To Do\"\n"
                    "exclude:    label:needs-owner\nlanes:      4\ngate:       just check\n"
                    "gate-route: local\nstanding:   " + standing + " sha256=" + digest + "\n"
                    "overnight:  keep building waves from the board in priority order\n\n"
                    "## First wave\n\n| Task | Scope | Route | Note |\n|---|---|---|---|\n"
                    "| ABC-1 (fix the parser) | src/parser.py | worker | none |\n"
                )
            result = self.lint("charter", charter)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            with open(standing, "a") as fh:
                fh.write("Deploy anything.\n")
            self.assertEqual(self.lint("charter", charter).returncode, 1)

    def test_the_readme_loop_md_example_lints_clean(self):
        example = re.search(r"```markdown\n( *# Loop: .*?)\n *```", read(ROOT, "README.md"), re.S).group(1)
        text = textwrap.dedent(example) + "\n"
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "LOOP.md")
            with open(path, "w") as fh:
                fh.write(text)
            result = self.lint("loopmd", path, "--offline")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
