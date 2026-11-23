"""The tracker shim's commands, run in-process against a fake gh."""
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

import io
import json
from pathlib import Path
import subprocess
import unittest

import tracker
from harness import REPO, FakeGh, comment, issue

I = f"/repos/{REPO}/issues"
L = f"/repos/{REPO}/labels"
SECRET = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"  # a token shape the shipped identifier patterns refuse


class Base(unittest.TestCase):
    def setUp(self):
        self.gh = FakeGh()
        self.addCleanup(self.gh.cleanup)
        # Every working label already exists unless a test says otherwise.
        self.gh.route(rf"GET {L}/.*", {"name": "x"})

    def run_cmd(self, *argv, stdin="", env=None, cwd=None):
        out, err = io.StringIO(), io.StringIO()
        code = tracker.main(list(argv), env=env or self.gh.env(), stdin=io.StringIO(stdin), out=out, err=err,
                            cwd=cwd or str(self.gh.root))
        text = out.getvalue()
        try:
            data = json.loads(text) if text.strip() else None
        except ValueError:
            data = text
        return code, data, err.getvalue()

    def write_paths(self):
        return [(c["method"], c["path"]) for c in self.gh.writes()]


class NoAppNoProject(Base):
    def test_no_call_carries_an_app_token_or_reaches_a_project(self):
        self.gh.route(rf"GET {I}/5", issue(5, labels=["ready"]))
        self.gh.route(rf"DELETE {I}/5/labels/ready", [])
        env = self.gh.env(TRACKER_APP_TOKEN="app-token-fixture", TRACKER_PROJECT="7", TRACKER_PROJECT_OWNER="example-org",
                          TRACKER_ISSUE_IDENTITY="app", TRACKER_PROJECT_IDENTITY="user")
        code, data, err = self.run_cmd("claim", "5", "--lane", "L1", "--project", "7", env=env)
        self.assertEqual(code, 0, err)
        for call in self.gh.calls():
            self.assertNotIn("projectsV2", call["path"])
            self.assertNotEqual(call["gh_token"], "app-token-fixture")
            self.assertNotIn("app-token-fixture", json.dumps(call["argv"]))

    def test_the_app_and_project_modules_are_gone(self):
        self.assertFalse((_TOOL / "ghclient.py").exists())
        source = (_TOOL / "tracker.py").read_text(encoding="utf-8")
        for word in ("ghclient", "appmint", "projectsV2", "TRACKER_APP_TOKEN"):
            self.assertNotIn(word, source)


class Queue(Base):
    def test_queue_lists_open_issues_labelled_ready_and_skips_pull_requests(self):
        self.gh.route(rf"GET {I}\?state=open&labels=ready&per_page=100&page=1",
                      [issue(3, "Three", labels=["ready", "p1"]), issue(4, "A PR", labels=["ready"], pull_request={})])
        code, data, err = self.run_cmd("queue")
        self.assertEqual(code, 0, err)
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["truncated"], False)
        row = data["items"][0]
        self.assertEqual((row["repo"], row["number"], row["title"], row["priority"]), (REPO, 3, "Three", "p1"))
        self.assertEqual(self.gh.writes(), [])

    def test_queue_adds_each_label_and_reads_every_page(self):
        first = [issue(n, labels=["ready", "docs"]) for n in range(1, 101)]
        self.gh.route(rf"GET {I}\?state=open&labels=ready%2Cdocs&per_page=100&page=1", first)
        self.gh.route(rf"GET {I}\?state=open&labels=ready%2Cdocs&per_page=100&page=2", [issue(101, labels=["ready"])])
        code, data, err = self.run_cmd("queue", "--label", "docs")
        self.assertEqual(code, 0, err)
        self.assertEqual(data["count"], 101)

    def test_limit_marks_a_cut_short_listing(self):
        self.gh.route(rf"GET {I}\?state=open&labels=ready&per_page=100&page=1", [issue(1), issue(2), issue(3)])
        code, data, _ = self.run_cmd("queue", "--limit", "2")
        self.assertEqual((code, data["count"], data["truncated"]), (0, 2, True))
        code, data, _ = self.run_cmd("queue", "--limit", "3")
        self.assertEqual((data["count"], data["truncated"]), (3, False))

    def test_status_names_map_to_their_label(self):
        self.gh.route(rf"GET {I}\?state=open&labels=needs-owner&per_page=100&page=1", [issue(8)])
        code, data, err = self.run_cmd("queue", "--status", "Needs input")
        self.assertEqual((code, data["count"]), (0, 1), err)

    def test_all_repos_searches_the_owner(self):
        self.gh.route(r"GET /users/example-org", {"type": "Organization"})
        found = issue(9)
        found["html_url"] = "https://github.com/example-org/other/issues/9"
        self.gh.route(r"GET /search/issues\?q=is%3Aissue%20is%3Aopen%20org%3Aexample-org%20label%3A%22ready%22&per_page=100&page=1",
                      {"total_count": 1, "items": [found]})
        code, data, err = self.run_cmd("queue", "--all-repos")
        self.assertEqual(code, 0, err)
        self.assertEqual((data["items"][0]["repo"], data["items"][0]["number"]), ("example-org/other", 9))

    def test_a_listing_that_is_not_a_list_is_an_error(self):
        self.gh.route(rf"GET {I}\?.*", {"message": "odd"})
        code, data, err = self.run_cmd("queue")
        self.assertEqual(code, 4)
        self.assertIn("not a list", err)


class View(Base):
    def test_view_returns_the_issue_and_the_last_human_comments(self):
        self.gh.route(rf"GET {I}/5", issue(5, "Five", labels=["ready"], body="b"))
        self.gh.route(rf"GET {I}/5/comments\?per_page=100&page=1",
                      [comment(1, "first"), comment(2, "bot", login="ci[bot]", kind="Bot"), comment(3, "second")])
        code, data, err = self.run_cmd("view", "5", "--comments", "1")
        self.assertEqual(code, 0, err)
        self.assertEqual((data["number"], data["title"], data["labels"], data["body"]), (5, "Five", ["ready"], "b"))
        self.assertEqual([c["body"] for c in data["comments"]], ["second"])

    def test_edges(self):
        self.gh.route(rf"GET {I}/5", issue(5))
        self.gh.route(rf"GET {I}/5/dependencies/blocked_by\?per_page=100", [issue(2)])
        self.gh.route(rf"GET {I}/5/sub_issues\?per_page=100", [])
        code, data, err = self.run_cmd("view", "5", "--edges")
        self.assertEqual(code, 0, err)
        self.assertEqual(([b["number"] for b in data["blocked_by"]], data["sub_issues"]), ([2], []))


class Create(Base):
    def setUp(self):
        super().setUp()
        self.body = Path(self.gh.root, "body.md")
        self.body.write_text("## Acceptance criteria\n\n- [ ] AC1 works\n", encoding="utf-8")

    def test_create_posts_one_issue_with_its_labels_and_no_project(self):
        self.gh.route(rf"POST {I}", {"status": 201, "body": issue(12)})
        code, data, err = self.run_cmd("create", "--title", "New", "--body-file", str(self.body), "--label", "docs",
                                       "--status", "Ready", "--priority", "high", "--project", "7")
        self.assertEqual(code, 0, err)
        self.assertEqual(data["number"], 12)
        (post,) = [c for c in self.gh.writes() if c["path"] == I]
        self.assertEqual(post["body"]["labels"], ["docs", "ready", "p1"])
        self.assertEqual(post["body"]["title"], "New")
        self.assertEqual(self.write_paths(), [("POST", I)])

    def test_field_status_and_priority_become_labels_and_other_fields_are_noted(self):
        self.gh.route(rf"POST {I}", {"status": 201, "body": issue(12)})
        code, _, err = self.run_cmd("create", "--title", "New", "--body-stdin", "--field", "Status=Ready", "--field", "Priority=p2",
                                    "--field", "Lane=x", stdin="body")
        self.assertEqual(code, 0, err)
        self.assertIn("--field Lane is ignored", err)
        (post,) = self.gh.writes()
        self.assertEqual(post["body"]["labels"], ["ready", "p2"])

    def test_a_missing_working_label_is_created_first(self):
        self.gh.routes.clear()
        self.gh.route(rf"GET {L}/ready", {"status": 404, "body": {"message": "Not Found"}})
        self.gh.route(rf"POST {L}", {"status": 201, "body": {"name": "ready"}})
        self.gh.route(rf"POST {I}", {"status": 201, "body": issue(12)})
        code, _, err = self.run_cmd("create", "--title", "New", "--body-stdin", "--status", "ready", stdin="body")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.write_paths(), [("POST", L), ("POST", I)])

    def test_parent_and_blockers_are_linked(self):
        self.gh.route(rf"POST {I}", {"status": 201, "body": issue(12)})
        self.gh.route(rf"POST {I}/3/sub_issues", {"status": 201, "body": {}})
        self.gh.route(rf"GET {I}/4", issue(4))
        self.gh.route(rf"POST {I}/12/dependencies/blocked_by", {"status": 201, "body": {}})
        code, _, err = self.run_cmd("create", "--title", "New", "--body-stdin", "--parent", "3", "--blocked-by", "4", stdin="b")
        self.assertEqual(code, 0, err)
        self.assertEqual([c["body"] for c in self.gh.writes()[1:]], [{"sub_issue_id": 1012}, {"issue_id": 1004}])

    def test_a_failed_link_still_names_the_new_issue(self):
        self.gh.route(rf"POST {I}", {"status": 201, "body": issue(12)})
        self.gh.route(rf"POST {I}/3/sub_issues", {"status": 422, "body": {"message": "nope"}})
        code, data, _ = self.run_cmd("create", "--title", "New", "--body-stdin", "--parent", "3", stdin="b")
        self.assertEqual((code, data["number"], data["failed"]), (4, 12, "parent"))

    def test_a_secret_in_the_body_is_refused_before_any_write(self):
        code, _, err = self.run_cmd("create", "--title", "New", "--body-stdin", stdin=f"token {SECRET}")
        self.assertEqual(code, 3)
        self.assertNotIn(SECRET, err)
        self.assertEqual(self.gh.writes(), [])

    def test_a_bad_priority_is_a_usage_error(self):
        code, _, _ = self.run_cmd("create", "--title", "New", "--body-stdin", "--priority", "whenever", stdin="b")
        self.assertEqual(code, 2)
        self.assertEqual(self.gh.calls(), [])


class Labels(Base):
    def test_claim_takes_the_issue_out_of_the_queue(self):
        self.gh.route(rf"GET {I}/5", issue(5, labels=["ready", "p2"]))
        self.gh.route(rf"DELETE {I}/5/labels/ready", [])
        code, data, err = self.run_cmd("claim", "5", "--lane", "L1", "--force")
        self.assertEqual(code, 0, err)
        self.assertEqual((data["lane"], data["removed"]), ("L1", ["ready"]))
        self.assertEqual(self.write_paths(), [("DELETE", f"{I}/5/labels/ready")])

    def test_claim_of_an_issue_not_in_the_queue_writes_nothing(self):
        self.gh.route(rf"GET {I}/5", issue(5, labels=[]))
        code, _, err = self.run_cmd("claim", "5", "--lane", "L1")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.gh.writes(), [])

    def test_status_with_a_state_sets_that_label_and_drops_the_other_two(self):
        self.gh.route(rf"GET {I}/5", issue(5, labels=["ready", "blocked"]))
        self.gh.route(rf"POST {I}/5/labels", [])
        self.gh.route(rf"DELETE {I}/5/labels/.*", [])
        code, data, err = self.run_cmd("status", "5", "needs-owner")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.write_paths(), [("POST", f"{I}/5/labels"), ("DELETE", f"{I}/5/labels/ready"),
                                              ("DELETE", f"{I}/5/labels/blocked")])
        self.assertEqual(self.gh.writes()[0]["body"], {"labels": ["needs-owner"]})

    def test_status_text_that_is_a_state_word_sets_the_label(self):
        self.gh.route(rf"GET {I}/5", issue(5, labels=[]))
        self.gh.route(rf"POST {I}/5/labels", [])
        code, _, err = self.run_cmd("status", "5", "--text", "Blocked")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.gh.writes()[0]["body"], {"labels": ["blocked"]})

    def test_status_text_is_posted_as_a_comment(self):
        self.gh.route(rf"GET {I}/5", issue(5))
        self.gh.route(rf"POST {I}/5/comments", {"status": 201, "body": {"html_url": "u"}})
        note = Path(self.gh.root, "note.md")
        note.write_text("halfway there", encoding="utf-8")
        code, data, err = self.run_cmd("status", "5", "--file", str(note))
        self.assertEqual(code, 0, err)
        self.assertEqual(data["comment"], "u")
        self.assertEqual(self.gh.writes()[0]["body"], {"body": "halfway there"})

    def test_status_needs_something_to_do_and_a_known_state(self):
        self.assertEqual(self.run_cmd("status", "5")[0], 2)
        self.assertEqual(self.run_cmd("status", "5", "someday")[0], 2)
        self.assertEqual(self.gh.calls(), [])

    def test_handoff_sets_needs_owner_and_posts_the_four_parts(self):
        self.gh.route(rf"GET {I}/5", issue(5, labels=["ready"]))
        self.gh.route(rf"POST {I}/5/labels", [])
        self.gh.route(rf"DELETE {I}/5/labels/ready", [])
        self.gh.route(rf"POST {I}/5/comments", {"status": 201, "body": {"html_url": "c"}})
        code, data, err = self.run_cmd("handoff", "5", "--scope", "s", "--verified", "v", "--blockers", "b", "--next", "n",
                                       "--status", "Needs input")
        self.assertEqual(code, 0, err)
        self.assertEqual(data["added"], ["needs-owner"])
        text = self.gh.writes()[-1]["body"]["body"]
        for part in ("**Scope:** s", "**Verified:** v", "**Blockers:** b", "**Next:** n"):
            self.assertIn(part, text)

    def test_priority_sets_one_p_label(self):
        self.gh.route(rf"GET {I}/5", issue(5, labels=["p3", "priority:low"]))
        self.gh.route(rf"POST {I}/5/labels", [])
        self.gh.route(rf"DELETE {I}/5/labels/p3", [])
        code, data, err = self.run_cmd("priority", "5", "high")
        self.assertEqual(code, 0, err)
        self.assertEqual((data["priority"], data["added"], data["removed"]), ("p1", ["p1"], ["p3"]))


class Tick(Base):
    BODY = "Intro\n\n## Acceptance criteria\n\n- [ ] AC1 first\n- [ ] AC2 second\n\n## Notes\n- [ ] AC2 not in the section\n"

    def test_tick_edits_one_box_in_the_criteria_section(self):
        ticked = self.BODY.replace("- [ ] AC2 second", "- [x] AC2 second")
        self.gh.route(rf"GET {I}/5", issue(5, body=self.BODY))
        self.gh.route(rf"PATCH {I}/5", issue(5, body=ticked))
        code, data, err = self.run_cmd("tick", "5", "AC2")
        self.assertEqual(code, 0, err)
        self.assertEqual(data["changed"], True)
        self.assertEqual(self.gh.writes()[0]["body"], {"body": ticked})

    def test_an_already_ticked_box_writes_nothing(self):
        self.gh.route(rf"GET {I}/5", issue(5, body=self.BODY.replace("[ ] AC1", "[x] AC1")))
        code, data, _ = self.run_cmd("tick", "5", "1")
        self.assertEqual((code, data["changed"], self.gh.writes()), (0, False, []))

    def test_a_missing_criterion_is_refused(self):
        self.gh.route(rf"GET {I}/5", issue(5, body=self.BODY))
        self.assertEqual(self.run_cmd("tick", "5", "AC9")[0], 3)

    def test_a_body_edited_since_it_was_read_is_not_overwritten(self):
        self.gh.route(rf"GET {I}/5", issue(5, body=self.BODY), issue(5, body=self.BODY + "\nedited meanwhile\n"))
        self.gh.route(rf"PATCH {I}/5", issue(5, body=self.BODY))
        code, _, err = self.run_cmd("tick", "5", "AC1")
        self.assertEqual(code, 3, err)
        self.assertIn("changed while ticking", err)
        self.assertEqual(self.gh.writes(), [])

    def test_a_body_that_does_not_read_back_ticked_fails(self):
        self.gh.route(rf"GET {I}/5", issue(5, body=self.BODY))
        self.gh.route(rf"PATCH {I}/5", issue(5, body=self.BODY))
        self.assertEqual(self.run_cmd("tick", "5", "AC1")[0], 4)


class Close(Base):
    BODY = "## Acceptance criteria\n\n- [x] AC1 done\n- [ ] AC2 not done\n"

    def routes(self):
        self.gh.route(rf"GET {I}/5", issue(5, body=self.BODY))
        self.gh.route(rf"POST {I}/5/comments", {"status": 201, "body": {"html_url": "c"}})
        self.gh.route(rf"PATCH {I}/5", issue(5, state="closed"))

    def test_close_posts_the_evidence_then_closes(self):
        self.routes()
        code, data, err = self.run_cmd("close", "5", "--pr", "12", "--why", "Merged in PR 12; the gate passed.")
        self.assertEqual(code, 0, err)
        comment_call, close_call = self.gh.writes()
        text = comment_call["body"]["body"]
        self.assertIn("Pull request: #12", text)
        self.assertIn("Merged in PR 12; the gate passed.", text)
        self.assertIn("Acceptance criteria left unticked: AC2", text)
        self.assertEqual(close_call["body"], {"state": "closed", "state_reason": "completed"})
        self.assertEqual((data["state"], data["unticked"]), ("closed", ["AC2"]))

    def test_close_needs_no_merged_pull_request_and_reads_no_pull_request(self):
        self.routes()
        code, _, err = self.run_cmd("close", "5", "--reason", "not-planned", "--why", "Superseded by the label model.")
        self.assertEqual(code, 0, err)
        self.assertFalse([c for c in self.gh.calls() if "/pulls" in c["path"] or "check-runs" in c["path"]])
        self.assertEqual(self.gh.writes()[-1]["body"]["state_reason"], "not_planned")

    def test_close_reads_the_evidence_from_a_file(self):
        self.routes()
        evidence = Path(self.gh.root, "evidence.md")
        evidence.write_text("commit abc1234 on main", encoding="utf-8")
        code, _, err = self.run_cmd("close", "5", "--evidence-file", str(evidence))
        self.assertEqual(code, 0, err)
        self.assertIn("commit abc1234 on main", self.gh.writes()[0]["body"]["body"])

    def test_close_with_only_a_pull_request_names_it_as_the_evidence(self):
        self.routes()
        code, data, err = self.run_cmd("close", "5", "--pr", "12")
        self.assertEqual(code, 0, err)
        text = self.gh.writes()[0]["body"]["body"]
        self.assertIn("Closed by pull request #12.", text)
        self.assertEqual((data["state"], self.gh.writes()[1]["body"]["state"]), ("closed", "closed"))

    def test_close_refuses_empty_evidence_before_any_call(self):
        for argv in (["close", "5"], ["close", "5", "--why", "   "]):
            with self.subTest(argv=argv):
                code, data, err = self.run_cmd(*argv)
                self.assertEqual((code, data), (2, None))
                self.assertIn("evidence", err)
        self.assertEqual(self.gh.calls(), [])

    def test_a_closed_issue_is_left_alone(self):
        self.gh.route(rf"GET {I}/5", issue(5, state="closed"))
        code, data, _ = self.run_cmd("close", "5", "--why", "done")
        self.assertEqual((code, data["changed"], self.gh.writes()), (0, False, []))

    def test_a_secret_in_the_evidence_is_refused(self):
        self.gh.route(rf"GET {I}/5", issue(5))
        code, _, err = self.run_cmd("close", "5", "--why", f"see {SECRET}")
        self.assertEqual(code, 3)
        self.assertNotIn(SECRET, err)
        self.assertEqual(self.gh.writes(), [])


class PullRequests(Base):
    """Issues and pull requests share numbers: a writing verb aimed at a pull request writes nothing."""

    def test_every_writing_verb_refuses_a_pull_request_before_any_write(self):
        self.gh.route(rf"GET {I}/7", issue(7, labels=["ready"], body="## Acceptance criteria\n\n- [ ] AC1 x\n",
                                           pull_request={"url": "u"}))
        self.gh.route(r"(POST|PATCH|DELETE|PUT) .*", {"status": 201, "body": {}})
        for argv in (["close", "7", "--pr", "12"], ["close", "7", "--why", "done"], ["tick", "7", "AC1"],
                     ["status", "7", "blocked"], ["status", "7", "--text", "a note"], ["claim", "7", "--lane", "L1"],
                     ["handoff", "7", "--scope", "s", "--verified", "v", "--blockers", "b", "--next", "n"],
                     ["priority", "7", "p1"]):
            with self.subTest(argv=argv):
                code, _, err = self.run_cmd(*argv)
                self.assertEqual(code, 3, err)
                self.assertIn("#7 is a pull request", err)
        self.assertEqual(self.gh.writes(), [])


class OldCommandLines(Base):
    """The command lines the vended recipes and the loop protocol already use keep parsing and working."""

    def test_close_with_a_reason_and_why(self):
        self.gh.route(rf"GET {I}/5", issue(5))
        self.gh.route(rf"POST {I}/5/comments", {"status": 201, "body": {"html_url": "c"}})
        self.gh.route(rf"PATCH {I}/5", issue(5, state="closed"))
        code, _, err = self.run_cmd("close", "5", "--reason", "not-planned", "--why", "Superseded.")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.gh.writes()[-1]["body"], {"state": "closed", "state_reason": "not_planned"})

    def test_handoff_with_needs_input(self):
        self.gh.route(rf"GET {I}/5", issue(5, labels=["ready"]))
        self.gh.route(rf"POST {I}/5/labels", [])
        self.gh.route(rf"DELETE {I}/5/labels/ready", [])
        self.gh.route(rf"POST {I}/5/comments", {"status": 201, "body": {"html_url": "c"}})
        code, data, err = self.run_cmd("handoff", "5", "--scope", "s", "--verified", "v", "--blockers", "b", "--next", "n",
                                       "--status", "Needs input")
        self.assertEqual((code, data["added"], data["removed"]), (0, ["needs-owner"], ["ready"]), err)

    def test_a_leading_project_option_and_claim_force(self):
        self.gh.route(rf"GET {I}/5", issue(5, labels=["ready"]))
        self.gh.route(rf"DELETE {I}/5/labels/ready", [])
        code, data, err = self.run_cmd("--project", "2", "claim", "5", "--lane", "L1", "--force")
        self.assertEqual((code, data["removed"]), (0, ["ready"]), err)

    def test_status_with_text_alone(self):
        self.gh.route(rf"GET {I}/5", issue(5))
        self.gh.route(rf"POST {I}/5/comments", {"status": 201, "body": {"html_url": "u"}})
        code, data, err = self.run_cmd("status", "5", "--text", "Lane L1 is on it; gate green locally.")
        self.assertEqual((code, data["comment"]), (0, "u"), err)
        self.assertEqual(self.write_paths(), [("POST", f"{I}/5/comments")])


class Retired(Base):
    def test_priority_check_succeeds_with_a_note_and_no_call(self):
        code, data, err = self.run_cmd("priority-check")
        self.assertEqual((code, data), (0, {"retired": "priority-check"}))
        self.assertEqual(len(err.strip().splitlines()), 1)
        self.assertIn("no longer does anything", err)
        self.assertEqual(self.gh.calls(), [])


class Settings(Base):
    def test_the_repository_comes_from_the_origin_remote_when_unset(self):
        checkout = Path(self.gh.root, "checkout")
        checkout.mkdir()
        subprocess.run(["git", "init", "-q", str(checkout)], check=True)
        subprocess.run(["git", "-C", str(checkout), "remote", "add", "origin", "git@github.com:other-owner/other-repo.git"], check=True)
        self.gh.route(r"GET /repos/other-owner/other-repo/issues/5", issue(5))
        env = self.gh.env()
        env.pop("TRACKER_REPO")
        code, _, err = self.run_cmd("view", "5", env=env, cwd=str(checkout))
        self.assertEqual(code, 0, err)

    def test_a_bad_command_line_is_exit_2(self):
        self.assertEqual(self.run_cmd("nonsense")[0], 2)
        self.assertEqual(self.run_cmd("view", "5", "--repo", "not-a-repo")[0], 2)

    def test_a_gh_failure_is_exit_4_with_its_message(self):
        self.gh.route(rf"GET {I}/5", {"status": 500, "body": {"message": "boom"}})
        code, _, err = self.run_cmd("view", "5")
        self.assertEqual(code, 4)
        self.assertIn("boom", err)


class Legacy(Base):
    def test_legacy_reads_the_committed_map_without_calling_github(self):
        checkout = Path(self.gh.root, "checkout")
        (checkout / ".github/tracker").mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(checkout)], check=True)
        (checkout / ".github/tracker/id-map.json").write_text(json.dumps({"schema": 1, "repo": REPO, "issues": {"ABC-0001": {"number": 7}}}))
        code, data, err = self.run_cmd("legacy", "abc-0001", cwd=str(checkout))
        self.assertEqual(code, 0, err)
        self.assertEqual(data["number"], 7)
        self.assertEqual(self.gh.calls(), [])


if __name__ == "__main__":
    unittest.main()
