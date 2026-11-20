#!/usr/bin/env python3
"""The GitHub tracker: the loop root's only way to write issues and the Project (README.md).

Issue writes go out as TRACKER_ISSUE_IDENTITY (default the GitHub App, TRACKER_APP_TOKEN); Project writes as
TRACKER_PROJECT_IDENTITY (default the ambient gh login). Lanes never run this; they return status and tick
requests and the root applies them.
"""
import argparse
from datetime import datetime
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import urllib.parse

import ghclient
import textsafe

STATUS_MARKER = "<!-- agent-tracker:status -->"
HANDOFF_MARKER = "<!-- agent-tracker:handoff -->"
OVERFLOW_MARKER = "<!-- agent-tracker:overflow -->"
MARKERS = (STATUS_MARKER, HANDOFF_MARKER, OVERFLOW_MARKER)
# The check run that proves a pull request's acceptance evidence. A job called through a reusable workflow is
# named "<caller job> / <called job>", so a name ending in " / " + this counts too.
DEFAULT_EVIDENCE_CHECK = "Acceptance evidence"
# The reusable workflow that check must come from: a referenced workflow path (`owner/repo/path@ref`) starting with
# this, at a full commit on that repository's default branch. Only GitHub Actions runs count. This is a default a
# deployment overrides with TRACKER_EVIDENCE_WORKFLOW (its own reusable workflow).
DEFAULT_EVIDENCE_WORKFLOW = "rknightion/.github/.github/workflows/ac-evidence.yml@"
EVIDENCE_WORKFLOW_SHAPE = re.compile(r"(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/[^\s@]+\.ya?ml@")
EVIDENCE_APP = "github-actions"
FULL_SHA = re.compile(r"[0-9a-f]{40}")
# The evidence job writes exactly one notice annotation with this title on its own check run, whose message is
# "pr=<PR number> head=<head sha>" (github.event.pull_request): check runs belong to a commit, so this says which PR it judged.
BINDING_TITLE = "Acceptance evidence binding"
CHECK_RUN_PAGES = 10  # the most pages of check runs read; a listing that fills them may be cut short
CI_PATHS = (".github/workflows/", ".github/actions/")  # a PR touching these could rewrite the check that judges it
PR_FILES_CAP = 3000  # GitHub lists at most this many files of a pull request; a list that reaches it may be cut short
ID_MAP_PATH = ".github/tracker/id-map.json"  # written by a separate import tool (not part of this distribution) and committed
LEGACY_ID = re.compile(r"[A-Za-z][A-Za-z0-9]*-[0-9]+(?:\.[0-9]+)*")
REPO_SHAPE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
IDENTITIES = ("app", "user")
COMMENT_LIMIT = 60000
# A close with a reason writes its line first and keeps the status text it found beneath this separator.
CLOSE_LEAD = "Closed as "
CLOSED_OVER = "\n\n---\nStatus before closing:\n\n"
MAX_RAW = 200000  # characters of lane text read before any escaping; GitHub's own comment limit is far below this
# The classes GitHub's renderer gives an @mention, a team mention and an issue or pull request reference.
LIVE = re.compile(r"""class\s*=\s*["'][^"']*\b(?:user-mention|team-mention|issue-link)\b""")
USAGE, REFUSED, GITHUB, LIMITED = 2, 3, 4, 75
DRIFT = 5  # priority-check found items whose label and Project field disagree (the result is still printed); never 1, which a Python crash exits with
ITEM_PAGES = 500  # the most pages of Project items read in one listing; a listing that fills them may be cut short
# On a user-owned owner priority is a label and a Project Priority field kept equal; these are the values both have.
PRIORITY_PREFIX = "priority:"
PRIORITY_VALUES = ("low", "medium", "high")  # the values that have both a label and a Project option
URGENT = "urgent"  # the Project's Urgent option has no label: it is field-only, and a label with it is a mismatch
PRIORITY_SETTABLE = (*PRIORITY_VALUES, URGENT)
LEGACY_LINE = re.compile(r"^Legacy ID: (\S+)\s*$")  # the first line the separate import tool writes on an imported issue


# The closing grammar of the acceptance evidence checker (CLOSING, parseClosesTargets in ac-evidence-check.mjs),
# mirrored; the parity test holds this to a corpus whose answers were taken from that checker. ASCII digits and word characters, as a JavaScript regular expression reads them.
_REPO = r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+"
_CLOSING = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b:?[ \t]+(?:(?P<repo>" + _REPO + r")#(?P<n1>[0-9]+)|#(?P<n2>[0-9]+)"
    r"|https?://github\.com/(?P<urepo>" + _REPO + r")/issues/(?P<n3>[0-9]+))(?!\w)", re.I | re.A)


def closes_targets(body, default_repo):
    """Return the `repo#number` targets a pull request body closes, in order and without repeats."""
    seen, found = set(), []
    for match in _CLOSING.finditer(re.sub(r"<!--[\s\S]*?-->", "", body or "")):
        g = match.groupdict()
        target = f"{g['repo'] or g['urepo'] or default_repo}#{int(g['n1'] or g['n2'] or g['n3'])}"
        if target not in seen:
            seen.add(target)
            found.append(target)
    return found


# The acceptance-criteria section rules of the same checker (sectionOf, parseAcceptanceCriteria), mirrored with
# offsets into the original body so a tick edits one character in place.
_AC_HEADING = re.compile(r"^#{1,6}[ \t]+Acceptance criteria[ \t]*#*[ \t]*$", re.I | re.A)
_AC_LINE = re.compile(r"^[ \t]*[-*][ \t]+\[([ xX])\][ \t]+AC([0-9]+)[ \t]+\S.*$")


def criteria_lines(body):
    """Return (n, offset of its checkbox character) for each criterion line in the issue's criteria section.

    The section is the body under its one "Acceptance criteria" heading, down to the next heading of the same or
    a higher level; a body with no such heading is read whole. More than one such heading raises, as the checker fails.
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
            found.append((int(match.group(2)), offset + match.start(1)))
    return found


def _instant(stamp):
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        raise Fail(GITHUB, "GitHub sent a timestamp this tool cannot read; not guessing") from None


class Fail(Exception):
    """A command that cannot go on; code is the process exit status."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class Findings(Exception):
    """A read-only check that found something: the result is printed and the exit status is DRIFT."""

    def __init__(self, result):
        super().__init__("the check found items to look at")
        self.result = result


class Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse messages quote or list what was typed; a fixed message cannot repeat it.
        raise Fail(USAGE, "the command line is not valid; see `tracker --help` and `tracker <command> --help`")


def _value(raw):
    """Flatten a Project field value to a string, number or None, whatever wrapping the API gives it.

    A select can arrive as an object with a name, a text value as an object with raw and html parts;
    a shape none of those fit raises rather than reading as empty, because an empty read would let a
    claim overwrite another lane's.
    """
    if raw is None or isinstance(raw, (str, int, float)):
        return raw
    if isinstance(raw, dict):
        for key in ("name", "title", "text", "raw", "value"):
            if key in raw:
                return _value(raw[key])
    raise Fail(GITHUB, "a Project field value came back in a shape this tool does not know; not guessing")


def _label(name):
    """A field name for a message: the tool's own fields by name, anything the caller typed as 'such field'."""
    return f"field {name}" if name in ("Status", "Lane", "Priority") else "such field"


class Project:
    """A Project owned by an organisation or a user, reached through REST; field and option ids are resolved by name once per run.

    Reads use the ambient login; writes go out as the Project identity. The owner kind decides the REST prefix
    (`/orgs/<login>` or `/users/<login>`).
    """

    def __init__(self, client, repo, number, owner, kind, identity):
        self.client, self.number, self.owner, self.identity = client, number, owner, identity
        self.repo_owner, self.name = repo.split("/")
        self.base = f"/{'orgs' if kind == ORGANIZATION else 'users'}/{owner}/projectsV2/{number}"
        self._fields = None

    def fields(self):
        if self._fields is None:
            data = self.client.get(f"{self.base}/fields?per_page=100").data or []
            self._fields = {f["name"]: f for f in data}
        return self._fields

    def field(self, name):
        found = self.fields().get(name)
        if found is None:
            raise Fail(USAGE, f"the Project has no {_label(name)}; its fields are: {', '.join(sorted(self.fields()))}")
        return found

    def encode(self, name, raw):
        """Turn a field name and a human value into the REST value: an option id for a select, the text otherwise."""
        field = self.field(name)
        options = field.get("options")
        if options is None:
            if field.get("data_type") != "number":
                return str(raw)
            try:
                number = float(raw)
            except ValueError:
                raise Fail(USAGE, "that field is a number field and the value is not a number") from None
            if not math.isfinite(number):
                raise Fail(USAGE, "that field needs a finite number")
            return int(number) if number.is_integer() else number
        names = [(option, _value(option["name"])) for option in options]
        for option, label in names:
            if isinstance(label, str) and (label == raw or label.casefold() == str(raw).casefold()):
                return option["id"]
        raise Fail(USAGE, f"that field has no such option; its options are: {', '.join(str(label) for _, label in names)}")

    def item(self, number):
        """Add the issue to the Project and return the item id; an issue already on it keeps its existing item.

        GitHub answers a second add with 422 "Content already exists in this project", not the item, so that answer
        is resolved by reading the issue's Project items and taking this Project's one. A parent's sub-issues are
        added with it, so this is the ordinary path for a child created under a parent already on the Project."""
        body = {"type": "Issue", "owner": self.repo_owner, "repo": self.name, "number": number}
        try:
            return self.client.write("POST", f"{self.base}/items", body, identity=self.identity, issue=number).data["id"]
        except ghclient.GhError as error:
            # The message is the top-level one or sits in an errors list ({"type": "UNPROCESSABLE", "message": ...}).
            if error.status != 422 or not any("already exists" in text.lower() for text in ghclient._strings(error.data or error.message)):
                raise
        answer = self.client.graphql(
            "query($owner: String!, $name: String!, $number: Int!) { repository(owner: $owner, name: $name) { issue(number: $number) {"
            " projectItems(first: 50, includeArchived: true) { nodes { fullDatabaseId project { number owner { ... on Organization { login } ... on User { login } } } } } } } }",
            {"owner": self.repo_owner, "name": self.name, "number": number})
        nodes = ((((answer["data"].get("repository") or {}).get("issue") or {}).get("projectItems") or {}).get("nodes")) or []
        for node in nodes:
            project = (node or {}).get("project") or {}
            if str(project.get("number")) == str(self.number) and ((project.get("owner") or {}).get("login") or "").lower() == self.owner.lower():
                return int(node["fullDatabaseId"])
        raise Fail(GITHUB, f"GitHub says issue {number} is already on the Project but lists no item for it there")

    def read(self, item_id, names):
        query = "".join(f"&fields%5B%5D={self.field(n)['id']}" for n in names)
        data = self.client.get(f"{self.base}/items/{item_id}?{query[1:]}").data
        if not isinstance(data, dict) or not isinstance(data.get("fields"), list):
            raise Fail(GITHUB, "the Project item came back without an explicit fields array; not reading that as unset")
        return {f.get("name"): _value(f.get("value")) for f in data["fields"]}

    def set(self, item_id, values, issue):
        """Write several fields of one item in a single request; values maps field name to human value."""
        fields = [{"id": self.field(n)["id"], "value": self.encode(n, v)} for n, v in values.items()]
        self.client.write("PATCH", f"{self.base}/items/{item_id}", {"fields": fields}, identity=self.identity, issue=issue)


ORGANIZATION, USER_OWNER = "Organization", "User"


class Tracker:
    def __init__(self, client, repo, project_number, app_login, stdin, project_owner=None, issue_identity=ghclient.APP,
                 project_identity=ghclient.USER, evidence_check=DEFAULT_EVIDENCE_CHECK, evidence_workflow=DEFAULT_EVIDENCE_WORKFLOW):
        self.client, self.repo, self.stdin = client, repo, stdin
        self.app_login = app_login
        if project_number is not None and not re.fullmatch(r"[0-9]+", str(project_number)):
            raise Fail(USAGE, "the Project number is digits only")
        self.project_number = project_number
        self.project_owner = project_owner or repo.split("/")[0]
        self.issue_identity, self.project_identity = issue_identity, project_identity
        self.evidence_check = evidence_check
        found = EVIDENCE_WORKFLOW_SHAPE.fullmatch(evidence_workflow or "")
        if found is None:
            raise Fail(USAGE, "TRACKER_EVIDENCE_WORKFLOW is owner/repo/<path to the workflow file>@ (ending at the @)")
        self.evidence_workflow, self._workflow_repo = evidence_workflow, found.group("repo")
        self._suite_binding = {}
        self._suite_run = {}
        self._jobs = {}
        self._on_default_cache = {}
        self._workflow_default = None
        self._project = None
        self._kinds = {}
        self._writer = None
        self.cwd = os.getcwd()

    def owner_kind(self, login):
        """Organization or User for an account, read once per run; anything else is not guessed at."""
        if login.lower() not in self._kinds:
            data = self.client.get(f"/users/{login}").data
            kind = data.get("type") if isinstance(data, dict) else None
            if kind not in (ORGANIZATION, USER_OWNER):
                raise Fail(GITHUB, f"GitHub did not say whether {login} is an organisation or a user; not guessing")
            self._kinds[login.lower()] = kind
        return self._kinds[login.lower()]

    def require_org_repo(self, feature):
        """Refuse an organisation-only feature on a user-owned repository before anything is written."""
        owner = self.repo.split("/")[0]
        if self.owner_kind(owner) != ORGANIZATION:
            raise Fail(USAGE, f"{feature} needs an organisation-owned repository; {owner} is a user account, so nothing was sent")

    def require_user_repo(self, feature):
        """Refuse a user-owner feature on an organisation-owned repository before anything is written."""
        owner = self.repo.split("/")[0]
        if self.owner_kind(owner) != USER_OWNER:
            raise Fail(USAGE, f"{feature} needs a user-owned repository, where priority is a label and a Project field; {owner} is an "
                              "organisation, whose Priority is an issue field set at creation with create --priority. Nothing was sent")

    @property
    def project(self):
        if self._project is None:
            if not self.project_number:
                raise Fail(USAGE, "this command needs the Project: set TRACKER_PROJECT or pass --project")
            kind = self.owner_kind(self.project_owner)
            if kind == USER_OWNER and self.project_identity == ghclient.APP:
                raise Fail(USAGE, "TRACKER_PROJECT_IDENTITY=app cannot write a user-owned Project (GitHub Apps and fine-grained "
                                  "tokens cannot); use the user identity with a login that has the project scope. Nothing was sent")
            self._project = Project(self.client, self.repo, self.project_number, self.project_owner, kind, self.project_identity)
        return self._project

    @property
    def writer(self):
        """The login issue writes are made as, which a managed comment must have as its author."""
        if self._writer is None:
            if self.issue_identity == ghclient.APP:
                if not self.app_login:
                    raise Fail(USAGE, "TRACKER_APP_LOGIN is not set; it names the App's bot login (such as my-app[bot]) that owns the managed comments")
                self._writer = self.app_login
            else:
                login = (self.client.get("/user").data or {}).get("login")
                if not isinstance(login, str) or not login:
                    raise Fail(GITHUB, "GitHub did not say which login the ambient gh session is; not guessing")
                self._writer = login
        return self._writer

    def _issue(self, number):
        return f"/repos/{self.repo}/issues/{number}"

    def _issue_write(self, method, path, body=None, issue=None, baseline=None):
        return self.client.write(method, path, body, identity=self.issue_identity, issue=issue, baseline=baseline)

    # -- text intake ---------------------------------------------------------------------------------

    def _safe(self, label, text):
        """Refuse tracker markers and sweep hits in raw text, then escape it; a refusal names the kind, never the value."""
        if len(text) > MAX_RAW:
            self.client.audit_refusal(self.issue_identity, label, "text over the raw size bound")
            raise Fail(REFUSED, f"the {label} is over {MAX_RAW} characters; nothing was sent")
        if any(marker in text for marker in MARKERS):
            self.client.audit_refusal(self.issue_identity, label, "tracker marker in text")
            raise Fail(REFUSED, f"the {label} carries a tracker marker")
        try:
            hits = textsafe.sweep(text)
        except textsafe.SweepUnavailable as unavailable:
            self.client.audit_refusal(self.issue_identity, label, "sweep: patterns unavailable")
            raise Fail(REFUSED, f"the {label} cannot be checked: {unavailable}; nothing was sent") from None
        if hits:
            kinds = ", ".join(sorted({h.kind for h in hits}))
            self.client.audit_refusal(self.issue_identity, label, f"sweep: {kinds}")
            raise Fail(REFUSED, f"the {label} holds {len(hits)} sweep hit(s) ({kinds}); nothing was sent")
        return textsafe.escape(text)

    def _live(self, text):
        return bool(LIVE.search(self.client.render(text, self.repo, self.issue_identity)))

    def _refuse_live(self, label):
        self.client.audit_refusal(self.issue_identity, label, "live mention or reference after render")
        raise Fail(REFUSED, f"the {label} still renders a live mention or reference; nothing was sent")

    def _inert(self, label, prefix, safe, raw):
        """Return the text to post: safe if GitHub renders it with no mention or reference, else raw inside a fence.

        The escaper works on Markdown source; GitHub notifies on the rendered result, so the renderer has the
        last word. prefix is trusted text (a marker) that precedes the lane text and is rendered with it.
        """
        if not self._live(prefix + safe):
            return safe
        longest = max((len(run) for run in re.findall(r"`+", raw)), default=0)
        fence = "`" * max(3, longest + 1)
        fenced = f"{fence}\n{raw}\n{fence}"
        if self._live(prefix + fenced):
            self._refuse_live(label)
        return fenced

    def _inert_inline(self, label, safe):
        """Check text that cannot be fenced (a title, a field value): refuse if it renders live."""
        if self._live(safe):
            self._refuse_live(label)
        return safe

    # -- comments --------------------------------------------------------------------------------------

    def _page(self, number, page):
        """One page of comments; anything but a list means the read did not complete, which must never look like no comments."""
        data = self.client.get(f"{self._issue(number)}/comments?per_page=100&page={page}").data
        if not isinstance(data, list):
            raise Fail(GITHUB, f"issue {number}'s comments came back in a shape that is not a list; not reading that as empty")
        return data

    def _comments(self, number):
        page, found = 1, []
        while True:
            data = self._page(number, page)
            found += data
            if len(data) < 100:
                return found
            page += 1

    def _is_app(self, comment):
        return (comment.get("user") or {}).get("login") == self.writer

    def _is_human(self, comment):
        user = comment.get("user") or {}
        return user.get("type") != "Bot" and not str(user.get("login", "")).endswith("[bot]") and not any(m in (comment.get("body") or "") for m in MARKERS)

    def _own(self, comments, marker):
        return [c for c in comments if self._is_app(c) and marker in (c.get("body") or "")]

    def _set_status(self, number, text, keep_earlier=False):
        """Write the managed status comment. `keep_earlier` (a close with a reason) puts `text` first and keeps
        what the comment said before beneath it: the earlier text is often the acceptance evidence."""
        self.writer  # the managed comment's author must be known before anything is sent
        safe = self._safe("status text", text)
        if len(STATUS_MARKER) + len(safe) + 1 > COMMENT_LIMIT:
            raise Fail(REFUSED, f"the status comment would be over the {COMMENT_LIMIT} characters allowed")
        body = f"{STATUS_MARKER}\n{self._inert('status text', STATUS_MARKER + chr(10), safe, text)}"
        if len(body) > COMMENT_LIMIT:
            raise Fail(REFUSED, f"the status comment would be {len(body)} characters, over the {COMMENT_LIMIT} allowed")
        with self.client.issue_lock(self.repo, number):
            if keep_earlier:
                earlier = self._earlier_status(number)
                if earlier:
                    body += CLOSED_OVER + earlier
                    if len(body) > COMMENT_LIMIT:
                        raise Fail(REFUSED, f"the status comment would be {len(body)} characters with the earlier status kept, "
                                            f"over the {COMMENT_LIMIT} allowed; shorten --why")
            return self._upsert_status(number, body)

    def _earlier_status(self, number):
        """What the managed status comment says now, less any close line an earlier close attempt put above it, so a
        retried close replaces its own line and never stacks. Empty when there is no single managed comment (the
        upsert refuses more than one) or nothing but an earlier close line."""
        managed = self._own(self._comments(number), STATUS_MARKER)
        if len(managed) != 1:
            return ""
        text = managed[0]["body"].split(STATUS_MARKER, 1)[1].strip()
        if text.startswith(CLOSE_LEAD):
            return text.split(CLOSED_OVER, 1)[1].strip() if CLOSED_OVER in text else ""
        return text

    def _upsert_status(self, number, body):
        comments = self._comments(number)
        stray = [c for c in comments if self._is_app(c) and not any(m in (c.get("body") or "") for m in MARKERS)]
        if stray:
            raise Fail(REFUSED, f"issue {number} already has an App comment that is not its status comment; a second non-handoff comment is refused")
        managed = self._own(comments, STATUS_MARKER)
        if len(managed) > 1:
            raise Fail(REFUSED, f"issue {number} has {len(managed)} managed status comments ({', '.join(str(c['id']) for c in managed)}); remove the extras by hand")
        if managed:
            if managed[0]["body"] == body:
                return {"action": "unchanged", "comment": managed[0]["id"]}
            self._issue_write("PATCH", f"/repos/{self.repo}/issues/comments/{managed[0]['id']}", {"body": body}, issue=number)
            return {"action": "updated", "comment": managed[0]["id"]}
        made = self._issue_write("POST", f"{self._issue(number)}/comments", {"body": body}, issue=number)
        return {"action": "created", "comment": (made.data or {}).get("id")}

    # -- commands --------------------------------------------------------------------------------------

    def view(self, args):
        if not 0 <= args.comments <= 100:
            raise Fail(USAGE, "--comments is 0 to 100")
        issue = self.client.get(self._issue(args.number)).data
        out = {
            "number": issue["number"], "title": issue["title"], "state": issue["state"], "state_reason": issue.get("state_reason"),
            "labels": [x["name"] for x in issue.get("labels", [])], "milestone": (issue.get("milestone") or {}).get("title"),
            "type": (issue.get("type") or {}).get("name"), "assignees": [x["login"] for x in issue.get("assignees", [])],
            "parent": issue.get("parent_issue_url"), "sub_issue_summary": issue.get("sub_issues_summary"),
            "dependencies": issue.get("issue_dependencies_summary"), "url": issue.get("html_url"),
            "updated_at": issue.get("updated_at"), "body": issue.get("body"),
        }
        if args.comments:
            out["comments"] = self._recent_human_comments(args.number, issue.get("comments", 0), args.comments)
        if args.edges:
            def brief(rows):
                return [{"number": r["number"], "title": r["title"], "state": r["state"]} for r in rows or []]
            out["blocked_by"] = brief(self.client.get(f"{self._issue(args.number)}/dependencies/blocked_by?per_page=100").data)
            out["sub_issues"] = brief(self.client.get(f"{self._issue(args.number)}/sub_issues?per_page=100").data)
        return out

    def _recent_human_comments(self, number, count, wanted):
        if count == 0:
            return []
        first = max(1, math.ceil(count / 100))
        page = first
        found = []
        data = self._page(number, page)
        newer = [c for c in data if self._is_human(c)]
        # A comment posted since the count was read can push the tail onto the next page: follow a full page.
        while len(data) == 100:
            page += 1
            data = self._page(number, page)
            newer += [c for c in data if self._is_human(c)]
        found = newer
        for earlier in range(first - 1, 0, -1):
            if len(found) >= wanted:
                break
            data = self._page(number, earlier)
            found = [c for c in data if self._is_human(c)] + found
        return [{"id": c["id"], "author": c["user"]["login"], "at": c.get("created_at"), "body": c["body"]} for c in found[-wanted:]]

    def _project_items(self, q, names, per_page):
        """Every Project item matching the filter q, page by page, following the Link header's cursor to the last page.

        A page that is not a list, a repeated cursor or a listing that fills ITEM_PAGES is an error: a listing cut short must
        never read as the whole of it. Only the pages the caller asks for are fetched (the caller stops iterating).
        """
        after, seen = None, set()
        for _ in range(ITEM_PAGES):
            params = {"q": q, "per_page": per_page}
            if after:
                params["after"] = after
            query = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
            query += "".join(f"&fields%5B%5D={self.project.field(n)['id']}" for n in names)
            answer = self.client.get(f"{self.project.base}/items?{query}")
            if not isinstance(answer.data, list):
                raise Fail(GITHUB, "the Project's items came back in a shape that is not a list; not reading that as no items")
            yield from answer.data
            after = ghclient.next_cursor(answer)
            if after is None:
                return
            if after in seen:
                raise Fail(GITHUB, "the Project's items repeated a page cursor; not reading that as the end of the listing")
            seen.add(after)
        raise Fail(GITHUB, f"the Project's items run past {ITEM_PAGES} pages; not reading a cut-short listing as the whole of it")

    @staticmethod
    def _issue_item(item):
        """(repo, number, content, field values) of a Project item that is an issue, else None."""
        content = item.get("content") or {}
        found = re.search(r"github\.com/([^/]+/[^/]+)/issues/(\d+)", content.get("html_url", ""))
        if item.get("content_type") != "Issue" or not found:
            return None
        return found.group(1), int(found.group(2)), content, {f.get("name"): _value(f.get("value")) for f in item.get("fields", [])}

    def queue(self, args):
        if args.limit is not None and args.limit < 1:
            raise Fail(USAGE, "--limit is 1 or more")
        for value in [args.status, *(args.label or [])]:
            if '"' in value or "\n" in value or not value.strip():
                raise Fail(USAGE, "a status or label name is not empty and carries no quotes or line breaks")
        names = [n for n in ("Status", "Lane", "Priority") if n in self.project.fields()]
        labels = "".join(f' label:"{name}"' for name in args.label or [])
        scope = "" if args.all_repos else f" repo:{self.repo}"
        # With a limit, one row over it is asked for: it is what tells a listing the limit cut short from one that ended there.
        per_page = 100 if args.limit is None else min(args.limit + 1, 100)
        rows, truncated = [], False
        for item in self._project_items(f'is:issue is:open{scope} status:"{args.status}"{labels}', names, per_page):
            found = self._issue_item(item)
            if found is None:
                continue
            if args.limit is not None and len(rows) == args.limit:
                truncated = True
                break
            repo, number, content, values = found
            rows.append({"repo": repo, "number": number, "title": content.get("title"),
                         "status": values.get("Status"), "lane": values.get("Lane"), "priority": values.get("Priority")})
        return {"items": rows, "count": len(rows), "truncated": truncated}

    def _sync_priority(self, labels, wanted):
        """Make a user-owned create's priority label and Project Priority field the same value; returns the labels to send.

        wanted (field name to value) gains the Priority field when a label asks for one and a Project is configured; the
        label is added when only a low, medium or high field was given. Urgent is field-only: it adds no label and is a
        conflict beside one. Two different values, or a label with no Project counterpart, are a usage error raised before
        anything is written. Any other option the Project's field carries passes through `--field` untouched and is ignored.
        """
        values = {l[len(PRIORITY_PREFIX):].casefold() for l in labels if l.casefold().startswith(PRIORITY_PREFIX)}
        if len(values) > 1:
            raise Fail(USAGE, "two priority labels were given")
        if values - set(PRIORITY_VALUES):
            raise Fail(USAGE, f"a priority label is one of: {', '.join(PRIORITY_PREFIX + v for v in PRIORITY_VALUES)} (Urgent is a Project field value with no label)")
        field = wanted["Priority"].casefold() if "Priority" in wanted else None
        if field is not None and field not in PRIORITY_SETTABLE:
            return labels
        if field == URGENT:
            if values:
                raise Fail(USAGE, "the priority label and the Project Priority field name different values (Urgent has no label)")
            return labels
        values.update({field} if field else set())
        if len(values) > 1:
            raise Fail(USAGE, "the priority label and the Project Priority field name different values")
        (value,) = values
        if self.project_number:
            wanted["Priority"] = value
        if not any(l.casefold().startswith(PRIORITY_PREFIX) for l in labels):
            labels = [*labels, PRIORITY_PREFIX + value]
        return labels

    def _priority(self, wanted):
        """The `issue_field_values` entry for a Priority name, read from the organisation's issue fields before any write.

        This is the organisation-level issue field, not a Project field (`--field` reaches only those): its id and options
        are the organisation's, and the value rides the issue creation itself. The name is matched without case and sent
        as the option is spelled; a value the field does not offer is refused with the ones it does.
        """
        fields = self.client.get(f"/orgs/{self.repo.split('/')[0]}/issue-fields").data
        if not isinstance(fields, list):
            raise Fail(GITHUB, "the organisation's issue fields came back in a shape that is not a list; not reading that as no Priority field")
        field = next((f for f in fields if isinstance(f, dict) and f.get("name") == "Priority"), None)
        if field is None or field.get("data_type") != "single_select" or not isinstance(field.get("options"), list) or field.get("id") is None:
            raise Fail(USAGE, "the organisation has no single-select Priority issue field, so --priority cannot be used; nothing was sent")
        names = [o["name"] for o in field["options"]]
        for name in names:
            if isinstance(name, str) and name.casefold() == wanted.casefold():
                return [{"field_id": field["id"], "value": name}]
        raise Fail(USAGE, f"that Priority has no such value; the allowed values are: {', '.join(str(n) for n in names)}")

    def create(self, args):
        body = self.stdin.read() if args.body_stdin else Path(args.body_file).read_text(encoding="utf-8")
        if not body.strip():
            raise Fail(USAGE, "the issue body is empty")
        if args.type:
            self.require_org_repo("--type (issue types)")
        if args.priority is not None:
            self.require_org_repo("--priority (the organisation's Priority issue field)")
        wanted = {}
        if args.status:
            wanted["Status"] = args.status
        for pair in args.field or []:
            name, sep, value = pair.partition("=")
            if not sep:
                raise Fail(USAGE, "--field wants NAME=VALUE")
            wanted[name] = value
        if wanted and not self.project_number:
            raise Fail(USAGE, "--status and --field need the Project: set TRACKER_PROJECT or pass --project")
        labels = list(args.label or [])
        if "Priority" in wanted or any(l.casefold().startswith(PRIORITY_PREFIX) for l in labels):
            if self.owner_kind(self.repo.split("/")[0]) == USER_OWNER:
                labels = self._sync_priority(labels, wanted)
        for name, value in wanted.items():
            self._safe("field name", name)
            safe = self._safe("field value", value)
            if self.project.field(name).get("data_type") == "text":
                wanted[name] = value = self._inert_inline("field value", safe)
            self.project.encode(name, value)
        priority = self._priority(args.priority) if args.priority is not None else None
        title, text = self._safe("title", args.title), self._safe("body", body)
        if not title.strip() or "\n" in title or len(title) > 256:
            raise Fail(USAGE, "the title must be one line of 1 to 256 characters")
        title = self._inert_inline("title", title)
        text = self._inert("body", "", text, body)
        payload = {"title": title, "body": text}
        for key, value in (("labels", labels), ("type", args.type), ("milestone", args.milestone)):
            if value:
                payload[key] = value
        if priority:
            payload["issue_field_values"] = priority
        made = self._issue_write("POST", f"/repos/{self.repo}/issues", payload).data
        if not made.get("number"):
            raise PartialResult({"url": made.get("html_url"), "failed": "create-response"}, Fail(GITHUB, "the issue was created but GitHub's answer carries no number; find it by its URL before retrying"))
        number = made["number"]
        result = {"number": number, "url": made.get("html_url")}
        step = "create"
        try:
            if args.parent:
                step = "parent"
                self._issue_write("POST", f"{self._issue(args.parent)}/sub_issues", {"sub_issue_id": made["id"]}, issue=args.parent)
            for blocker in args.blocked_by or []:
                step = "blocked-by"
                blocking = self.client.get(self._issue(blocker)).data
                self._issue_write("POST", f"{self._issue(number)}/dependencies/blocked_by", {"issue_id": blocking["id"]}, issue=number)
            if self.project_number:
                step = "project"
                item = self.project.item(number)
                if wanted:
                    self.project.set(item, wanted, number)
        except (Fail, ghclient.GhError, ghclient.Refused, ghclient.RateLimited, KeyError, TypeError, IndexError) as error:
            result.update(failed=step, error=str(error))
            raise PartialResult(result, error) from None
        return result

    def priority(self, args):
        """Change an issue's priority on a user-owned owner: the one priority:* label and the Project Priority field, together.

        Urgent is field-only: it sets the field and removes every priority label. The wanted label is added before any other is
        removed, so the issue is never without one; the result says what each half actually wrote, even when a step fails.
        """
        value = args.value.casefold()
        if value not in PRIORITY_SETTABLE:
            raise Fail(USAGE, f"a priority is one of: {', '.join(PRIORITY_SETTABLE)}")
        self.require_user_repo("priority")
        self.project.encode("Priority", value)  # the Project and its option are known before anything is written
        wanted = None if value == URGENT else PRIORITY_PREFIX + value
        result = {"number": args.number, "priority": value.capitalize(), "changed": False, "label": "unchanged", "field": "unchanged"}
        step = "read"
        try:
            with self.client.issue_lock(self.repo, args.number):
                names = [x["name"] for x in self.client.get(self._issue(args.number)).data.get("labels", [])]
                item = self.project.item(args.number)
                current = self.project.read(item, ["Priority"]).get("Priority")
                stale = [n for n in names if n.casefold().startswith(PRIORITY_PREFIX) and n.casefold() != wanted]
                add = wanted is not None and not any(n.casefold() == wanted for n in names)
                set_field = str(current or "").casefold() != value
                total, done = len(stale) + add, 0
                result.update(label="not written" if total else "unchanged", field="not written" if set_field else "unchanged")
                step = "label"
                if add:
                    self._issue_write("POST", f"{self._issue(args.number)}/labels", {"labels": [wanted]}, issue=args.number)
                    done += 1
                    result.update(changed=True, label="written" if done == total else "partly written")
                for name in stale:
                    self._issue_write("DELETE", f"{self._issue(args.number)}/labels/{urllib.parse.quote(name, safe='')}", issue=args.number)
                    done += 1
                    result.update(changed=True, label="written" if done == total else "partly written")
                step = "project"
                if set_field:
                    self.project.set(item, {"Priority": value}, args.number)
                    result.update(changed=True, field="written")
        except (Fail, ghclient.GhError, ghclient.Refused, ghclient.RateLimited, KeyError, TypeError, IndexError) as error:
            if step == "read":
                raise
            result.update(failed=step, error=str(error))
            raise PartialResult(result, error) from None
        return result

    def priority_check(self, args):
        """List open issues whose priority label and Project Priority field disagree or of which only one is set; read-only.

        Urgent is field-only, so an Urgent field with no label is in sync and with any priority label a mismatch; a field
        option other than low, medium, high or urgent with no label is ignored.
        """
        self.require_user_repo("priority-check")
        self.project.field("Priority")  # the Project must have one before anything is read
        found, checked = [], 0
        for item in self._project_items(f"is:issue is:open repo:{self.repo}", ["Priority"], 100):
            issue = self._issue_item(item)
            if issue is None:
                continue
            _, number, content, values = issue
            if not isinstance(content.get("labels"), list):
                raise Fail(GITHUB, "a Project item came back without its labels; not reading that as no priority label")
            checked += 1
            labels = sorted(x["name"] for x in content["labels"] if str(x.get("name", "")).casefold().startswith(PRIORITY_PREFIX))
            value = values.get("Priority")
            seen = None if value is None else str(value).casefold()
            if not labels and value is None:
                continue
            if labels and value is None:
                problem = "field missing"
            elif not labels:
                if seen not in PRIORITY_VALUES:
                    continue
                problem = "label missing"
            elif seen in PRIORITY_VALUES and {n[len(PRIORITY_PREFIX):].casefold() for n in labels} == {seen}:
                continue
            else:
                problem = "mismatch"
            found.append({"number": number, "title": content.get("title"), "label": labels, "field": value, "problem": problem})
        result = {"items": found, "count": len(found), "checked": checked}
        if found:
            raise Findings(result)
        return result

    def claim(self, args):
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", args.lane):
            raise Fail(USAGE, "a lane name is 1 to 64 letters, digits, dot, dash or underscore")
        self.project.field("Status"), self.project.field("Lane")
        self.project.encode("Status", "In progress")
        with self.client.issue_lock(self.repo, args.number):
            item = self.project.item(args.number)
            current = self.project.read(item, ["Status", "Lane"])
            holder = current.get("Lane")
            if str(current.get("Status", "")).casefold() == "in progress" and holder:
                if holder == args.lane:
                    return {"number": args.number, "lane": args.lane, "changed": False}
                if not args.force:
                    raise Fail(REFUSED, f"issue {args.number} is already claimed by lane {holder}; pass --force to take it over")
            self.project.set(item, {"Status": "In progress", "Lane": args.lane}, args.number)
        return {"number": args.number, "lane": args.lane, "changed": True}

    def status(self, args):
        text = self.stdin.read() if args.file == "-" else Path(args.file).read_text(encoding="utf-8") if args.file else args.text
        if not (text or "").strip():
            raise Fail(USAGE, "the status text is empty; refusing to replace the managed status comment")
        return {"number": args.number, **self._set_status(args.number, text)}

    def handoff(self, args):
        self.writer  # the managed comment's author must be known before anything is sent
        parts = {"Scope": args.scope, "Verified": args.verified, "Blockers": args.blockers, "Next": args.next}
        template = "**Handoff**\n" + "\n".join(f"- {label}: {text}" for label, text in parts.items())
        body = f"{HANDOFF_MARKER}\n{self._inert('handoff text', HANDOFF_MARKER + chr(10), self._safe('handoff text', template), template)}"
        if len(body) > COMMENT_LIMIT:
            raise Fail(REFUSED, "the handoff comment is too long")
        self.project.encode("Status", args.status)
        with self.client.issue_lock(self.repo, args.number):
            return self._post_handoff(args, body)

    def _post_handoff(self, args, body):
        comments = self._comments(args.number)
        last = max((i for i, c in enumerate(comments) if self._own([c], HANDOFF_MARKER)), default=None)
        if last is not None and not any(self._is_human(c) for c in comments[last + 1:]):
            raise Fail(REFUSED, f"issue {args.number} already has a handoff no human has answered; a second one is refused")
        self.project.set(self.project.item(args.number), {"Status": args.status}, args.number)
        made = self._issue_write("POST", f"{self._issue(args.number)}/comments", {"body": body}, issue=args.number)
        return {"number": args.number, "status": args.status, "comment": (made.data or {}).get("id")}

    def tick(self, args):
        if not re.fullmatch(r"(?i)(?:AC)?[0-9]+", args.criterion):
            raise Fail(USAGE, "a criterion is written AC2 or 2")
        wanted = int(re.sub(r"(?i)^ac", "", args.criterion))
        with self.client.issue_lock(self.repo, args.number):
            body = self.client.get(self._issue(args.number)).data["body"] or ""
            found = [offset for n, offset in criteria_lines(body) if n == wanted]
            if len(found) != 1:
                raise Fail(REFUSED, f"issue {args.number} has {len(found)} criterion lines for AC{wanted} in its criteria section; exactly one is needed")
            if body[found[0]] in "xX":
                return {"number": args.number, "criterion": f"AC{wanted}", "changed": False}
            ticked = body[:found[0]] + "x" + body[found[0] + 1:]
            self._issue_write("PATCH", self._issue(args.number), {"body": ticked}, issue=args.number, baseline=body)
            if (self.client.get(self._issue(args.number)).data["body"] or "") != ticked:
                raise Fail(GITHUB, f"verification failed: issue {args.number} does not read back with AC{wanted} ticked")
        return {"number": args.number, "criterion": f"AC{wanted}", "changed": True}

    def close(self, args):
        if bool(args.pr) == bool(args.reason) or (args.reason and not args.why) or (args.pr and (args.why or args.reason)):
            raise Fail(USAGE, "close needs --pr <merged PR>, or --reason not-planned|duplicate|completed with --why <text>, never both")
        if args.reason:
            self._safe("reason", args.why)
            reason = {"not-planned": "not_planned", "duplicate": "duplicate", "completed": "completed"}[args.reason]
            if args.reason == "completed" and not self._evidence_link.search(args.why):
                raise Fail(REFUSED, f"--reason completed needs an https://github.com/{self.repo}/ link to an issue comment, pull request, commit or run in --why")
        else:
            reason = "completed"
        result = {"number": args.number, "changed": False}
        if args.pr:
            self._require_evidence(args.number, args.pr)
        found = self.client.get(self._issue(args.number)).data
        if args.reason == "completed":
            legacy = LEGACY_LINE.match((found.get("body") or "").split("\n", 1)[0])
            if not legacy:
                raise Fail(REFUSED, f"issue {args.number} is not an imported issue (its body does not start with a Legacy ID line); a native issue closes through a merged pull request with --pr")
            self._require_mapped(legacy.group(1), args.number)
        if found["state"] != "closed":
            if args.reason:
                lead = "Closed as completed (code landed before the cutover)" if args.reason == "completed" else f"Closed as {args.reason.replace('-', ' ')}"
                result["status_comment"] = self._set_status(args.number, f"{lead}: {args.why}", keep_earlier=True)
            self._issue_write("PATCH", self._issue(args.number), {"state": "closed", "state_reason": reason}, issue=args.number)
            result.update(changed=True, state_reason=reason)
        try:
            result["project"] = self._mark_done(args.number)
        except (Fail, ghclient.GhError, ghclient.Refused, ghclient.RateLimited, KeyError, TypeError, IndexError) as error:
            result.update(failed="project", error=str(error))
            raise PartialResult(result, error) from None
        return result

    @property
    def _evidence_link(self):
        """An https github.com link into this repository to an issue (or comment), pull request, commit or workflow run."""
        return re.compile(rf"(?<![\w./@:-])https://github\.com/{re.escape(self.repo)}/(?:issues/[0-9]+|pull/[0-9]+|commit/[0-9a-f]{{7,40}}|actions/runs/[0-9]+)(?![\w/-])", re.I)

    def _mark_done(self, number):
        """Set Project Status Done; idempotent, so a retry after a failed Project write heals it."""
        if not self.project_number:
            return "skipped: TRACKER_PROJECT is not set"
        self.project.encode("Status", "Done")
        item = self.project.item(number)
        if str(self.project.read(item, ["Status"]).get("Status", "")).casefold() == "done":
            return "unchanged"
        self.project.set(item, {"Status": "Done"}, number)
        return "done"

    def _require_evidence(self, number, pr_number):
        pr = self.client.get(f"/repos/{self.repo}/pulls/{pr_number}").data
        if not pr.get("merged"):
            raise Fail(REFUSED, f"PR {pr_number} is not merged")
        if ((pr.get("base") or {}).get("repo") or {}).get("full_name", "").lower() != self.repo.lower():
            raise Fail(REFUSED, f"PR {pr_number} is not a pull request of {self.repo}")
        default = self.client.get(f"/repos/{self.repo}").data["default_branch"]
        if (pr.get("base") or {}).get("ref") != default:
            raise Fail(REFUSED, f"PR {pr_number} was not merged into the default branch")
        if f"{self.repo}#{number}".lower() not in [t.lower() for t in closes_targets(pr.get("body"), self.repo)]:
            raise Fail(REFUSED, f"PR {pr_number} does not close issue {number} (no closing keyword for it in its description)")
        self._refuse_ci_changes(number, pr_number, pr.get("changed_files"))
        check, head = self.evidence_check, pr["head"]["sha"]
        runs = [r for r in self._check_runs(head) if isinstance(r, dict) and _is_evidence(r.get("name"), check)
                and (r.get("app") or {}).get("slug") == EVIDENCE_APP]
        if not runs:
            raise Fail(REFUSED, f"PR {pr_number} has no {check} check run from GitHub Actions")
        by_name = {}
        for run in runs:
            by_name.setdefault(run["name"], []).append(run)
        # Every name that matches must pass on its newest run: a stub job carrying the name cannot outvote the real one.
        edited = unread = object()  # the description's last edit, read once when a run first needs it
        for name in sorted(by_name):
            latest = max(by_name[name], key=lambda r: r["id"])
            if latest.get("status") != "completed" or latest.get("conclusion") != "success":
                raise Fail(REFUSED, f"the latest {name} run of PR {pr_number} is {latest.get('conclusion') or latest.get('status')}, not success")
            # Bind the success to what it checked. The run must be for the merged head, and must have started after the
            # PR description was last edited: the job reads the description live but is not re-run by an edit, so an old
            # success says nothing about a closing keyword added later. (The issue's own criteria are not bound: a tick
            # edits them legitimately after the check.)
            if latest.get("head_sha") != head:
                raise Fail(REFUSED, f"the {name} run is not for the head commit PR {pr_number} was merged at")
            unbound = self._unbound(latest) or self._unbound_run(latest, pr_number, head)
            if unbound:
                raise Fail(REFUSED, f"the latest {name} run of PR {pr_number} is not bound to {self.evidence_workflow}: {unbound}")
            if edited is unread:
                owner, repo_name = self.repo.split("/")
                edited = self.client.graphql(
                    "query($owner: String!, $name: String!, $pr: Int!) { repository(owner: $owner, name: $name) { pullRequest(number: $pr) { lastEditedAt } } }",
                    {"owner": owner, "name": repo_name, "pr": pr_number})["data"]["repository"]["pullRequest"]["lastEditedAt"]
            if edited is not None and _instant(edited) > _instant(latest["started_at"]):
                raise Fail(REFUSED, f"PR {pr_number}'s description was edited after the {name} run started; re-run the check")

    def _refuse_ci_changes(self, number, pr_number, changed):
        """Refuse a PR that changed CI (.github/workflows or .github/actions): its own run cannot judge it.

        Every page of the PR's files is read; a list that cannot be read whole (an error, a shape that is not a
        list of named files, a count that disagrees with the PR's, or one at GitHub's cap) refuses too.
        """
        unreadable = f"PR {pr_number}'s changed files cannot be read whole, so whether it changes CI is unknown; refusing"
        files, page = [], 1
        while True:
            try:
                data = self.client.get(f"/repos/{self.repo}/pulls/{pr_number}/files?per_page=100&page={page}").data
            except ghclient.GhError as error:
                raise Fail(REFUSED, f"{unreadable} ({error})") from None
            if not isinstance(data, list) or not all(isinstance(f, dict) and isinstance(f.get("filename"), str) and f["filename"] for f in data):
                raise Fail(REFUSED, unreadable)
            files += data
            if len(data) < 100 or len(files) >= PR_FILES_CAP:
                break
            page += 1
        if len(files) >= PR_FILES_CAP:
            raise Fail(REFUSED, f"PR {pr_number} lists {len(files)} changed files, GitHub's cap of {PR_FILES_CAP}, so the list may be cut short; refusing")
        if not isinstance(changed, bool) and isinstance(changed, int) and changed != len(files):
            raise Fail(REFUSED, f"{unreadable} (it says {changed} files, the list holds {len(files)})")
        touched = sorted({path for f in files for path in (f.get("filename"), f.get("previous_filename"))
                          if isinstance(path, str) and path.startswith(CI_PATHS)})
        if touched:
            raise Fail(REFUSED, f"PR {pr_number} changes CI ({', '.join(touched[:5])}{', ...' if len(touched) > 5 else ''}); a PR that changes CI "
                                f"cannot judge itself, so hand issue {number} to the owner with `tracker handoff`")

    def _unbound(self, run):
        """Why a check run is not proof from the real evidence workflow, or None when it is; read once per check suite.

        The run's workflow run (found by its check suite) must reference the evidence workflow at a full commit
        that is on the default branch of the workflow's repository, so a pull request cannot supply its own copy.
        """
        suite = (run.get("check_suite") or {}).get("id")
        if isinstance(suite, bool) or not isinstance(suite, int):
            return "the run names no check suite"
        if suite not in self._suite_binding:
            data = self.client.get(f"/repos/{self.repo}/actions/runs?check_suite_id={suite}").data
            found = data.get("workflow_runs") if isinstance(data, dict) else None
            if not isinstance(found, list):
                raise Fail(GITHUB, "the workflow runs came back in a shape that is not a list; not reading that as unbound")
            if len(found) != 1 or not isinstance(found[0], dict) or isinstance(found[0].get("id"), bool) or not isinstance(found[0].get("id"), int):
                why = f"its check suite has {len(found)} workflow runs, not one"
            else:
                self._suite_run[suite] = found[0]["id"]
                refs = found[0].get("referenced_workflows")
                if not isinstance(refs, list):
                    refs = []
                candidates = [r for r in refs if isinstance(r, dict) and isinstance(r.get("path"), str)
                              and r["path"].startswith(self.evidence_workflow)]
                if not candidates:
                    why = "its workflow run does not call that workflow"
                elif not any(isinstance(r.get("sha"), str) and FULL_SHA.fullmatch(r["sha"]) for r in candidates):
                    why = "its workflow run does not call that workflow at a full commit"
                elif any(isinstance(r.get("sha"), str) and FULL_SHA.fullmatch(r["sha"]) and self._on_default(r["sha"]) for r in candidates):
                    why = None
                else:
                    why = f"it calls that workflow at a commit that is not on the default branch of {self._workflow_repo}"
            self._suite_binding[suite] = why
        return self._suite_binding[suite]

    def _unbound_run(self, run, pr_number, head):
        """Why one check run is not this PR's evidence, or None: it must be a job of its workflow run and carry the
        one binding annotation naming this PR and its merged head. Unreadable answers refuse."""
        workflow_run = self._suite_run.get(run["check_suite"]["id"])
        if run["id"] not in self._job_check_runs(workflow_run):
            return f"check run {run['id']} is not a job of its workflow run {workflow_run} (an API-made check run has no job)"
        notes = [a for a in self._annotations(run["id"]) if a.get("title") == BINDING_TITLE]
        if len(notes) != 1:
            return f"it carries {len(notes)} '{BINDING_TITLE}' annotations, not exactly one"
        want = f"pr={pr_number} head={head}"
        if notes[0].get("annotation_level") != "notice" or notes[0].get("message") != want:
            return f"its '{BINDING_TITLE}' annotation does not read `{want}` as a notice, so it judged another pull request or head"
        return None

    def _pages(self, path, key, what):
        """Every page of a listing (key None for a bare list); an error or a page that is not a list of objects refuses."""
        found, page = [], 1
        while True:
            try:
                data = self.client.get(f"{path}per_page=100&page={page}").data
            except ghclient.GhError as error:
                raise Fail(REFUSED, f"the {what} cannot be read ({error}); not judging the evidence without them") from None
            batch = data if key is None else (data.get(key) if isinstance(data, dict) else None)
            if not isinstance(batch, list) or not all(isinstance(x, dict) for x in batch):
                raise Fail(REFUSED, f"the {what} came back in a shape that is not a list; not judging the evidence without them")
            found += batch
            if len(batch) < 100:
                return found
            page += 1

    def _job_check_runs(self, workflow_run):
        """The check run ids of every job of a workflow run, all attempts; read once per run."""
        if workflow_run not in self._jobs:
            jobs = self._pages(f"/repos/{self.repo}/actions/runs/{workflow_run}/jobs?filter=all&", "jobs", f"jobs of workflow run {workflow_run}")
            ids = set()
            for job in jobs:
                found = re.search(r"/check-runs/([0-9]+)$", job.get("check_run_url") or "") if isinstance(job.get("check_run_url"), str) else None
                if found:
                    ids.add(int(found.group(1)))
            self._jobs[workflow_run] = ids
        return self._jobs[workflow_run]

    def _annotations(self, check_run):
        return self._pages(f"/repos/{self.repo}/check-runs/{check_run}/annotations?", None, f"annotations of check run {check_run}")

    def _on_default(self, sha):
        """True when sha is the default branch of the evidence workflow's repository or one of its ancestors."""
        if sha not in self._on_default_cache:
            if self._workflow_default is None:
                default = (self.client.get(f"/repos/{self._workflow_repo}").data or {}).get("default_branch")
                if not isinstance(default, str) or not default:
                    raise Fail(GITHUB, f"GitHub did not name the default branch of {self._workflow_repo}; not guessing")
                self._workflow_default = default
            branch = urllib.parse.quote(self._workflow_default, safe="")
            try:
                status = (self.client.get(f"/repos/{self._workflow_repo}/compare/{sha}...{branch}?per_page=1").data or {}).get("status")
            except ghclient.GhError as error:
                if error.status not in (404, 422):
                    raise
                status = None  # a commit the repository does not know, or shares no history with
            self._on_default_cache[sha] = status in ("ahead", "identical")
        return self._on_default_cache[sha]

    def _require_mapped(self, legacy_id, number):
        """Refuse unless the committed id map says legacy_id became this issue of this repository."""
        data = self._id_map(committed=True)
        wanted = legacy_id.upper()
        entry = next((v for k, v in data["issues"].items() if str(k).upper() == wanted), None)
        mapped = entry.get("number") if isinstance(entry, dict) else None
        if isinstance(mapped, bool) or mapped != number:
            raise Fail(REFUSED, f"{ID_MAP_PATH} does not map {wanted if LEGACY_ID.fullmatch(wanted) else 'its Legacy ID'} to issue {number} of {self.repo}; "
                                "only an issue the import created closes with --reason completed")

    def _check_runs(self, sha):
        """Every check run on a commit, all pages: the evidence check may be a called job, whose name a check_name filter misses."""
        runs, page = [], 1
        while True:
            data = self.client.get(f"/repos/{self.repo}/commits/{sha}/check-runs?per_page=100&page={page}").data
            batch = (data or {}).get("check_runs") if isinstance(data, dict) else None
            if not isinstance(batch, list):
                raise Fail(GITHUB, "the check runs came back in a shape that is not a list; not reading that as no runs")
            runs += batch
            if len(batch) < 100:
                return runs
            if page >= CHECK_RUN_PAGES:
                raise Fail(REFUSED, f"commit {sha} lists {len(runs)} check runs, the {CHECK_RUN_PAGES} pages this tool reads, so the list may be cut short; refusing")
            page += 1

    def legacy(self, args):
        """The issue a legacy Backlog id became, from the repository's committed id map; no GitHub call."""
        if not LEGACY_ID.fullmatch(args.id):
            raise Fail(USAGE, "a legacy id looks like ABC-0560 or ABC-0560.01")
        data = self._id_map()
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

    def _id_map(self, committed=False):
        """The checkout's id map, checked to be a schema 1 map for this repository; refused (exit 3) otherwise.

        committed reads it from HEAD (`git show HEAD:.github/tracker/id-map.json`), so an uncommitted edit cannot vouch
        for an issue; otherwise (the `legacy` lookup) the working-tree file is read.
        """
        root = _git(["rev-parse", "--show-toplevel"], self.cwd)
        if root is None:
            raise Fail(REFUSED, f"not inside a git checkout, so there is no {ID_MAP_PATH} to read")
        try:
            if committed:
                try:
                    shown = subprocess.run(["git", "-C", root, "show", f"HEAD:{ID_MAP_PATH}"], capture_output=True, timeout=30, check=False)
                except (OSError, subprocess.TimeoutExpired):
                    raise Fail(REFUSED, f"git could not read {ID_MAP_PATH} at HEAD") from None
                if shown.returncode != 0:
                    raise Fail(REFUSED, f"{ID_MAP_PATH} is not committed at HEAD; the import writes and commits it")
                data = json.loads(shown.stdout.decode("utf-8"))
            else:
                data = json.loads((Path(root) / ID_MAP_PATH).read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise Fail(REFUSED, f"{ID_MAP_PATH} is missing; the import writes it") from None
        except (OSError, UnicodeDecodeError, ValueError):
            raise Fail(REFUSED, f"{ID_MAP_PATH} is unreadable or not valid JSON") from None
        if (not isinstance(data, dict) or data.get("schema") != 1 or not isinstance(data.get("repo"), str)
                or data["repo"].lower() != self.repo.lower() or not isinstance(data.get("issues"), dict)):
            raise Fail(REFUSED, f"{ID_MAP_PATH} is not a schema 1 map for {self.repo}")
        return data


def _is_evidence(name, check):
    """The evidence check run by its own name, or as a job called through a reusable workflow ("<caller> / <check>")."""
    return isinstance(name, str) and (name == check or name.endswith(" / " + check))


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


class PartialResult(Exception):
    """A multi-step command that failed after its first write; the result so far must still reach the caller."""

    def __init__(self, result, cause):
        super().__init__(str(cause))
        self.result, self.cause = result, cause


def build_parser():
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--repo", default=argparse.SUPPRESS, help="owner/name; default TRACKER_REPO, else the github.com origin remote of the current checkout")
    shared.add_argument("--project", default=argparse.SUPPRESS, help="Project number; default TRACKER_PROJECT")
    parser = Parser(prog="tracker", description=__doc__.splitlines()[0], parents=[shared])
    sub = parser.add_subparsers(dest="command", required=True, parser_class=Parser)

    def add(name, helptext):
        return sub.add_parser(name, help=helptext, parents=[shared])

    p = add("view", "read one issue, field-selected")
    p.add_argument("number", type=int)
    p.add_argument("--comments", type=int, default=0, metavar="N", help="also the last N human-authored comments")
    p.add_argument("--edges", action="store_true", help="also blockers and sub-issues")
    p = add("queue", "list Project items waiting at a status")
    p.add_argument("--status", default="Ready")
    p.add_argument("--limit", type=int, default=None, help="at most this many rows (default: every one); the result says truncated when it stopped short")
    p.add_argument("--all-repos", action="store_true", help="every repository on the Project, not only this one")
    p.add_argument("--label", action="append", help="only issues carrying this label; repeat to require several")
    p = add("create", "create an issue and place it")
    p.add_argument("--title", required=True)
    p.add_argument("--body-file")
    p.add_argument("--body-stdin", action="store_true")
    p.add_argument("--label", action="append")
    p.add_argument("--type")
    p.add_argument("--milestone", type=int)
    p.add_argument("--parent", type=int)
    p.add_argument("--blocked-by", type=int, action="append")
    p.add_argument("--status")
    p.add_argument("--field", action="append", metavar="NAME=VALUE", help="set a Project field; not the organisation's Priority (use --priority)")
    p.add_argument("--priority", metavar="VALUE", help="set the organisation's Priority issue field (Urgent, High, Medium or Low), not a Project field")
    p = add("priority", "set an issue's priority label and Project Priority field together (user-owned repositories)")
    p.add_argument("number", type=int)
    p.add_argument("value", help="low, medium, high or urgent (urgent is the Project field only, with no label)")
    p = add("priority-check", "list open issues whose priority label and Project Priority field disagree (user-owned repositories); exits 5 on any")
    p = add("claim", "mark an issue In progress for a lane")
    p.add_argument("number", type=int)
    p.add_argument("--lane", required=True)
    p.add_argument("--force", action="store_true")
    p = add("status", "create or update the one managed status comment")
    p.add_argument("number", type=int)
    p.add_argument("--text")
    p.add_argument("--file", help="read the text from a file, or - for stdin")
    p = add("tick", "tick one acceptance criterion in the issue body")
    p.add_argument("number", type=int)
    p.add_argument("criterion", help="AC2 or 2")
    p = add("handoff", "hand an issue to a human: set a status and post the one handoff comment")
    p.add_argument("number", type=int)
    for part in ("scope", "verified", "blockers", "next"):
        p.add_argument(f"--{part}", required=True)
    p.add_argument("--status", default="Needs input")
    p = add("close", "close with merged-PR proof, or with a reason (not planned, duplicate, or completed for an imported issue)")
    p.add_argument("number", type=int)
    p.add_argument("--pr", type=int)
    p.add_argument("--reason", choices=["not-planned", "duplicate", "completed"])
    p.add_argument("--why")
    p = add("legacy", "print the issue a legacy Backlog id became, from the committed .github/tracker/id-map.json")
    p.add_argument("id", help="a legacy id such as ABC-0560 or ABC-0560.01")
    return parser


ISSUE_WRITERS = {"create", "status", "tick", "handoff", "close", "priority"}
PROJECT_WRITERS = {"create", "claim", "handoff", "close", "priority"}


def _identity(env, name, default):
    value = env.get(name) or default
    if value not in IDENTITIES:
        raise Fail(USAGE, f"{name} is app or user")
    return value


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


def main(argv=None, env=None, clock=None, sleep=None, stdin=None, out=None, err=None, cwd=None):
    env = dict(os.environ if env is None else env)
    out, err, stdin = out or sys.stdout, err or sys.stderr, stdin or sys.stdin
    cwd = cwd or os.getcwd()
    try:
        args = build_parser().parse_args(argv)
        textsafe.configure(env.get("TRACKER_IDENTIFIER_PATTERNS") or None)
        client = ghclient.Client(env, clock=clock, sleep=sleep)
        repo = resolve_repo(getattr(args, "repo", None), env, cwd)
        owner = env.get("TRACKER_PROJECT_OWNER") or None
        if owner is not None and not re.fullmatch(r"[A-Za-z0-9-]{1,39}", owner):
            raise Fail(USAGE, "TRACKER_PROJECT_OWNER is a GitHub login")
        issue_identity = _identity(env, "TRACKER_ISSUE_IDENTITY", ghclient.APP)
        project_identity = _identity(env, "TRACKER_PROJECT_IDENTITY", ghclient.USER)
        check = env.get("TRACKER_EVIDENCE_CHECK") or DEFAULT_EVIDENCE_CHECK
        workflow = env.get("TRACKER_EVIDENCE_WORKFLOW") or DEFAULT_EVIDENCE_WORKFLOW
        tracker = Tracker(client, repo, getattr(args, "project", None) or env.get("TRACKER_PROJECT"), env.get("TRACKER_APP_LOGIN") or None,
                          stdin, project_owner=owner, issue_identity=issue_identity, project_identity=project_identity, evidence_check=check,
                          evidence_workflow=workflow)
        tracker.cwd = cwd
        needs_app = ((args.command in ISSUE_WRITERS and issue_identity == ghclient.APP)
                     or (args.command in PROJECT_WRITERS and project_identity == ghclient.APP and tracker.project_number))
        if needs_app and not env.get("TRACKER_APP_TOKEN"):
            raise ghclient.Unconfigured("TRACKER_APP_TOKEN is not set; App writes need the installation token (run through with-token)")
        if args.command in PROJECT_WRITERS and project_identity == ghclient.APP and tracker.project_number:
            tracker.project  # refuses the App on a user-owned Project before an issue write could happen
        if args.command == "status" and bool(args.text) == bool(args.file):
            raise Fail(USAGE, "status needs exactly one of --text or --file")
        if args.command == "create" and bool(args.body_file) == bool(args.body_stdin):
            raise Fail(USAGE, "create needs exactly one of --body-file or --body-stdin")
        result = getattr(tracker, args.command.replace("-", "_"))(args)
    except Findings as findings:
        print(json.dumps(findings.result, separators=(",", ":"), ensure_ascii=False), file=out)
        return DRIFT
    except PartialResult as partial:
        print(json.dumps(partial.result, separators=(",", ":")), file=out)
        print(f"tracker: {partial.cause}", file=err)
        return LIMITED if isinstance(partial.cause, ghclient.RateLimited) else GITHUB
    except Fail as failure:
        print(f"tracker: {failure}", file=err)
        return failure.code
    except UnicodeDecodeError:
        print("tracker: an input file is not UTF-8 text", file=err)
        return USAGE
    except OSError as unreadable:
        what = f"cannot read {unreadable.filename}" if unreadable.filename else "cannot use the state directory or its lock files"
        print(f"tracker: {what}: {unreadable.strerror}", file=err)
        return USAGE
    except ghclient.Refused as refused:
        print(f"tracker: {refused}", file=err)
        return REFUSED
    except ghclient.StateError as damaged:
        print(f"tracker: {damaged}", file=err)
        return USAGE
    except ghclient.Unconfigured as missing:
        print(f"tracker: {missing}", file=err)
        return USAGE
    except ghclient.RateLimited as limited:
        print(f"tracker: {limited}", file=err)
        return LIMITED
    except ghclient.GhError as failed:
        print(f"tracker: {failed}", file=err)
        return GITHUB
    except (KeyError, TypeError, IndexError, AttributeError) as shape:
        print(f"tracker: GitHub answered in a shape this tool does not know ({type(shape).__name__}: {shape})", file=err)
        return GITHUB
    print(json.dumps(result, separators=(",", ":"), ensure_ascii=False), file=out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
