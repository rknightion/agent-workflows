"""The Project-write guard: which shell commands it refuses and which it lets through."""
import sys as _sys
from pathlib import Path as _Path
import unittest as _unittest

# The tracker needs Python 3.12 or newer. Skip the whole module before any tracker module is imported, so an older
# interpreter (the rest of this tree supports 3.9) reports a skip, not an import error.
if _sys.version_info < (3, 12):
    raise _unittest.SkipTest("the tracker needs Python 3.12 or newer")

# The tracker's modules sit beside each other in tools/tracker, in this source tree or a published one.
_TOOL = next(p for p in (_Path(__file__).resolve().parent.parent / "tools" / "tracker",
                         _Path(__file__).resolve().parent.parent / "plugins" / "agent-workflows" / "tools" / "tracker") if p.is_dir())
if str(_TOOL) not in _sys.path:
    _sys.path.insert(0, str(_TOOL))

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

import guard
from harness import HERE

BLOCKED = [
    "gh project item-add 1 --owner o --url u",
    "gh project item-edit --id x",
    "gh project create --owner o",
    "gh project close 1",
    "gh api /orgs/o/projectsV2/1/items -F number=3",
    "gh api graphql --input query.json",
    "gh api graphql -f query='mutation { addProjectV2ItemById(input: {}) { item { id } } }'",
    "gh api -X DELETE /orgs/o/projectsV2/1/items/9",
    "bash <<'EOF'\ngh project close 5\nEOF",
    "sh -s <<EOF\ngh project item-add 1 -b x\nEOF",
    "echo \"not a heredoc <<EOF\"\ngh project close 5",
    "cat <<EOF | bash\ngh project close 5\nEOF",
    "cat <<EOF\nreport: $(gh project close 5)\nEOF",
    "cat <<EOF\nreport: `gh project close 5`\nEOF",
    "bash <<'EOF'  # don't retry\ngh project close 5\nEOF",
    "{ gh project close 5; }",
    "cd /tmp && gh project close 5",
    "ls; gh project close 5",
    "false || gh project close 5",
    "echo 5 | xargs -n1 gh project close",
    "env GH_TOKEN=x gh project item-add 1 -b y",
    "FOO=1 gh project edit 3 -t z",
    "/opt/homebrew/bin/gh project close 5",
    "command gh project close 5",
    "sudo -u nobody gh project close 5",
    "timeout 30 gh project close 5",
    "bash -c 'gh project close 5'",
    "sh -lc \"gh project item-add 1 -b x\"",
    "eval 'gh project close 5'",
    "sh -c'gh project close 5'",
    "g\\\nh project close 5",
    "gh proj\\\nect close 5",
    "gh project clo\\\nse 5",
    "gh project \\\n  close 5",
    "bash -lc'gh project item-add 1 -b x'",
    "echo 'not a heredoc <<EOF'\ngh project close 5\nEOF",
    "< /dev/null gh project close 5",
    "2>/dev/null gh project close 5",
    ">/tmp/o gh project item-add 5 -b x",
    "FOO=1 </dev/null gh project close 5",
    "echo x >/dev/null; </dev/null gh project close 5",
    "echo \"$(gh project close 5)\"",
    "echo \"a 'b' $(gh project close 5)\"",
    "echo 'it'\"'\"'s' $(gh project close 5)",
    "echo \"`gh project close 5`\"",
    "echo $'\\'' $(gh project close 5) $'\\'' \\'",
    "echo $'it\\'s' $(gh project close 5)",
    "echo hi # <<EOF\ngh project close 5\nEOF",
    "cat <<EOF\nliteral '$(gh project close 5)' text\nEOF",
    "cat <<EOF\nit's `gh project close 5` here\nEOF",
    "cat <<-EOF\n\tit's $(echo $(gh project close 5))\nEOF",
    "echo $(gh project close 5)",
    "echo `gh project close 5`",
    "(gh project close 5)",
    "{ gh project close 5; }",
    "if true; then gh project close 5; fi",
    "echo hi\ngh project close 5",
    "for n in 1 2; do gh project close $n; done",
    "gh project close 5 &",
    "gh project item-add 5 -b \"unterminated",
    "gh project close 5  # don't wait",
]

# Issue and pull request writes with plain gh: the tracker model is issues and labels, so these run.
ISSUE_WRITES = [
    "gh issue comment 5 --body hi",
    "gh issue create --title x --body y",
    "gh issue edit 5 --add-label bug",
    "gh issue close 5",
    "gh issue reopen 5",
    "gh issue develop 5",
    "gh issue delete 5 --yes",
    "gh issue lock 5",
    "gh issue transfer 5 o/r",
    "gh issue pin 5",
    "gh pr comment 3 --body hi",
    "gh api -X POST repos/o/r/pulls/3/comments -f body=x",
    "gh api -X POST repos/o/r/issues/3/comments -f body=x",
    "gh api -X POST /repos/o/r/issues/5/comments -f body=x",
    "gh api --method PATCH repos/o/r/issues/5 --input body.json",
    "gh api --method=DELETE /repos/o/r/issues/comments/9",
    "gh api -XPUT repos/o/r/issues/5/labels",
    "gh api repos/o/r/issues -f title=x",
    "gh api repos/o/r/issues/5/sub_issues --input -",
    "gh api graphql -f query='mutation { addComment(input: {}) { clientMutationId } }'",
    "gh api graphql -F query='mutation{x}'",
    "gh api -X POST /graphql -f query='mutation { addComment(input: {}) { clientMutationId } }'",
    "gh api https://api.github.com/graphql -f query='mutation { x }'",
    "gh issue -R owner/repo close 5",
    "gh pr --repo owner/repo comment 3 -b x",
    "gh issue --repo=owner/repo comment 5 -b x",
]

ALLOWED = [
    "",
    "ls -la",
    "gh issue view 5",
    "gh issue list --state open",
    "gh pr view 3 --json state",
    "gh pr list",
    "gh pr checks 3",
    "gh pr diff 3",
    "gh pr create --fill",
    "gh pr edit 3 --title x --body y --add-label bug",
    "gh pr ready 3",
    "gh pr close 3",
    "gh pr merge 3 --squash",
    "gh pr create --fill  # don't retry",
    "gh pr create --title \"it's here",
    "gh pr reopen 3",
    "gh pr --repo owner/repo create --fill",
    "gh api -X PUT repos/o/r/pulls/3/merge",
    "gh api -X PATCH repos/o/r/pulls/3 -f title=x",
    "gh project list --owner o",
    "gh project view 1",
    "gh project item-list 1 --owner o",
    "gh project field-list 1 --owner o",
    "gh api /zen",
    "gh api repos/o/r/issues/5",
    "gh api -X GET repos/o/r/issues -f state=open",
    "gh api repos/o/r/issues --paginate --jq '.[].number'",
    "gh api graphql -f query='query { viewer { login } }'",
    "gh api /graphql -f query='query { viewer { login } }'",
    "gh issue -R owner/repo view 5",
    "git commit -m 'run $(gh project close 5) later'",
    "echo 'see `gh project close 5`'",
    "gh issue view 5 < /dev/null",
    "gh pr create --fill > /dev/null 2>&1",
    "gh issue list 2>/dev/null | head",
    "gh project --help",
    "gh project",
    "gh api repos/o/r/commits/abc/check-runs",
    "gh run view 1",
    "gh repo view",
    "git commit -m 'close the issue with gh project close 5'",
    "echo gh project close 5",
    "grep -n \"gh project close\" README.md",
    "just tracker view 5",
    "python3 tools/tracker/tracker.py close 5 --pr 3",
    "git commit -F - <<'EOF'\nnotes\ngh project close 5\nEOF",
    "cat <<'EOF'\nliteral $(gh project close 5) is data\nEOF",
    "cat <<'EOF'\nit's $(gh project close 5)\nEOF",
    "cat <<EOF\nescaped \\$(gh project close 5) and \\`gh project close 5\\`\nEOF",
]


class CheckTests(unittest.TestCase):
    def test_every_raw_write_is_refused(self):
        for command in BLOCKED:
            with self.subTest(command=command):
                self.assertTrue(guard.check(command), f"allowed: {command!r}")

    def test_an_end_of_options_marker_does_not_hide_the_program(self):
        for command in ("command -- gh project close 5", "exec -- gh project close 5", "timeout -- 30 gh project close 5",
                        "nice -n 5 -- gh project close 5", "env -- gh project close 5", "sudo -u me -- gh project close 5"):
            with self.subTest(command=command):
                self.assertTrue(guard.check(command), f"allowed: {command!r}")
        for command in ("command -- gh issue view 5", "exec -- git status", "echo -- gh project close 5"):
            with self.subTest(command=command):
                self.assertEqual(guard.check(command), [], f"blocked: {command!r}")

    def test_plain_gh_issue_and_pull_request_writes_are_allowed(self):
        for command in ISSUE_WRITES:
            with self.subTest(command=command):
                self.assertEqual(guard.check(command), [], f"blocked: {command!r}")

    def test_reads_and_look_alikes_are_allowed(self):
        for command in ALLOWED:
            with self.subTest(command=command):
                self.assertEqual(guard.check(command), [], f"blocked: {command!r}")

    def test_a_graphql_mutation_inside_a_file_is_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            write = os.path.join(tmp, "m.graphql")
            read = os.path.join(tmp, "q.graphql")
            issue = os.path.join(tmp, "i.graphql")
            pathlib.Path(write).write_text("mutation { addProjectV2ItemById(input: {}) { item { id } } }")
            pathlib.Path(read).write_text("query { x }")
            pathlib.Path(issue).write_text("mutation { closeIssue(input: {}) { issue { id } } }")
            self.assertTrue(guard.check(f"gh api graphql -F query=@{write}"))
            self.assertTrue(guard.check(f"gh api graphql --input {write}"))
            self.assertEqual(guard.check(f"gh api graphql -F query=@{read}"), [])
            self.assertEqual(guard.check(f"gh api graphql -F query=@{issue}"), [])
            self.assertEqual(guard.check(f"gh api graphql --input {issue}"), [])
            self.assertTrue(guard.check(f"gh api graphql -F query=@{tmp}/missing"))

    def test_the_message_names_the_rule_and_the_way_out(self):
        (message,) = guard.check("gh project close 5")
        self.assertIn("gh project close", message)
        self.assertIn("nothing writes it", message)


class CommandLineTests(unittest.TestCase):
    def run_guard(self, *argv, stdin=""):
        return subprocess.run([sys.executable, str(HERE / "guard.py"), *argv], input=stdin, capture_output=True, text=True, timeout=30, check=False)

    def test_a_command_argument_blocks_with_exit_2_and_a_stderr_reason(self):
        done = self.run_guard("--command", "gh project close 5")
        self.assertEqual(done.returncode, 2)
        self.assertIn("gh project close", done.stderr)

    def test_an_allowed_command_exits_0_silently(self):
        done = self.run_guard("--command", "gh issue view 5")
        self.assertEqual((done.returncode, done.stdout, done.stderr), (0, "", ""))

    def test_a_pre_tool_use_payload_on_stdin_is_understood(self):
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "gh project item-add 5 -b x"}})
        self.assertEqual(self.run_guard(stdin=payload).returncode, 2)
        ok = json.dumps({"tool_name": "Bash", "tool_input": {"command": "gh issue view 5"}})
        self.assertEqual(self.run_guard(stdin=ok).returncode, 0)

    def test_a_payload_for_another_tool_is_ignored(self):
        payload = json.dumps({"tool_name": "Write", "tool_input": {"file_path": "x", "content": "gh project close 5"}})
        self.assertEqual(self.run_guard(stdin=payload).returncode, 0)

    def test_raw_text_on_stdin_is_treated_as_the_command(self):
        self.assertEqual(self.run_guard(stdin="gh project close 5").returncode, 2)

    def test_text_that_is_not_json_is_read_as_the_command_even_when_it_opens_with_a_brace(self):
        self.assertEqual(self.run_guard(stdin="{not json").returncode, 0)
        self.assertEqual(self.run_guard(stdin="{ gh project close 5; }").returncode, 2)


if __name__ == "__main__":
    unittest.main()
