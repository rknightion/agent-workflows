"""The Python 3.15 launcher every Python hook starts through.

Each case runs a copy of scripts/py3 under /bin/sh with a private PATH of stub programs. The copy's
Homebrew paths point into the test's own directory, so a host that really has Homebrew's 3.15 does not
change the outcome. The same file runs from the public tree's tests/ directory.
"""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
PLUGIN = HERE.parent if (HERE.parent / "agents").is_dir() else HERE.parent / "plugins/agent-workflows"
LAUNCHER = PLUGIN / "scripts/py3"
HOMEBREW = ("/opt/homebrew/bin/python3.15", "/home/linuxbrew/.linuxbrew/bin/python3.15")
HOOK_PREFIX = 'sh "${CLAUDE_PLUGIN_ROOT}/scripts/py3" --hook "${CLAUDE_PLUGIN_ROOT}/scripts/'

# Reports which interpreter ran, its arguments and the first line of stdin, using shell builtins only.
PYTHON_STUB = '#!/bin/sh\nIFS= read -r line\nprintf "%s|%s|%s\\n" "LABEL" "$*" "$line"\nexit ${FAKE_EXIT:-0}\n'
# Logs where and how it ran and anything on stdin. `python install` marks the interpreter installed; `python
# find` then answers the way real uv would from a repository: VIRTUAL_ENV first, then the directory's .venv.
UV_STUB = ('#!/bin/sh\nprintf "cwd=%s venv=%s conda=%s args=%s\\n" "$PWD" "${VIRTUAL_ENV-}" "${CONDA_PREFIX-}" "$*"'
           ' >> "$UV_LOG"\n'
           'while IFS= read -r line; do printf "stdin:%s\\n" "$line" >> "$UV_LOG"; done\n'
           'if [ "$1" = --directory ]; then shift 2; elif [ -n "${UV_WORKING_DIR-}" ]; then cd "$UV_WORKING_DIR"; fi\n'
           'if [ "$2" = install ]; then : > "$UV_LOG.installed"; exit 0; fi\n'
           'if [ -n "${VIRTUAL_ENV-}" ]; then printf "%s\\n" "$VIRTUAL_ENV/bin/python"; exit 0; fi\n'
           'if [ -e .venv/pyvenv.cfg ]; then printf "%s\\n" "$PWD/.venv/bin/python"; exit 0; fi\n'
           'if [ -n "${UV_NEEDS_INSTALL-}" ] && [ ! -e "$UV_LOG.installed" ]; then exit 1; fi\n'
           'printf "%s\\n" "$UV_RESULT"\nexit ${UV_EXIT:-0}\n')
FIND = "cwd=/ venv= conda= args=--directory / python find --no-project --no-config --no-python-downloads 3.15"
INSTALL = "cwd=/ venv= conda= args=--directory / python install --no-config 3.15"


class Launcher(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.brew = self.root / "brew"
        text = LAUNCHER.read_text(encoding="utf-8")
        for number, path in enumerate(HOMEBREW):
            self.assertIn(path, text)
            text = text.replace(path, str(self.brew / str(number) / "python3.15"))
        self.launcher = self.root / "py3"
        self.launcher.write_text(text, encoding="utf-8")
        self.script = self.root / "hook.py"
        self.script.write_text("# never read by a stub\n", encoding="utf-8")
        self.uv_log = self.root / "uv.log"
        self.managed = self.root / "managed" / "python3.15"
        self.env = {"PATH": str(self.bin), "UV_LOG": str(self.uv_log), "UV_RESULT": str(self.managed)}

    def stub(self, path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        path.chmod(0o755)

    def python(self, path, label):
        self.stub(path, PYTHON_STUB.replace("LABEL", label))

    def run_launcher(self, *args, cwd=None, **env):
        return subprocess.run(["/bin/sh", str(self.launcher), *args], input="payload\n", capture_output=True,
                              text=True, timeout=20, cwd=cwd, env={**self.env, **env})

    def hook(self, cwd=None, **env):
        return self.run_launcher("--hook", str(self.script), "ask", cwd=cwd, **env)

    def hostile_repository(self):
        """A directory whose .venv interpreter writes a marker if anything ever runs it."""
        repo = self.root / "hostile"
        self.marker = self.root / "marker"
        (repo / ".venv").mkdir(parents=True)
        (repo / ".venv/pyvenv.cfg").write_text("home = /nowhere\nversion = 3.15.0\n", encoding="utf-8")
        self.stub(repo / ".venv/bin/python", f'#!/bin/sh\n: > "{self.marker}"\n')
        (repo / "uv.toml").write_text("this is [not valid toml\n", encoding="utf-8")
        return repo

    def assert_skipped(self, result):
        # Not 0, which hides the warning from Claude Code, and not 2, which blocks the action.
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(len(result.stderr.splitlines()), 1, result.stderr)
        self.assertIn("hook skipped", result.stderr)

    def test_python315_on_path_runs_with_args_and_stdin_and_never_calls_uv(self):
        self.python(self.bin / "python3.15", "path")
        self.python(self.bin / "python3", "older")
        self.stub(self.bin / "uv", UV_STUB)
        result = self.hook()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, f"path|{self.script} ask|payload\n")
        self.assertFalse(self.uv_log.exists())

    def test_the_programs_exit_status_passes_through(self):
        self.python(self.bin / "python3.15", "path")
        self.assertEqual(self.hook(FAKE_EXIT="7").returncode, 7)

    def test_each_homebrew_path_is_used_when_path_has_none(self):
        for number in range(len(HOMEBREW)):
            with self.subTest(path=HOMEBREW[number]):
                candidate = self.brew / str(number) / "python3.15"
                self.python(candidate, f"brew{number}")
                self.assertEqual(self.hook().stdout, f"brew{number}|{self.script} ask|payload\n")
                candidate.unlink()

    def test_older_python_and_uv_runs_uvs_interpreter_with_the_payload_intact(self):
        self.python(self.bin / "python3", "older")
        self.python(self.managed, "managed")
        self.stub(self.bin / "uv", UV_STUB)
        result = self.hook()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, f"managed|{self.script} ask|payload\n")
        log = self.uv_log.read_text(encoding="utf-8")
        self.assertEqual(log.splitlines(), [FIND])

    def test_a_command_line_tool_may_let_uv_install_then_finds_again(self):
        self.python(self.managed, "managed")
        self.stub(self.bin / "uv", UV_STUB)
        result = self.run_launcher(str(self.script), UV_NEEDS_INSTALL="1")
        self.assertEqual(result.stdout, f"managed|{self.script}|payload\n")
        self.assertEqual(self.uv_log.read_text(encoding="utf-8").splitlines(), [FIND, INSTALL, FIND])

    def test_a_hook_never_lets_uv_install(self):
        self.python(self.managed, "managed")
        self.stub(self.bin / "uv", UV_STUB)
        self.assert_skipped(self.hook(UV_NEEDS_INSTALL="1"))
        self.assertEqual(self.uv_log.read_text(encoding="utf-8").splitlines(), [FIND])

    def test_a_hostile_repository_is_never_executed(self):
        repo = self.hostile_repository()
        self.python(self.managed, "managed")
        self.stub(self.bin / "uv", UV_STUB)
        for name, run in (("hook", lambda **e: self.hook(cwd=repo, **e)),
                          ("tool", lambda **e: self.run_launcher(str(self.script), cwd=repo, **e))):
            for env in ({}, {"VIRTUAL_ENV": str(repo / ".venv")}, {"CONDA_PREFIX": str(repo / ".venv")}):
                with self.subTest(mode=name, env=env):
                    self.assertTrue(run(**env).stdout.startswith("managed|"))
                    self.assertFalse(self.marker.exists())

    def test_a_program_in_the_current_directory_is_never_run(self):
        repo = self.hostile_repository()
        for name in ("python3.15", "uv"):
            self.stub(repo / name, f'#!/bin/sh\n: > "{self.marker}"\n')
        for path in (f".:{self.bin}", f"{self.bin}:", f":{self.bin}", f"{self.bin}::{self.bin}"):
            with self.subTest(path=path):
                self.assert_skipped(self.hook(cwd=repo, PATH=path))
                self.assertFalse(self.marker.exists())

    def test_python_after_a_relative_path_element_is_still_found(self):
        repo = self.hostile_repository()
        self.stub(repo / "python3.15", f'#!/bin/sh\n: > "{self.marker}"\n')
        self.python(self.bin / "python3.15", "path")
        result = self.hook(cwd=repo, PATH=f".::{self.bin}")
        self.assertEqual(result.stdout, f"path|{self.script} ask|payload\n")
        self.assertFalse(self.marker.exists())

    def test_uv_working_dir_cannot_point_uv_at_a_repository(self):
        repo = self.hostile_repository()
        self.python(self.managed, "managed")
        self.stub(self.bin / "uv", UV_STUB)
        result = self.hook(cwd=repo, UV_WORKING_DIR=str(repo))
        self.assertEqual(result.stdout, f"managed|{self.script} ask|payload\n")
        self.assertFalse(self.marker.exists())

    def test_a_virtual_environment_named_by_uv_is_refused(self):
        repo = self.hostile_repository()
        self.stub(self.bin / "uv", UV_STUB)
        for path in (repo / ".venv/bin/python", self.root / "elsewhere/bin/python"):
            with self.subTest(path=path):
                if path.parent.parent.name == "elsewhere":
                    self.stub(path, f'#!/bin/sh\n: > "{self.marker}"\n')
                    (path.parent.parent / "pyvenv.cfg").write_text("version = 3.15.0\n", encoding="utf-8")
                self.assert_skipped(self.hook(UV_RESULT=str(path)))
                self.assertFalse(self.marker.exists())

    @unittest.skipUnless(shutil.which("uv"), "needs a real uv")
    def test_real_uv_never_runs_a_hostile_repository(self):
        repo = self.hostile_repository()
        (self.bin / "uv").symlink_to(shutil.which("uv"))
        for env in ({}, {"VIRTUAL_ENV": str(repo / ".venv")}, {"UV_WORKING_DIR": str(repo)}):
            with self.subTest(env=env):
                self.hook(cwd=repo, HOME=str(self.root), **env)
                self.assertFalse(self.marker.exists())

    def test_the_program_keeps_the_callers_directory_and_environment(self):
        self.stub(self.managed, '#!/bin/sh\nprintf "%s|%s\\n" "$PWD" "$KEEP"\n')
        self.stub(self.bin / "uv", UV_STUB)
        repo = self.root / "caller"
        repo.mkdir()
        result = self.hook(cwd=repo, KEEP="kept")
        self.assertEqual(result.stdout, f"{os.path.realpath(repo)}|kept\n")

    def test_neither_a_hook_exits_1_with_one_line(self):
        self.python(self.bin / "python3", "older")
        self.assert_skipped(self.hook())

    def test_neither_a_tool_fails_and_says_how_to_get_it(self):
        self.python(self.bin / "python3", "older")
        result = self.run_launcher(str(self.script))
        self.assertEqual(result.returncode, 127)
        self.assertEqual(result.stdout, "")
        self.assertIn("brew install python@3.15", result.stderr)
        self.assertIn("uv python install 3.15", result.stderr)

    def test_a_failing_uv_never_passes_its_status_to_a_hook(self):
        self.stub(self.bin / "uv", UV_STUB)
        for code in ("1", "2"):
            with self.subTest(uv_exit=code):
                self.assert_skipped(self.hook(UV_EXIT=code, UV_RESULT=""))

    def test_uv_naming_a_missing_interpreter_skips_the_hook(self):
        self.stub(self.bin / "uv", UV_STUB)
        self.assert_skipped(self.hook())

    def test_a_missing_hook_script_exits_1_and_never_starts_python(self):
        # python exits 2 on a missing script, and exit 2 from a hook blocks the action.
        self.python(self.bin / "python3.15", "path")
        self.assert_skipped(self.run_launcher("--hook", str(self.root / "gone.py"), "stop"))


class Registrations(unittest.TestCase):
    def test_every_python_hook_starts_through_the_launcher(self):
        manifest = json.loads((PLUGIN / ".claude-plugin/plugin.json").read_text(encoding="utf-8"))
        hooks = json.loads((PLUGIN / manifest.get("hooks", "./hooks/hooks.json")).read_text(encoding="utf-8"))["hooks"]
        commands = [h["command"] for event in hooks.values() for group in event for h in group["hooks"]]
        python = [c for c in commands if ".py" in c]
        self.assertEqual(len(python), 3)
        for command in python:
            with self.subTest(command=command):
                self.assertTrue(command.startswith(HOOK_PREFIX), command)
                self.assertTrue(command.endswith(" || exit 1"), command)
        for command in commands:
            self.assertFalse(command.startswith("python"), command)

    def test_a_missing_launcher_cannot_block(self):
        # dash exits 2 when it cannot open a script, and exit 2 from a hook blocks the action.
        manifest = json.loads((PLUGIN / ".claude-plugin/plugin.json").read_text(encoding="utf-8"))
        hooks = json.loads((PLUGIN / manifest.get("hooks", "./hooks/hooks.json")).read_text(encoding="utf-8"))["hooks"]
        commands = [h["command"] for event in hooks.values() for group in event for h in group["hooks"]
                    if ".py" in h["command"]]
        with tempfile.TemporaryDirectory() as empty:
            for command in commands:
                with self.subTest(command=command):
                    result = subprocess.run(command, shell=True, input="{}", capture_output=True, text=True,
                                            timeout=20, env={**os.environ, "CLAUDE_PLUGIN_ROOT": empty})
                    self.assertEqual(result.returncode, 1, result.stderr)

    def test_the_launcher_is_executable(self):
        self.assertTrue(os.access(LAUNCHER, os.X_OK))


if __name__ == "__main__":
    unittest.main()
