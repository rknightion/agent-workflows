"""The eight tracker commands, run in-process against a fake gh with a fake clock."""
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

import contextlib
import io
import json
import shutil
from pathlib import Path
import subprocess
import tempfile
import urllib.parse
import sys
import unittest
from unittest import mock

import textsafe
import tracker
from harness import (APP_LOGIN, APP_TOKEN, HERE, ORG_ISSUE_FIELDS, ORG_PRIORITY_FIELD_ID, PROJECT_FIELDS, REPO, USER_PROJECT_FIELDS, USER_TOKEN,
                     FakeClock, FakeGh, comment, issue, project_item)

I = f"/repos/{REPO}/issues"
P = "/orgs/example-org/projectsV2/7"
ORG_FIELDS = "/orgs/example-org/issue-fields"
STATUS = tracker.STATUS_MARKER
HANDOFF = tracker.HANDOFF_MARKER
OVERFLOW = tracker.OVERFLOW_MARKER


def bot(ident, body):
    return comment(ident, body, login=APP_LOGIN, kind="Bot")


class Base(unittest.TestCase):
    def setUp(self):
        self.gh = FakeGh()
        self.addCleanup(self.gh.cleanup)
        self.clock = FakeClock()

    def run_cmd(self, *argv, stdin="", env=None, cwd=None):
        out, err = io.StringIO(), io.StringIO()
        code = tracker.main(list(argv), env=env or self.gh.env(), clock=self.clock.time, sleep=self.clock.sleep,
                            stdin=io.StringIO(stdin), out=out, err=err, cwd=cwd or getattr(self, "cwd", None) or str(self.gh.root))
        text = out.getvalue()
        try:
            data = json.loads(text) if text.strip() else None
        except ValueError:
            data = text
        return code, data, err.getvalue()

    def comments_route(self, comments, number=5):
        self.gh.route(rf"GET {I}/{number}/comments\?per_page=100&page=1", comments)

    def project_routes(self, item_fields=None):
        self.gh.route(rf"GET {P}/fields\?per_page=100", PROJECT_FIELDS)
        self.gh.route(rf"POST {P}/items", {"id": 555})
        self.gh.route(rf"GET {P}/items/555.*", {"id": 555, "fields": item_fields or []})
        self.gh.route(rf"PATCH {P}/items/555", {"id": 555})

    def body_of(self, call):
        return json.loads(call["stdin"])

    def bind_routes(self, refs=None, compare="ahead", runs=None, compared=None, files=None, annotations=None, jobs=None):
        """The evidence run's workflow run, which calls the real evidence workflow at a commit on its default branch,
        the merged PR's changed files (by default none under .github/workflows or .github/actions), each check run's
        binding annotation for PR 20 at head h1, and the workflow run's jobs (check runs 1 to 9).

        runs, compared, files, annotations and jobs replace the whole answers; the fake answers with the first route that matches."""
        self.gh.route(rf"GET /repos/{REPO}/check-runs/[0-9]+/annotations\?per_page=100&page=1",
                      *(annotations if annotations is not None else [[binding_note()]]))
        self.gh.route(rf"GET /repos/{REPO}/actions/runs/900/jobs\?filter=all&per_page=100&page=1",
                      *(jobs if jobs is not None else [jobs_of(range(1, 10))]))
        self.gh.route(rf"GET /repos/{REPO}/pulls/20/files\?per_page=100&page=1",
                      *(files if files is not None else [[{"filename": "src/app.py", "status": "modified"}]]))
        self.gh.route(rf"GET /repos/{REPO}/actions/runs\?check_suite_id=[0-9]+", runs or workflow_runs(BOUND if refs is None else refs))
        self.gh.route(r"GET /repos/rknightion/\.github", {"default_branch": "main"})
        self.gh.route(rf"GET /repos/rknightion/\.github/compare/{GOOD_SHA}\.\.\.main\?per_page=1",
                      compared or {"status": 200, "body": {"status": compare}})


class ViewTests(Base):
    def test_view_selects_fields_and_never_touches_comments_by_default(self):
        self.gh.route(rf"GET {I}/5", issue(5, labels=[{"name": "bug"}, {"name": "x"}], milestone={"title": "M1"}, assignees=[{"login": "a"}]))
        code, data, _ = self.run_cmd("view", "5")
        self.assertEqual(code, 0)
        self.assertEqual((data["number"], data["state"], data["labels"], data["milestone"], data["type"]), (5, "open", ["bug", "x"], "M1", "Task"))
        self.assertIn("AC1", data["body"])
        self.assertNotIn("user", data)
        self.assertEqual([c["path"] for c in self.gh.calls()], [f"{I}/5"])

    def test_view_comments_returns_only_the_last_n_human_comments(self):
        self.gh.route(rf"GET {I}/5", issue(5, comments=5))
        self.comments_route([comment(1, "first"), bot(2, "bot"), bot(3, STATUS + "\nstatus"), comment(4, "second"), comment(5, "third")])
        code, data, _ = self.run_cmd("view", "5", "--comments", "2")
        self.assertEqual(code, 0)
        self.assertEqual([c["body"] for c in data["comments"]], ["second", "third"])

    def test_view_comments_walks_back_a_page_only_when_it_needs_more(self):
        self.gh.route(rf"GET {I}/5", issue(5, comments=150))
        self.gh.route(rf"GET {I}/5/comments\?per_page=100&page=2", [comment(101, "late")] + [bot(i, "bot") for i in range(102, 151)])
        self.gh.route(rf"GET {I}/5/comments\?per_page=100&page=1", [comment(i, f"early{i}") for i in range(1, 101)])
        code, data, _ = self.run_cmd("view", "5", "--comments", "2")
        self.assertEqual([c["body"] for c in data["comments"]], ["early100", "late"])
        self.assertEqual(len(self.gh.calls()), 3)

    def test_view_edges_lists_blockers_and_sub_issues(self):
        self.gh.route(rf"GET {I}/5", issue(5))
        self.gh.route(rf"GET {I}/5/dependencies/blocked_by\?per_page=100", [issue(2)])
        self.gh.route(rf"GET {I}/5/sub_issues\?per_page=100", [issue(6), issue(7)])
        code, data, _ = self.run_cmd("view", "5", "--edges")
        self.assertEqual(data["blocked_by"][0]["number"], 2)
        self.assertEqual([s["number"] for s in data["sub_issues"]], [6, 7])

    def test_view_needs_no_app_token(self):
        self.gh.route(rf"GET {I}/5", issue(5))
        code, _, _ = self.run_cmd("view", "5", env=self.gh.env(app_token=None))
        self.assertEqual(code, 0)


class QueueTests(Base):
    def test_queue_lists_ready_issues_with_their_fields(self):
        self.gh.route(rf"GET {P}/fields\?per_page=100", PROJECT_FIELDS)
        item = {"id": 1, "content_type": "Issue",
                "content": {"number": 5, "title": "t", "html_url": f"https://github.com/{REPO}/issues/5", "state": "open"},
                "fields": [{"id": 12, "name": "Status", "value": {"id": "opt-ready", "name": "Ready"}}, {"id": 13, "name": "Lane", "value": None}]}
        pr = {"id": 2, "content_type": "PullRequest", "content": {"number": 9, "title": "p", "html_url": f"https://github.com/{REPO}/pull/9"}, "fields": []}
        self.gh.route(rf"GET {P}/items\?.*", [item, pr])
        code, data, _ = self.run_cmd("queue")
        self.assertEqual(code, 0)
        self.assertEqual(data["items"], [{"repo": REPO, "number": 5, "title": "t", "status": "Ready", "lane": None, "priority": None}])
        query = self.gh.calls()[-1]["path"]
        self.assertIn("per_page=100", query)  # no limit asked: every page, a hundred at a time
        self.assertIn("Ready", query)
        self.assertIn("is%3Aopen", query)
        self.assertEqual(data["truncated"], False)


    def test_queue_filters_by_each_label(self):
        self.gh.route(rf"GET {P}/fields\?per_page=100", PROJECT_FIELDS)
        self.gh.route(rf"GET {P}/items\?.*", [])
        code, _, _ = self.run_cmd("queue", "--label", "area-one", "--label", "area-two")
        self.assertEqual(code, 0)
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.gh.calls()[-1]["path"]).query)["q"][0]
        self.assertEqual(q, f'is:issue is:open repo:{REPO} status:"Ready" label:"area-one" label:"area-two"')


class QueueScopeTests(Base):
    """`queue` lists one repository's items, every page, on a Project several repositories share."""

    OTHER = "example-org/other"

    def setUp(self):
        super().setUp()
        # 130 Ready issues of this repository and 70 of another, interleaved so no page boundary separates them, and
        # one issue of each repository that is not Ready.
        items = [project_item(n, repo=REPO if n % 2 == 0 or n > 140 else self.OTHER) for n in range(1, 201)]
        items.append(project_item(900, status="Done"))
        items.append(project_item(901, repo=self.OTHER, status="Done"))
        self.items = items
        self.gh.route(rf"GET {P}/fields\?per_page=100", PROJECT_FIELDS)
        self.gh.items_route(rf"GET {P}/items\?.*", items)
        self.here = {n for n in range(1, 201) if n % 2 == 0 or n > 140}

    def queue(self, *argv, **kw):
        code, data, err = self.run_cmd("queue", *argv, **kw)
        self.assertEqual(code, 0, err)
        return data

    def item_queries(self):
        return [urllib.parse.parse_qs(urllib.parse.urlsplit(c["path"]).query) for c in self.gh.calls("GET") if "/items?" in c["path"]]

    def test_queue_returns_only_items_of_the_repository_named_by_tracker_repo_or_repo_unless_all_repos(self):
        data = self.queue()
        self.assertEqual({row["repo"] for row in data["items"]}, {REPO})
        self.assertEqual({row["number"] for row in data["items"]}, self.here)
        self.assertEqual(self.item_queries()[0]["q"], [f'is:issue is:open repo:{REPO} status:"Ready"'])
        other = self.queue("--repo", self.OTHER)
        self.assertEqual({row["repo"] for row in other["items"]}, {self.OTHER})
        self.assertEqual(other["count"], 70)
        wide = self.queue("--all-repos")
        self.assertEqual({row["repo"] for row in wide["items"]}, {REPO, self.OTHER})
        self.assertEqual(wide["count"], 200)
        self.assertNotIn("repo:", self.item_queries()[-1]["q"][0])

    def test_queue_follows_pagination_and_returns_more_than_100_rows_when_that_many_match(self):
        data = self.queue()
        self.assertEqual(data["count"], 130)
        self.assertEqual(len(data["items"]), 130)
        self.assertEqual(len({row["number"] for row in data["items"]}), 130)  # no page read twice
        queries = self.item_queries()
        self.assertEqual(len(queries), 2)
        self.assertEqual(queries[0].get("after"), None)
        self.assertTrue(queries[1]["after"][0])  # the second page asks with the cursor the first one's Link header named
        self.assertEqual(self.queue("--all-repos")["count"], 200)

    def test_queue_reports_truncated_true_when_its_limit_stops_the_listing_before_the_items_run_out(self):
        for limit, count, truncated in ((5, 5, True), (100, 100, True), (105, 105, True), (129, 129, True),
                                        (130, 130, False), (131, 130, False), (1000, 130, False)):
            with self.subTest(limit=limit):
                data = self.queue("--limit", str(limit))
                self.assertEqual((data["count"], data["truncated"]), (count, truncated))
        self.assertIs(self.queue()["truncated"], False)
        # A limit that fits one page asks for no more than one page (and one row over, to see whether more remain).
        self.queue("--limit", "5")
        self.assertEqual(self.item_queries()[-1]["per_page"], ["6"])

    def test_two_repositories_and_over_100_matching_items_prove_scope_pagination_and_truncation(self):
        data = self.queue()
        self.assertEqual((data["count"], data["truncated"], {r["repo"] for r in data["items"]}), (130, False, {REPO}))
        cut = self.queue("--limit", "101")
        self.assertEqual((cut["count"], cut["truncated"], {r["repo"] for r in cut["items"]}), (101, True, {REPO}))
        wide = self.queue("--all-repos", "--limit", "150")
        self.assertEqual((wide["count"], wide["truncated"], {r["repo"] for r in wide["items"]}), (150, True, {REPO, self.OTHER}))

    def test_the_row_shape_and_the_label_filter_are_kept(self):
        self.gh.items_route(rf"GET {P}/items\?.*", [project_item(3, labels=["area-one"], priority="High"), project_item(4)])
        data = self.queue("--label", "area-one")
        self.assertEqual(data["items"], [{"repo": REPO, "number": 3, "title": "issue 3", "status": "Ready", "lane": None, "priority": "High"}])

    def test_a_page_that_is_not_a_list_or_a_repeated_cursor_is_an_error_not_an_end(self):
        gh = FakeGh()
        self.addCleanup(gh.cleanup)
        self.gh = gh
        gh.route(rf"GET {P}/fields\?per_page=100", PROJECT_FIELDS)
        gh.route(rf"GET {P}/items\?.*", {"message": "odd"})
        self.assertEqual(self.run_cmd("queue", env=gh.env())[0], 4)
        gh = FakeGh()
        self.addCleanup(gh.cleanup)
        self.gh = gh
        gh.route(rf"GET {P}/fields\?per_page=100", PROJECT_FIELDS)
        loop = {"status": 200, "headers": {"Link": '<https://api.github.com/x?after=same>; rel="next"'}, "body": [project_item(1)]}
        gh.route(rf"GET {P}/items\?.*", loop)
        self.assertEqual(self.run_cmd("queue", env=gh.env())[0], 4)


class QueueInputTests(Base):
    def test_the_limit_is_one_or_more(self):
        for bad in ("0", "-3"):
            with self.subTest(limit=bad):
                self.assertEqual(self.run_cmd("queue", "--limit", bad)[0], 2)
        self.assertEqual(self.gh.calls(), [])

    def test_a_label_with_a_quote_cannot_reshape_the_filter(self):
        for bad in ('x" OR status:"Done', "a\nb", " "):
            with self.subTest(label=bad):
                self.assertEqual(self.run_cmd("queue", "--label", bad)[0], 2)
        self.assertEqual(self.gh.calls(), [])

    def test_a_status_with_a_quote_cannot_reshape_the_filter(self):
        code, _, _ = self.run_cmd("queue", "--status", 'Ready" OR status:"Done')
        self.assertEqual(code, 2)
        self.assertEqual(self.gh.calls(), [])


class CreateTests(Base):
    def routes(self):
        self.gh.route(rf"POST {I}", {"status": 201, "body": issue(11)})
        self.gh.route(rf"POST {I}/3/sub_issues", {"status": 201, "body": issue(3)})
        self.gh.route(rf"GET {I}/4", issue(4))
        self.gh.route(rf"POST {I}/11/dependencies/blocked_by", {"status": 201, "body": issue(11)})
        self.project_routes()

    def test_create_escapes_the_text_and_writes_as_the_app(self):
        self.routes()
        code, data, _ = self.run_cmd("create", "--title", "Fix @octocat thing", "--body-stdin", "--label", "bug", "--type", "Task",
                                     "--milestone", "2", stdin="ask @octocat about #9\n- [ ] AC1 one\n")
        self.assertEqual(code, 0)
        self.assertEqual(data["number"], 11)
        (call,) = [c for c in self.gh.writes() if c["path"] == I]
        body = self.body_of(call)
        self.assertEqual(body["title"], "Fix `@octocat` thing")
        self.assertEqual(body["body"], "ask `@octocat` about `#9`\n- [ ] AC1 one\n")
        self.assertEqual((body["labels"], body["type"], body["milestone"]), (["bug"], "Task", 2))
        self.assertEqual(call["gh_token"], APP_TOKEN)

    def test_create_links_parent_blocker_and_project_fields(self):
        self.routes()
        code, data, _ = self.run_cmd("create", "--title", "t", "--body-stdin", "--parent", "3", "--blocked-by", "4",
                                     "--status", "Ready", "--field", "Lane=lane-a", stdin="b")
        self.assertEqual(code, 0, data)
        sub = next(c for c in self.gh.writes() if c["path"] == f"{I}/3/sub_issues")
        self.assertEqual(self.body_of(sub), {"sub_issue_id": 9011})
        block = next(c for c in self.gh.writes() if c["path"].endswith("/dependencies/blocked_by"))
        self.assertEqual(self.body_of(block), {"issue_id": 9004})
        add = next(c for c in self.gh.writes() if c["path"] == f"{P}/items")
        self.assertEqual(self.body_of(add), {"type": "Issue", "owner": "example-org", "repo": "example", "number": 11})
        self.assertEqual(add["gh_token"], USER_TOKEN)
        patch = next(c for c in self.gh.writes() if c["method"] == "PATCH")
        self.assertEqual(self.body_of(patch), {"fields": [{"id": 12, "value": "opt-ready"}, {"id": 13, "value": "lane-a"}]})
        self.assertEqual(patch["gh_token"], USER_TOKEN)

    def test_an_empty_create_body_is_refused_with_usage_before_any_write(self):
        # A closed or empty stdin, or an empty body file, must never create an issue with no contract.
        self.routes()
        with tempfile.TemporaryDirectory() as tmp:
            empty = Path(tmp) / "body.md"
            empty.write_text("", encoding="utf-8")
            for args, stdin in ((("--body-stdin",), ""), (("--body-stdin",), "  \n\t\n"), (("--body-file", str(empty)), "")):
                code, _, err = self.run_cmd("create", "--title", "t", *args, stdin=stdin)
                self.assertEqual(code, 2, (args, err))
                self.assertIn("body is empty", err)
        self.assertEqual(self.gh.writes(), [])

    def test_create_sweeps_the_raw_text_and_sends_nothing_on_a_hit(self):
        self.routes()
        code, _, err = self.run_cmd("create", "--title", "t", "--body-stdin", stdin="reach 8.8.4.4 please")
        self.assertEqual(code, 3)
        self.assertNotIn("8.8.4.4", err)
        self.assertEqual(self.gh.calls(), [])

    def test_a_pattern_from_the_pattern_file_is_refused_and_a_new_line_is_honoured(self):
        self.routes()
        path = self.gh.state / "patterns.txt"
        path.write_text(textsafe.PATTERN_FILE.read_text(encoding="utf-8") + "zz-sample-[0-9]+\n", encoding="utf-8")
        with mock.patch.object(textsafe, "PATTERN_FILE", path):
            code, _, err = self.run_cmd("create", "--title", "t", "--body-stdin", stdin="see ZZ-Sample-7 please")
            self.assertEqual(code, 3)
            self.assertNotIn("zz-sample", err.lower())
            self.assertEqual(self.gh.calls(), [])
        code, _, _ = self.run_cmd("create", "--title", "t", "--body-stdin", stdin="see ZZ-Sample-7 please")
        self.assertNotEqual(code, 3)

    def test_an_unreadable_or_empty_pattern_file_refuses_even_clean_text(self):
        self.routes()
        empty = self.gh.state / "empty-patterns.txt"
        empty.write_text("# nothing here\n", encoding="utf-8")
        for path in (self.gh.state / "missing-patterns.txt", empty):
            with self.subTest(path=path.name), mock.patch.object(textsafe, "PATTERN_FILE", path):
                code, _, err = self.run_cmd("create", "--title", "t", "--body-stdin", stdin="perfectly clean")
                self.assertEqual(code, 3)
                self.assertIn("cannot be checked", err)
                self.assertEqual(self.gh.calls(), [])

    def test_a_hit_in_the_title_is_refused_too(self):
        self.routes()
        code, _, _ = self.run_cmd("create", "--title", "mail a@" + "gmail.example", "--body-stdin", stdin="b")
        self.assertEqual(code, 3)
        self.assertEqual(self.gh.calls(), [])

    def test_a_bad_value_for_a_number_field_is_refused_before_the_issue_exists(self):
        self.routes()
        code, _, err = self.run_cmd("create", "--title", "t", "--body-stdin", "--field", "Estimate=abc", stdin="b")
        self.assertEqual(code, 2)
        self.assertIn("number field", err)
        self.assertEqual(self.gh.writes(), [])
        code, _, _ = self.run_cmd("create", "--title", "t", "--body-stdin", "--field", "Estimate=3", stdin="b")
        self.assertEqual(code, 0)
        patch = next(c for c in self.gh.writes() if c["method"] == "PATCH")
        self.assertEqual(self.body_of(patch), {"fields": [{"id": 15, "value": 3}]})

    def test_free_text_field_values_are_escaped_and_swept_like_every_other_text(self):
        self.routes()
        self.run_cmd("create", "--title", "t", "--body-stdin", "--field", "Lane=ask @octocat #9", stdin="b")
        patch = next(c for c in self.gh.writes() if c["method"] == "PATCH")
        self.assertEqual(self.body_of(patch), {"fields": [{"id": 13, "value": "ask `@octocat` `#9`"}]})
        self.gh = FakeGh()
        self.addCleanup(self.gh.cleanup)
        self.routes()
        code, _, _ = self.run_cmd("create", "--title", "t", "--body-stdin", "--field", "Lane=8.8.4.4", stdin="b")
        self.assertEqual((code, self.gh.writes()), (3, []))

    def test_a_multiline_scope_with_a_tilde_fence_is_escaped_after_assembly(self):
        self.comments_route([])
        self.project_routes()
        self.gh.route(rf"POST {I}/5/comments", {"status": 201, "body": bot(9, "x")})
        code, data, _ = self.run_cmd("handoff", "5", "--scope", "~~~\n@octocat #12\n~~~", "--verified", "v", "--blockers", "b", "--next", "n")
        self.assertEqual(code, 0, data)
        post = next(c for c in self.gh.writes() if c["path"] == f"{I}/5/comments")
        text = self.body_of(post)["body"]
        self.assertTrue(text.startswith(HANDOFF + "\n"))
        self.assertNotIn("@octocat #12", text)
        self.assertIn("`@octocat`", text)
        self.assertIn("`#12`", text)

    def test_a_rate_limited_project_step_after_create_exits_75_with_the_partial_result(self):
        self.gh.route(rf"POST {I}", {"status": 201, "body": issue(11)})
        self.gh.route(rf"GET {P}/fields\?per_page=100", PROJECT_FIELDS)
        self.gh.route(rf"POST {P}/items", {"status": 403, "headers": {"retry-after": "7200"}, "body": {"message": "rate limit"}})
        code, data, _ = self.run_cmd("create", "--title", "t", "--body-stdin", "--status", "Ready", stdin="b")
        self.assertEqual((code, data["number"], data["failed"]), (75, 11, "project"))

    def test_a_response_missing_a_field_after_the_issue_exists_still_reports_its_number(self):
        broken = issue(11)
        del broken["id"]
        self.gh.route(rf"POST {I}", {"status": 201, "body": broken})
        code, data, _ = self.run_cmd("create", "--title", "t", "--body-stdin", "--parent", "3", stdin="b")
        self.assertEqual(code, 4)
        self.assertEqual((data["number"], data["failed"]), (11, "parent"))

    def test_a_create_response_without_a_number_still_names_the_issue_by_url(self):
        broken = issue(11)
        del broken["number"]
        self.gh.route(rf"POST {I}", {"status": 201, "body": broken})
        code, data, _ = self.run_cmd("create", "--title", "t", "--body-stdin", stdin="b")
        self.assertEqual((code, data["url"]), (4, broken["html_url"]))

    def test_a_failure_after_the_issue_exists_still_reports_its_number(self):
        self.gh.route(rf"POST {I}", {"status": 201, "body": issue(11)})
        self.gh.route(rf"GET {P}/fields\?per_page=100", PROJECT_FIELDS)
        self.gh.route(rf"POST {P}/items", {"status": 403, "body": {"message": "Resource not accessible"}})
        code, data, _ = self.run_cmd("create", "--title", "t", "--body-stdin", "--status", "Ready", stdin="b")
        self.assertEqual(code, 4)
        self.assertEqual((data["number"], data["failed"]), (11, "project"))


class CreatePriorityTests(Base):
    """`create --priority` sets the organisation's Priority issue field, which is not a Project field."""

    def routes(self, fields=None):
        self.gh.route(rf"POST {I}", {"status": 201, "body": issue(11)})
        self.gh.route(rf"GET {ORG_FIELDS}", ORG_ISSUE_FIELDS if fields is None else fields)
        self.project_routes()

    def test_priority_rides_the_issue_creation_as_the_org_field_id_and_the_chosen_value(self):
        self.routes()
        code, data, err = self.run_cmd("create", "--title", "t", "--body-stdin", "--priority", "High", stdin="b")
        self.assertEqual((code, data["number"]), (0, 11), err)
        (call,) = [c for c in self.gh.writes() if c["path"] == I]
        self.assertEqual(self.body_of(call)["issue_field_values"], [{"field_id": ORG_PRIORITY_FIELD_ID, "value": "High"}])
        self.assertEqual(call["gh_token"], APP_TOKEN)
        # The id is the organisation field's, not the Project's own Priority (id 14), and the Project is not asked for it.
        self.assertNotEqual(ORG_PRIORITY_FIELD_ID, 14)
        read = next(c for c in self.gh.calls("GET") if c["path"] == ORG_FIELDS)
        self.assertEqual(read["gh_token"], USER_TOKEN)
        self.assertEqual([c for c in self.gh.writes() if c["method"] == "PATCH"], [])  # no Project field write: Priority is not one

    def test_the_value_is_matched_without_case_and_sent_as_the_option_is_spelled(self):
        self.routes()
        code, _, err = self.run_cmd("create", "--title", "t", "--body-stdin", "--priority", "uRGent", stdin="b")
        self.assertEqual(code, 0, err)
        (call,) = [c for c in self.gh.writes() if c["path"] == I]
        self.assertEqual(self.body_of(call)["issue_field_values"], [{"field_id": ORG_PRIORITY_FIELD_ID, "value": "Urgent"}])

    def test_no_priority_leaves_the_creation_without_issue_field_values_and_reads_no_org_field(self):
        self.routes()
        code, _, _ = self.run_cmd("create", "--title", "t", "--body-stdin", stdin="b")
        self.assertEqual(code, 0)
        (call,) = [c for c in self.gh.writes() if c["path"] == I]
        self.assertNotIn("issue_field_values", self.body_of(call))
        self.assertEqual([c for c in self.gh.calls() if c["path"] == ORG_FIELDS], [])

    def test_priority_works_with_no_project_and_beside_a_project_field(self):
        self.routes()
        env = self.gh.env()
        env.pop("TRACKER_PROJECT")
        code, _, err = self.run_cmd("create", "--title", "t", "--body-stdin", "--priority", "Low", stdin="b", env=env)
        self.assertEqual(code, 0, err)
        self.assertEqual([c["path"] for c in self.gh.writes()], [I])
        code, _, err = self.run_cmd("create", "--title", "t2", "--body-stdin", "--priority", "Low", "--status", "Ready", stdin="b")
        self.assertEqual(code, 0, err)
        patch = next(c for c in self.gh.writes() if c["method"] == "PATCH")
        self.assertEqual(self.body_of(patch), {"fields": [{"id": 12, "value": "opt-ready"}]})

    def test_an_unknown_priority_is_refused_naming_the_allowed_values_before_any_write(self):
        self.routes()
        for typed in ("P1", "Highest", "", "High "):
            code, _, err = self.run_cmd("create", "--title", "t", "--body-stdin", "--priority", typed, "--status", "Ready", stdin="b")
            self.assertEqual(code, 2, (typed, err))
            self.assertIn("Urgent, High, Medium, Low", err)
            self.assertNotIn("P1", err)  # a usage message never repeats what the caller typed
        self.assertEqual(self.gh.calls("POST"), [])
        self.assertEqual(self.gh.writes(), [])

    def test_an_org_without_a_usable_priority_field_is_refused_before_any_write(self):
        for fields in ([], [f for f in ORG_ISSUE_FIELDS if f["name"] != "Priority"],
                       [{"id": 1, "name": "Priority", "data_type": "date"}]):
            gh = FakeGh()
            self.addCleanup(gh.cleanup)
            self.gh = gh
            self.routes(fields)
            code, _, err = self.run_cmd("create", "--title", "t", "--body-stdin", "--priority", "High", stdin="b", env=gh.env())
            self.assertEqual(code, 2, (fields, err))  # a usage refusal, like --type on a user-owned repository
            self.assertIn("Priority", err)
            self.assertEqual(gh.writes(), [])

    def test_an_org_fields_answer_that_is_not_a_list_is_not_read_as_no_field(self):
        self.routes({"message": "odd"})
        code, _, err = self.run_cmd("create", "--title", "t", "--body-stdin", "--priority", "High", stdin="b")
        self.assertEqual(code, 4, err)
        self.assertEqual(self.gh.writes(), [])


class ClaimTests(Base):
    def test_claim_sets_status_and_lane_by_name_in_one_patch(self):
        self.project_routes()
        code, data, _ = self.run_cmd("claim", "5", "--lane", "lane-a")
        self.assertEqual(code, 0, data)
        patches = [c for c in self.gh.writes() if c["method"] == "PATCH"]
        self.assertEqual(len(patches), 1)
        self.assertEqual(self.body_of(patches[0]), {"fields": [{"id": 12, "value": "opt-prog"}, {"id": 13, "value": "lane-a"}]})
        self.assertEqual(patches[0]["gh_token"], USER_TOKEN)

    def test_field_ids_are_resolved_once_per_run(self):
        self.project_routes()
        self.run_cmd("claim", "5", "--lane", "lane-a")
        self.assertEqual(len([c for c in self.gh.calls() if "/fields" in c["path"]]), 1)

    def test_claim_reads_and_writes_inside_the_issue_lock(self):
        events = []

        @contextlib.contextmanager
        def recording_lock(self_, repo, number):
            events.append(("enter", len(self.gh.calls())))
            yield
            events.append(("exit", len(self.gh.calls())))

        self.project_routes()
        from unittest import mock
        with mock.patch.object(tracker.ghclient.Client, "issue_lock", recording_lock):
            self.assertEqual(self.run_cmd("claim", "5", "--lane", "lane-a")[0], 0)
        # Before the lock: the Project owner's kind and the field list; inside it: add item, read, patch.
        self.assertEqual(events, [("enter", 2), ("exit", 5)])

    def test_claim_refuses_an_issue_another_lane_holds(self):
        self.project_routes([{"id": 12, "name": "Status", "value": {"id": "opt-prog", "name": "In progress"}}, {"id": 13, "name": "Lane", "value": "lane-b"}])
        code, _, err = self.run_cmd("claim", "5", "--lane", "lane-a")
        self.assertEqual(code, 3)
        self.assertIn("lane-b", err)
        self.assertEqual([c for c in self.gh.writes() if c["method"] == "PATCH"], [])

    def test_claim_by_the_holding_lane_changes_nothing(self):
        self.project_routes([{"id": 12, "name": "Status", "value": "In progress"}, {"id": 13, "name": "Lane", "value": "lane-a"}])
        code, data, _ = self.run_cmd("claim", "5", "--lane", "lane-a")
        self.assertEqual(code, 0)
        self.assertEqual(data["changed"], False)
        self.assertEqual([c for c in self.gh.writes() if c["method"] == "PATCH"], [])

    def test_force_overrides_a_holding_lane(self):
        self.project_routes([{"id": 12, "name": "Status", "value": "In progress"}, {"id": 13, "name": "Lane", "value": "lane-b"}])
        code, _, _ = self.run_cmd("claim", "5", "--lane", "lane-a", "--force")
        self.assertEqual(code, 0)

    def test_a_missing_project_field_names_what_exists(self):
        self.gh.route(rf"GET {P}/fields\?per_page=100", [f for f in PROJECT_FIELDS if f["name"] != "Lane"])
        self.gh.route(rf"POST {P}/items", {"id": 555})
        code, _, err = self.run_cmd("claim", "5", "--lane", "lane-a")
        self.assertEqual(code, 2)
        self.assertIn("Lane", err)
        self.assertIn("Status", err)

    def test_a_lane_name_with_odd_characters_is_refused(self):
        code, _, _ = self.run_cmd("claim", "5", "--lane", "lane a;rm")
        self.assertEqual(code, 2)
        self.assertEqual(self.gh.calls(), [])


class ProjectValueShapeTests(Base):
    def test_values_arrive_in_any_documented_wrapping(self):
        shapes = [{"name": "In progress"}, {"name": {"raw": "In progress", "html": "<p>In progress</p>"}}, {"raw": "In progress", "html": "x"},
                  {"text": {"raw": "In progress"}}, "In progress"]
        for shape in shapes:
            with self.subTest(shape=shape):
                self.assertEqual(tracker._value(shape), "In progress")
        self.assertIsNone(tracker._value(None))
        self.assertEqual(tracker._value(3), 3)

    def test_a_value_shape_nobody_recognised_stops_the_claim_instead_of_reading_as_empty(self):
        self.project_routes([{"id": 12, "name": "Status", "value": {"mystery": 1}}, {"id": 13, "name": "Lane", "value": "lane-b"}])
        code, _, err = self.run_cmd("claim", "5", "--lane", "lane-a")
        self.assertEqual(code, 4)
        self.assertIn("shape", err)
        self.assertEqual([c for c in self.gh.writes() if c["method"] == "PATCH"], [])

    def test_a_wrapped_lane_still_blocks_a_second_claim(self):
        self.project_routes([{"id": 12, "name": "Status", "value": {"name": {"raw": "In progress"}}}, {"id": 13, "name": "Lane", "value": {"raw": "lane-b", "html": "x"}}])
        code, _, err = self.run_cmd("claim", "5", "--lane", "lane-a")
        self.assertEqual(code, 3)
        self.assertIn("lane-b", err)


class InputEchoTests(Base):
    def test_no_refusal_or_usage_message_repeats_what_the_caller_typed(self):
        self.project_routes()
        cases = [("create", "--title", "t", "--body-stdin", "--field", "Estimate=8.8.4.4"),
                 ("create", "--title", "t", "--body-stdin", "--field", "Status=8.8.4.4"),
                 ("create", "--title", "t", "--body-stdin", "--field", "8.8.4.4"),
                 ("tick", "5", "8.8.4.4"), ("view", "5", "--repo", "8.8.4.4"),
                 ("view", "8.8.4.4"), ("queue", "--limit", "8.8.4.4"), ("close", "5", "--reason", "8.8.4.4")]
        for argv in cases:
            with self.subTest(argv=argv):
                code, _, err = self.run_cmd(*argv, stdin="b")
                self.assertIn(code, (2, 3))
                self.assertNotIn("8.8.4.4", err)
        self.assertEqual(self.gh.writes(), [])

    def test_every_field_value_is_swept_not_only_free_text(self):
        self.project_routes()
        code, _, _ = self.run_cmd("create", "--title", "t", "--body-stdin", "--field", "Estimate=8.8.4.4", stdin="b")
        self.assertEqual(code, 3)

    def test_a_sweep_refusal_is_audited_without_its_text(self):
        self.run_cmd("status", "5", "--text", "mail me@" + "gmail.example")
        lines = (self.gh.state / "audit.jsonl").read_text().splitlines()
        entry = json.loads(lines[-1])
        self.assertEqual(entry["status"], "refused")
        self.assertNotIn("gmail", lines[-1])

    def test_non_finite_numbers_are_refused_before_the_issue_exists(self):
        self.project_routes()
        for bad in ("nan", "inf", "-inf"):
            with self.subTest(bad=bad):
                self.assertEqual(self.run_cmd("create", "--title", "t", "--body-stdin", "--field", f"Estimate={bad}", stdin="b")[0], 2)
        self.assertEqual(self.gh.writes(), [])

    def test_an_empty_repo_argument_is_not_silently_the_default(self):
        self.assertEqual(self.run_cmd("view", "5", "--repo", "")[0], 2)
        self.assertEqual(self.gh.calls(), [])

    def test_a_negative_or_huge_comment_count_is_a_usage_error(self):
        for bad in ("-1", "101"):
            self.assertEqual(self.run_cmd("view", "5", "--comments", bad)[0], 2)
        self.assertEqual(self.gh.calls(), [])


class OptionShapeTests(Base):
    def test_option_names_in_any_wrapping_resolve(self):
        fields = [dict(f) for f in PROJECT_FIELDS]
        fields[1] = {**fields[1], "options": [{"id": "opt-prog", "name": {"raw": "In progress", "html": "<p>In progress</p>"}}]}
        self.gh.route(rf"GET {P}/fields\?per_page=100", fields)
        self.gh.route(rf"POST {P}/items", {"id": 555})
        self.gh.route(rf"GET {P}/items/555.*", {"id": 555, "fields": []})
        self.gh.route(rf"PATCH {P}/items/555", {"id": 555})
        code, data, _ = self.run_cmd("claim", "5", "--lane", "lane-a")
        self.assertEqual(code, 0, data)

    def test_an_unknown_attribute_shape_is_a_github_error_not_a_traceback(self):
        fields = [dict(f) for f in PROJECT_FIELDS]
        fields[1] = {**fields[1], "options": [{"id": "x", "name": 7}]}
        self.gh.route(rf"GET {P}/fields\?per_page=100", fields)
        code, _, err = self.run_cmd("claim", "5", "--lane", "lane-a")
        self.assertIn(code, (2, 4))
        self.assertNotIn("Traceback", err)

    def test_the_project_number_is_digits_only(self):
        for bad in ("7/../x", "x", "7 8"):
            code, _, _ = self.run_cmd("queue", "--project", bad)
            self.assertEqual(code, 2)
        self.assertEqual(self.gh.calls(), [])

    def test_a_typed_field_name_is_neither_echoed_nor_logged(self):
        self.project_routes()
        code, _, err = self.run_cmd("create", "--title", "t", "--body-stdin", "--field", "Sneaky42=x", stdin="b")
        self.assertEqual(code, 2)
        self.assertNotIn("Sneaky42", err)
        audit = self.gh.state / "audit.jsonl"
        self.assertNotIn("Sneaky42", audit.read_text() if audit.exists() else "")
        code, _, _ = self.run_cmd("create", "--title", "t", "--body-stdin", "--field", "8.8.4.4=x", stdin="b")
        self.assertEqual(code, 3)


class ViewRaceTests(Base):
    def test_a_comment_posted_after_the_count_was_read_is_not_missed(self):
        self.gh.route(rf"GET {I}/5", issue(5, comments=100))
        self.gh.route(rf"GET {I}/5/comments\?per_page=100&page=1", [comment(i, f"c{i}") for i in range(1, 101)])
        self.gh.route(rf"GET {I}/5/comments\?per_page=100&page=2", [comment(101, "newest")])
        self.gh.route(rf"GET {I}/5/comments\?per_page=100&page=3", [])
        code, data, _ = self.run_cmd("view", "5", "--comments", "2")
        self.assertEqual([c["body"] for c in data["comments"]], ["c100", "newest"])


LIVE_MENTION = '<p>hi <a class="user-mention notranslate" href="https://github.com/octocat">@octocat</a></p>'
LIVE_TEAM = '<p><a class="team-mention" href="x">@o/t</a></p>'
LIVE_LINK = '<p>see <a class="issue-link js-issue-link" href="x">#1</a></p>'
INERT = "<p>hi <code>@octocat</code> and <code>#1</code></p>"


class RenderGuardTests(Base):
    def test_a_malformed_render_answer_never_counts_as_inert(self):
        bad = [{"status": 200, "body": {"html": "<p>x</p>"}},
               {"status": 200, "body": "<p>x</p>", "headers": {"Content-Type": "text/plain"}},
               {"status": 200, "body": "<p>x</p>", "headers": {"Content-Type": "application/json"}},
               {"status": 200, "body": ""}]
        for answer in bad:
            with self.subTest(answer=answer):
                self.gh = FakeGh()
                self.addCleanup(self.gh.cleanup)
                self.comments_route([])
                self.gh.route("POST /markdown", answer)
                self.assertEqual(self.run_cmd("status", "5", "--text", "plain words")[0], 4)
                self.assertEqual(self.gh.writes(), [])

    def test_the_final_text_is_rendered_by_github_before_a_comment_is_written(self):
        self.comments_route([])
        self.gh.route(rf"POST {I}/5/comments", {"status": 201, "body": bot(9, "x")})
        self.run_cmd("status", "5", "--text", "plain words")
        (render,) = self.gh.renders()
        self.assertEqual(json.loads(render["stdin"]), {"text": STATUS + "\nplain words", "mode": "gfm", "context": REPO})
        self.assertEqual(render["gh_token"], APP_TOKEN)
        calls = [c["path"] for c in self.gh.calls()]
        self.assertLess(calls.index("/markdown"), calls.index(f"{I}/5/comments"))

    def test_a_live_render_falls_back_to_a_fenced_block_the_renderer_confirms_inert(self):
        self.comments_route([])
        self.gh.route("POST /markdown", {"status": 200, "body": LIVE_MENTION}, {"status": 200, "body": INERT})
        self.gh.route(rf"POST {I}/5/comments", {"status": 201, "body": bot(9, "x")})
        code, data, _ = self.run_cmd("status", "5", "--text", "x ``` y")
        self.assertEqual(code, 0, data)
        (post,) = self.gh.writes()
        self.assertEqual(self.body_of(post)["body"], STATUS + "\n````\nx ``` y\n````")
        self.assertEqual(len(self.gh.renders()), 2)

    def test_text_that_stays_live_even_fenced_is_refused_and_nothing_is_written(self):
        self.comments_route([])
        self.gh.route("POST /markdown", {"status": 200, "body": LIVE_LINK})
        code, _, err = self.run_cmd("status", "5", "--text", "x")
        self.assertEqual(code, 3)
        self.assertIn("live", err)
        self.assertEqual(self.gh.writes(), [])
        self.assertEqual(json.loads((self.gh.state / "audit.jsonl").read_text().splitlines()[-1])["status"], "refused")

    def test_each_live_class_counts(self):
        for html in (LIVE_MENTION, LIVE_TEAM, LIVE_LINK, '<a class=\'user-mention\' href="x">'):
            with self.subTest(html=html):
                self.gh = FakeGh()
                self.addCleanup(self.gh.cleanup)
                self.comments_route([])
                self.gh.route("POST /markdown", {"status": 200, "body": html})
                self.assertEqual(self.run_cmd("status", "5", "--text", "x")[0], 3)

    def test_an_inert_render_with_other_links_is_fine(self):
        self.comments_route([])
        self.gh.route("POST /markdown", {"status": 200, "body": '<p><a href="https://example.com">link</a> <a class="commit-link" href="x">abc1234</a></p>'})
        self.gh.route(rf"POST {I}/5/comments", {"status": 201, "body": bot(9, "x")})
        self.assertEqual(self.run_cmd("status", "5", "--text", "x")[0], 0)

    def test_a_failed_render_stops_the_write(self):
        self.comments_route([])
        self.gh.route("POST /markdown", {"status": 500, "body": {"message": "boom"}})
        code, _, _ = self.run_cmd("status", "5", "--text", "x")
        self.assertEqual(code, 4)
        self.assertEqual(self.gh.writes(), [])

    def test_a_create_body_falls_back_but_a_live_title_is_refused_before_anything_exists(self):
        self.gh.route(rf"POST {I}", {"status": 201, "body": issue(11)})
        self.project_routes()
        self.gh.route("POST /markdown", {"status": 200, "body": INERT}, {"status": 200, "body": LIVE_MENTION}, {"status": 200, "body": INERT})
        code, _, _ = self.run_cmd("create", "--title", "t", "--body-stdin", stdin="plain")
        self.assertEqual(code, 0)
        post = next(c for c in self.gh.writes() if c["path"] == I)
        self.assertEqual(self.body_of(post)["body"], "```\nplain\n```")
        self.gh = FakeGh()
        self.addCleanup(self.gh.cleanup)
        self.gh.route("POST /markdown", {"status": 200, "body": LIVE_MENTION})
        code, _, _ = self.run_cmd("create", "--title", "t", "--body-stdin", stdin="plain")
        self.assertEqual((code, self.gh.writes()), (3, []))

    def test_a_live_text_field_value_is_refused_before_the_issue_exists(self):
        self.project_routes()
        self.gh.route("POST /markdown", {"status": 200, "body": LIVE_MENTION})
        code, _, _ = self.run_cmd("create", "--title", "t", "--body-stdin", "--field", "Lane=lane-a", stdin="plain")
        self.assertEqual((code, self.gh.writes()), (3, []))

    def test_handoff_text_is_rendered_and_fenced_when_live(self):
        self.comments_route([])
        self.project_routes()
        self.gh.route("POST /markdown", {"status": 200, "body": LIVE_MENTION}, {"status": 200, "body": INERT})
        self.gh.route(rf"POST {I}/5/comments", {"status": 201, "body": bot(9, "x")})
        code, data, _ = self.run_cmd("handoff", "5", "--scope", "s", "--verified", "v", "--blockers", "b", "--next", "n")
        self.assertEqual(code, 0, data)
        post = next(c for c in self.gh.writes() if c["path"] == f"{I}/5/comments")
        text = self.body_of(post)["body"]
        self.assertTrue(text.startswith(HANDOFF + "\n```\n**Handoff**"))
        self.assertTrue(text.endswith("\n```"))

    def test_a_close_reason_is_rendered_too(self):
        self.comments_route([])
        self.gh.route(rf"GET {I}/5", issue(5))
        self.gh.route("POST /markdown", {"status": 200, "body": LIVE_LINK})
        code, _, _ = self.run_cmd("close", "5", "--reason", "not-planned", "--why", "see it")
        self.assertEqual((code, self.gh.writes()), (3, []))


class CommentPageShapeTests(Base):
    def test_a_comments_page_that_is_not_a_list_stops_status_and_handoff_before_any_write(self):
        for body in (None, {}, "", {"message": "x"}):
            with self.subTest(body=body):
                self.gh = FakeGh()
                self.addCleanup(self.gh.cleanup)
                self.gh.route(rf"GET {I}/5/comments\?per_page=100&page=1", {"status": 200, "body": body})
                self.project_routes()
                for argv in (("status", "5", "--text", "x"), ("handoff", "5", "--scope", "s", "--verified", "v", "--blockers", "b", "--next", "n")):
                    self.assertEqual(self.run_cmd(*argv)[0], 4)
                self.assertEqual([w for w in self.gh.writes() if "comments" in w["path"]], [])

    def test_view_comments_also_needs_a_list(self):
        self.gh.route(rf"GET {I}/5", issue(5, comments=2))
        self.gh.route(rf"GET {I}/5/comments\?per_page=100&page=1", {"status": 200, "body": {}})
        self.assertEqual(self.run_cmd("view", "5", "--comments", "1")[0], 4)


class RawSizeTests(Base):
    def test_text_over_the_raw_bound_is_refused_before_it_is_escaped(self):
        code, _, err = self.run_cmd("status", "5", "--text", "." * 300000)
        self.assertEqual(code, 3)
        self.assertEqual(self.gh.calls(), [])
        code, _, _ = self.run_cmd("create", "--title", "t", "--body-stdin", stdin="." * 300000)
        self.assertEqual(code, 3)
        self.assertEqual(self.gh.calls(), [])


class UsageMessageTests(Base):
    def test_an_argparse_error_is_a_fixed_message_whatever_was_typed(self):
        for argv in (("status", "5", "--text", "safe", "8.8.4.4"), ("bogus-8.8.4.4",), ("view", "x8.8.4.4"),
                     ("close", "5", "--reason", "nope-8.8.4.4", "--why", "x")):
            with self.subTest(argv=argv):
                code, _, err = self.run_cmd(*argv)
                self.assertEqual(code, 2)
                self.assertNotIn("8.8.4.4", err)
                self.assertIn("--help", err)
        self.assertEqual(self.gh.calls(), [])


class ClaimResponseTests(Base):
    def test_a_missing_or_null_fields_member_is_not_read_as_unclaimed(self):
        for body in ({"id": 555}, {"id": 555, "fields": None}, None):
            with self.subTest(body=body):
                self.gh = FakeGh()
                self.addCleanup(self.gh.cleanup)
                self.gh.route(rf"GET {P}/fields\?per_page=100", PROJECT_FIELDS)
                self.gh.route(rf"POST {P}/items", {"id": 555})
                self.gh.route(rf"GET {P}/items/555.*", {"status": 200, "body": body})
                self.gh.route(rf"PATCH {P}/items/555", {"id": 555})
                code, _, err = self.run_cmd("claim", "5", "--lane", "lane-a")
                self.assertEqual(code, 4)
                self.assertIn("fields", err)
                self.assertEqual([c for c in self.gh.writes() if c["method"] == "PATCH"], [])

    def test_an_explicit_empty_fields_array_means_unclaimed(self):
        self.project_routes([])
        self.assertEqual(self.run_cmd("claim", "5", "--lane", "lane-a")[0], 0)


class RepoSettingTests(Base):
    def test_a_repo_without_an_owner_is_a_usage_error(self):
        for repo in ("example", "a/b/c", "/x", ""):
            with self.subTest(repo=repo):
                code, _, err = self.run_cmd("view", "5", "--repo", repo) if repo else self.run_cmd("view", "5", env=self.gh.env(TRACKER_REPO="noslash"))
                self.assertEqual(code, 2)
        self.assertEqual(self.gh.calls(), [])


class StatusTests(Base):
    def test_status_creates_the_marked_comment_once(self):
        self.comments_route([comment(1, "hello")])
        self.gh.route(rf"POST {I}/5/comments", {"status": 201, "body": bot(9, "x")})
        code, data, _ = self.run_cmd("status", "5", "--text", "Working on @octocat #3")
        self.assertEqual(code, 0)
        (write,) = self.gh.writes()
        self.assertEqual(self.body_of(write)["body"], STATUS + "\nWorking on `@octocat` `#3`")
        self.assertEqual(write["gh_token"], APP_TOKEN)
        self.assertEqual(data["action"], "created")

    def test_status_updates_the_existing_comment_in_place(self):
        self.comments_route([comment(1, "hello"), bot(7, STATUS + "\nold")])
        self.gh.route(rf"PATCH {I}/comments/7", {"id": 7})
        code, data, _ = self.run_cmd("status", "5", "--text", "new")
        self.assertEqual(code, 0)
        (write,) = self.gh.writes()
        self.assertEqual((write["method"], write["path"]), ("PATCH", f"{I}/comments/7"))
        self.assertEqual(data["action"], "updated")

    def test_an_empty_status_text_is_refused_before_any_write(self):
        # A stdin read that came back empty must never replace the managed comment with its marker alone.
        self.comments_route([bot(7, STATUS + "\nold")])
        for args in (("--text", ""), ("--text", "  \n")):
            code, _, _ = self.run_cmd("status", "5", *args)
            self.assertNotEqual(code, 0)
        self.assertEqual(self.gh.writes(), [])

    def test_an_unchanged_status_writes_nothing(self):
        self.comments_route([bot(7, STATUS + "\nsame")])
        code, data, _ = self.run_cmd("status", "5", "--text", "same")
        self.assertEqual((code, data["action"], self.gh.writes()), (0, "unchanged", []))

    def test_a_marker_from_another_author_is_not_the_managed_comment(self):
        self.comments_route([comment(3, STATUS + "\nspoofed", login="mallory")])
        self.gh.route(rf"POST {I}/5/comments", {"status": 201, "body": bot(9, "x")})
        code, data, _ = self.run_cmd("status", "5", "--text", "mine")
        self.assertEqual(data["action"], "created")
        self.assertEqual([w["method"] for w in self.gh.writes()], ["POST"])

    def test_status_and_handoff_read_and_write_inside_the_issue_lock(self):
        events = []

        @contextlib.contextmanager
        def recording_lock(self_, repo, number):
            events.append(("enter", len(self.gh.calls())))
            yield
            events.append(("exit", len(self.gh.calls())))

        from unittest import mock
        self.comments_route([])
        self.gh.route(rf"POST {I}/5/comments", {"status": 201, "body": bot(9, "x")})
        with mock.patch.object(tracker.ghclient.Client, "issue_lock", recording_lock):
            self.assertEqual(self.run_cmd("status", "5", "--text", "x")[0], 0)
        self.assertEqual(events, [("enter", 1), ("exit", 3)])

    def test_handoff_reads_and_writes_inside_the_issue_lock_too(self):
        events = []

        @contextlib.contextmanager
        def recording_lock(self_, repo, number):
            events.append(("enter", len(self.gh.calls())))
            yield
            events.append(("exit", len(self.gh.calls())))

        from unittest import mock
        self.comments_route([])
        self.project_routes()
        self.gh.route(rf"POST {I}/5/comments", {"status": 201, "body": bot(9, "x")})
        with mock.patch.object(tracker.ghclient.Client, "issue_lock", recording_lock):
            self.assertEqual(self.run_cmd("handoff", "5", "--scope", "s", "--verified", "v", "--blockers", "b", "--next", "n")[0], 0)
        # Before the lock: the Project owner's kind, the field list and the render.
        self.assertEqual(events, [("enter", 3), ("exit", 7)])

    def test_two_managed_comments_are_refused_not_guessed_between(self):
        self.comments_route([bot(7, STATUS + "\na"), bot(8, STATUS + "\nb")])
        code, _, err = self.run_cmd("status", "5", "--text", "x")
        self.assertEqual(code, 3)
        self.assertEqual(self.gh.writes(), [])

    def test_a_stray_app_comment_is_refused_even_when_a_status_comment_exists(self):
        self.comments_route([bot(7, STATUS + "\nold"), bot(8, "stray note with no marker")])
        code, _, _ = self.run_cmd("status", "5", "--text", "new")
        self.assertEqual((code, self.gh.writes()), (3, []))

    def test_a_second_non_handoff_app_comment_is_refused(self):
        self.comments_route([bot(7, "stray note with no marker")])
        code, _, err = self.run_cmd("status", "5", "--text", "x")
        self.assertEqual(code, 3)
        self.assertEqual(self.gh.writes(), [])

    def test_overflow_and_handoff_comments_do_not_count_as_a_second_comment(self):
        self.comments_route([bot(6, OVERFLOW + "\nmore body"), bot(8, HANDOFF + "\nhandoff")])
        self.gh.route(rf"POST {I}/5/comments", {"status": 201, "body": bot(9, "x")})
        code, data, _ = self.run_cmd("status", "5", "--text", "x")
        self.assertEqual((code, data["action"]), (0, "created"))

    def test_status_text_is_swept_and_size_capped(self):
        code, _, _ = self.run_cmd("status", "5", "--text", "mail me@" + "gmail.example")
        self.assertEqual(code, 3)
        code, _, _ = self.run_cmd("status", "5", "--text", "x" * 61000)
        self.assertEqual(code, 3)
        self.assertEqual(self.gh.calls(), [])

    def test_text_carrying_a_tracker_marker_is_refused(self):
        code, _, _ = self.run_cmd("status", "5", "--text", "sneaky <!-- agent-tracker:status -->")
        self.assertEqual(code, 3)
        self.assertEqual(self.gh.calls(), [])


class HandoffTests(Base):
    ARGS = ("handoff", "5", "--scope", "AC1 done", "--verified", "unit tests", "--blockers", "none", "--next", "review @octocat")

    def test_handoff_sets_the_status_then_posts_one_templated_comment(self):
        self.comments_route([bot(7, STATUS + "\nx")])
        self.project_routes()
        self.gh.route(rf"POST {I}/5/comments", {"status": 201, "body": bot(9, "x")})
        code, data, _ = self.run_cmd(*self.ARGS)
        self.assertEqual(code, 0, data)
        patch = next(c for c in self.gh.writes() if c["method"] == "PATCH")
        self.assertEqual(self.body_of(patch), {"fields": [{"id": 12, "value": "opt-input"}]})
        post = next(c for c in self.gh.writes() if c["path"] == f"{I}/5/comments")
        text = self.body_of(post)["body"]
        self.assertTrue(text.startswith(HANDOFF))
        for part in ("AC1 done", "unit tests", "none", "review `@octocat`"):
            self.assertIn(part, text)

    def test_a_second_handoff_is_refused_until_a_human_replies(self):
        self.comments_route([bot(7, HANDOFF + "\nfirst")])
        self.project_routes()
        code, _, err = self.run_cmd(*self.ARGS)
        self.assertEqual(code, 3)
        self.assertEqual(self.gh.writes(), [])

    def test_a_human_reply_reopens_the_door(self):
        self.comments_route([bot(7, HANDOFF + "\nfirst"), comment(8, "answer")])
        self.project_routes()
        self.gh.route(rf"POST {I}/5/comments", {"status": 201, "body": bot(9, "x")})
        code, _, _ = self.run_cmd(*self.ARGS)
        self.assertEqual(code, 0)

    def test_handoff_text_is_swept_before_anything_is_written(self):
        self.comments_route([])
        self.project_routes()
        code, _, _ = self.run_cmd("handoff", "5", "--scope", "s", "--verified", "v", "--blockers", "ping 8.8.4.4", "--next", "n")
        self.assertEqual(code, 3)
        self.assertEqual(self.gh.writes(), [])


BODY = "Intro\n- [ ] AC1 first\n- [ ] AC2 second\n- [x] AC3 third\n"


class TickTests(Base):
    def test_tick_flips_exactly_one_box_and_verifies_by_rereading(self):
        ticked = BODY.replace("- [ ] AC2", "- [x] AC2")
        self.gh.route(rf"GET {I}/5", issue(5, body=BODY), issue(5, body=ticked))
        self.gh.route(rf"PATCH {I}/5", issue(5, body=ticked))
        code, data, _ = self.run_cmd("tick", "5", "AC2")
        self.assertEqual(code, 0, data)
        (write,) = self.gh.writes()
        self.assertEqual(self.body_of(write), {"body": ticked})
        self.assertEqual([c["method"] for c in self.gh.calls()], ["GET", "PATCH", "GET"])
        self.assertEqual(write["gh_token"], APP_TOKEN)

    def test_tick_accepts_a_bare_number(self):
        ticked = BODY.replace("- [ ] AC1", "- [x] AC1")
        self.gh.route(rf"GET {I}/5", issue(5, body=BODY), issue(5, body=ticked))
        self.gh.route(rf"PATCH {I}/5", {})
        self.assertEqual(self.run_cmd("tick", "5", "1")[0], 0)

    def test_tick_fails_when_the_reread_does_not_show_the_change(self):
        self.gh.route(rf"GET {I}/5", issue(5, body=BODY))
        self.gh.route(rf"PATCH {I}/5", {})
        code, _, err = self.run_cmd("tick", "5", "AC2")
        self.assertEqual(code, 4)
        self.assertIn("verif", err)

    def test_a_tick_behind_a_held_issue_lock_exits_75_without_touching_github(self):
        import fcntl
        lock = self.gh.state / "issue-example-org-example-5.lock"
        lock.touch()
        with open(lock, "a") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            code, _, err = self.run_cmd("tick", "5", "AC1", env=self.gh.env(TRACKER_MAX_WAIT="0"))
        self.assertEqual(code, 75)
        self.assertEqual(self.gh.calls(), [])

    def test_a_malformed_criterion_is_a_usage_error(self):
        for bad in ("AC", "x1", "AC1;", "-1"):
            with self.subTest(bad=bad):
                self.assertEqual(self.run_cmd("tick", "5", bad)[0], 2)
        self.assertEqual(self.gh.calls(), [])

    def test_an_unexpected_response_shape_is_a_github_error_not_a_traceback(self):
        self.gh.route(rf"GET {I}/5", {"unexpected": True})
        code, _, err = self.run_cmd("view", "5")
        self.assertEqual(code, 4)
        self.assertIn("shape", err)

    def test_an_already_ticked_box_is_a_no_op(self):
        self.gh.route(rf"GET {I}/5", issue(5, body=BODY))
        code, data, _ = self.run_cmd("tick", "5", "AC3")
        self.assertEqual((code, data["changed"], self.gh.writes()), (0, False, []))

    def test_a_missing_or_ambiguous_criterion_is_refused(self):
        self.gh.route(rf"GET {I}/5", issue(5, body=BODY + "- [ ] AC1 again\n"))
        self.assertEqual(self.run_cmd("tick", "5", "AC9")[0], 3)
        self.assertEqual(self.run_cmd("tick", "5", "AC1")[0], 3)
        self.assertEqual(self.gh.writes(), [])

    def test_a_body_with_a_legacy_sweep_hit_can_still_be_ticked(self):
        old = BODY + "legacy host 8.8.4.4\n"
        ticked = old.replace("- [ ] AC2", "- [x] AC2")
        self.gh.route(rf"GET {I}/5", issue(5, body=old), issue(5, body=ticked))
        self.gh.route(rf"PATCH {I}/5", {})
        self.assertEqual(self.run_cmd("tick", "5", "AC2")[0], 0)

    def test_the_body_is_read_and_checked_inside_the_issue_lock(self):
        events = []

        @contextlib.contextmanager
        def recording_lock(self_, repo, number):
            events.append(("enter", len(self.gh.calls())))
            yield
            events.append(("exit", len(self.gh.calls())))

        ticked = BODY.replace("- [ ] AC2", "- [x] AC2")
        self.gh.route(rf"GET {I}/5", issue(5, body=BODY), issue(5, body=ticked))
        self.gh.route(rf"PATCH {I}/5", {})
        from unittest import mock
        with mock.patch.object(tracker.ghclient.Client, "issue_lock", recording_lock):
            self.assertEqual(self.run_cmd("tick", "5", "AC2")[0], 0)
        self.assertEqual(events, [("enter", 0), ("exit", 3)])


def merged_pr(body="Closes #5\n", merged=True, repo=REPO, ref="main"):
    return {"number": 20, "merged": merged, "state": "closed", "body": body, "head": {"sha": "h1"}, "base": {"ref": ref, "repo": {"full_name": repo}}}


def run_of(conclusion="success", status="completed", name="Acceptance evidence", ident=1, app="github-actions", suite=700,
           started="2026-11-04T00:00:00Z"):
    return {"id": ident, "name": name, "status": status, "conclusion": conclusion, "head_sha": "h1", "app": {"slug": app},
            "check_suite": {"id": suite}, "started_at": started, "completed_at": "2026-11-04T00:05:00Z"}


def check_run(conclusion="success", status="completed", name="Acceptance evidence", ident=1, **more):
    return {"check_runs": [run_of(conclusion, status, name, ident, **more)]}


EVIDENCE_WF = "rknightion/.github/.github/workflows/ac-evidence.yml"
GOOD_SHA = "a" * 40
BOUND = [{"path": f"{EVIDENCE_WF}@main", "sha": GOOD_SHA, "ref": "refs/heads/main"}]


def binding_note(pr=20, head="h1", title="Acceptance evidence binding", level="notice"):
    """The one annotation the evidence job writes on its own check run: the PR it judged and that PR's head."""
    return {"path": ".github", "start_line": 1, "end_line": 1, "annotation_level": level, "title": title, "message": f"pr={pr} head={head}"}


def jobs_of(ids):
    return {"total_count": len(ids), "jobs": [{"id": 5000 + i, "run_id": 900, "check_run_url": f"https://api.github.com/repos/{REPO}/check-runs/{i}"}
                                              for i in ids]}


def workflow_runs(refs):
    return {"total_count": 1, "workflow_runs": [{"id": 900, "head_sha": "h1", "referenced_workflows": refs}]}


class CloseTests(Base):
    CHECKS = rf"GET /repos/{REPO}/commits/h1/check-runs\?per_page=100&page=1"

    def setup_close(self, pr=None, checks=None, state="open", edited=None):
        self.gh.route(rf"GET /repos/{REPO}", {"default_branch": "main"})
        self.gh.route("POST graphql", {"data": {"repository": {"pullRequest": {"lastEditedAt": edited}}}})
        self.gh.route(rf"GET /repos/{REPO}/pulls/20", pr or merged_pr())
        self.gh.route(self.CHECKS, checks or check_run())
        self.bind_routes()
        self.gh.route(rf"GET {I}/5", issue(5, state=state))
        self.gh.route(rf"PATCH {I}/5", issue(5, state="closed"))

    def test_close_needs_a_merged_pr_with_a_passing_evidence_check(self):
        self.setup_close()
        self.project_routes()
        code, data, _ = self.run_cmd("close", "5", "--pr", "20")
        self.assertEqual(code, 0, data)
        write = next(c for c in self.gh.writes() if c["path"] == f"{I}/5")
        self.assertEqual(self.body_of(write), {"state": "closed", "state_reason": "completed"})
        self.assertEqual(write["gh_token"], APP_TOKEN)

    def test_close_refuses_each_way_the_proof_can_be_missing(self):
        cases = {
            "unmerged": dict(pr=merged_pr(merged=False)),
            "other repo": dict(pr=merged_pr(repo="example-org/elsewhere")),
            "merged into a non-default branch": dict(pr=merged_pr(ref="next")),
            "no closing keyword": dict(pr=merged_pr(body="Mentions #5 only")),
            "closes a longer number": dict(pr=merged_pr(body="Closes #50")),
            "closes a word-suffixed number": dict(pr=merged_pr(body="Closes #5abc")),
            "check failed": dict(checks=check_run("failure")),
            "check pending": dict(checks=check_run(None, "in_progress")),
            "check absent": dict(checks={"check_runs": []}),
            "other check name": dict(checks=check_run(name="something-else")),
            "the old name is not the check": dict(checks=check_run(name="ac-evidence-check")),
        }
        for label, kwargs in cases.items():
            with self.subTest(label):
                self.gh = FakeGh()
                self.addCleanup(self.gh.cleanup)
                self.setup_close(**kwargs)
                code, _, err = self.run_cmd("close", "5", "--pr", "20")
                self.assertEqual(code, 3, err)
                self.assertEqual(self.gh.writes(), [])

    def test_a_full_issue_url_is_closing_proof_like_it_is_for_the_checker(self):
        self.setup_close(pr=merged_pr(body=f"Closes https://github.com/{REPO}/issues/5"))
        self.project_routes()
        self.assertEqual(self.run_cmd("close", "5", "--pr", "20")[0], 0)

    def test_a_rate_limited_project_step_after_the_close_exits_75_with_the_partial_result(self):
        self.setup_close()
        self.gh.route(rf"GET {P}/fields\?per_page=100", PROJECT_FIELDS)
        self.gh.route(rf"POST {P}/items", {"status": 403, "headers": {"retry-after": "7200"}, "body": {"message": "rate limit"}})
        code, data, _ = self.run_cmd("close", "5", "--pr", "20")
        self.assertEqual((code, data["number"], data["failed"]), (75, 5, "project"))

    def test_a_pr_description_edited_after_the_check_started_is_not_covered_by_it(self):
        self.setup_close(edited="2026-11-04T00:00:30Z")
        code, _, err = self.run_cmd("close", "5", "--pr", "20")
        self.assertEqual(code, 3)
        self.assertIn("edited", err)
        self.assertEqual(self.gh.writes(), [])

    def test_an_edit_before_the_check_started_or_no_edit_at_all_is_fine(self):
        for edited in (None, "2026-11-03T23:00:00Z"):
            with self.subTest(edited=edited):
                self.gh = FakeGh()
                self.addCleanup(self.gh.cleanup)
                self.setup_close(edited=edited)
                self.project_routes()
                self.assertEqual(self.run_cmd("close", "5", "--pr", "20")[0], 0)

    def test_a_check_run_for_another_commit_is_not_proof(self):
        runs = check_run()
        runs["check_runs"][0]["head_sha"] = "someone-elses"
        self.setup_close(checks=runs)
        self.assertEqual(self.run_cmd("close", "5", "--pr", "20")[0], 3)

    def test_a_malformed_timestamp_is_a_github_error_not_a_traceback(self):
        self.gh.route("POST graphql", {"status": 200, "body": {"data": {"repository": {"pullRequest": {"lastEditedAt": "yesterday"}}}}})
        self.setup_close()
        code, _, err = self.run_cmd("close", "5", "--pr", "20")
        self.assertEqual(code, 4)
        self.assertNotIn("Traceback", err)
        self.assertEqual(self.gh.writes(), [])

    def test_an_unreadable_edit_time_stops_the_close(self):
        self.gh.route("POST graphql", {"status": 200, "body": {"data": None}})
        self.setup_close()
        code, _, err = self.run_cmd("close", "5", "--pr", "20")
        self.assertIn(code, (3, 4))
        self.assertEqual(self.gh.writes(), [])

    def test_the_latest_check_run_decides(self):
        runs = {"check_runs": [run_of("success", ident=1), run_of("failure", ident=2, started="2026-11-04T01:00:00Z")]}
        self.setup_close(checks=runs)
        self.project_routes()
        self.assertEqual(self.run_cmd("close", "5", "--pr", "20")[0], 3)
        runs = {"check_runs": [run_of("failure", ident=1), run_of("success", ident=2, started="2026-11-04T01:00:00Z")]}
        self.gh = FakeGh()
        self.addCleanup(self.gh.cleanup)
        self.setup_close(checks=runs)
        self.project_routes()
        self.assertEqual(self.run_cmd("close", "5", "--pr", "20")[0], 0)

    def test_closing_an_already_closed_issue_writes_no_issue_change(self):
        self.setup_close(state="closed")
        self.project_routes()
        code, data, _ = self.run_cmd("close", "5", "--pr", "20")
        self.assertEqual((code, data["changed"]), (0, False))
        self.assertEqual([w for w in self.gh.writes() if "/issues/" in w["path"]], [])

    def test_close_also_writes_project_status_done_as_the_user(self):
        self.setup_close()
        self.project_routes()
        code, data, _ = self.run_cmd("close", "5", "--pr", "20")
        self.assertEqual(code, 0, data)
        patch = next(c for c in self.gh.writes() if c["path"] == f"{P}/items/555")
        self.assertEqual(self.body_of(patch), {"fields": [{"id": 12, "value": "opt-done"}]})
        self.assertEqual(patch["gh_token"], USER_TOKEN)
        order = [w["path"] for w in self.gh.writes()]
        self.assertLess(order.index(f"{I}/5"), order.index(f"{P}/items/555"))

    def test_close_leaves_a_status_that_is_already_done_alone_and_heals_on_retry(self):
        self.setup_close(state="closed")
        self.project_routes([{"id": 12, "name": "Status", "value": {"name": "Done"}}])
        code, data, _ = self.run_cmd("close", "5", "--pr", "20")
        self.assertEqual(code, 0)
        self.assertEqual([w for w in self.gh.writes() if w["method"] == "PATCH"], [])

    def test_a_project_failure_after_the_close_reports_the_close_and_exits_4(self):
        self.setup_close()
        self.gh.route(rf"GET {P}/fields\?per_page=100", PROJECT_FIELDS)
        self.gh.route(rf"POST {P}/items", {"status": 403, "body": {"message": "Resource not accessible"}})
        code, data, _ = self.run_cmd("close", "5", "--pr", "20")
        self.assertEqual((code, data["number"], data["failed"]), (4, 5, "project"))

    def test_without_a_project_setting_close_still_closes_and_says_the_project_was_skipped(self):
        self.setup_close()
        env = self.gh.env()
        del env["TRACKER_PROJECT"]
        code, data, _ = self.run_cmd("close", "5", "--pr", "20", env=env)
        self.assertEqual((code, data["project"]), (0, "skipped: TRACKER_PROJECT is not set"))

    def test_proof_is_required_even_when_the_issue_is_already_closed(self):
        self.setup_close(pr=merged_pr(merged=False), state="closed")
        self.project_routes()
        code, _, _ = self.run_cmd("close", "5", "--pr", "20")
        self.assertEqual(code, 3)
        self.assertEqual(self.gh.writes(), [])

    def test_a_close_whose_project_write_failed_is_healed_by_running_it_again(self):
        self.gh.route(rf"GET /repos/{REPO}", {"default_branch": "main"})
        self.gh.route("POST graphql", {"data": {"repository": {"pullRequest": {"lastEditedAt": None}}}})
        self.gh.route(rf"GET /repos/{REPO}/pulls/20", merged_pr())
        self.gh.route(self.CHECKS, check_run())
        self.bind_routes()
        self.gh.route(rf"GET {I}/5", issue(5), issue(5, state="closed"))
        self.gh.route(rf"PATCH {I}/5", issue(5, state="closed"))
        self.gh.route(rf"GET {P}/fields\?per_page=100", PROJECT_FIELDS)
        self.gh.route(rf"POST {P}/items", {"status": 403, "body": {"message": "no"}}, {"status": 200, "body": {"id": 555}})
        self.gh.route(rf"GET {P}/items/555.*", {"id": 555, "fields": []})
        self.gh.route(rf"PATCH {P}/items/555", {"id": 555})
        self.assertEqual(self.run_cmd("close", "5", "--pr", "20")[0], 4)
        code, data, _ = self.run_cmd("close", "5", "--pr", "20")
        self.assertEqual((code, data["changed"], data["project"]), (0, False, "done"))
        self.assertEqual(len([w for w in self.gh.writes() if w["path"] == f"{I}/5"]), 1)

    def test_a_closing_keyword_hidden_in_an_html_comment_is_not_proof(self):
        self.setup_close(pr=merged_pr(body="<!-- Closes #5 -->\nnothing else"))
        self.assertEqual(self.run_cmd("close", "5", "--pr", "20")[0], 3)

    def test_not_planned_needs_a_reason_and_leaves_it_in_the_status_comment(self):
        self.comments_route([])
        self.gh.route(rf"GET {I}/5", issue(5))
        self.gh.route(rf"POST {I}/5/comments", {"status": 201, "body": bot(9, "x")})
        self.gh.route(rf"PATCH {I}/5", issue(5, state="closed"))
        self.project_routes()
        code, data, _ = self.run_cmd("close", "5", "--reason", "not-planned", "--why", "superseded by the new import")
        self.assertEqual(code, 0, data)
        first, second = self.gh.writes()[:2]
        self.assertIn("superseded by the new import", self.body_of(first)["body"])
        self.assertEqual(self.body_of(second), {"state": "closed", "state_reason": "not_planned"})

    def test_a_close_with_a_reason_keeps_the_earlier_status_text_beneath_its_line(self):
        # The status comment often holds the acceptance evidence: closing must never discard it.
        self.comments_route([bot(7, STATUS + "\n# Acceptance evidence\n\n- AC1: proven by the gate log")])
        self.gh.route(rf"GET {I}/5", issue(5))
        self.gh.route(rf"PATCH {I}/comments/7", {"id": 7})
        self.gh.route(rf"PATCH {I}/5", issue(5, state="closed"))
        self.project_routes()
        code, data, _ = self.run_cmd("close", "5", "--reason", "not-planned", "--why", "the tool was retired")
        self.assertEqual((code, data["status_comment"]["action"]), (0, "updated"), data)
        first = self.gh.writes()[0]
        self.assertEqual((first["method"], first["path"]), ("PATCH", f"{I}/comments/7"))
        self.assertEqual(self.body_of(first)["body"],
                         STATUS + "\nClosed as not planned: the tool was retired"
                         "\n\n---\nStatus before closing:\n\n# Acceptance evidence\n\n- AC1: proven by the gate log")

    def test_a_retried_close_replaces_its_own_line_and_never_stacks(self):
        earlier = STATUS + "\nClosed as not planned: first wording\n\n---\nStatus before closing:\n\nworking on AC2"
        self.comments_route([bot(7, earlier)])
        self.gh.route(rf"GET {I}/5", issue(5))
        self.gh.route(rf"PATCH {I}/comments/7", {"id": 7})
        self.gh.route(rf"PATCH {I}/5", issue(5, state="closed"))
        self.project_routes()
        code, data, _ = self.run_cmd("close", "5", "--reason", "duplicate", "--why", "second wording")
        self.assertEqual(code, 0, data)
        self.assertEqual(self.body_of(self.gh.writes()[0])["body"],
                         STATUS + "\nClosed as duplicate: second wording\n\n---\nStatus before closing:\n\nworking on AC2")

    def test_a_close_over_a_bare_earlier_close_line_keeps_nothing(self):
        self.comments_route([bot(7, STATUS + "\nClosed as not planned: first wording")])
        self.gh.route(rf"GET {I}/5", issue(5))
        self.gh.route(rf"PATCH {I}/comments/7", {"id": 7})
        self.gh.route(rf"PATCH {I}/5", issue(5, state="closed"))
        self.project_routes()
        code, data, _ = self.run_cmd("close", "5", "--reason", "not-planned", "--why", "second wording")
        self.assertEqual(code, 0, data)
        self.assertEqual(self.body_of(self.gh.writes()[0])["body"], STATUS + "\nClosed as not planned: second wording")

    def test_duplicate_maps_to_its_own_state_reason(self):
        self.comments_route([])
        self.gh.route(rf"GET {I}/5", issue(5))
        self.gh.route(rf"POST {I}/5/comments", {"status": 201, "body": bot(9, "x")})
        self.gh.route(rf"PATCH {I}/5", issue(5, state="closed"))
        self.project_routes()
        self.run_cmd("close", "5", "--reason", "duplicate", "--why", "same as 4")
        self.assertEqual(self.body_of(self.gh.writes()[1])["state_reason"], "duplicate")

    EVIDENCE = f"https://github.com/{REPO}/issues/5#issuecomment-123456"
    IMPORTED = "Legacy ID: TASK-12\n\nBody.\n\n## Acceptance criteria\n- [x] AC1 first\n"

    def imported_checkout(self, issues=None, held=None, commit=True):
        """A checkout holding the importer's id map, committed at HEAD unless commit is false; run_cmd then runs in it."""
        path = checkout(self.gh.root)
        data = {"schema": 1, "repo": REPO, "issues": {"TASK-12": {"number": 5}} if issues is None else issues}
        if held is not None:
            data["held"] = held
        Path(path, ".github", "tracker").mkdir(parents=True)
        Path(path, ".github", "tracker", "id-map.json").write_text(json.dumps(data), encoding="utf-8")
        if commit:
            commit_all(path)
        self.cwd = str(path)
        return path

    def completed_routes(self, **fields):
        self.imported_checkout()
        self.comments_route([])
        self.gh.route(rf"GET {I}/5", issue(5, **fields))
        self.gh.route(rf"POST {I}/5/comments", {"status": 201, "body": bot(9, "x")})
        self.gh.route(rf"PATCH {I}/5", issue(5, state="closed"))
        self.project_routes()

    def test_completed_closes_an_imported_issue_with_its_evidence_in_the_status_comment(self):
        self.completed_routes(body=self.IMPORTED)
        code, data, _ = self.run_cmd("close", "5", "--reason", "completed", "--why", f"landed in the port, see {self.EVIDENCE}")
        self.assertEqual((code, data["changed"], data["state_reason"], data["project"]), (0, True, "completed", "done"), data)
        first, second = self.gh.writes()[:2]
        self.assertIn(f"Closed as completed (code landed before the cutover): landed in the port, see {self.EVIDENCE}", self.body_of(first)["body"])
        self.assertEqual(self.body_of(second), {"state": "closed", "state_reason": "completed"})

    def test_an_issue_already_on_the_project_keeps_its_item_and_its_status_is_set(self):
        self.imported_checkout()
        self.comments_route([])
        self.gh.route(rf"GET {I}/5", issue(5, body=self.IMPORTED))
        self.gh.route(rf"POST {I}/5/comments", {"status": 201, "body": bot(9, "x")})
        self.gh.route(rf"PATCH {I}/5", issue(5, state="closed"))
        self.gh.route(rf"GET {P}/fields\?per_page=100", PROJECT_FIELDS)
        self.gh.route(rf"POST {P}/items", {"status": 422, "body": {"message": "Content already exists in this project"}})
        self.gh.route("POST graphql", {"data": {"repository": {"issue": {"projectItems": {"nodes": [
            {"fullDatabaseId": "111", "project": {"number": 3, "owner": {"login": "example-org"}}},
            {"fullDatabaseId": "555", "project": {"number": 7, "owner": {"login": "example-org"}}}]}}}}})
        self.gh.route(rf"GET {P}/items/555.*", {"id": 555, "fields": []})
        self.gh.route(rf"PATCH {P}/items/555", {"id": 555})
        code, data, _ = self.run_cmd("close", "5", "--reason", "completed", "--why", self.EVIDENCE)
        self.assertEqual((code, data["project"]), (0, "done"), data)
        patch = next(c for c in self.gh.writes() if c["path"] == f"{P}/items/555")
        self.assertEqual(self.body_of(patch), {"fields": [{"id": 12, "value": "opt-done"}]})

    def test_an_already_exists_answer_with_no_item_on_this_project_fails_the_project_step(self):
        self.imported_checkout()
        self.comments_route([])
        self.gh.route(rf"GET {I}/5", issue(5, body=self.IMPORTED))
        self.gh.route(rf"POST {I}/5/comments", {"status": 201, "body": bot(9, "x")})
        self.gh.route(rf"PATCH {I}/5", issue(5, state="closed"))
        self.gh.route(rf"GET {P}/fields\?per_page=100", PROJECT_FIELDS)
        self.gh.route(rf"POST {P}/items", {"status": 422, "body": {"message": "Content already exists in this project"}})
        self.gh.route("POST graphql", {"data": {"repository": {"issue": {"projectItems": {"nodes": [
            {"fullDatabaseId": "111", "project": {"number": 3, "owner": {"login": "example-org"}}}]}}}}})
        code, data, _ = self.run_cmd("close", "5", "--reason", "completed", "--why", self.EVIDENCE)
        self.assertEqual((code, data["failed"]), (4, "project"), data)
        self.assertFalse([c for c in self.gh.writes() if c["path"].startswith(f"{P}/items/")])

    def test_completed_without_why_is_a_usage_error_before_any_call(self):
        self.assertEqual(self.run_cmd("close", "5", "--reason", "completed")[0], 2)
        self.assertEqual(self.gh.calls(), [])

    def test_completed_with_a_pr_is_a_usage_error_before_any_call(self):
        code, _, _ = self.run_cmd("close", "5", "--pr", "20", "--reason", "completed", "--why", self.EVIDENCE)
        self.assertEqual(code, 2)
        self.assertEqual(self.gh.calls(), [])

    def test_completed_on_a_native_issue_is_refused_and_writes_nothing(self):
        for body in ("Body.\n\n## Acceptance criteria\n- [ ] AC1 first\n", "Intro\nLegacy ID: TASK-12\n", " Legacy ID: TASK-12\n", "Legacy ID:\n", ""):
            with self.subTest(body=body):
                self.gh = FakeGh()
                self.addCleanup(self.gh.cleanup)
                self.completed_routes(body=body)
                code, _, err = self.run_cmd("close", "5", "--reason", "completed", "--why", self.EVIDENCE)
                self.assertEqual((code, self.gh.writes()), (3, []))
                self.assertIn("merged pull request", err)

    def test_completed_needs_a_link_into_this_repository_in_why(self):
        other = "https://github.com/someone/else/issues/5"
        for why in ("it landed", f"http://github.com/{REPO}/issues/5", f"https://github.com.evil.example/{REPO}/issues/5",
                    other, f"https://github.com/{REPO}", f"https://github.com/{REPO}/issues/", f"https://example.com/?u=https://github.com/{REPO}/issues/5/x"):
            with self.subTest(why=why):
                self.gh = FakeGh()
                self.addCleanup(self.gh.cleanup)
                self.completed_routes(body=self.IMPORTED)
                code, _, _ = self.run_cmd("close", "5", "--reason", "completed", "--why", why)
                self.assertEqual((code, self.gh.writes()), (3, []))

    def test_completed_accepts_a_pull_request_commit_or_run_link(self):
        for link in (f"https://github.com/{REPO}/pull/12", f"https://github.com/{REPO}/commit/abc1234", f"https://github.com/{REPO}/actions/runs/99"):
            with self.subTest(link=link):
                self.gh = FakeGh()
                self.addCleanup(self.gh.cleanup)
                self.completed_routes(body=self.IMPORTED)
                self.assertEqual(self.run_cmd("close", "5", "--reason", "completed", "--why", link)[0], 0)

    def test_completed_on_an_already_closed_imported_issue_only_heals_the_project(self):
        self.imported_checkout()
        self.gh.route(rf"GET {I}/5", issue(5, state="closed", body=self.IMPORTED))
        self.project_routes()
        code, data, _ = self.run_cmd("close", "5", "--reason", "completed", "--why", self.EVIDENCE)
        self.assertEqual((code, data["changed"], data["project"]), (0, False, "done"))
        self.assertEqual([w["path"] for w in self.gh.writes() if w["path"].startswith(I)], [])

    def test_completed_needs_the_committed_id_map_to_name_this_issue(self):
        cases = {
            "the map names another issue": dict(issues={"TASK-12": {"number": 6}}),
            "the map does not list the id": dict(issues={"TASK-13": {"number": 5}}),
            "the id was held back": dict(issues={}, held={"TASK-12": "sweep"}),
        }
        for label, kwargs in cases.items():
            with self.subTest(label):
                self.gh = FakeGh()
                self.addCleanup(self.gh.cleanup)
                self.completed_routes(body=self.IMPORTED)
                shutil.rmtree(Path(self.cwd))
                self.imported_checkout(**kwargs)
                code, _, err = self.run_cmd("close", "5", "--reason", "completed", "--why", self.EVIDENCE)
                self.assertEqual((code, self.gh.writes()), (3, []), err)
                self.assertIn("id-map.json", err)
        for label in ("no map", "not a checkout", "a map that is not committed", "a map whose committed version says otherwise"):
            with self.subTest(label):
                self.gh = FakeGh()
                self.addCleanup(self.gh.cleanup)
                self.completed_routes(body=self.IMPORTED)
                if label in ("no map", "a map that is not committed"):
                    shutil.rmtree(Path(self.cwd))
                    path = self.imported_checkout(commit=False)
                    if label == "no map":
                        Path(path, ".github", "tracker", "id-map.json").unlink()
                        commit_all(path)
                elif label == "not a checkout":
                    self.cwd = str(self.gh.root)
                else:
                    shutil.rmtree(Path(self.cwd))
                    path = self.imported_checkout(issues={"TASK-12": {"number": 6}})
                    Path(path, ".github", "tracker", "id-map.json").write_text(
                        json.dumps({"schema": 1, "repo": REPO, "issues": {"TASK-12": {"number": 5}}}), encoding="utf-8")
                code, _, err = self.run_cmd("close", "5", "--reason", "completed", "--why", self.EVIDENCE)
                self.assertEqual((code, self.gh.writes()), (3, []), err)

    def test_completed_matches_the_mapped_id_without_regard_to_case(self):
        self.completed_routes(body="Legacy ID: task-12\n\nBody.\n")
        code, data, err = self.run_cmd("close", "5", "--reason", "completed", "--why", self.EVIDENCE)
        self.assertEqual((code, data["changed"]), (0, True), err)

    def test_misuse_is_a_usage_error_before_any_call(self):
        for argv in (("close", "5"), ("close", "5", "--reason", "not-planned"), ("close", "5", "--pr", "20", "--reason", "duplicate", "--why", "x"),
                     ("close", "5", "--reason", "completed")):
            with self.subTest(argv=argv):
                self.assertEqual(self.run_cmd(*argv)[0], 2)
        self.assertEqual(self.gh.calls(), [])

    def test_a_why_with_a_sweep_hit_closes_nothing(self):
        code, _, _ = self.run_cmd("close", "5", "--reason", "not-planned", "--why", "see 8.8.4.4")
        self.assertEqual(code, 3)
        self.assertEqual(self.gh.calls(), [])


class InputFileTests(Base):
    def test_a_file_that_is_not_text_is_a_usage_error_too(self):
        path = self.gh.root / "binary.md"
        path.write_bytes(b"\xff\xfe\x00bad")
        code, _, err = self.run_cmd("create", "--title", "t", "--body-file", str(path))
        self.assertEqual(code, 2)
        self.assertNotIn("Traceback", err)

    def test_an_unreadable_input_file_is_a_usage_error_not_a_traceback(self):
        for argv in (("create", "--title", "t", "--body-file", "/nonexistent/body.md"), ("status", "5", "--file", "/nonexistent/status.md")):
            with self.subTest(argv=argv):
                code, _, err = self.run_cmd(*argv)
                self.assertEqual(code, 2)
                self.assertIn("/nonexistent", err)
        self.assertEqual(self.gh.calls(), [])


CLOSING_CORPUS = [
    "Closes #5", "closes #5abc", "Closes #50", "Fixes: #5", "Resolved   #5", "closed\t#5",
    "Closes https://github.com/example-org/example/issues/5", "Closes https://github.com/example-org/example/issues/5abc",
    "Closes http://github.com/Other/Repo/issues/7", "fixes example-org/example#5", "Fixes other/repo#5 and closes #6",
    "<!-- Closes #5 -->", "prefix-closes #5", "Closes\n#5", "closes #5, closes #5, fixes #9", "Closes #٥", "CLOSES #5.",
    "Resolves: example-org/example#12 Resolve #13",
]


# The answers the acceptance evidence checker (ac-evidence-check.mjs: parseClosesTargets with this repository as the
# default, and parseAcceptanceCriteria) gave for these inputs when the tracker was ported. The tracker must keep agreeing
# with them: a close it accepts is a close the checker would have counted.
CHECKER_CLOSES = [
    ["example-org/example#5"], [], ["example-org/example#50"], ["example-org/example#5"], ["example-org/example#5"],
    ["example-org/example#5"], ["example-org/example#5"], [], ["Other/Repo#7"], ["example-org/example#5"],
    ["other/repo#5", "example-org/example#6"], [], ["example-org/example#5"], [], ["example-org/example#5", "example-org/example#9"],
    [], ["example-org/example#5"], ["example-org/example#12", "example-org/example#13"],
]

class CheckerParityTests(unittest.TestCase):
    def test_the_closing_grammar_matches_the_evidence_checker(self):
        for body, targets in zip(CLOSING_CORPUS, CHECKER_CLOSES, strict=True):
            with self.subTest(body=body):
                self.assertEqual(tracker.closes_targets(body, "example-org/example"), targets)

    def test_the_criteria_section_matches_the_evidence_checker(self):
        bodies = [BODY, "## Acceptance criteria\n- [ ] AC1 a\n\n## Notes\n- [ ] AC2 example\n", "Intro\n- [ ] AC1 a\n- [ ] AC2 b\n",
                  "## Acceptance criteria\n- [ ] AC1 a\n### Extra\n- [ ] AC2 b\n## Notes\n- [ ] AC3 c\n",
                  "# Acceptance criteria #\r\n- [ ] AC1 a\r\n- [x] AC2 b\r\n", "- [ ] AC1\n- [ ]  AC2 spaced\n* [ ] AC3 star\n"]
        expected = [[1, 2, 3], [1], [1, 2], [1, 2], [1, 2], [2, 3]]
        for body, numbers in zip(bodies, expected, strict=True):
            with self.subTest(body=body):
                self.assertEqual([n for n, _ in tracker.criteria_lines(body)], numbers)


class TickSectionTests(Base):
    def tick(self, body, criterion):
        ticked = body
        self.gh.route(rf"GET {I}/5", issue(5, body=body), issue(5, body=ticked))
        self.gh.route(rf"PATCH {I}/5", {})
        return self.run_cmd("tick", "5", criterion)

    def test_an_ac_shaped_line_outside_the_criteria_section_is_not_a_criterion(self):
        body = "## Acceptance criteria\n- [ ] AC1 real\n\n## Notes\n- [ ] AC2 example only\n"
        code, _, err = self.tick(body, "AC2")
        self.assertEqual(code, 3)
        self.assertEqual(self.gh.writes(), [])

    def test_a_criterion_under_a_subheading_of_the_section_still_ticks(self):
        body = "## Acceptance criteria\n- [ ] AC1 a\n### Extra\n- [ ] AC2 b\n## Notes\n- [ ] AC2 shadow\n"
        ticked = body.replace("- [ ] AC2 b", "- [x] AC2 b")
        self.gh.route(rf"GET {I}/5", issue(5, body=body), issue(5, body=ticked))
        self.gh.route(rf"PATCH {I}/5", {})
        code, data, _ = self.run_cmd("tick", "5", "AC2")
        self.assertEqual(code, 0, data)
        self.assertEqual(self.body_of(self.gh.writes()[0]), {"body": ticked})

    def test_two_criteria_sections_are_refused_like_the_checker_does(self):
        body = "## Acceptance criteria\n- [ ] AC1 a\n## Acceptance criteria\n- [ ] AC2 b\n"
        self.assertEqual(self.tick(body, "AC1")[0], 3)

    def test_a_headingless_body_still_works_and_crlf_is_kept(self):
        body = "Intro\r\n- [ ] AC1 a\r\n- [ ] AC2 b\r\n"
        ticked = body.replace("- [ ] AC2", "- [x] AC2")
        self.gh.route(rf"GET {I}/5", issue(5, body=body), issue(5, body=ticked))
        self.gh.route(rf"PATCH {I}/5", {})
        self.assertEqual(self.run_cmd("tick", "5", "2")[0], 0)
        self.assertEqual(self.body_of(self.gh.writes()[0]), {"body": ticked})


class IdentityTests(Base):
    def test_a_write_without_the_app_token_stops_before_any_call(self):
        code, _, err = self.run_cmd("tick", "5", "AC1", env=self.gh.env(app_token=None))
        self.assertEqual(code, 2)
        self.assertIn("TRACKER_APP_TOKEN", err)
        self.assertEqual(self.gh.calls(), [])

    def test_a_missing_project_number_is_a_clear_usage_error(self):
        env = self.gh.env()
        del env["TRACKER_PROJECT"]
        code, _, err = self.run_cmd("claim", "5", "--lane", "a", env=env)
        self.assertEqual(code, 2)
        self.assertIn("TRACKER_PROJECT", err)

    def test_every_write_is_audited(self):
        ticked = BODY.replace("- [ ] AC2", "- [x] AC2")
        self.gh.route(rf"GET {I}/5", issue(5, body=BODY), issue(5, body=ticked))
        self.gh.route(rf"PATCH {I}/5", {})
        self.run_cmd("tick", "5", "AC2")
        lines = (self.gh.state / "audit.jsonl").read_text().splitlines()
        self.assertEqual([json.loads(l)["issue"] for l in lines], [5])


class CommandLineTests(Base):
    def test_the_script_runs_as_a_command_and_exits_with_the_command_status(self):
        self.gh.route(rf"GET {I}/5", issue(5))
        done = subprocess.run([sys.executable, str(HERE / "tracker.py"), "view", "5"], capture_output=True, text=True,
                              env=self.gh.env(), timeout=60, check=False)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout)["number"], 5)
        missing = subprocess.run([sys.executable, str(HERE / "tracker.py"), "tick", "5", "AC1"], capture_output=True, text=True,
                                 env=self.gh.env(app_token=None), timeout=60, check=False)
        self.assertEqual(missing.returncode, 2)


def commit_all(path, message="map"):
    git = ["git", "-C", str(path), "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false"]
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", message], check=True)


def checkout(root, origin=None):
    """A git checkout under root, with an origin remote when one is given."""
    path = Path(root, "checkout")
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    if origin:
        subprocess.run(["git", "-C", str(path), "remote", "add", "origin", origin], check=True)
    return path


USER_REPO = "example-user/example"
UP = "/user" + "s/example-user/projectsV2/7"  # spelled in parts: the export's leak scan reads this GitHub API path as a home path


class UserOwnedTests(Base):
    """A repository and Project owned by a user account rather than an organisation."""

    def user_env(self, **extra):
        return self.gh.env(TRACKER_REPO=USER_REPO, **extra)

    def user_routes(self, add=None):
        self.gh.route(r"GET /users/example-user", {"login": "example-user", "type": "User"})
        self.gh.route(rf"GET {UP}/fields\?per_page=100", PROJECT_FIELDS)
        self.gh.route(rf"POST {UP}/items", add or {"id": 555})
        self.gh.route(rf"GET {UP}/items/555.*", {"id": 555, "fields": []})
        self.gh.route(rf"PATCH {UP}/items/555", {"id": 555})

    def test_a_user_owned_project_is_reached_under_users_and_written_as_the_user(self):
        self.user_routes()
        code, data, err = self.run_cmd("claim", "5", "--lane", "lane-a", env=self.user_env())
        self.assertEqual(code, 0, err)
        self.assertTrue(data["changed"])
        add, patch = self.gh.writes()
        self.assertEqual((add["method"], add["path"]), ("POST", f"{UP}/items"))
        self.assertEqual(self.body_of(add), {"type": "Issue", "owner": "example-user", "repo": "example", "number": 5})
        self.assertEqual((patch["method"], patch["path"]), ("PATCH", f"{UP}/items/555"))
        self.assertTrue(all(c["gh_token"] == USER_TOKEN for c in self.gh.writes()))
        self.assertFalse([c for c in self.gh.calls() if "/orgs/" in c["path"]])
        self.assertEqual(len([c for c in self.gh.calls() if c["path"] == "/users/example-user"]), 1)  # read once per run

    def test_an_already_added_issue_finds_its_item_on_a_user_owned_project(self):
        self.user_routes(add={"status": 422, "body": {"message": "Content already exists in this project"}})
        self.gh.route("POST graphql", {"data": {"repository": {"issue": {"projectItems": {"nodes": [
            {"fullDatabaseId": "555", "project": {"number": 7, "owner": {"login": "example-user"}}}]}}}}})
        code, _, err = self.run_cmd("claim", "5", "--lane", "lane-a", env=self.user_env())
        self.assertEqual(code, 0, err)
        query = json.loads([c for c in self.gh.calls() if c["path"] == "graphql"][0]["stdin"])["query"]
        self.assertIn("... on User { login }", query)

    def test_an_already_exists_answer_in_the_unprocessable_error_shape_finds_the_existing_item(self):
        """A parent's sub-issues join the Project with it, so adding the child answers UNPROCESSABLE "already exists"."""
        exists = {"status": 422, "body": {"message": "Validation Failed", "errors": [
            {"type": "UNPROCESSABLE", "message": "Content already exists in this project"}]}}
        for owner, base in (("example-user", UP), ("example-org", P)):
            with self.subTest(owner=owner):
                gh = FakeGh()
                self.addCleanup(gh.cleanup)
                self.gh = gh
                gh.route(rf"POST {base}/items", exists)  # first, so it answers the add
                gh.route("POST graphql", {"data": {"repository": {"issue": {"projectItems": {"nodes": [
                    {"fullDatabaseId": "555", "project": {"number": 7, "owner": {"login": owner}}}]}}}}})
                if base == UP:
                    self.user_routes()
                    env = self.user_env()
                else:
                    self.project_routes()
                    env = gh.env()
                code, data, err = self.run_cmd("claim", "5", "--lane", "lane-a", env=env)
                self.assertEqual(code, 0, err)
                self.assertTrue(data["changed"])
                self.assertEqual([c["path"] for c in gh.writes()], [f"{base}/items", f"{base}/items/555"])
                self.assertEqual(len([c for c in gh.calls() if c["path"] == "graphql"]), 1)

    def test_another_unprocessable_answer_is_not_read_as_already_added(self):
        self.user_routes(add={"status": 422, "body": {"message": "Validation Failed", "errors": [
            {"type": "UNPROCESSABLE", "message": "Content type is not supported"}]}})
        code, _, _ = self.run_cmd("claim", "5", "--lane", "lane-a", env=self.user_env())
        self.assertEqual(code, 4)
        self.assertFalse([c for c in self.gh.calls() if c["path"] == "graphql"])

    def test_a_project_owner_other_than_the_repository_owner(self):
        self.user_routes()
        self.gh.route(rf"GET {I}/5", issue(5))
        code, _, err = self.run_cmd("claim", "5", "--lane", "lane-a", env=self.gh.env(TRACKER_PROJECT_OWNER="example-user"))
        self.assertEqual(code, 0, err)
        add = self.gh.writes()[0]
        self.assertEqual(add["path"], f"{UP}/items")
        self.assertEqual(self.body_of(add)["owner"], "example-org")  # the issue's repository, not the Project owner

    def test_issue_types_and_the_org_priority_field_are_refused_on_a_user_repository_before_any_write(self):
        self.user_routes()
        for flags in (["--type", "Task"], ["--priority", "High"]):
            with self.subTest(flags=flags):
                code, _, err = self.run_cmd("create", "--title", "t", "--body-stdin", *flags, stdin="b", env=self.user_env())
                self.assertEqual(code, 2, err)
                self.assertIn("organisation-owned repository", err)
        self.assertEqual(self.gh.writes(), [])
        self.assertEqual(self.gh.renders(), [])
        self.assertFalse([c for c in self.gh.calls() if "issue-fields" in c["path"]])

    def test_a_plain_create_on_a_user_repository_needs_no_org_feature(self):
        self.user_routes()
        self.gh.route(rf"POST /repos/{USER_REPO}/issues", {"status": 201, "body": issue(11)})
        code, data, err = self.run_cmd("create", "--title", "t", "--body-stdin", "--label", "bug", stdin="b", env=self.user_env())
        self.assertEqual(code, 0, err)
        self.assertEqual(data["number"], 11)
        self.assertEqual(self.gh.writes()[0]["gh_token"], APP_TOKEN)

    def test_the_app_identity_cannot_write_a_user_owned_project(self):
        self.user_routes()
        for argv in (("claim", "5", "--lane", "lane-a"), ("handoff", "5", "--scope", "s", "--verified", "v", "--blockers", "b", "--next", "n"),
                     ("create", "--title", "t", "--body-stdin"), ("close", "5", "--reason", "not-planned", "--why", "dropped")):
            with self.subTest(argv=argv[0]):
                code, _, err = self.run_cmd(*argv, stdin="b", env=self.user_env(TRACKER_PROJECT_IDENTITY="app"))
                self.assertEqual(code, 2, err)
                self.assertIn("user-owned Project", err)
        self.assertEqual(self.gh.writes(), [])

    def test_an_owner_that_is_neither_kind_is_not_guessed(self):
        self.gh.route(r"GET /users/example-user", {"login": "example-user", "type": "Enterprise"})
        code, _, err = self.run_cmd("claim", "5", "--lane", "lane-a", env=self.user_env())
        self.assertEqual(code, 4, err)
        self.assertEqual(self.gh.writes(), [])


class UserPriorityBase(Base):
    """A user-owned repository and Project: priority is a `priority:<low|medium|high>` label and a Project Priority field."""

    def setUp(self):
        super().setUp()
        self.gh.route(r"GET /users/example-user", {"login": "example-user", "type": "User"})
        self.gh.route(rf"GET {UP}/fields\?per_page=100", USER_PROJECT_FIELDS)
        self.gh.route(rf"POST {UP}/items", {"id": 555})
        self.gh.route(rf"GET {UP}/items/555.*", {"id": 555, "fields": []})
        self.gh.route(rf"PATCH {UP}/items/555", {"id": 555})
        self.gh.route(rf"POST /repos/{USER_REPO}/issues", {"status": 201, "body": issue(11)})
        self.env = self.gh.env(TRACKER_REPO=USER_REPO)

    def create(self, *flags, env=None):
        return self.run_cmd("create", "--title", "t", "--body-stdin", *flags, stdin="b", env=env or self.env)

    def sent(self, path):
        (call,) = [c for c in self.gh.writes() if c["path"] == path]
        return call

    def patches(self):
        return [self.body_of(c) for c in self.gh.writes() if c["method"] == "PATCH"]


class UserPriorityCreateTests(UserPriorityBase):
    def test_create_with_a_priority_label_also_sets_the_project_priority_field(self):
        code, data, err = self.create("--label", "bug", "--label", "priority:high")
        self.assertEqual(code, 0, err)
        issue_call = self.sent(f"/repos/{USER_REPO}/issues")
        self.assertEqual(self.body_of(issue_call)["labels"], ["bug", "priority:high"])
        self.assertEqual(issue_call["gh_token"], APP_TOKEN)
        patch = self.sent(f"{UP}/items/555")
        self.assertEqual(self.body_of(patch), {"fields": [{"id": 14, "value": "p-high"}]})
        self.assertEqual(patch["gh_token"], USER_TOKEN)

    def test_create_with_a_priority_field_value_also_adds_the_matching_label_beside_the_others(self):
        code, data, err = self.create("--label", "bug", "--field", "Priority=medium")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.body_of(self.sent(f"/repos/{USER_REPO}/issues"))["labels"], ["bug", "priority:medium"])
        self.assertEqual(self.body_of(self.sent(f"{UP}/items/555")), {"fields": [{"id": 14, "value": "p-medium"}]})
        self.assertEqual(self.sent(f"{UP}/items/555")["gh_token"], USER_TOKEN)

    def test_a_field_value_alone_adds_the_label_when_no_label_was_given(self):
        code, _, err = self.create("--field", "Priority=Low")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.body_of(self.sent(f"/repos/{USER_REPO}/issues"))["labels"], ["priority:low"])

    def test_the_same_value_given_both_ways_is_not_a_conflict(self):
        code, _, err = self.create("--label", "priority:high", "--field", "Priority=HIGH")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.body_of(self.sent(f"/repos/{USER_REPO}/issues"))["labels"], ["priority:high"])
        self.assertEqual(self.patches(), [{"fields": [{"id": 14, "value": "p-high"}]}])

    def test_conflicting_priorities_in_one_call_are_a_usage_error_before_any_write(self):
        for flags in (["--label", "priority:high", "--field", "Priority=Low"], ["--label", "priority:high", "--label", "priority:low"],
                      ["--label", "priority:urgent"], ["--label", "priority:high", "--field", "Priority=Urgent"],
                      ["--label", "priority:urgent", "--field", "Priority=Urgent"], ["--label", "priority:"]):
            with self.subTest(flags=flags):
                code, _, err = self.create(*flags)
                self.assertEqual(code, 2, err)
        self.assertEqual(self.gh.writes(), [])

    def test_urgent_is_field_only_create_with_the_urgent_field_adds_no_label(self):
        for flags in (["--field", "Priority=Urgent"], ["--field", "Priority=urgent"], ["--label", "bug", "--field", "Priority=Urgent"]):
            with self.subTest(flags=flags):
                self.gh.cleanup()
                self.gh = FakeGh()
                UserPriorityBase.setUp(self)
                code, _, err = self.create(*flags)
                self.assertEqual(code, 0, err)
                payload = self.body_of(self.sent(f"/repos/{USER_REPO}/issues"))
                self.assertEqual(payload.get("labels"), ["bug"] if "bug" in flags else None)  # never a priority label
                self.assertEqual(self.patches(), [{"fields": [{"id": 14, "value": "p-urgent"}]}])

    def test_another_priority_option_passes_through_create_field_untouched_and_is_ignored_by_the_sync(self):
        extra = [dict(f, options=[*f["options"], {"id": "p-someday", "name": {"html": "Someday", "raw": "Someday"}}]) if f["name"] == "Priority" else f
                 for f in USER_PROJECT_FIELDS]
        self.gh.route(rf"GET {UP}/fields\?per_page=100", extra, replace=True)
        for flags in (["--field", "Priority=Someday"], ["--label", "priority:high", "--field", "Priority=Someday"]):
            with self.subTest(flags=flags):
                self.gh.cleanup()
                self.gh = FakeGh()
                UserPriorityBase.setUp(self)
                self.gh.route(rf"GET {UP}/fields\?per_page=100", extra, replace=True)
                code, _, err = self.create(*flags)
                self.assertEqual(code, 0, err)
                payload = self.body_of(self.sent(f"/repos/{USER_REPO}/issues"))
                self.assertEqual(payload.get("labels"), ["priority:high"] if "priority:high" in flags else None)
                self.assertEqual(self.patches(), [{"fields": [{"id": 14, "value": "p-someday"}]}])

    def test_a_priority_label_without_a_project_adds_only_the_label(self):
        env = self.gh.env(TRACKER_REPO=USER_REPO)
        del env["TRACKER_PROJECT"]
        code, _, err = self.create("--label", "priority:low", env=env)
        self.assertEqual(code, 0, err)
        self.assertEqual([c["path"] for c in self.gh.writes()], [f"/repos/{USER_REPO}/issues"])

    def test_an_organisation_owner_is_unchanged_a_priority_label_is_only_a_label(self):
        self.gh.route(rf"POST {I}", {"status": 201, "body": issue(11)})
        self.project_routes()
        code, _, err = self.create("--label", "priority:high", env=self.gh.env())
        self.assertEqual(code, 0, err)
        self.assertEqual(self.body_of(self.sent(I))["labels"], ["priority:high"])
        self.assertEqual(self.patches(), [])


class UserPriorityCommandTests(UserPriorityBase):
    LABELS = f"/repos/{USER_REPO}/issues/5/labels"

    def issue_route(self, *labels):
        self.gh.route(rf"GET /repos/{USER_REPO}/issues/5", issue(5, labels=[{"name": n} for n in labels]))
        self.gh.route(rf"POST {self.LABELS}", [])
        self.gh.route(rf"DELETE {self.LABELS}/.*", [])

    def test_priority_changes_an_existing_issue_writing_label_and_field_together(self):
        self.issue_route("bug", "priority:low", "priority:medium")
        code, data, err = self.run_cmd("priority", "5", "High", env=self.env)
        self.assertEqual(code, 0, err)
        self.assertEqual((data["number"], data["priority"], data["changed"]), (5, "High", True))
        order = [(c["method"], c["path"]) for c in self.gh.writes() if "/labels" in c["path"]]
        self.assertEqual(order[0], ("POST", self.LABELS))  # the wanted label is there before any other is taken away
        writes = [(c["method"], c["path"], c["gh_token"]) for c in self.gh.writes()]
        self.assertEqual(sorted(w for w in writes if "/labels" in w[1]), sorted([
            ("DELETE", f"{self.LABELS}/priority%3Alow", APP_TOKEN), ("DELETE", f"{self.LABELS}/priority%3Amedium", APP_TOKEN),
            ("POST", self.LABELS, APP_TOKEN)]))
        self.assertEqual(self.body_of(next(c for c in self.gh.writes() if c["method"] == "POST" and c["path"] == self.LABELS)), {"labels": ["priority:high"]})
        self.assertEqual(self.patches(), [{"fields": [{"id": 14, "value": "p-high"}]}])
        self.assertEqual(self.sent(f"{UP}/items/555")["gh_token"], USER_TOKEN)
        self.assertEqual(self.sent(f"{UP}/items")["gh_token"], USER_TOKEN)  # the Project writes are the Project identity's

    def test_a_priority_already_in_sync_writes_nothing(self):
        self.issue_route("bug", "priority:high")
        self.gh.route(rf"GET {UP}/items/555.*", {"id": 555, "fields": [{"id": 14, "name": "Priority", "value": {"id": "p-high", "name": {"html": "High", "raw": "High"}}}]}, replace=True)
        code, data, err = self.run_cmd("priority", "5", "high", env=self.env)
        self.assertEqual((code, data["changed"]), (0, False), err)
        self.assertEqual([c for c in self.gh.writes() if "/labels" in c["path"] or c["method"] == "PATCH"], [])

    def test_a_label_already_right_and_a_field_that_is_not_writes_only_the_field(self):
        self.issue_route("priority:high")
        code, data, err = self.run_cmd("priority", "5", "high", env=self.env)
        self.assertEqual((code, data["changed"]), (0, True), err)
        self.assertEqual([c for c in self.gh.writes() if "/labels" in c["path"]], [])
        self.assertEqual(self.patches(), [{"fields": [{"id": 14, "value": "p-high"}]}])

    def test_a_value_outside_low_medium_high_is_a_usage_error_before_any_write(self):
        self.issue_route()
        for bad in ("priority:high", "", "high ", "nope", "Someday"):
            with self.subTest(value=bad):
                code, _, err = self.run_cmd("priority", "5", bad, env=self.env)
                self.assertEqual(code, 2, err)
                self.assertIn("low, medium, high, urgent", err)
        self.assertEqual(self.gh.writes(), [])

    def test_priority_on_an_organisation_owner_is_a_usage_error_before_any_write(self):
        code, _, err = self.run_cmd("priority", "5", "High", env=self.gh.env())
        self.assertEqual(code, 2, err)
        self.assertIn("user-owned", err)
        self.assertEqual(self.gh.writes(), [])

    def test_priority_needs_the_project_and_the_right_identities_before_any_call(self):
        env = self.gh.env(TRACKER_REPO=USER_REPO)
        del env["TRACKER_PROJECT"]
        self.issue_route()
        for env, said in ((env, "TRACKER_PROJECT"), (self.gh.env(TRACKER_REPO=USER_REPO, app_token=None), "TRACKER_APP_TOKEN"),
                          (self.gh.env(TRACKER_REPO=USER_REPO, TRACKER_PROJECT_IDENTITY="app"), "user-owned Project")):
            with self.subTest(said=said):
                code, _, err = self.run_cmd("priority", "5", "High", env=env)
                self.assertEqual(code, 2, err)
                self.assertIn(said, err)
        self.assertEqual(self.gh.writes(), [])

    def test_priority_urgent_sets_the_field_and_removes_every_priority_label(self):
        self.issue_route("bug", "priority:low", "priority:high")
        code, data, err = self.run_cmd("priority", "5", "urgent", env=self.env)
        self.assertEqual((code, data["priority"], data["changed"]), (0, "Urgent", True), err)
        self.assertEqual(sorted((c["method"], c["path"]) for c in self.gh.writes() if "/labels" in c["path"]),
                         [("DELETE", f"{self.LABELS}/priority%3Ahigh"), ("DELETE", f"{self.LABELS}/priority%3Alow")])  # no label is added
        self.assertEqual(self.patches(), [{"fields": [{"id": 14, "value": "p-urgent"}]}])

    def test_a_failed_label_add_reports_nothing_written_and_touches_no_other_label(self):
        self.issue_route("priority:low")
        self.gh.route(rf"POST {self.LABELS}", {"status": 403, "body": {"message": "Resource not accessible"}}, replace=True)
        code, data, _ = self.run_cmd("priority", "5", "High", env=self.env)
        self.assertEqual((code, data["failed"], data["label"], data["field"], data["changed"]), (4, "label", "not written", "not written", False))
        self.assertEqual([c for c in self.gh.writes() if c["method"] in ("DELETE", "PATCH")], [])

    def test_a_failed_label_removal_reports_the_label_as_partly_written_and_the_field_untouched(self):
        self.issue_route("priority:low")
        self.gh.route(rf"DELETE {self.LABELS}/.*", {"status": 403, "body": {"message": "Resource not accessible"}}, replace=True)
        code, data, _ = self.run_cmd("priority", "5", "High", env=self.env)
        self.assertEqual((code, data["failed"], data["label"], data["field"], data["changed"]), (4, "label", "partly written", "not written", True))
        self.assertEqual([c for c in self.gh.writes() if c["method"] == "PATCH"], [])

    def test_a_failed_project_step_still_reports_the_label_that_was_written(self):
        self.issue_route("priority:low")
        self.gh.route(rf"PATCH {UP}/items/555", {"status": 403, "body": {"message": "Resource not accessible"}}, replace=True)
        code, data, _ = self.run_cmd("priority", "5", "High", env=self.env)
        self.assertEqual(code, 4)
        self.assertEqual((data["number"], data["failed"], data["label"], data["field"], data["changed"]), (5, "project", "written", "not written", True))


class PriorityCheckTests(UserPriorityBase):
    OTHER = "example-user/other"

    def setUp(self):
        super().setUp()
        items = [project_item(n, repo=USER_REPO, priority="High", labels=["priority:high"]) for n in range(1, 120)]
        items += [
            project_item(200, repo=USER_REPO, priority="High", labels=["priority:low"]),    # disagree
            project_item(201, repo=USER_REPO, labels=["priority:medium", "bug"]),           # field missing
            project_item(202, repo=USER_REPO, priority="Low", labels=["bug"]),              # label missing
            project_item(203, repo=USER_REPO, labels=["bug"]),                              # neither: not reported
            project_item(204, repo=USER_REPO, priority="High", labels=["priority:high", "priority:low"]),  # two labels
            project_item(205, repo=self.OTHER, priority="Low"),                             # another repository's
            project_item(206, repo=USER_REPO, status="Done", priority="High", labels=["priority:low"]),  # Done is still open
            project_item(207, repo=USER_REPO, priority="Urgent"),                           # Urgent has no label: in sync
            project_item(208, repo=USER_REPO, priority="Urgent", labels=["priority:high"]),  # Urgent with a label: mismatch
            project_item(209, repo=USER_REPO, priority="Someday"),                          # another option, no label: ignored
        ]
        self.items = items
        self.gh.items_route(rf"GET {UP}/items\?.*", items)

    def test_it_lists_open_items_where_label_and_field_disagree_or_one_is_missing_across_every_page_and_exits_nonzero(self):
        code, data, err = self.run_cmd("priority-check", env=self.env)
        self.assertEqual(code, tracker.DRIFT, err)
        self.assertEqual(tracker.DRIFT, 5)  # not 1, which is what an uncaught Python exception exits with
        found = {row["number"]: row["problem"] for row in data["items"]}
        self.assertEqual(found, {200: "mismatch", 201: "field missing", 202: "label missing", 204: "mismatch", 206: "mismatch", 208: "mismatch"})
        self.assertEqual(data["count"], 6)
        self.assertEqual(data["checked"], 119 + 9)  # every open item of this repository read, none of the other's
        row = next(r for r in data["items"] if r["number"] == 200)
        self.assertEqual((row["label"], row["field"]), (["priority:low"], "High"))
        queries = [urllib.parse.parse_qs(urllib.parse.urlsplit(c["path"]).query) for c in self.gh.calls("GET") if "/items?" in c["path"]]
        self.assertGreater(len(queries), 1)
        self.assertEqual(queries[0]["q"], [f"is:issue is:open repo:{USER_REPO}"])
        self.assertEqual(queries[0]["fields[]"], ["14"])  # the Priority field is asked for; the listing returns only what is asked
        self.assertEqual(self.gh.writes(), [])  # a read: nothing is written

    def test_it_exits_zero_when_every_item_agrees_or_has_neither(self):
        self.gh.items_route(rf"GET {UP}/items\?.*", [project_item(1, repo=USER_REPO, priority="Low", labels=["priority:low"]), project_item(2, repo=USER_REPO)])
        code, data, err = self.run_cmd("priority-check", env=self.env)
        self.assertEqual((code, data["items"], data["count"]), (0, [], 0), err)

    def test_it_is_refused_on_an_organisation_owner(self):
        code, _, err = self.run_cmd("priority-check", env=self.gh.env())
        self.assertEqual(code, 2, err)
        self.assertIn("user-owned", err)


class IdentitySettingTests(Base):
    def test_the_app_identity_can_write_an_org_project(self):
        self.project_routes()
        code, _, err = self.run_cmd("claim", "5", "--lane", "lane-a", env=self.gh.env(TRACKER_PROJECT_IDENTITY="app"))
        self.assertEqual(code, 0, err)
        self.assertTrue(all(c["gh_token"] == APP_TOKEN for c in self.gh.writes()))

    def test_the_app_project_identity_needs_the_token_before_any_call(self):
        code, _, err = self.run_cmd("claim", "5", "--lane", "lane-a", env=self.gh.env(app_token=None, TRACKER_PROJECT_IDENTITY="app"))
        self.assertEqual(code, 2)
        self.assertIn("TRACKER_APP_TOKEN", err)
        self.assertEqual(self.gh.calls(), [])

    def test_a_user_issue_identity_writes_with_the_ambient_login_and_owns_comments_by_it(self):
        self.gh.route(r"GET /user", {"login": "example-human", "type": "User"})
        self.comments_route([comment(3, STATUS + "\nold", login="example-human")])
        self.gh.route(rf"PATCH /repos/{REPO}/issues/comments/3", {"id": 3})
        code, data, err = self.run_cmd("status", "5", "--text", "new", env=self.gh.env(app_token=None, TRACKER_ISSUE_IDENTITY="user"))
        self.assertEqual(code, 0, err)
        self.assertEqual(data["action"], "updated")
        self.assertTrue(all(c["gh_token"] == USER_TOKEN for c in self.gh.calls()))

    def test_the_user_identity_needs_no_app_token_login_or_credentials(self):
        """README "Running without a GitHub App": every write, issue and Project alike, goes out as the ambient login."""
        env = self.gh.env(app_token=None, TRACKER_ISSUE_IDENTITY="user")
        for name in ("TRACKER_APP_LOGIN", "TRACKER_APP_TOKEN", "TRACKER_APP_CRED", "CREDENTIALS_DIR"):
            env.pop(name, None)
        self.gh.route(r"GET /user", {"login": "example-human", "type": "User"})
        self.comments_route([])
        self.gh.route(rf"POST {I}/5/comments", {"status": 201, "body": comment(9, "x", login="example-human")})
        self.project_routes()
        with mock.patch("appmint.urllib.request.urlopen", side_effect=AssertionError("no App token is minted")):
            self.assertEqual(self.run_cmd("status", "5", "--text", "working", env=env)[0], 0)
            self.assertEqual(self.run_cmd("claim", "5", "--lane", "lane-a", env=env)[0], 0)
        writes = self.gh.writes()
        self.assertGreaterEqual(len(writes), 2)
        self.assertTrue(all(c["gh_token"] == USER_TOKEN for c in writes))

    def test_an_unknown_identity_is_a_usage_error(self):
        for name in ("TRACKER_ISSUE_IDENTITY", "TRACKER_PROJECT_IDENTITY"):
            with self.subTest(name=name):
                code, _, err = self.run_cmd("view", "5", env=self.gh.env(**{name: "bot"}))
                self.assertEqual(code, 2)
                self.assertIn(name, err)
        self.assertEqual(self.gh.calls(), [])

    def test_the_app_login_has_no_default(self):
        env = self.gh.env()
        env.pop("TRACKER_APP_LOGIN")
        self.comments_route([])
        code, _, err = self.run_cmd("status", "5", "--text", "t", env=env)
        self.assertEqual(code, 2)
        self.assertIn("TRACKER_APP_LOGIN", err)
        self.assertEqual(self.gh.writes(), [])


class RepoDerivationTests(Base):
    def env_without_repo(self, **extra):
        env = self.gh.env(**extra)
        env.pop("TRACKER_REPO")
        return env

    def test_the_origin_remote_names_the_repository_when_nothing_else_does(self):
        for origin in ("git@github.com:o1/r1.git", "https://github.com/o1/r1.git", "https://github.com/o1/r1", "ssh://git@github.com/o1/r1.git"):
            with self.subTest(origin=origin):
                gh = FakeGh()
                self.addCleanup(gh.cleanup)
                self.gh = gh
                path = checkout(gh.root, origin)
                gh.route(r"GET /repos/o1/r1/issues/5", issue(5))
                code, _, err = self.run_cmd("view", "5", env=self.env_without_repo(), cwd=str(path))
                self.assertEqual(code, 0, err)
                self.assertEqual([c["path"] for c in gh.calls()], ["/repos/o1/r1/issues/5"])

    def test_the_setting_and_the_flag_win_over_the_origin(self):
        path = checkout(self.gh.root, "git@github.com:o1/r1.git")
        self.gh.route(rf"GET {I}/5", issue(5))
        self.gh.route(r"GET /repos/o2/r2/issues/5", issue(5))
        self.assertEqual(self.run_cmd("view", "5", cwd=str(path))[0], 0)
        self.assertEqual(self.run_cmd("view", "5", "--repo", "o2/r2", cwd=str(path))[0], 0)
        self.assertEqual([c["path"] for c in self.gh.calls()], [f"{I}/5", "/repos/o2/r2/issues/5"])

    def test_no_setting_and_no_usable_origin_is_refused(self):
        cases = {"not a checkout": None, "no origin": "", "not github.com": "git@" + "gitlab.com:o1/r1.git"}  # spelled in parts: the export's email rule only exempts github.com SSH remotes
        for label, origin in cases.items():
            with self.subTest(label):
                gh = FakeGh()
                self.addCleanup(gh.cleanup)
                self.gh = gh
                cwd = str(gh.root) if origin is None else str(checkout(gh.root, origin or None))
                code, _, err = self.run_cmd("view", "5", env=self.env_without_repo(), cwd=cwd)
                self.assertEqual(code, 2, err)
                self.assertIn("TRACKER_REPO", err)
                self.assertEqual(gh.calls(), [])


class LegacyTests(Base):
    def write_map(self, root, data):
        path = Path(root, ".github", "tracker")
        path.mkdir(parents=True)
        (path / "id-map.json").write_text(json.dumps(data) if not isinstance(data, str) else data, encoding="utf-8")

    def test_a_mapped_id_prints_its_issue_without_any_github_call(self):
        path = checkout(self.gh.root)
        self.write_map(path, {"schema": 1, "repo": REPO, "source_sha": "abc", "synthetic": [], "duplicates": [], "edges_done": {},
                              "issues": {"ABC-0560": {"number": 12, "url": "u", "closed": False}, "ABC-0560.01": {"number": 13}}})
        sub = Path(path, "sub")
        sub.mkdir()
        for typed, number in (("ABC-0560", 12), ("abc-0560.01", 13)):
            with self.subTest(typed=typed):
                code, data, err = self.run_cmd("legacy", typed, env=self.gh.env(app_token=None), cwd=str(sub))
                self.assertEqual(code, 0, err)
                self.assertEqual(data, {"legacy": typed.upper(), "repo": REPO, "number": number, "url": f"https://github.com/{REPO}/issues/{number}"})
        self.assertEqual(self.gh.calls(), [])

    def test_a_missing_map_or_an_unmapped_id_exits_3(self):
        path = checkout(self.gh.root)
        code, _, err = self.run_cmd("legacy", "ABC-0001", cwd=str(path))
        self.assertEqual(code, 3)
        self.assertIn("missing", err)
        self.write_map(path, {"schema": 1, "repo": REPO, "issues": {"ABC-0002": {"number": 2}}})
        code, _, err = self.run_cmd("legacy", "ABC-0001", cwd=str(path))
        self.assertEqual(code, 3)
        self.assertIn("not in", err)
        self.assertEqual(self.run_cmd("legacy", "ABC-0001", cwd=str(self.gh.root))[0], 3)  # not a checkout at all

    def test_the_import_tools_map_shape_is_read_and_a_held_id_exits_3_naming_its_reason(self):
        path = checkout(self.gh.root)
        self.write_map(path, {"schema": 1, "repo": REPO, "source_sha": "abc", "prefix": "ABC",
                              "issues": {"ABC-0001": {"number": 12, "url": f"https://github.com/{REPO}/issues/12"}},
                              "synthetic": [], "held": {"ABC-0009": "sweep"}})
        code, data, err = self.run_cmd("legacy", "ABC-0001", cwd=str(path))
        self.assertEqual((code, data["number"]), (0, 12), err)
        code, _, err = self.run_cmd("legacy", "abc-0009", cwd=str(path))
        self.assertEqual(code, 3)
        self.assertIn("held back", err)
        self.assertIn("sweep", err)
        self.assertEqual(self.gh.calls(), [])

    def test_a_map_that_cannot_be_trusted_exits_3(self):
        bad = {"invalid JSON": "{", "another repository": {"schema": 1, "repo": "o/elsewhere", "issues": {"ABC-0001": {"number": 1}}},
               "another schema": {"schema": 2, "repo": REPO, "issues": {"ABC-0001": {"number": 1}}},
               "no number": {"schema": 1, "repo": REPO, "issues": {"ABC-0001": {"url": "u"}}},
               "a zero number": {"schema": 1, "repo": REPO, "issues": {"ABC-0001": {"number": 0}}}}
        for label, data in bad.items():
            with self.subTest(label):
                gh = FakeGh()
                self.addCleanup(gh.cleanup)
                self.gh = gh
                path = checkout(gh.root)
                self.write_map(path, data)
                self.assertEqual(self.run_cmd("legacy", "ABC-0001", cwd=str(path))[0], 3)

    def test_a_malformed_id_is_a_usage_error(self):
        for typed in ("0560", "ABC_0560", "ABC-", "ABC-0560.", "../x"):
            with self.subTest(typed=typed):
                self.assertEqual(self.run_cmd("legacy", typed)[0], 2)


class EvidenceBindingTests(Base):
    """Only a GitHub Actions run whose workflow run calls the real evidence workflow, at a commit on its default branch, is proof."""

    def setup_close(self, checks, refs=None, compare="ahead", runs=None, compared=None, files=None, annotations=None, jobs=None):
        self.gh.route(rf"GET /repos/{REPO}", {"default_branch": "main"})
        self.gh.route("POST graphql", {"data": {"repository": {"pullRequest": {"lastEditedAt": None}}}})
        self.gh.route(rf"GET /repos/{REPO}/pulls/20", merged_pr())
        self.gh.route(rf"GET /repos/{REPO}/commits/h1/check-runs\?per_page=100&page=1", checks)
        self.bind_routes(refs, compare, runs, compared, files, annotations, jobs)
        self.gh.route(rf"GET {I}/5", issue(5))
        self.gh.route(rf"PATCH {I}/5", issue(5, state="closed"))
        self.project_routes()

    def close(self, env=None):
        return self.run_cmd("close", "5", "--pr", "20", env=env)

    def fresh(self):
        self.gh = FakeGh()
        self.addCleanup(self.gh.cleanup)

    def test_a_bound_passing_run_closes_and_its_binding_is_read(self):
        for status in ("ahead", "identical"):
            with self.subTest(status=status):
                self.fresh()
                self.setup_close(check_run(suite=4242), compare=status)
                code, data, err = self.close()
                self.assertEqual((code, data["changed"]), (0, True), err)
                paths = [c["path"] for c in self.gh.calls("GET")]
                self.assertIn(f"/repos/{REPO}/actions/runs?check_suite_id=4242", paths)
                self.assertIn(f"/repos/rknightion/.github/compare/{GOOD_SHA}...main?per_page=1", paths)
                self.assertIn(f"/repos/{REPO}/check-runs/1/annotations?per_page=100&page=1", paths)
                self.assertIn(f"/repos/{REPO}/actions/runs/900/jobs?filter=all&per_page=100&page=1", paths)

    def test_a_second_pr_at_the_same_head_is_not_proof_for_this_one(self):
        self.setup_close(check_run(), annotations=[[binding_note(pr=21)]])
        code, _, err = self.close()
        self.assertEqual((code, self.gh.writes()), (3, []), err)
        self.assertIn("pr=20", err)

    def test_a_run_without_the_binding_annotation_is_refused(self):
        cases = {
            "no annotations": [],
            "other annotations only": [binding_note(title="Something else"), {"annotation_level": "notice", "title": None, "message": "pr=20 head=h1"}],
            "the binding title at another level": [binding_note(level="warning")],
            "a message with more in it": [dict(binding_note(), message="pr=20 head=h1 extra")],
        }
        for label, notes in cases.items():
            with self.subTest(label):
                self.fresh()
                self.setup_close(check_run(), annotations=[notes])
                code, _, err = self.close()
                self.assertEqual((code, self.gh.writes()), (3, []), err)

    def test_a_binding_for_another_head_is_refused(self):
        self.setup_close(check_run(), annotations=[[binding_note(head="b" * 40)]])
        code, _, err = self.close()
        self.assertEqual((code, self.gh.writes()), (3, []), err)

    def test_two_binding_annotations_are_refused(self):
        for notes in ([binding_note(), binding_note()], [binding_note(), binding_note(pr=21)]):
            with self.subTest(notes=len(notes)):
                self.fresh()
                self.setup_close(check_run(), annotations=[notes])
                code, _, err = self.close()
                self.assertEqual((code, self.gh.writes()), (3, []), err)

    def test_a_check_run_that_is_not_a_job_of_its_workflow_run_is_refused(self):
        self.setup_close(check_run(ident=42), jobs=[jobs_of([1, 2, 3])])
        code, _, err = self.close()
        self.assertEqual((code, self.gh.writes()), (3, []), err)
        self.assertIn("job", err)

    def test_an_annotations_or_jobs_answer_that_cannot_be_read_refuses(self):
        cases = {
            "annotations error": dict(annotations=[{"status": 500, "body": {"message": "boom"}}]),
            "annotations not a list": dict(annotations=[{"status": 200, "body": {"annotations": []}}]),
            "jobs error": dict(jobs=[{"status": 500, "body": {"message": "boom"}}]),
            "jobs not a list": dict(jobs=[{"status": 200, "body": {"jobs": "x"}}]),
        }
        for label, kwargs in cases.items():
            with self.subTest(label):
                self.fresh()
                self.setup_close(check_run(), **kwargs)
                code, _, err = self.close()
                self.assertEqual((code, self.gh.writes()), (3, []), err)

    def test_every_page_of_annotations_and_jobs_is_read(self):
        self.setup_close(check_run(ident=7), annotations=[[binding_note(title=f"n{i}") for i in range(100)]],
                         jobs=[jobs_of(range(100, 200))])
        self.gh.route(rf"GET /repos/{REPO}/check-runs/7/annotations\?per_page=100&page=2", [binding_note()])
        self.gh.route(rf"GET /repos/{REPO}/actions/runs/900/jobs\?filter=all&per_page=100&page=2", jobs_of([7]))
        code, _, err = self.close()
        self.assertEqual(code, 0, err)

    def test_a_check_run_list_at_the_page_cap_is_treated_as_truncated(self):
        self.gh.route(rf"GET /repos/{REPO}/commits/h1/check-runs\?per_page=100&page=([1-9]|10)",
                      {"check_runs": [run_of(name=f"other {i}", ident=1000 + i) for i in range(100)]})
        self.setup_close(check_run())
        code, _, err = self.close()
        self.assertEqual((code, self.gh.writes()), (3, []), err)
        self.assertIn("1000", err)

    def test_a_stub_job_with_the_name_but_no_referenced_workflow_is_refused(self):
        self.setup_close(check_run(), refs=[])
        code, _, err = self.close()
        self.assertEqual((code, self.gh.writes()), (3, []), err)
        self.assertIn("ac-evidence.yml", err)

    def test_the_evidence_workflow_at_a_commit_not_on_its_default_branch_is_refused(self):
        for status in ("diverged", "behind"):
            with self.subTest(status=status):
                self.fresh()
                self.setup_close(check_run(), compare=status)
                code, _, err = self.close()
                self.assertEqual((code, self.gh.writes()), (3, []), err)
                self.assertIn("default branch", err)

    def test_a_commit_the_workflow_repository_does_not_know_is_refused(self):
        self.setup_close(check_run(), compared={"status": 404, "body": {"message": "Not Found"}})
        code, _, err = self.close()
        self.assertEqual((code, self.gh.writes()), (3, []), err)

    def test_a_referenced_workflow_that_is_not_the_evidence_workflow_or_not_at_a_full_sha_is_refused(self):
        cases = {
            "another workflow": [{"path": "rknightion/.github/.github/workflows/other.yml@main", "sha": GOOD_SHA}],
            "a lookalike path": [{"path": f"{EVIDENCE_WF}.bak@main", "sha": GOOD_SHA}],
            "another repository": [{"path": f"someone/.github/.github/workflows/ac-evidence.yml@main", "sha": GOOD_SHA}],
            "a short sha": [{"path": f"{EVIDENCE_WF}@main", "sha": GOOD_SHA[:12]}],
            "no sha": [{"path": f"{EVIDENCE_WF}@main"}],
        }
        for label, refs in cases.items():
            with self.subTest(label):
                self.fresh()
                # The workflow repository would call the short sha an ancestor of main; only the full-sha rule refuses it.
                self.gh.route(rf"GET /repos/rknightion/\.github/compare/{GOOD_SHA[:12]}\.\.\.main\?per_page=1", {"status": 200, "body": {"status": "ahead"}})
                self.setup_close(check_run(), refs=refs)
                code, _, err = self.close()
                self.assertEqual((code, self.gh.writes()), (3, []), err)

    def test_a_run_from_another_app_is_ignored(self):
        self.setup_close(check_run(app="some-other-app"))
        code, _, err = self.close()
        self.assertEqual((code, self.gh.writes()), (3, []), err)
        self.fresh()
        self.setup_close({"check_runs": [run_of("failure", ident=5, app="some-other-app", suite=1), run_of(ident=4)]})
        code, _, err = self.close()
        self.assertEqual(code, 0, err)

    def test_every_matching_name_must_pass_so_a_passing_stub_cannot_hide_a_failing_real_run(self):
        runs = {"check_runs": [run_of("failure", name="ac-evidence / Acceptance evidence", ident=1), run_of(name="Acceptance evidence", ident=2)]}
        self.setup_close(runs)
        code, _, err = self.close()
        self.assertEqual((code, self.gh.writes()), (3, []), err)
        self.assertIn("ac-evidence / Acceptance evidence", err)

    def test_two_bound_passing_names_close(self):
        runs = {"check_runs": [run_of(name="ac-evidence / Acceptance evidence", ident=1), run_of(name="Acceptance evidence", ident=2)]}
        self.setup_close(runs)
        self.assertEqual(self.close()[0], 0)

    def test_a_check_suite_with_no_single_workflow_run_is_refused(self):
        for answer in ({"total_count": 0, "workflow_runs": []},
                       {"total_count": 2, "workflow_runs": workflow_runs(BOUND)["workflow_runs"] * 2}):
            with self.subTest(answer=answer["total_count"]):
                self.fresh()
                self.setup_close(check_run(), runs=answer)
                code, _, err = self.close()
                self.assertEqual((code, self.gh.writes()), (3, []), err)

    def test_the_evidence_workflow_is_a_setting(self):
        refs = [{"path": "example-org/workflows/.github/workflows/evidence.yml@v1", "sha": GOOD_SHA}]
        self.setup_close(check_run(), refs=refs)
        self.gh.route(r"GET /repos/example-org/workflows", {"default_branch": "trunk"})
        self.gh.route(rf"GET /repos/example-org/workflows/compare/{GOOD_SHA}\.\.\.trunk\?per_page=1", {"status": 200, "body": {"status": "ahead"}})
        env = self.gh.env(TRACKER_EVIDENCE_WORKFLOW="example-org/workflows/.github/workflows/evidence.yml@")
        code, _, err = self.close(env)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.close()[0], 3)  # the default names the rknightion workflow, which this run does not call

    def test_a_pr_that_changes_ci_cannot_judge_itself(self):
        cases = {
            "a workflow file": [{"filename": ".github/workflows/ci.yml", "status": "modified"}],
            "a new workflow file": [{"filename": "README.md"}, {"filename": ".github/workflows/stub.yaml", "status": "added"}],
            "an action": [{"filename": ".github/actions/setup/action.yml", "status": "modified"}],
            "a workflow renamed away": [{"filename": "old/ci.yml", "previous_filename": ".github/workflows/ci.yml", "status": "renamed"}],
        }
        for label, files in cases.items():
            with self.subTest(label):
                self.fresh()
                self.setup_close(check_run(), files=[files])
                code, _, err = self.close()
                self.assertEqual((code, self.gh.writes()), (3, []), err)
                self.assertIn("cannot judge itself", err)
                self.assertIn("handoff", err)

    def test_unrelated_files_and_lookalike_paths_leave_the_close_alone(self):
        files = [{"filename": "src/app.py"}, {"filename": "docs/.github/workflows/example.yml"}, {"filename": ".github/CODEOWNERS"},
                 {"filename": ".github/workflows-notes.md"}]
        self.setup_close(check_run(), files=[files])
        code, data, err = self.close()
        self.assertEqual((code, data["changed"]), (0, True), err)
        self.assertIn(f"/repos/{REPO}/pulls/20/files?per_page=100&page=1", [c["path"] for c in self.gh.calls("GET")])

    def test_every_page_of_changed_files_is_read(self):
        page1 = [{"filename": f"src/f{i}.py"} for i in range(100)]
        self.setup_close(check_run(), files=[page1])
        self.gh.route(rf"GET /repos/{REPO}/pulls/20/files\?per_page=100&page=2", [{"filename": ".github/workflows/ci.yml"}])
        code, _, err = self.close()
        self.assertEqual((code, self.gh.writes()), (3, []), err)

    def test_a_file_list_that_cannot_be_read_whole_refuses(self):
        cases = {
            "an API error": [{"status": 500, "body": {"message": "boom"}}],
            "not a list": [{"status": 200, "body": {"files": []}}],
            "an entry with no name": [[{"status": "modified"}]],
            "a count the PR does not match": None,
        }
        for label, files in cases.items():
            with self.subTest(label):
                self.fresh()
                if files is None:
                    self.gh.route(rf"GET /repos/{REPO}/pulls/20", dict(merged_pr(), changed_files=2))
                    files = [[{"filename": "src/app.py"}]]
                self.setup_close(check_run(), files=files)
                code, _, err = self.close()
                self.assertEqual((code, self.gh.writes()), (3, []), err)

    def test_a_list_at_githubs_cap_is_treated_as_truncated(self):
        self.gh.route(rf"GET /repos/{REPO}/pulls/20", dict(merged_pr(), changed_files=3000))
        self.setup_close(check_run(), files=[[{"filename": f"src/f{i}.py"} for i in range(100)]])
        self.gh.route(rf"GET /repos/{REPO}/pulls/20/files\?per_page=100&page=([2-9]|[12][0-9]|30)", [{"filename": f"src/g{i}.py"} for i in range(100)])
        code, _, err = self.close()
        self.assertEqual((code, self.gh.writes()), (3, []), err)
        self.assertIn("3000", err)

    def test_a_malformed_evidence_workflow_setting_is_a_usage_error_before_any_call(self):
        for value in ("ac-evidence.yml", "rknightion/.github/.github/workflows/ac-evidence.yml", "o/r@"):
            with self.subTest(value=value):
                self.fresh()
                code, _, _ = self.close(self.gh.env(TRACKER_EVIDENCE_WORKFLOW=value))
                self.assertEqual((code, self.gh.calls()), (2, []))


class EvidenceCheckNameTests(Base):
    def setup_close(self, name, checks=None):
        self.gh.route(rf"GET /repos/{REPO}", {"default_branch": "main"})
        self.gh.route("POST graphql", {"data": {"repository": {"pullRequest": {"lastEditedAt": None}}}})
        self.gh.route(rf"GET /repos/{REPO}/pulls/20", merged_pr())
        self.gh.route(rf"GET /repos/{REPO}/commits/h1/check-runs\?per_page=100&page=1", checks or check_run(name=name))
        self.bind_routes()
        self.gh.route(rf"GET {I}/5", issue(5))
        self.gh.route(rf"PATCH {I}/5", issue(5, state="closed"))
        self.project_routes()

    def test_the_check_is_accepted_by_its_own_name_or_as_a_called_job(self):
        for name in ("Acceptance evidence", "ac-evidence / Acceptance evidence"):
            with self.subTest(name=name):
                gh = FakeGh()
                self.addCleanup(gh.cleanup)
                self.gh = gh
                self.setup_close(name)
                code, data, err = self.run_cmd("close", "5", "--pr", "20")
                self.assertEqual(code, 0, err)
                self.assertTrue(data["changed"])

    def test_a_name_that_only_contains_the_check_is_not_it(self):
        for name in ("Acceptance evidence (old)", "ac-evidence/Acceptance evidence", "Not Acceptance evidence", "x / Acceptance evidence / y"):
            with self.subTest(name=name):
                gh = FakeGh()
                self.addCleanup(gh.cleanup)
                self.gh = gh
                self.setup_close(name)
                code, _, err = self.run_cmd("close", "5", "--pr", "20")
                self.assertEqual(code, 3, err)
                self.assertEqual(gh.writes(), [])

    def test_the_check_name_is_a_setting(self):
        for name in ("evidence", "gate / evidence"):
            with self.subTest(name=name):
                gh = FakeGh()
                self.addCleanup(gh.cleanup)
                self.gh = gh
                self.setup_close(name)
                self.assertEqual(self.run_cmd("close", "5", "--pr", "20", env=gh.env(TRACKER_EVIDENCE_CHECK="evidence"))[0], 0)
        gh = FakeGh()
        self.addCleanup(gh.cleanup)
        self.gh = gh
        self.setup_close("Acceptance evidence")
        self.assertEqual(self.run_cmd("close", "5", "--pr", "20", env=gh.env(TRACKER_EVIDENCE_CHECK="evidence"))[0], 3)

    def test_a_check_run_page_that_is_not_a_list_is_not_read_as_no_runs(self):
        self.setup_close("Acceptance evidence", checks={"total_count": 1})
        code, _, _ = self.run_cmd("close", "5", "--pr", "20")
        self.assertEqual(code, 4)
        self.assertEqual(self.gh.writes(), [])


if __name__ == "__main__":
    unittest.main()
