"""Shared test fixtures: a fake gh on PATH and an environment that points at it."""
import json
import os
from pathlib import Path
import stat
import sys
import tempfile

HERE = Path(__file__).resolve().parent
OWNER = "example-org"
REPO = f"{OWNER}/example"


class FakeGh:
    """A temp directory holding the fake gh executable, its route table and its call log."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        gh = self.bin / "gh"
        gh.write_text(f"#!/bin/sh\nexec {sys.executable} {HERE / 'fake_gh.py'} \"$@\"\n", encoding="utf-8")
        gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
        self.routes = []
        self._save()
        (self.root / "calls.jsonl").write_text("", encoding="utf-8")

    def cleanup(self):
        self.tmp.cleanup()

    def _save(self):
        (self.root / "routes.json").write_text(json.dumps(self.routes), encoding="utf-8")

    def route(self, pattern, *responses):
        """Answer METHOD /path?query matching pattern with each response in turn (a bare body means status 200)."""
        self.routes.append({"pattern": pattern, "responses": [r if isinstance(r, dict) and "status" in r else {"status": 200, "body": r}
                                                              for r in responses]})
        self._save()

    def calls(self):
        return [json.loads(line) for line in (self.root / "calls.jsonl").read_text(encoding="utf-8").splitlines() if line]

    def writes(self):
        return [c for c in self.calls() if c["method"] != "GET"]

    def env(self, **extra):
        env = {"PATH": f"{self.bin}{os.pathsep}/usr/bin:/bin", "FAKE_GH_DIR": str(self.root), "HOME": str(self.root),
               "TRACKER_REPO": REPO}
        env.update(extra)
        return env


def issue(number, title="An issue", labels=(), body="", state="open", **extra):
    return {"id": 1000 + number, "number": number, "title": title, "state": state, "body": body,
            "labels": [{"name": n} for n in labels], "html_url": f"https://github.com/{REPO}/issues/{number}", **extra}


def comment(ident, body, login="someone", kind="User"):
    return {"id": ident, "body": body, "user": {"login": login, "type": kind}, "created_at": "2026-11-23T00:00:00Z"}
