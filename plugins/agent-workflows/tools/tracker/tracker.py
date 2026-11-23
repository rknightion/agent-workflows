#!/usr/bin/env python3.15
"""The GitHub tracker: a thin shim over gh for issues and labels (README.md).

A repository's queue is its open issues labelled ready. Writes go out as the ambient gh login: there is no GitHub
App, no token minting and no Project. Verbs whose Project meaning is gone succeed with a one-line note.
"""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import urllib.parse

import textsafe

READY, BLOCKED, NEEDS_OWNER = "ready", "blocked", "needs-owner"
STATUS_LABELS = (READY, BLOCKED, NEEDS_OWNER)
# Status names the Project used, and their label. Any other name takes the issue out of the queue (drops ready).
STATUS_NAMES = {"ready": READY, "blocked": BLOCKED, "needs-owner": NEEDS_OWNER, "needs owner": NEEDS_OWNER,
                "needs input": NEEDS_OWNER, "needs-input": NEEDS_OWNER}
PRIORITY_LABELS = ("p1", "p2", "p3")
PRIORITY_NAMES = {"p1": "p1", "p2": "p2", "p3": "p3", "urgent": "p1", "high": "p1", "medium": "p2", "low": "p3"}
LABEL_COLOURS = {READY: "0e8a16", BLOCKED: "b60205", NEEDS_OWNER: "d93f0b", "p1": "b60205", "p2": "fbca04", "p3": "c5def5"}
STATE_REASONS = {"completed": "completed", "not-planned": "not_planned", "duplicate": "duplicate"}
ID_MAP_PATH = ".github/tracker/id-map.json"  # written by a separate import tool (not part of this distribution) and committed
LEGACY_ID = re.compile(r"[A-Za-z][A-Za-z0-9]*-[0-9]+(?:\.[0-9]+)*")
REPO_SHAPE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
MAX_TEXT = 60000  # GitHub's comment and body limit is 65,536 characters
PAGES = 100  # the most pages of 100 read in one listing; a listing that fills them is an error, never the whole of it
USAGE, REFUSED, GITHUB = 2, 3, 4

# The acceptance-criteria section rules of the acceptance evidence checker (sectionOf, parseAcceptanceCriteria in
# ac-evidence-check.mjs), mirrored with offsets into the original body so a tick edits one character in place.
_AC_HEADING = re.compile(r"^#{1,6}[ \t]+Acceptance criteria[ \t]*#*[ \t]*$", re.I | re.A)
_AC_LINE = re.compile(r"^[ \t]*[-*][ \t]+\[([ xX])\][ \t]+AC([0-9]+)[ \t]+\S.*$")


class Fail(Exception):
    """A command that cannot go on; code is the process exit status."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class Partial(Exception):
    """A command that failed after its first write; the result so far must still reach the caller."""

    def __init__(self, result, cause):
        super().__init__(str(cause))
        self.result = result


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Fail(USAGE, "bad command line; see tracker --help")


def criteria_lines(body):
    """Return (n, offset of its checkbox character, ticked) for each criterion line in the issue's criteria section.

    The section is the body under its one "Acceptance criteria" heading, down to the next heading of the same or a
    higher level; a body with no such heading is read whole. More than one such heading is refused, as the checker fails.
    """
    lines, position = [], 0
    for newline in re.finditer(r"\r\n|\r|\n", body):
        lines.append((position, body[position:newline.start()]))
        position = newline.end()
    lines.append((position, body[position:]))
    start = next((i for i, (_, text) in enumerate(lines) if _AC_HEADING.match(text)), None)
    scope = lines
    if start is not None:
        if sum(1 for _, text in lines[start:] if _AC_HEADING.match(text)) > 1:
            raise Fail(REFUSED, 'the issue has more than one "Acceptance criteria" section')
        level = len(re.match(r"#+", lines[start][1]).group(0))
        end = len(lines)
        for i in range(start + 1, len(lines)):
            nxt = re.match(r"(#{1,6})[ \t]", lines[i][1])
            if nxt and len(nxt.group(1)) <= level:
                end = i
                break
        scope = lines[start + 1:end]
    found = []
    for offset, text in scope:
        match = _AC_LINE.match(text)
        if match:
            found.append((int(match.group(2)), offset + match.start(1), match.group(1) != " "))
    return found


def _status_label(name):
    """The status label a status name means, or None for a name that only takes the issue out of the queue."""
    return STATUS_NAMES.get(" ".join(name.split()).casefold())


def _git(args, cwd):
    """One line of git output run in cwd, or None when git cannot answer (not a checkout, no such remote)."""
    try:
        done = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    line = done.stdout.strip()
    return line if done.returncode == 0 and line else None


_REMOTE = re.compile(r"(?:https://(?:[^@/]+@)?github\.com/|ssh://git@github\.com/|git@github\.com:)(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?")


def repo_from_origin(cwd):
    """owner/name of a github.com origin remote of the checkout at cwd, or None."""
    url = _git(["remote", "get-url", "origin"], cwd)
    found = _REMOTE.fullmatch(url) if url else None
    return found.group("repo") if found else None


class Tracker:
    def __init__(self, repo, env, stdin, err, cwd):
        self.repo, self.env, self.stdin, self.err, self.cwd = repo, env, stdin, err, cwd
        self.gh = shutil.which("gh", path=env.get("PATH")) or "gh"
        self.notes = []

    # -- gh ----------------------------------------------------------------------------------------------

    def api(self, method, path, body=None, missing_ok=False):
        """One gh api call as the ambient login; the parsed JSON answer, or None for no body (or a tolerated 404)."""
        command = [self.gh, "api", "-X", method, path]
        if body is not None:
            command += ["--input", "-"]
        try:
            done = subprocess.run(command, input=json.dumps(body) if body is not None else None, capture_output=True,
                                  text=True, timeout=120, env=self.env, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise Fail(GITHUB, f"gh api {method} {path} did not run: {type(error).__name__}") from None
        if done.returncode != 0:
            said = (done.stderr.strip() or done.stdout.strip()).splitlines()
            if missing_ok and any("HTTP 404" in line for line in said):
                return None
            raise Fail(GITHUB, f"gh api {method} {path} failed: {(said[-1] if said else 'exit ' + str(done.returncode))[:300]}")
        if not done.stdout.strip():
            return None
        try:
            return json.loads(done.stdout)
        except ValueError:
            raise Fail(GITHUB, f"gh api {method} {path} answered with something that is not JSON") from None

    def _issue(self, number):
        return f"/repos/{self.repo}/issues/{number}"

    def note(self, text):
        self.notes.append(text)
        print(f"tracker: {text}", file=self.err)

    # -- text and labels ------------------------------------------------------------------------------------

    def _checked(self, label, text):
        """Refuse empty, oversized or swept text; a refusal names the kind of finding, never the matched text."""
        if len(text) > MAX_TEXT:
            raise Fail(REFUSED, f"the {label} is over {MAX_TEXT} characters; nothing was sent")
        try:
            hits = textsafe.sweep(text)
        except textsafe.SweepUnavailable as unavailable:
            raise Fail(REFUSED, f"the {label} cannot be checked: {unavailable}; nothing was sent") from None
        if hits:
            kinds = ", ".join(sorted({h.kind for h in hits}))
            raise Fail(REFUSED, f"the {label} holds {len(hits)} sweep hit(s) ({kinds}); nothing was sent")
        return text

    def _ensure_labels(self, names):
        """Create any working label the repository does not have yet, so adding it cannot fail on a fresh repository."""
        for name in names:
            if self.api("GET", f"/repos/{self.repo}/labels/{urllib.parse.quote(name, safe='')}", missing_ok=True) is None:
                self.api("POST", f"/repos/{self.repo}/labels", {"name": name, "color": LABEL_COLOURS.get(name, "ededed")})

    def _target(self, number):
        """The issue a writing verb is about to change; a pull request (same number space) is refused before any write."""
        issue = self.api("GET", self._issue(number))
        if not isinstance(issue, dict):
            raise Fail(GITHUB, f"#{number} came back in a shape that is not an issue")
        if "pull_request" in issue:
            raise Fail(REFUSED, f"#{number} is a pull request, not an issue; nothing was sent")
        return issue

    def _labels(self, number):
        issue = self._target(number)
        return [x["name"] for x in issue.get("labels", [])]

    def _relabel(self, number, add=(), remove=()):
        """Add labels first, then drop the others, so an issue is never left without a status or priority it had."""
        current = {n.casefold() for n in self._labels(number)}
        adding = [n for n in add if n.casefold() not in current]
        dropping = [n for n in remove if n.casefold() in current and n not in add]
        if adding:
            self._ensure_labels(adding)
            self.api("POST", f"{self._issue(number)}/labels", {"labels": adding})
        for name in dropping:
            self.api("DELETE", f"{self._issue(number)}/labels/{urllib.parse.quote(name, safe='')}", missing_ok=True)
        return {"added": adding, "removed": dropping}

    def _set_status(self, number, name):
        """Give an issue the one status label a name means and drop the other two; a name with no label drops ready."""
        label = _status_label(name)
        if label is None:
            return self._relabel(number, remove=[READY])
        return self._relabel(number, add=[label], remove=[x for x in STATUS_LABELS if x != label])

    def _comment(self, number, text):
        made = self.api("POST", f"{self._issue(number)}/comments", {"body": text})
        return (made or {}).get("html_url")

    def _read_text(self, text, path, label):
        if text is not None and path is not None:
            raise Fail(USAGE, f"give the {label} once: --text or --file, not both")
        if path is not None:
            return self.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
        return text

    # -- commands -----------------------------------------------------------------------------------------

    def view(self, args):
        if not 0 <= args.comments <= 100:
            raise Fail(USAGE, "--comments is 0 to 100")
        issue = self.api("GET", self._issue(args.number))
        out = {
            "number": issue["number"], "title": issue["title"], "state": issue["state"], "state_reason": issue.get("state_reason"),
            "labels": [x["name"] for x in issue.get("labels", [])], "milestone": (issue.get("milestone") or {}).get("title"),
            "type": (issue.get("type") or {}).get("name"), "assignees": [x["login"] for x in issue.get("assignees", [])],
            "parent": issue.get("parent_issue_url"), "sub_issue_summary": issue.get("sub_issues_summary"),
            "dependencies": issue.get("issue_dependencies_summary"), "url": issue.get("html_url"),
            "updated_at": issue.get("updated_at"), "body": issue.get("body"),
        }
        if args.comments:
            human = [c for c in self._all(f"{self._issue(args.number)}/comments") if not _is_bot(c)]
            out["comments"] = [{"id": c["id"], "author": c["user"]["login"], "at": c.get("created_at"), "body": c["body"]}
                               for c in human[-args.comments:]]
        if args.edges:
            def brief(rows):
                return [{"number": r["number"], "title": r["title"], "state": r["state"]} for r in rows or []]
            out["blocked_by"] = brief(self.api("GET", f"{self._issue(args.number)}/dependencies/blocked_by?per_page=100"))
            out["sub_issues"] = brief(self.api("GET", f"{self._issue(args.number)}/sub_issues?per_page=100"))
        return out

    def _all(self, path, key=None):
        """Every row of a listing, 100 a page; a page that is not a list, or one past PAGES pages, is an error."""
        rows = []
        for page in range(1, PAGES + 1):
            sep = "&" if "?" in path else "?"
            data = self.api("GET", f"{path}{sep}per_page=100&page={page}")
            data = data.get(key) if key and isinstance(data, dict) else data
            if not isinstance(data, list):
                raise Fail(GITHUB, "a listing came back in a shape that is not a list; not reading that as empty")
            rows += data
            if len(data) < 100:
                return rows
        raise Fail(GITHUB, f"the listing runs past {PAGES} pages; not reading a cut-short listing as the whole of it")

    def queue(self, args):
        if args.limit is not None and args.limit < 1:
            raise Fail(USAGE, "--limit is 1 or more")
        for value in [args.status, *(args.label or [])]:
            if '"' in value or "," in value or "\n" in value or not value.strip():
                raise Fail(USAGE, "a status or label name is not empty and carries no quotes, commas or line breaks")
        status = _status_label(args.status) or "-".join(args.status.split()).casefold()
        labels = [status, *(args.label or [])]
        if args.all_repos:
            owner = self.repo.split("/")[0]
            kind = (self.api("GET", f"/users/{owner}") or {}).get("type")
            scope = "org" if kind == "Organization" else "user"
            terms = " ".join(["is:issue", "is:open", f"{scope}:{owner}", *(f'label:"{name}"' for name in labels)])
            found = self._all(f"/search/issues?q={urllib.parse.quote(terms)}", key="items")
        else:
            found = self._all(f"/repos/{self.repo}/issues?state=open&labels={urllib.parse.quote(','.join(labels))}")
        rows = []
        for issue in found:
            if "pull_request" in issue:
                continue
            names = [x["name"] for x in issue.get("labels", [])]
            hit = re.search(r"github\.com/([^/]+/[^/]+)/issues/", issue.get("html_url", ""))
            rows.append({"repo": hit.group(1) if hit else self.repo, "number": issue["number"], "title": issue.get("title"),
                         "status": args.status, "lane": None,
                         "priority": next((n for n in names if n.casefold() in PRIORITY_LABELS), None), "labels": names})
        truncated = args.limit is not None and len(rows) > args.limit
        rows = rows[:args.limit] if args.limit is not None else rows
        return {"items": rows, "count": len(rows), "truncated": truncated}

    def create(self, args):
        if bool(args.body_file) == bool(args.body_stdin):
            raise Fail(USAGE, "create needs exactly one of --body-file or --body-stdin")
        body = self.stdin.read() if args.body_stdin else Path(args.body_file).read_text(encoding="utf-8")
        if not body.strip():
            raise Fail(USAGE, "the issue body is empty")
        title = args.title
        if not title.strip() or "\n" in title or len(title) > 256:
            raise Fail(USAGE, "the title must be one line of 1 to 256 characters")
        labels = list(args.label or [])
        status, priority = args.status, args.priority
        for pair in args.field or []:
            name, sep, value = pair.partition("=")
            if not sep:
                raise Fail(USAGE, "--field wants NAME=VALUE")
            if name.casefold() == "status":
                status = value
            elif name.casefold() == "priority":
                priority = value
            else:
                self.note(f"--field {name} is ignored: there is no Project any more")
        if status is not None:
            label = _status_label(status)
            if label is None:
                self.note(f"status {status} has no label; the issue is created without a status label")
            else:
                labels.append(label)
        if priority is not None:
            if priority.casefold() not in PRIORITY_NAMES:
                raise Fail(USAGE, "a priority is p1, p2 or p3 (urgent and high are p1, medium p2, low p3)")
            labels.append(PRIORITY_NAMES[priority.casefold()])
        labels = list(dict.fromkeys(labels))
        payload = {"title": self._checked("title", title), "body": self._checked("body", body)}
        for key, value in (("labels", labels), ("type", args.type), ("milestone", args.milestone)):
            if value:
                payload[key] = value
        self._ensure_labels([n for n in labels if n in LABEL_COLOURS])
        made = self.api("POST", f"/repos/{self.repo}/issues", payload) or {}
        if not made.get("number"):
            raise Partial({"url": made.get("html_url"), "failed": "create-response"},
                          Fail(GITHUB, "the issue was created but the answer carries no number; find it before retrying"))
        number = made["number"]
        result = {"number": number, "url": made.get("html_url"), "labels": labels}
        step = "create"
        try:
            if args.parent:
                step = "parent"
                self.api("POST", f"{self._issue(args.parent)}/sub_issues", {"sub_issue_id": made["id"]})
            for blocker in args.blocked_by or []:
                step = "blocked-by"
                blocking = self.api("GET", self._issue(blocker))
                self.api("POST", f"{self._issue(number)}/dependencies/blocked_by", {"issue_id": blocking["id"]})
        except (Fail, KeyError, TypeError) as error:
            result.update(failed=step, error=str(error))
            raise Partial(result, error) from None
        return result

    def priority(self, args):
        value = PRIORITY_NAMES.get(args.value.casefold())
        if value is None:
            raise Fail(USAGE, "a priority is p1, p2 or p3 (urgent and high are p1, medium p2, low p3)")
        changed = self._relabel(args.number, add=[value], remove=[x for x in PRIORITY_LABELS if x != value])
        return {"number": args.number, "priority": value, "changed": bool(changed["added"] or changed["removed"]), **changed}

    def priority_check(self, args):
        return self.retired("priority-check", "priority is one p1, p2 or p3 label, so there is no field to drift from")

    def claim(self, args):
        """Take the issue out of the queue (drop ready); the lane is not recorded, there is no Lane field any more."""
        changed = self._relabel(args.number, remove=[READY])
        return {"number": args.number, "lane": args.lane, **changed}

    def status(self, args):
        text = self._read_text(args.text, args.file, "status text")
        if args.state is None and text is not None and _status_label(text.strip()) and "\n" not in text.strip():
            args.state, text = text.strip(), None
        if args.state is None and not (text and text.strip()):
            raise Fail(USAGE, "status needs a state (ready, blocked or needs-owner), --text or --file")
        if args.state is not None and _status_label(args.state) is None:
            raise Fail(USAGE, "a status state is ready, blocked or needs-owner")
        if text is not None and text.strip():
            self._checked("status text", text)
        result = {"number": args.number}
        if args.state is None:
            self._target(args.number)  # a state reads the issue through _set_status; a comment alone reads it here
        if args.state is not None:
            result.update(self._set_status(args.number, args.state))
        if text is not None and text.strip():
            result["comment"] = self._comment(args.number, text)
        return result

    def handoff(self, args):
        parts = {name: getattr(args, name) for name in ("scope", "verified", "blockers", "next")}
        text = "\n\n".join(f"**{name.capitalize()}:** {value}" for name, value in parts.items())
        text = self._checked("handoff text", "Handoff\n\n" + text)
        result = {"number": args.number, "status": args.status}
        result.update(self._set_status(args.number, args.status))
        result["comment"] = self._comment(args.number, text)
        return result

    def tick(self, args):
        found = re.fullmatch(r"(?:AC)?([0-9]+)", args.criterion, re.I)
        if not found:
            raise Fail(USAGE, "a criterion is AC2 or 2")
        wanted = int(found.group(1))
        issue = self._target(args.number)
        body = issue.get("body") or ""
        lines = [(offset, ticked) for n, offset, ticked in criteria_lines(body) if n == wanted]
        if len(lines) != 1:
            raise Fail(REFUSED, f"the issue has {len(lines)} AC{wanted} lines in its acceptance criteria; ticking needs exactly one")
        offset, ticked = lines[0]
        if ticked:
            return {"number": args.number, "criterion": f"AC{wanted}", "changed": False}
        edited = body[:offset] + "x" + body[offset + 1:]
        if (self._target(args.number).get("body") or "") != body:
            raise Fail(REFUSED, "the issue body changed while ticking; nothing was sent, run tick again")
        answer = self.api("PATCH", self._issue(args.number), {"body": edited}) or {}
        if (answer.get("body") or "") != edited:
            raise Fail(GITHUB, f"the body did not read back with AC{wanted} ticked")
        return {"number": args.number, "criterion": f"AC{wanted}", "changed": True}

    def close(self, args):
        evidence = self._read_text(args.why, args.evidence_file, "evidence")
        if (evidence is None or not evidence.strip()) and args.pr:
            evidence = f"Closed by pull request #{args.pr}."  # the merged pull request is the evidence
        if evidence is None or not evidence.strip():
            raise Fail(USAGE, "close needs evidence: --pr N, --why TEXT or --evidence-file PATH (the merged pull request "
                              "or commit, and any acceptance criterion left unmet); nothing was sent")
        issue = self._target(args.number)
        if issue.get("state") == "closed":
            return {"number": args.number, "state": "closed", "changed": False}
        lines = [f"Closed as {args.reason}."]
        if args.pr:
            lines.append(f"Pull request: #{args.pr}")
        lines += ["", evidence.strip()]
        try:
            open_criteria = [f"AC{n}" for n, _, ticked in criteria_lines(issue.get("body") or "") if not ticked]
        except Fail:
            open_criteria = []
        if open_criteria:
            lines += ["", "Acceptance criteria left unticked: " + ", ".join(open_criteria)]
        text = self._checked("evidence", "\n".join(lines))
        comment = self._comment(args.number, text)
        try:
            self.api("PATCH", self._issue(args.number), {"state": "closed", "state_reason": STATE_REASONS[args.reason]})
        except Fail as error:
            raise Partial({"number": args.number, "comment": comment, "failed": "close", "error": str(error)}, error) from None
        return {"number": args.number, "state": "closed", "reason": args.reason, "comment": comment, "unticked": open_criteria}

    def retired(self, verb, why):
        self.note(f"{verb} no longer does anything ({why}); nothing was sent")
        return {"retired": verb}

    def legacy(self, args):
        """The issue a legacy Backlog id became, from the repository's committed id map; no GitHub call."""
        if not LEGACY_ID.fullmatch(args.id):
            raise Fail(USAGE, "a legacy id looks like ABC-0560 or ABC-0560.01")
        root = _git(["rev-parse", "--show-toplevel"], self.cwd)
        if root is None:
            raise Fail(REFUSED, f"not inside a git checkout, so there is no {ID_MAP_PATH} to read")
        try:
            data = json.loads((Path(root) / ID_MAP_PATH).read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise Fail(REFUSED, f"{ID_MAP_PATH} is missing; the import writes it") from None
        except (OSError, UnicodeDecodeError, ValueError):
            raise Fail(REFUSED, f"{ID_MAP_PATH} is unreadable or not valid JSON") from None
        if (not isinstance(data, dict) or data.get("schema") != 1 or not isinstance(data.get("repo"), str)
                or data["repo"].lower() != self.repo.lower() or not isinstance(data.get("issues"), dict)):
            raise Fail(REFUSED, f"{ID_MAP_PATH} is not a schema 1 map for {self.repo}")
        wanted = args.id.upper()
        held = data.get("held") if isinstance(data.get("held"), dict) else {}
        reason = next((v for k, v in held.items() if str(k).upper() == wanted), None)
        if reason is not None:
            label = reason if isinstance(reason, str) and re.fullmatch(r"[A-Za-z0-9 _.-]{1,64}", reason) else "unstated"
            raise Fail(REFUSED, f"{wanted} was held back from the import (reason: {label}); it has no issue")
        entry = next((v for k, v in data["issues"].items() if str(k).upper() == wanted), None)
        if entry is None:
            raise Fail(REFUSED, f"{wanted} is not in {ID_MAP_PATH}")
        number = entry.get("number") if isinstance(entry, dict) else None
        if isinstance(number, bool) or not isinstance(number, int) or number < 1:
            raise Fail(REFUSED, f"{ID_MAP_PATH} has no issue number for {wanted}")
        return {"legacy": wanted, "repo": self.repo, "number": number, "url": f"https://github.com/{self.repo}/issues/{number}"}


def _is_bot(comment):
    user = comment.get("user") or {}
    return user.get("type") == "Bot" or str(user.get("login", "")).endswith("[bot]")


def build_parser():
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--repo", default=argparse.SUPPRESS, help="owner/name; default TRACKER_REPO, else the github.com origin remote of the current checkout")
    shared.add_argument("--project", default=argparse.SUPPRESS, help="ignored: there is no Project any more")
    parser = Parser(prog="tracker", description=__doc__.splitlines()[0], parents=[shared])
    sub = parser.add_subparsers(dest="command", required=True, parser_class=Parser)

    def add(name, helptext):
        return sub.add_parser(name, help=helptext, parents=[shared])

    p = add("view", "read one issue, field-selected")
    p.add_argument("number", type=int)
    p.add_argument("--comments", type=int, default=0, metavar="N", help="also the last N comments not written by a bot")
    p.add_argument("--edges", action="store_true", help="also blockers and sub-issues")
    p = add("queue", "list the open issues labelled ready (or another status)")
    p.add_argument("--status", default="Ready", help="Ready, Blocked or Needs owner; another name is read as a label")
    p.add_argument("--limit", type=int, default=None, help="at most this many rows (default: every one); the result says truncated when it stopped short")
    p.add_argument("--all-repos", action="store_true", help="every repository of this repository's owner, through issue search")
    p.add_argument("--label", action="append", help="only issues carrying this label too; repeat to require several")
    p = add("create", "create an issue, optionally labelled ready")
    p.add_argument("--title", required=True)
    p.add_argument("--body-file")
    p.add_argument("--body-stdin", action="store_true")
    p.add_argument("--label", action="append")
    p.add_argument("--type")
    p.add_argument("--milestone", type=int)
    p.add_argument("--parent", type=int)
    p.add_argument("--blocked-by", type=int, action="append")
    p.add_argument("--status", help="Ready, Blocked or Needs owner: adds that label")
    p.add_argument("--field", action="append", metavar="NAME=VALUE", help="Status and Priority become labels; any other field is ignored")
    p.add_argument("--priority", metavar="VALUE", help="p1, p2 or p3 (urgent and high are p1, medium p2, low p3): adds that label")
    p = add("priority", "set an issue's one priority label (p1, p2 or p3)")
    p.add_argument("number", type=int)
    p.add_argument("value", help="p1, p2 or p3 (urgent and high are p1, medium p2, low p3)")
    add("priority-check", "retired: does nothing")
    p = add("claim", "take an issue out of the queue (removes the ready label)")
    p.add_argument("number", type=int)
    p.add_argument("--lane", required=True)
    p.add_argument("--force", action="store_true", help="accepted; nothing to override any more")
    p = add("status", "set a status label (ready, blocked, needs-owner) and/or post a comment")
    p.add_argument("number", type=int)
    p.add_argument("state", nargs="?", help="ready, blocked or needs-owner")
    p.add_argument("--text")
    p.add_argument("--file", help="read the text from a file, or - for stdin")
    p = add("tick", "tick one acceptance criterion in the issue body")
    p.add_argument("number", type=int)
    p.add_argument("criterion", help="AC2 or 2")
    p = add("handoff", "hand an issue to a human: set a status label (default needs-owner) and post a handoff comment")
    p.add_argument("number", type=int)
    for part in ("scope", "verified", "blockers", "next"):
        p.add_argument(f"--{part}", required=True)
    p.add_argument("--status", default="Needs owner")
    p = add("close", "close an issue with an evidence comment")
    p.add_argument("number", type=int)
    p.add_argument("--pr", type=int, help="the pull request that did the work, named in the comment")
    p.add_argument("--reason", choices=sorted(STATE_REASONS), default="completed")
    p.add_argument("--why", help="the evidence text (required unless --pr is given; or --evidence-file)")
    p.add_argument("--evidence-file", help="read the evidence text from a file, or - for stdin")
    p = add("legacy", "print the issue a legacy Backlog id became, from the committed .github/tracker/id-map.json")
    p.add_argument("id", help="a legacy id such as ABC-0560 or ABC-0560.01")
    return parser


def resolve_repo(args_repo, env, cwd):
    """--repo, else TRACKER_REPO, else the checkout's github.com origin; refuse when none gives owner/name."""
    repo = args_repo if args_repo is not None else (env.get("TRACKER_REPO") or None)
    if repo is None:
        repo = repo_from_origin(cwd)
        if repo is None:
            raise Fail(USAGE, "no repository: set TRACKER_REPO (owner/name) or run inside a checkout whose origin is on github.com")
    if not REPO_SHAPE.fullmatch(repo):
        raise Fail(USAGE, "the repository must be owner/name")
    return repo


def main(argv=None, env=None, stdin=None, out=None, err=None, cwd=None):
    env = dict(os.environ if env is None else env)
    out, err, stdin = out or sys.stdout, err or sys.stderr, stdin or sys.stdin
    cwd = cwd or os.getcwd()
    try:
        args = build_parser().parse_args(argv)
        textsafe.configure(env.get("TRACKER_IDENTIFIER_PATTERNS") or None)
        if args.command == "legacy" or args.command == "priority-check":
            repo = resolve_repo(getattr(args, "repo", None), env, cwd) if args.command == "legacy" else None
        else:
            repo = resolve_repo(getattr(args, "repo", None), env, cwd)
        tracker = Tracker(repo, env, stdin, err, cwd)
        result = getattr(tracker, args.command.replace("-", "_"))(args)
    except Partial as partial:
        print(json.dumps(partial.result, separators=(",", ":")), file=out)
        print(f"tracker: {partial}", file=err)
        return GITHUB
    except Fail as failure:
        print(f"tracker: {failure}", file=err)
        return failure.code
    except UnicodeDecodeError:
        print("tracker: an input file is not UTF-8 text", file=err)
        return USAGE
    except OSError as unreadable:
        print(f"tracker: cannot read {unreadable.filename}: {unreadable.strerror}", file=err)
        return USAGE
    except (KeyError, TypeError, IndexError, AttributeError) as shape:
        print(f"tracker: GitHub answered in a shape this tool does not know ({type(shape).__name__}: {shape})", file=err)
        return GITHUB
    print(json.dumps(result, separators=(",", ":"), ensure_ascii=False), file=out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
