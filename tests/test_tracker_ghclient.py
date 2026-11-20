"""The write budget, retry rules, audit log and credential handling of the gh client."""
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

import fcntl
import json
import os
import time
import unittest
from unittest import mock

import ghclient
import textsafe
from harness import APP_TOKEN, USER_TOKEN, FakeClock, FakeGh


def limiter(tmp, clock, **kw):
    return ghclient.Limiter(tmp, clock=clock.time, sleep=clock.sleep, **kw)


class NextCursorTests(unittest.TestCase):
    def cursor(self, link):
        return ghclient.next_cursor(ghclient.Response(200, {"link": link} if link is not None else {}, []))

    def test_the_cursor_is_the_after_of_the_next_link(self):
        base = "https://api.github.com/orgs/o/projectsV2/7/items?q=is%3Aissue+repo%3Ao%2Fr&per_page=100"
        self.assertEqual(self.cursor(f'<{base}&after=abc123>; rel="next"'), "abc123")
        self.assertEqual(self.cursor(f'<{base}&before=zzz>; rel="prev", <{base}&after=a%3Db>; rel="next", <{base}>; rel="first"'), "a=b")

    def test_no_next_link_is_the_last_page(self):
        self.assertIsNone(self.cursor(None))
        self.assertIsNone(self.cursor(""))
        self.assertIsNone(self.cursor('<https://api.github.com/x?before=zzz>; rel="prev"'))

    def test_a_next_link_with_no_cursor_is_an_error_not_the_last_page(self):
        for link in ('<https://api.github.com/x?per_page=100>; rel="next"', '<https://api.github.com/x?after=>; rel="next"'):
            with self.subTest(link=link), self.assertRaises(ghclient.GhError):
                self.cursor(link)


class LimiterTests(unittest.TestCase):
    def setUp(self):
        self.gh = FakeGh()
        self.addCleanup(self.gh.cleanup)
        self.clock = FakeClock()

    def test_two_writes_are_at_least_1_2_seconds_apart(self):
        lim = limiter(self.gh.state, self.clock)
        lim.acquire("app")
        lim.acquire("app")
        self.assertEqual(len(self.clock.sleeps), 1)
        self.assertAlmostEqual(self.clock.sleeps[0], 1.2)

    def test_each_identity_has_its_own_bucket(self):
        lim = limiter(self.gh.state, self.clock)
        lim.acquire("app")
        lim.acquire("user")
        self.assertEqual(self.clock.sleeps, [])

    def test_the_bucket_survives_a_new_process(self):
        limiter(self.gh.state, self.clock).acquire("app")
        limiter(self.gh.state, self.clock).acquire("app")
        self.assertAlmostEqual(self.clock.sleeps[0], 1.2)

    def test_a_read_neither_waits_nor_spends_the_budget(self):
        lim = limiter(self.gh.state, self.clock)
        lim.acquire("app")
        lim.acquire("app", write=False)
        self.assertEqual(self.clock.sleeps, [])

    def test_the_per_minute_cap_waits_for_the_oldest_write_to_age_out(self):
        lim = limiter(self.gh.state, self.clock, min_gap=0, per_minute=3)
        start = self.clock.now
        for _ in range(4):
            lim.acquire("app")
        self.assertAlmostEqual(self.clock.now - start, 60)

    def test_the_per_hour_cap_refuses_when_the_wait_exceeds_max_wait(self):
        lim = limiter(self.gh.state, self.clock, min_gap=0, per_hour=2, max_wait=600)
        lim.acquire("app")
        lim.acquire("app")
        with self.assertRaises(ghclient.RateLimited):
            lim.acquire("app")
        self.assertEqual(self.clock.sleeps, [])

    def test_the_per_hour_cap_waits_when_the_wait_is_within_max_wait(self):
        lim = limiter(self.gh.state, self.clock, min_gap=0, per_hour=2, max_wait=7200)
        start = self.clock.now
        for _ in range(3):
            lim.acquire("app")
        self.assertAlmostEqual(self.clock.now - start, 3600)

    def test_a_recorded_block_holds_every_later_process(self):
        limiter(self.gh.state, self.clock).block("app", self.clock.now + 30)
        limiter(self.gh.state, self.clock).acquire("app", write=False)
        self.assertAlmostEqual(self.clock.sleeps[0], 30)

    def test_a_missing_state_file_starts_a_fresh_bucket_but_a_damaged_one_refuses(self):
        limiter(self.gh.state, self.clock).acquire("app")
        path = self.gh.state / "ratelimit.json"
        for damage in ("{truncated", "[]", ""):
            with self.subTest(damage=damage):
                path.write_text(damage)
                with self.assertRaises(ghclient.StateError):
                    limiter(self.gh.state, self.clock).acquire("app")
                self.assertEqual(path.read_text(), damage)
        path.unlink()
        path.mkdir()
        with self.assertRaises(ghclient.StateError):
            limiter(self.gh.state, self.clock).acquire("app")
        path.rmdir()
        limiter(self.gh.state, self.clock).acquire("app")

    def test_a_held_rate_limit_lock_is_waited_for_only_as_long_as_max_wait(self):
        (self.gh.state / "ratelimit.lock").touch()
        with open(self.gh.state / "ratelimit.lock", "a") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            started = time.monotonic()
            with self.assertRaises(ghclient.RateLimited):
                limiter(self.gh.state, self.clock, max_wait=0.3).acquire("app")
            self.assertLess(time.monotonic() - started, 5)
        limiter(self.gh.state, self.clock, max_wait=0.3).acquire("app")

    def test_the_wait_is_bounded_in_total_not_per_sleep(self):
        path = self.gh.state / "ratelimit.json"
        rounds = []

        def competing_sleep(seconds):
            self.clock.sleep(seconds)
            rounds.append(seconds)
            if len(rounds) > 50:
                raise AssertionError("still waiting after 50 rounds: the wait is not bounded in total")
            state = json.loads(path.read_text())
            state["app"]["last"] = self.clock.now  # another process just took the slot
            path.write_text(json.dumps(state))

        lim = ghclient.Limiter(self.gh.state, clock=self.clock.time, sleep=competing_sleep, min_gap=4, max_wait=10)
        lim.acquire("app")
        start = self.clock.now
        with self.assertRaises(ghclient.RateLimited):
            lim.acquire("app")
        self.assertLessEqual(self.clock.now - start, 10)

    def test_defaults_are_the_documented_budget(self):
        lim = limiter(self.gh.state, self.clock)
        self.assertEqual((lim.min_gap, lim.per_minute, lim.per_hour), (1.2, 80, 450))


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.gh = FakeGh()
        self.addCleanup(self.gh.cleanup)
        self.clock = FakeClock()
        self.env = self.gh.env()
        patcher = mock.patch.dict(os.environ, self.env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = ghclient.Client(self.env, clock=self.clock.time, sleep=self.clock.sleep)

    def audit(self):
        path = self.gh.state / "audit.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_app_writes_use_the_app_token_and_reads_use_the_ambient_login(self):
        self.gh.route(r"GET /repos/o/r/issues/1", {"number": 1})
        self.gh.route(r"PATCH /repos/o/r/issues/1", {"number": 1})
        self.client.get("/repos/o/r/issues/1")
        self.client.write("PATCH", "/repos/o/r/issues/1", {"title": "t"}, identity="app", issue=1)
        read, write = self.gh.calls()
        self.assertEqual(read["gh_token"], USER_TOKEN)
        self.assertEqual(write["gh_token"], APP_TOKEN)
        self.assertIsNone(read["tracker_app_token"])
        self.assertIsNone(write["tracker_app_token"])

    def test_a_token_never_appears_in_argv_or_the_audit_log(self):
        self.gh.route(r"POST /repos/o/r/issues", {"number": 1})
        self.client.write("POST", "/repos/o/r/issues", {"title": "t", "body": "b"}, identity="app")
        for call in self.gh.calls():
            self.assertNotIn(APP_TOKEN, json.dumps(call["argv"]))
            self.assertNotIn(USER_TOKEN, json.dumps(call["argv"]))
        self.assertNotIn(APP_TOKEN, (self.gh.state / "audit.jsonl").read_text())

    def test_an_app_write_without_the_app_token_is_refused_before_any_call(self):
        env = self.gh.env(app_token=None)
        client = ghclient.Client(env, clock=self.clock.time, sleep=self.clock.sleep)
        with self.assertRaises(ghclient.Unconfigured):
            client.write("PATCH", "/repos/o/r/issues/1", {"title": "t"}, identity="app", issue=1)
        self.assertEqual(self.gh.calls(), [])

    def test_a_write_is_audited_with_identity_endpoint_issue_bytes_status_and_duration(self):
        self.gh.route(r"PATCH /repos/o/r/issues/9", {"number": 9})
        self.client.write("PATCH", "/repos/o/r/issues/9", {"title": "t"}, identity="app", issue=9)
        (entry,) = self.audit()
        self.assertEqual((entry["identity"], entry["method"], entry["endpoint"], entry["issue"], entry["status"]),
                         ("app", "PATCH", "/repos/o/r/issues/9", 9, 200))
        self.assertEqual(entry["bytes"], len(json.dumps({"title": "t"}).encode()))
        self.assertIn("duration_ms", entry)
        self.assertNotIn("title", json.dumps(entry))

    def test_a_write_that_gets_no_http_answer_is_still_audited(self):
        self.gh.route(r"PATCH /repos/o/r/issues/9", {"exit": 4, "stderr": "dial tcp: lookup failed"})
        with self.assertRaises(ghclient.GhError):
            self.client.write("PATCH", "/repos/o/r/issues/9", {"title": "t"}, identity="app", issue=9)
        (entry,) = self.audit()
        self.assertEqual((entry["status"], entry["issue"]), ("error", 9))

    def test_an_app_read_without_the_app_token_is_refused_not_sent_with_no_token(self):
        client = ghclient.Client(self.gh.env(app_token=None), clock=self.clock.time, sleep=self.clock.sleep)
        with self.assertRaises(ghclient.Unconfigured):
            client.get("/x", identity="app")
        self.assertEqual(self.gh.calls(), [])

    def test_an_audit_log_that_cannot_be_written_does_not_fail_a_completed_write(self):
        env = self.gh.env(TRACKER_AUDIT_LOG=str(self.gh.state))  # a directory: appending to it fails
        client = ghclient.Client(env, clock=self.clock.time, sleep=self.clock.sleep)
        self.gh.route(r"PATCH /repos/o/r/issues/9", {"number": 9})
        with mock.patch("sys.stderr") as stderr:
            response = client.write("PATCH", "/repos/o/r/issues/9", {"title": "t"}, identity="app", issue=9)
        self.assertEqual(response.status, 200)
        self.assertIn("audit", "".join(call.args[0] for call in stderr.write.call_args_list))

    def test_a_non_numeric_max_wait_is_a_configuration_error(self):
        for bad in ("soon", "nan", "inf", "-5"):
            with self.subTest(bad=bad), self.assertRaises(ghclient.Unconfigured):
                ghclient.Client(self.gh.env(TRACKER_MAX_WAIT=bad))

    def test_a_refusal_before_any_request_can_be_audited_by_label_only(self):
        self.client.audit_refusal("app", "status text", "sweep: identifier")
        (entry,) = self.audit()
        self.assertEqual((entry["status"], entry["reason"], entry["endpoint"]), ("refused", "sweep: identifier", "status text"))

    def test_only_html_text_is_a_render_and_empty_is_allowed_only_for_empty_input(self):
        self.gh.route("POST /markdown", {"status": 200, "body": "<p>x</p>", "headers": {"Content-Type": "text/plain"}})
        with self.assertRaises(ghclient.GhError):
            self.client.render("hello", "o/r")
        self.gh = FakeGh()
        self.addCleanup(self.gh.cleanup)
        client = ghclient.Client(self.gh.env(), clock=self.clock.time, sleep=self.clock.sleep)
        self.gh.route("POST /markdown", {"status": 200, "body": {"a": 1}})
        with self.assertRaises(ghclient.GhError):
            client.render("hello", "o/r")
        self.gh = FakeGh()
        self.addCleanup(self.gh.cleanup)
        client = ghclient.Client(self.gh.env(), clock=self.clock.time, sleep=self.clock.sleep)
        self.gh.route("POST /markdown", {"status": 200, "body": ""})
        with self.assertRaises(ghclient.GhError):
            client.render("hello", "o/r")
        self.assertEqual(client.render("", "o/r"), "")

    def test_a_render_is_paced_but_neither_audited_nor_counted_against_the_hour(self):
        self.gh.route("POST /markdown", {"status": 200, "body": "<p>x</p>"})
        for _ in range(3):
            self.assertEqual(self.client.render("hello", "o/r"), "<p>x</p>")
        self.assertEqual(self.audit(), [])
        self.assertGreaterEqual(sum(self.clock.sleeps), 2.4)
        state = json.loads((self.gh.state / "ratelimit.json").read_text())
        self.assertEqual(state["app"]["stamps"], [])

    def test_an_http_date_or_junk_retry_after_backs_off_instead_of_crashing(self):
        date = "Thu, 15 Jan 2027 08:00:30 GMT"  # 1_800_000_000 is 2027-01-15 08:00:00 UTC
        self.gh.route(r"GET /x", {"status": 429, "headers": {"retry-after": date}, "body": {"message": "slow"}}, {"status": 200, "body": {}})
        self.assertEqual(self.client.get("/x").status, 200)
        self.assertGreaterEqual(sum(self.clock.sleeps), 30)
        self.assertLess(sum(self.clock.sleeps), 60)
        for number, junk in enumerate(("soon", "")):
            with self.subTest(junk=junk):
                self.clock.sleeps.clear()
                self.gh.route(rf"GET /y{number}", {"status": 429, "headers": {"retry-after": junk}, "body": {"message": "slow"}}, {"status": 200, "body": {}})
                self.assertEqual(self.client.get(f"/y{number}").status, 200)
                self.assertGreaterEqual(sum(self.clock.sleeps), 60)

    def test_non_finite_retry_headers_back_off_by_default_instead_of_breaking_sleep(self):
        for number, junk in enumerate(("nan", "inf", "-inf")):
            with self.subTest(junk=junk):
                self.clock.sleeps.clear()
                self.gh.route(rf"GET /f{number}", {"status": 429, "headers": {"retry-after": junk}, "body": {"message": "slow"}}, {"status": 200, "body": {}})
                self.assertEqual(self.client.get(f"/f{number}").status, 200)
                self.assertGreaterEqual(sum(self.clock.sleeps), 60)
                self.assertLess(sum(self.clock.sleeps), 3600)

    def test_a_non_numeric_ratelimit_reset_falls_back_to_the_default_backoff(self):
        self.gh.route(r"GET /z", {"status": 403, "headers": {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "tomorrow"}, "body": {"message": "API rate limit exceeded"}}, {"status": 200, "body": {}})
        self.assertEqual(self.client.get("/z").status, 200)
        self.assertGreaterEqual(sum(self.clock.sleeps), 60)

    def test_reads_are_not_audited(self):
        self.gh.route(r"GET /x", {})
        self.client.get("/x")
        self.assertEqual(self.audit(), [])

    def test_the_audit_log_only_ever_grows(self):
        self.gh.route(r"PATCH /repos/o/r/issues/9", {"number": 9})
        for _ in range(3):
            self.client.write("PATCH", "/repos/o/r/issues/9", {"title": "t"}, identity="app", issue=9)
        self.assertEqual(len(self.audit()), 3)

    def test_the_final_payload_is_swept_and_a_hit_refuses_the_write(self):
        with self.assertRaises(ghclient.Refused) as caught:
            self.client.write("POST", "/repos/o/r/issues", {"title": "t", "body": "mail me@" + "gmail.example"}, identity="app")
        self.assertNotIn("gmail", str(caught.exception))
        self.assertEqual(self.gh.calls(), [])
        (entry,) = self.audit()
        self.assertEqual(entry["status"], "refused")

    def test_an_unusable_pattern_file_refuses_every_write_and_audits_it(self):
        with mock.patch.object(textsafe, "PATTERN_FILE", self.gh.state / "missing-patterns.txt"):
            with self.assertRaises(ghclient.Refused) as caught:
                self.client.write("POST", "/repos/o/r/issues", {"title": "t", "body": "perfectly clean"}, identity="app")
        self.assertIn("cannot be checked", str(caught.exception))
        self.assertEqual(self.gh.calls(), [])
        (entry,) = self.audit()
        self.assertEqual(entry["status"], "refused")

    def test_a_baseline_lets_pre_existing_hits_through_but_not_new_ones(self):
        self.gh.route(r"PATCH /repos/o/r/issues/1", {})
        old = "legacy 8.8.4.4 text"
        self.client.write("PATCH", "/repos/o/r/issues/1", {"body": old + " more"}, identity="app", issue=1, baseline=old)
        with self.assertRaises(ghclient.Refused):
            self.client.write("PATCH", "/repos/o/r/issues/1", {"body": old + " 8.8.4.5"}, identity="app", issue=1, baseline=old)

    def test_retry_after_is_honoured_and_the_write_is_retried(self):
        self.gh.route(r"PATCH /repos/o/r/issues/1",
                      {"status": 403, "headers": {"retry-after": "7"}, "body": {"message": "secondary rate limit"}},
                      {"status": 200, "body": {"number": 1}})
        response = self.client.write("PATCH", "/repos/o/r/issues/1", {"title": "t"}, identity="app", issue=1)
        self.assertEqual(response.status, 200)
        self.assertEqual(len(self.gh.calls()), 2)
        self.assertGreaterEqual(sum(self.clock.sleeps), 7)

    def test_ratelimit_reset_is_honoured_when_the_primary_limit_is_spent(self):
        reset = int(self.clock.now) + 30
        self.gh.route(r"GET /x",
                      {"status": 403, "headers": {"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(reset)}, "body": {"message": "API rate limit exceeded"}},
                      {"status": 200, "body": {}})
        self.client.get("/x")
        self.assertGreaterEqual(self.clock.now, reset)

    def test_a_secondary_limit_without_headers_backs_off_a_minute(self):
        self.gh.route(r"GET /x", {"status": 429, "body": {"message": "slow down"}}, {"status": 200, "body": {}})
        self.client.get("/x")
        self.assertGreaterEqual(sum(self.clock.sleeps), 60)

    def test_a_wait_beyond_max_wait_is_not_slept_through(self):
        self.gh.route(r"GET /x", {"status": 403, "headers": {"retry-after": "7200"}, "body": {"message": "rate limit"}})
        with self.assertRaises(ghclient.RateLimited):
            self.client.get("/x")
        self.assertEqual(self.clock.sleeps, [])

    def test_a_422_spam_on_a_sub_issue_add_backs_off_and_retries(self):
        self.gh.route(r"POST /repos/o/r/issues/1/sub_issues",
                      {"status": 422, "body": {"message": "Validation Failed: was submitted too quickly, flagged as spam"}},
                      {"status": 201, "body": {"number": 2}})
        response = self.client.write("POST", "/repos/o/r/issues/1/sub_issues", {"sub_issue_id": 5}, identity="app", issue=1)
        self.assertEqual(response.status, 201)
        self.assertGreaterEqual(sum(self.clock.sleeps), 60)
        self.assertEqual([e["status"] for e in self.audit()], [422, 201])

    def test_any_other_422_fails_at_once(self):
        self.gh.route(r"POST /repos/o/r/issues", {"status": 422, "body": {"message": "Validation Failed: title missing"}})
        with self.assertRaises(ghclient.GhError) as caught:
            self.client.write("POST", "/repos/o/r/issues", {"title": "t"}, identity="app")
        self.assertEqual(caught.exception.status, 422)
        self.assertEqual(len(self.gh.calls()), 1)

    def test_persistent_limiting_stops_after_bounded_attempts(self):
        self.gh.route(r"GET /x", {"status": 429, "body": {"message": "slow down"}})
        with self.assertRaises(ghclient.RateLimited):
            self.client.get("/x")
        self.assertLessEqual(len(self.gh.calls()), 4)

    def test_gh_failing_without_an_http_answer_is_an_error_that_hides_the_token(self):
        self.gh.route(r"GET /x", {"exit": 4, "stderr": f"gh: could not authenticate with {APP_TOKEN}"})
        with self.assertRaises(ghclient.GhError) as caught:
            self.client.get("/x")
        self.assertNotIn(APP_TOKEN, str(caught.exception))

    def test_a_success_status_with_a_failed_gh_exit_is_an_error_not_an_answer(self):
        self.gh.route(r"GET /x", {"status": 200, "body": "", "exit_after": 1})
        with self.assertRaises(ghclient.GhError) as caught:
            self.client.get("/x")
        self.assertEqual(caught.exception.status, 0)

    def test_an_http_error_with_a_failed_exit_still_goes_through_backoff(self):
        self.gh.route(r"GET /x", {"status": 429, "body": {"message": "slow"}}, {"status": 200, "body": {}})
        self.assertEqual(self.client.get("/x").status, 200)

    def test_json_bodies_are_parsed_and_headers_lowercased(self):
        self.gh.route(r"GET /x", {"status": 200, "headers": {"Link": "<next>"}, "body": {"a": [1]}})
        response = self.client.get("/x")
        self.assertEqual(response.data, {"a": [1]})
        self.assertEqual(response.headers["link"], "<next>")

    def test_the_default_state_directory_follows_xdg_then_home(self):
        self.assertEqual(ghclient.state_dir({"TRACKER_STATE_DIR": "/a"}), "/a")
        self.assertEqual(ghclient.state_dir({"XDG_STATE_HOME": "/x", "HOME": "/h"}), "/x/tracker")
        self.assertEqual(ghclient.state_dir({"HOME": "/h"}), "/h/.local/state/tracker")

    def test_a_held_issue_lock_makes_a_second_caller_give_up_after_max_wait(self):
        client = ghclient.Client(self.gh.env(TRACKER_MAX_WAIT="0.3"), clock=self.clock.time, sleep=self.clock.sleep)
        with self.client.issue_lock("o/r", 5):
            started = time.monotonic()
            with self.assertRaises(ghclient.RateLimited):
                with client.issue_lock("o/r", 5):
                    self.fail("the lock was not held")
            self.assertLess(time.monotonic() - started, 5)
        with client.issue_lock("o/r", 5):
            pass

    def test_the_issue_lock_excludes_a_second_holder(self):
        with self.client.issue_lock("o/r", 5):
            path = next(p for p in self.gh.state.iterdir() if p.name.startswith("issue-"))
            with open(path, "w") as other:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with open(path, "w") as other:
            fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)


if __name__ == "__main__":
    unittest.main()
