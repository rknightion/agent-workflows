"""Shared fixtures for the loop tool tests: fake gh, backlog, agent-history and ping
binaries on PATH, plus helpers that build fixture repositories with `git init` in temp dirs."""
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import tempfile

def _bin():
    """The tools' directory: LOOP_TOOLS_BIN first, then `<root>/bin` in this repository, then
    `<root>/plugins/agent-workflows/bin` in the exported public tree (the tests sit in `<root>/tests/`)."""
    if os.environ.get('LOOP_TOOLS_BIN'):
        return Path(os.environ['LOOP_TOOLS_BIN'])
    root = Path(__file__).resolve().parents[1]
    return root / 'bin' if (root / 'bin' / 'loop-land').exists() else root / 'plugins' / 'agent-workflows' / 'bin'


BIN = _bin()

FAKE_GH = '''#!{python}
import json, os, sys
d = os.environ["FAKE_GH_DIR"]
a = sys.argv[1:]
open(d + "/calls.log", "a").write(json.dumps(a) + "\\n")
def http(code, reason, body):
    sys.stdout.write("HTTP/2.0 %d %s\\r\\nContent-Type: application/json\\r\\n\\r\\n%s" % (code, reason, body))
    if code >= 400:
        sys.stderr.write("gh: %s (HTTP %d)" % (json.loads(body).get("message"), code)); sys.exit(1)
    sys.exit(0)
path = next((x for x in a if x.startswith("repos/")), "")
if a[:1] == ["api"] and "/check-runs" in path:
    sha = path.split("/commits/")[1].split("/")[0]
    if os.path.exists(d + "/checks.fail"):
        sys.stderr.write("gh: boom"); sys.exit(1)
    f = d + "/checks-" + sha + ".json"
    for run in (json.load(open(f)) if os.path.exists(f) else []):
        sys.stdout.write(json.dumps(run) + "\\n")
    sys.exit(0)
if a[:1] == ["api"] and path.split("?")[0].endswith("/status") and "/commits/" in path:
    sha = path.split("/commits/")[1].split("/")[0]
    f = d + "/statuses-" + sha + ".json"
    sys.stdout.write(json.dumps({{"statuses": json.load(open(f)) if os.path.exists(f) else []}}))
    sys.exit(0)
if a[:1] == ["api"] and "/rules/branches/" in path:
    if os.path.exists(d + "/rules.404"):
        http(404, "Not Found", json.dumps({{"message": "Not Found"}}))
    if os.path.exists(d + "/rules.fail"):
        sys.stderr.write("gh: boom"); sys.exit(1)
    f = d + "/rules.json"
    http(200, "OK", open(f).read() if os.path.exists(f) else "[]")
if a[:1] == ["api"]:
    if os.path.exists(d + "/protection.json"):
        http(200, "OK", open(d + "/protection.json").read())
    if os.path.exists(d + "/protection.404"):
        http(404, "Not Found", json.dumps({{"message": "Branch not protected"}}))
    if os.path.exists(d + "/protection.notfound"):
        http(404, "Not Found", json.dumps({{"message": "Not Found"}}))
    sys.stderr.write("gh: boom"); sys.exit(1)
if a[:2] == ["issue", "view"]:
    f = d + "/issues.json"
    issue = (json.load(open(f)) if os.path.exists(f) else {{}}).get(a[a.index("-R") + 1] + "#" + a[2])
    if issue is None:
        sys.stderr.write("GraphQL: Could not resolve to an issue or pull request with the number of %s." % a[2]); sys.exit(1)
    sys.stdout.write(json.dumps({{"state": issue["state"], "labels": [{{"name": n}} for n in issue.get("labels", [])]}}))
    sys.exit(0)
if a[:2] == ["run", "list"]:
    sha = a[a.index("--commit") + 1]
    path = d + "/runs-" + sha + ".json"
    sys.stdout.write(open(path).read() if os.path.exists(path) else "[]")
    sys.exit(0)
sys.exit(2)
'''

FAKE_BACKLOG = '''#!{python}
import json, os, re, sys
path = os.path.join(os.getcwd(), "backlog", "tasks.json")
tasks = json.load(open(path))
a = sys.argv[1:]
def save():
    json.dump(tasks, open(path, "w"), indent=1, sort_keys=True)
if a[:2] == ["task", "list"]:
    want = a[a.index("-s") + 1] if "-s" in a else None
    groups = {{}}
    for tid, t in sorted(tasks.items()):
        groups.setdefault(t["status"], []).append(tid)
    for status, ids in groups.items():
        if want and status.lower() != want.lower():
            continue
        print(status + ":")
        for tid in ids:
            print("  [HIGH] " + tid + " - " + tasks[tid]["title"] + " (ac: 1/1)")
        print()
    sys.exit(0)
if a[:2] == ["task", "edit"]:
    t = tasks[a[2]]
    i = 3
    while i < len(a):
        flag = a[i]
        if flag == "-s":
            t["status"] = a[i + 1]
        elif flag == "--add-label":
            t["labels"].append(a[i + 1])
        elif flag == "--remove-label":
            t["labels"] = [l for l in t["labels"] if l != a[i + 1]]
        elif flag == "--append-notes":
            t["notes"] += "\\n" + a[i + 1]
        else:
            sys.stderr.write("unsupported " + flag); sys.exit(2)
        i += 2
    save(); sys.exit(0)
if a[:1] == ["task"] and a[-1] == "--plain":
    tid = a[2] if a[1] == "view" else a[1]
    found = {{k.lower(): k for k in tasks}}.get(tid.lower())
    if found is None:
        sys.stderr.write("Task " + tid + " not found.\\n"); sys.exit(1)
    tid, t = found, tasks[found]
    print("Task " + tid + " - " + t["title"])
    print("Status: " + {{"Done": "\\u2714 ", "In Progress": "\\u25d2 "}}.get(t["status"], "\\u25cb ") + t["status"])
    print("Labels: " + ", ".join(t["labels"]))
    print("Implementation Notes:\\n" + t["notes"])
    sys.exit(0)
sys.exit(2)
'''

FAKE_PING = '''#!{python}
import json, os, sys
open(os.environ["PING_LOG"], "a").write(json.dumps(sys.argv[1:]) + "\\n")
'''

FAKE_AH = '''#!{python}
import os, sys
d = os.environ.get("FAKE_AH_DIR")
a = sys.argv[1:]
if not d or not os.path.exists(d + "/loops.json"):
    sys.exit(1)
if "loops" in a:
    sys.stdout.write(open(d + "/loops.json").read())
elif "loop" in a:
    sys.stdout.write(open(d + "/loop.json").read())
'''


def hermetic_fixture_env():
    """Copy the fixture child's env without inherited command-scope Git config.

    Local fixture rewrites must win over a surrounding lane's transport config.
    The runner's environment and native lane controls are never mutated.
    """
    injected_config = re.compile(r'^GIT_CONFIG_(?:COUNT|KEY_\d+|VALUE_\d+|PARAMETERS)$')
    return {key: value for key, value in os.environ.items()
            if not injected_config.fullmatch(key)}


class Sandbox:
    """A temp world: bin/ with fakes, env for subprocesses, and repo-building helpers."""

    def __init__(self, with_ah=False):
        self.root = Path(tempfile.mkdtemp(prefix='loop-tools-'))
        self.bin = self.root / 'fakebin'
        self.bin.mkdir()
        self.gh_dir = self.root / 'gh'
        self.gh_dir.mkdir()
        self.ping_log = self.root / 'ping-log'
        self.ah_dir = self.root / 'ah'
        self.ah_dir.mkdir()
        self._fake('gh', FAKE_GH)
        self._fake('backlog', FAKE_BACKLOG)
        self._fake('ping', FAKE_PING)
        if with_ah:
            self._fake('agent-history', FAKE_AH)
        home = self.root / 'home'
        home.mkdir()
        self.env = {
            **hermetic_fixture_env(),
            'PATH': f'{self.bin}:{os.environ["PATH"]}',
            'FAKE_GH_DIR': str(self.gh_dir),
            'FAKE_AH_DIR': str(self.ah_dir),
            'PING_LOG': str(self.ping_log),
            'LOOP_PING_CMD': str(self.bin / 'ping'),
            'HOME': str(home),
            'GIT_AUTHOR_NAME': 'Test', 'GIT_AUTHOR_EMAIL': 't@example.com',
            'GIT_COMMITTER_NAME': 'Test', 'GIT_COMMITTER_EMAIL': 't@example.com',
            'GIT_CONFIG_GLOBAL': str(self.root / 'gitconfig'),
            'GIT_CONFIG_NOSYSTEM': '1',
        }
        (self.root / 'gitconfig').write_text('[init]\n\tdefaultBranch = main\n')

    def _fake(self, name, template):
        path = self.bin / name
        path.write_text(template.format(python=sys.executable))
        path.chmod(0o755)

    def run(self, script, *args, cwd=None, extra_env=None):
        env = {**self.env, **(extra_env or {})}
        return subprocess.run([sys.executable, str(BIN / script), *map(str, args)], capture_output=True,
                              text=True, env=env, cwd=str(cwd) if cwd else None, timeout=120)

    def git(self, repo, *args):
        result = subprocess.run(['git', '-C', str(repo), *args], capture_output=True, text=True,
                                env=self.env, timeout=60)
        assert result.returncode == 0, (args, result.stderr)
        return result.stdout.strip()

    def make_repo(self, name, parent=None, remote_url=None):
        """A repo with a bare origin it is pushed to; returns the working clone path."""
        base = (parent or self.root / 'repos')
        base.mkdir(parents=True, exist_ok=True)
        origin = self.root / 'origins' / f'{name}.git'
        origin.parent.mkdir(exist_ok=True)
        subprocess.run(['git', 'init', '--bare', '-q', str(origin)], env=self.env, check=True)
        repo = base / name
        subprocess.run(['git', 'init', '-q', str(repo)], env=self.env, check=True)
        self.git(repo, 'remote', 'add', 'origin', str(origin))
        (repo / 'README.md').write_text('# fixture\n')
        self.git(repo, 'add', '-A')
        self.git(repo, 'commit', '-q', '-m', 'init')
        self.git(repo, 'push', '-q', 'origin', 'main')
        if remote_url:
            self.git(repo, 'remote', 'set-url', 'origin', remote_url)
            self.git(repo, 'config', f'url.{origin}.insteadOf', remote_url)
        return repo

    def commit_file(self, repo, name, content, message, push=True):
        (repo / name).write_text(content)
        self.git(repo, 'add', '--', name)
        self.git(repo, 'commit', '-q', '-m', message)
        if push:
            self.git(repo, 'push', '-q', 'origin', 'main')
        return self.git(repo, 'rev-parse', 'HEAD')

    def set_runs(self, sha, runs):
        (self.gh_dir / f'runs-{sha}.json').write_text(json.dumps(runs))

    def set_checks(self, sha, checks):
        """Check runs on one commit, as `gh api .../check-runs --jq '.check_runs[] | {...}'` prints them."""
        (self.gh_dir / f'checks-{sha}.json').write_text(json.dumps(
            [{'id': i + 1, **c} for i, c in enumerate(checks)]))

    def set_statuses(self, sha, statuses):
        """Legacy commit statuses on one commit, as `gh api .../status` returns them."""
        (self.gh_dir / f'statuses-{sha}.json').write_text(json.dumps(statuses))

    def pings(self):
        if not self.ping_log.exists():
            return []
        return [json.loads(l) for l in self.ping_log.read_text().splitlines()]
