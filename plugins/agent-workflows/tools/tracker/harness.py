"""Shared test fixtures: a fake `gh` on PATH, a fake clock and an environment that points at them."""
import json
import os
from pathlib import Path
import stat
import sys
import tempfile

HERE = Path(__file__).resolve().parent
APP_TOKEN = "app-token-fixture"
USER_TOKEN = "user-token-fixture"
APP_LOGIN = "example-tracker[bot]"
OWNER = "example-org"
REPO = f"{OWNER}/example"


class FakeClock:
    """A clock whose sleep advances it, so rate-limit waits cost no real time."""

    def __init__(self, now=1_800_000_000.0):
        self.now = now
        self.sleeps = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class FakeGh:
    """A temp directory holding the fake gh executable, its route table and its call log."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.state = self.root / "state"
        self.state.mkdir()
        script = self.bin / "gh"
        script.write_text(f"#!{sys.executable}\n" + (HERE / "fake_gh.py").read_text().split("\n", 1)[1], encoding="utf-8")
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        # Whether an account is an organisation or a user is read once per run; the fixture owner is an organisation.
        (self.root / "routes.json").write_text(json.dumps([{"pattern": f"GET /users/{OWNER}", "fallback": True, "responses": [
            {"status": 200, "body": {"login": OWNER, "type": "Organization"}}]}]), encoding="utf-8")
        (self.root / "calls.jsonl").write_text("", encoding="utf-8")

    def cleanup(self):
        self.tmp.cleanup()

    def route(self, pattern, *responses, replace=False):
        """Answer METHOD /path matching pattern with each response in turn; a bare dict or body is wrapped.

        The first route that matches answers, so a test that refines a route its setUp made passes replace=True."""
        path = self.root / "routes.json"
        routes = json.loads(path.read_text(encoding="utf-8"))
        if replace:
            routes = [r for r in routes if r["pattern"] != pattern]
        wrapped = [r if isinstance(r, dict) and ("status" in r or "exit" in r) else {"status": 200, "body": r} for r in responses]
        routes.append({"pattern": pattern, "responses": wrapped})
        path.write_text(json.dumps(routes), encoding="utf-8")

    def items_route(self, pattern, items):
        """Serve a Project item listing from `items`, paged and filtered the way GitHub does (see fake_gh.py); replaces an earlier one."""
        path = self.root / "routes.json"
        routes = [r for r in json.loads(path.read_text(encoding="utf-8")) if r["pattern"] != pattern]
        routes.append({"pattern": pattern, "items": items})
        path.write_text(json.dumps(routes), encoding="utf-8")

    def calls(self, method=None):
        lines = (self.root / "calls.jsonl").read_text(encoding="utf-8").splitlines()
        records = [json.loads(line) for line in lines if line]
        return [r for r in records if method is None or r["method"] == method]

    def writes(self):
        """Every call that changes something; the /markdown render is a read in POST clothing."""
        return [r for r in self.calls() if r["method"] != "GET" and r["path"] not in ("/markdown", "graphql")]

    def renders(self):
        return [r for r in self.calls() if r["path"] == "/markdown"]

    def env(self, app_token=APP_TOKEN, **extra):
        env = {
            "PATH": f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}",
            "HOME": str(self.root),
            "FAKE_GH_DIR": str(self.root),
            "TRACKER_STATE_DIR": str(self.state),
            "TRACKER_REPO": REPO,
            "TRACKER_PROJECT": "7",
            "TRACKER_APP_LOGIN": APP_LOGIN,
            "GH_TOKEN": USER_TOKEN,
        }
        if app_token:
            env["TRACKER_APP_TOKEN"] = app_token
        env.update(extra)
        return env


def comment(ident, body, login="someone", kind="User"):
    return {"id": ident, "body": body, "user": {"login": login, "type": kind}, "created_at": f"2026-11-0{ident % 9 + 1}T00:00:00Z"}


def issue(number=5, **fields):
    base = {
        "id": 9000 + number,
        "number": number,
        "title": f"ABC-1: thing {number}",
        "state": "open",
        "state_reason": None,
        "body": "Body.\n\n## Acceptance criteria\n- [ ] AC1 first\n- [ ] AC2 second\n",
        "labels": [{"name": "bug"}],
        "milestone": None,
        "assignees": [],
        "type": {"name": "Task"},
        "comments": 0,
        "html_url": f"https://github.com/{REPO}/issues/{number}",
        "sub_issues_summary": {"total": 0, "completed": 0, "percent_completed": 0},
        "user": {"login": "someone"},
        "created_at": "2026-11-01T00:00:00Z",
        "updated_at": "2026-11-02T00:00:00Z",
    }
    base.update(fields)
    return base


PROJECT_FIELDS = [
    {"id": 11, "name": "Title", "data_type": "title"},
    {"id": 12, "name": "Status", "data_type": "single_select", "options": [
        {"id": "opt-ready", "name": "Ready"}, {"id": "opt-prog", "name": "In progress"},
        {"id": "opt-input", "name": "Needs input"}, {"id": "opt-done", "name": "Done"}]},
    {"id": 13, "name": "Lane", "data_type": "text"},
    {"id": 14, "name": "Priority", "data_type": "single_select", "options": [{"id": "p1", "name": "P1"}]},
    {"id": 15, "name": "Estimate", "data_type": "number"},
]

# The organisation's issue fields (GET /orgs/<owner>/issue-fields): a different object from the Project's own Priority
# above, with its own id, which an issue's issue_field_values names.
ORG_PRIORITY_FIELD_ID = 90210
ORG_ISSUE_FIELDS = [
    {"id": 90209, "name": "Effort", "data_type": "single_select", "options": [{"id": 1, "name": "High"}, {"id": 2, "name": "Low"}]},
    {"id": ORG_PRIORITY_FIELD_ID, "name": "Priority", "data_type": "single_select",
     "options": [{"id": 11, "name": "Urgent"}, {"id": 12, "name": "High"}, {"id": 13, "name": "Medium"}, {"id": 14, "name": "Low"}]},
    {"id": 90211, "name": "Target date", "data_type": "date"},
]


def project_item(number, repo=REPO, status="Ready", priority=None, labels=(), ident=None):
    """One Project item as the items listing returns it: an issue with its labels and the Status, Lane and Priority fields."""
    return {"id": ident or 100000 + number, "content_type": "Issue", "fields": [
        {"id": 12, "name": "Status", "data_type": "single_select", "value": {"id": "opt-ready", "name": status}},
        {"id": 13, "name": "Lane", "data_type": "text", "value": None},
        {"id": 14, "name": "Priority", "data_type": "single_select",
         "value": None if priority is None else {"id": "opt", "name": {"html": priority, "raw": priority}}}],
        "content": {"number": number, "title": f"issue {number}", "state": "open", "html_url": f"https://github.com/{repo}/issues/{number}",
                    "labels": [{"name": name} for name in labels]}}


# A user-owned Project's Priority field: the options are Urgent, High, Medium and Low, named as the API names them.
USER_PROJECT_FIELDS = [dict(f, options=[{"id": f"p-{n.lower()}", "name": {"html": n, "raw": n}} for n in ("Urgent", "High", "Medium", "Low")])
                       if f["name"] == "Priority" else f for f in PROJECT_FIELDS]
