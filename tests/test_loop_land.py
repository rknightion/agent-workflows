"""loop-land: ready, verify, abandon and summary, against temporary repositories and a stubbed gh."""
import ast
import datetime
import hashlib
import importlib.util
import json
import os
import re
import signal
import subprocess
import sys
import time
import unittest
import unittest.mock
from importlib.machinery import SourceFileLoader
from pathlib import Path

from loop_tools_support import BIN, Sandbox

LAND = BIN / 'loop-land'
FIXTURE = Path(__file__).resolve().parent / 'fixtures' / 'landings.sample.jsonl'

HEAD = '1' * 40          # the pull request's head in most cases; it never needs to exist as a commit
OTHER_HEAD = '2' * 40

FAKE_GH = r'''#!@PYTHON@
import glob, json, os, sys
d = os.environ["FAKE_GH_DIR"]
a = sys.argv[1:]
open(d + "/calls.log", "a").write(json.dumps(a) + "\n")
if os.path.exists(d + "/gh.fail"):
    sys.stderr.write("gh: boom"); sys.exit(1)
def load(name, default):
    p = d + "/" + name
    return json.load(open(p)) if os.path.exists(p) else default
def http(code, reason, body):
    sys.stdout.write("HTTP/2.0 %d %s\r\nContent-Type: application/json\r\n\r\n%s" % (code, reason, body))
    if code >= 400:
        sys.stderr.write("gh: %s (HTTP %d)" % (json.loads(body).get("message"), code)); sys.exit(1)
    sys.exit(0)
path = next((x for x in a if x.startswith("repos/")), "")
if a[:1] == ["api"] and "/actions/jobs/" in path and path.endswith("/rerun"):
    job = int(path.split("/jobs/")[1].split("/")[0])
    open(d + "/reruns.log", "a").write(str(job) + "\n")
    result = open(d + "/rerun-result").read().strip() if os.path.exists(d + "/rerun-result") else "success"
    for f in glob.glob(d + "/checks-*.json"):
        runs = json.load(open(f))
        if any(r["id"] == job for r in runs):
            name = next(r["name"] for r in runs if r["id"] == job)
            runs.append({"id": max(r["id"] for r in runs) + 1, "name": name, "status": "completed",
                         "conclusion": result})
            json.dump(runs, open(f, "w"))
    sys.exit(0)
if a[:1] == ["api"] and "/check-runs" in path:
    sha = path.split("/commits/")[1].split("/")[0]
    for run in load("checks-" + sha + ".json", []):
        sys.stdout.write(json.dumps(run) + "\n")
    sys.exit(0)
if a[:1] == ["api"] and path.split("?")[0].endswith("/status") and "/commits/" in path:
    sha = path.split("/commits/")[1].split("/")[0]
    sys.stdout.write(json.dumps({"statuses": load("statuses-" + sha + ".json", [])}))
    sys.exit(0)
if a[:1] == ["api"] and "/rules/branches/" in path:
    names = load("required.json", [])
    http(200, "OK", json.dumps([{"type": "required_status_checks",
                                 "parameters": {"required_status_checks": [{"context": n} for n in names]}}]))
if a[:1] == ["api"] and "/protection/" in path:
    http(404, "Not Found", json.dumps({"message": "Branch not protected"}))
if a[:2] == ["pr", "view"]:
    seq = load("prs-seq.json", {})
    if a[2] in seq:
        entry = seq[a[2]][0]
        if len(seq[a[2]]) > 1:
            seq[a[2]].pop(0)
            json.dump(seq, open(d + "/prs-seq.json", "w"))
    else:
        entry = load("prs.json", {}).get(a[2])
    if entry is None:
        sys.stderr.write("no such pull request"); sys.exit(1)
    sys.stdout.write(json.dumps(entry)); sys.exit(0)
if a[:2] == ["pr", "list"]:
    sys.stdout.write(json.dumps(load("merged.json", []))); sys.exit(0)
sys.stderr.write("fake gh: unsupported " + json.dumps(a)); sys.exit(2)
'''

LOOPMD = '''# Loop: widgets
tier: routine
gate: just check
ci-required: build
release-on-push: no
deploy-on-push: no
grafana-stack: none
{extra}
## Credentials
None.
'''

LEDGER_KEYS = ['v', 'ts', 'event', 'outcome', 'pr', 'head', 'issues', 'merge_sha', 'main', 'deploy',
               'fix_forward', 'slot_seconds', 'missing']
OUTCOMES = {
    'ready': {'go'},
    'refused': {'held', 'stale-head', 'checks-failed', 'main-undecided', 'slot-busy', 'not-merged', 'wrong-head'},
    'merged': {None},
    'verified': {'landed', 'main-red', 'unread', 'deploy-failed'},
    'abandoned': {'abandoned'},
    'closeout-check': {'ok', 'missing'},
    'error': {'error'},
}


def validate_event(record):
    """Problems with one ledger event against the design's schema; empty when it conforms."""
    problems = []
    # `reason` is optional and additive: it follows the fixed keys, on an `abandoned` or an adopted `merged` event.
    if list(record) not in (LEDGER_KEYS, LEDGER_KEYS + ['reason']):
        return [f'keys {list(record)} are not {LEDGER_KEYS}']
    if 'reason' in record and (record['event'] not in ('abandoned', 'merged') or not isinstance(record['reason'], str)
                               or not record['reason'].strip()):
        problems.append('reason is a non-empty string on an abandoned or merged event only')
    if record['v'] != 1:
        problems.append('v is not 1')
    if not re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ', str(record['ts'])):
        problems.append('ts is not YYYY-MM-DDTHH:MM:SSZ')
    event = record['event']
    if event not in OUTCOMES:
        return problems + [f'unknown event {event}']
    if record['outcome'] not in OUTCOMES[event]:
        problems.append(f'outcome {record["outcome"]} is not one of {OUTCOMES[event]} for {event}')
    if record['pr'] is not None and not isinstance(record['pr'], int):
        problems.append('pr is neither a number nor null')
    if record['head'] is not None and not isinstance(record['head'], str):
        problems.append('head is not a string')
    if not isinstance(record['issues'], list) or not all(isinstance(i, (int, str)) for i in record['issues']):
        problems.append('issues is not a list of numbers or ids')
    if (event in ('merged', 'verified')) != isinstance(record['merge_sha'], str):
        problems.append('merge_sha is set exactly from merged onward')
    if record['main'] not in ('green', 'red', 'unread', None) or (event == 'verified') != (record['main'] is not None):
        problems.append('main is set exactly on verified, to green, red or unread')
    if record['deploy'] not in ('synced', 'failed', 'unread', 'n/a', None) or \
            (event == 'verified') != (record['deploy'] is not None):
        problems.append('deploy is set exactly on verified, to synced, failed, unread or n/a')
    if record['fix_forward'] is not None and not isinstance(record['fix_forward'], str):
        problems.append('fix_forward is neither a reason nor null')
    if (event in ('verified', 'abandoned')) and record['slot_seconds'] is not None and \
            not isinstance(record['slot_seconds'], int):
        problems.append('slot_seconds is not a whole number')
    if event not in ('verified', 'abandoned') and record['slot_seconds'] is not None:
        problems.append('slot_seconds is set only on verified and abandoned')
    if (record['outcome'] == 'missing') != isinstance(record['missing'], list):
        problems.append('missing is a list exactly when the closeout check found one')
    return problems


class LandBase(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()
        gh = self.sb.bin / 'gh'
        gh.write_text(FAKE_GH.replace('@PYTHON@', sys.executable))
        gh.chmod(0o755)
        self.repo = self.sb.make_repo('widgets', remote_url='https://github.com/acme/widgets.git')
        self.codex = self.sb.root / 'codex'
        self.run_dir = self.codex / 'loop7'
        self.run_dir.mkdir(parents=True)
        self.slot_file = self.repo / '.git' / 'loop-land.slot.json'
        self.tmp = self.sb.root / 'tmp'
        self.tmp.mkdir()
        self.prs = {}
        self.set_loopmd()

    # --- world ---

    def gh_file(self, name, value):
        (self.sb.gh_dir / name).write_text(json.dumps(value))

    def tip(self):
        return self.sb.git(self.repo, 'rev-parse', 'HEAD')

    def set_loopmd(self, extra=''):
        self.sb.commit_file(self.repo, 'LOOP.md', LOOPMD.format(extra=extra), 'loop md')
        self.main_checks('success')
        return self.tip()

    def checks(self, sha, runs):
        self.gh_file(f'checks-{sha}.json', [{'status': 'completed', **r} for r in runs])

    def main_checks(self, conclusion, status='completed'):
        self.checks(self.tip(), [{'id': 1, 'name': 'build', 'status': status, 'conclusion': conclusion}])

    def head_checks(self, conclusion, head=HEAD, run_id=1, status='completed'):
        self.checks(head, [{'id': run_id, 'name': 'build', 'status': status, 'conclusion': conclusion}])

    def set_pr(self, pr, state='OPEN', head=HEAD, merge=None):
        self.prs[str(pr)] = {'state': state, 'headRefOid': head, 'mergeCommit': {'oid': merge} if merge else None}
        self.gh_file('prs.json', self.prs)

    def merge_on_main(self, pr, head=HEAD, path='src.txt', content='x', issues=None):
        """Land a commit on origin's main the way a squash merge does, and mark the pull request merged."""
        sha = self.sb.commit_file(self.repo, path, content, f'squash (#{pr})')
        self.set_pr(pr, 'MERGED', head, sha)
        return sha

    def hold_slot(self, pr, head=HEAD, taken=None, run_dir=None, issues=(11,)):
        taken = taken or datetime.datetime.now(datetime.timezone.utc)
        self.slot_file.write_text(json.dumps({
            'v': 1, 'run_dir': str((run_dir or self.run_dir).resolve()), 'pr': pr, 'head': head,
            'issues': list(issues), 'taken': taken.strftime('%Y-%m-%dT%H:%M:%SZ')}))

    # --- running ---

    def env(self):
        return {**self.sb.env, 'LOOP_LAND_INTERVAL': '0.05', 'LOOP_LAND_MINUTE_SECONDS': '0.3',
                'TMPDIR': str(self.tmp)}

    def land(self, *args):
        return self.sb.run('loop-land', *args, extra_env=self.env())

    def spawn(self, *args):
        return subprocess.Popen([sys.executable, str(LAND), *map(str, args)], env=self.env(), text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def assert_no_worktrees_left(self):
        listed = self.sb.git(self.repo, 'worktree', 'list', '--porcelain').count('worktree ')
        self.assertEqual(listed, 1, 'a worktree made for the commands was left behind')
        self.assertEqual(list(self.tmp.glob('loop-land-*')), [])

    def ready(self, pr=7, head=HEAD, issues='11,12', *extra):
        return self.land('ready', '--repo', self.repo, '--run-dir', self.run_dir, '--pr', pr, '--head', head,
                         '--issues', issues, *extra)

    def verify(self, pr=7):
        return self.land('verify', '--repo', self.repo, '--run-dir', self.run_dir, '--pr', pr)

    def out(self, result):
        lines = result.stdout.strip().splitlines()
        self.assertEqual(len(lines), 1, f'want one JSON line: {result.stdout!r} {result.stderr!r}')
        return json.loads(lines[0])

    def ledger(self):
        path = self.run_dir / 'landings.jsonl'
        events = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        for event in events:
            self.assertEqual(validate_event(event), [], event)
        return events

    def calls(self):
        path = self.sb.gh_dir / 'calls.log'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def reruns(self):
        path = self.sb.gh_dir / 'reruns.log'
        return path.read_text().split() if path.exists() else []

    def expect(self, result, outcome, code):
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        report = self.out(result)
        self.assertEqual((report['outcome'], report['exit']), (outcome, code), report)
        return report

    def slot(self):
        return json.loads(self.slot_file.read_text()) if self.slot_file.exists() else None


class ReadyTest(LandBase):
    def setUp(self):
        super().setUp()
        self.set_pr(7)
        self.head_checks('success')

    def test_go_on_green_main_takes_the_slot_and_names_the_merge_command(self):
        report = self.expect(self.ready(), 'go', 0)
        self.assertEqual(report['merge_command'], f'gh pr merge 7 --squash --match-head-commit {HEAD}')
        slot = self.slot()
        self.assertEqual((slot['pr'], slot['head'], slot['issues']), (7, HEAD, [11, 12]))
        self.assertEqual(slot['run_dir'], str(self.run_dir.resolve()))
        self.assertEqual(list(self.slot_file.parent.glob('loop-land.slot.json.*')), [])
        events = self.ledger()
        self.assertEqual([(e['event'], e['outcome'], e['pr'], e['head'], e['issues']) for e in events],
                         [('ready', 'go', 7, HEAD, [11, 12])])
        self.assertIsNone(events[0]['fix_forward'])
        self.assertFalse([c for c in self.calls() if 'merge' in c], 'ready must never merge')

    def test_held_when_main_is_red(self):
        self.main_checks('failure')
        report = self.expect(self.ready(), 'held', 20)
        self.assertIn('build', report['detail'])
        self.assertIsNone(self.slot())
        self.assertEqual([(e['event'], e['outcome']) for e in self.ledger()], [('refused', 'held')])

    def test_fix_forward_goes_on_a_red_main_and_records_the_reason(self):
        self.main_checks('failure')
        report = self.expect(self.ready(7, HEAD, '11', '--fix-forward', 'repairs the red build'), 'go', 0)
        self.assertEqual(report['main'], 'red')
        events = self.ledger()
        self.assertEqual((events[0]['event'], events[0]['outcome']), ('ready', 'go'))
        self.assertEqual(events[0]['fix_forward'], 'repairs the red build')
        self.assertEqual(self.slot()['pr'], 7)

    def test_slot_busy_when_the_slot_is_held_and_fresh(self):
        self.set_pr(9, 'OPEN', OTHER_HEAD)
        self.hold_slot(9, OTHER_HEAD)
        report = self.expect(self.ready(), 'slot-busy', 24)
        self.assertIn('pull request 9', report['detail'])
        self.assertEqual(self.slot()['pr'], 9)
        self.assertEqual([(e['event'], e['outcome']) for e in self.ledger()], [('refused', 'slot-busy')])

    def test_a_held_slot_whose_pull_request_merged_is_read_before_anything_else(self):
        self.hold_slot(9, OTHER_HEAD, issues=(21,))
        merge = self.merge_on_main(9, OTHER_HEAD)
        self.main_checks('success')
        self.checks(merge, [{'id': 1, 'name': 'build', 'conclusion': 'success'}])
        self.expect(self.ready(), 'go', 0)
        events = self.ledger()
        self.assertEqual([(e['event'], e['outcome'], e['pr']) for e in events],
                         [('merged', None, 9), ('verified', 'landed', 9), ('ready', 'go', 7)])
        self.assertEqual((events[0]['merge_sha'], events[0]['issues']), (merge, [21]))
        self.assertEqual((events[1]['main'], events[1]['deploy']), ('green', 'n/a'))
        self.assertEqual(self.slot()['pr'], 7)

    def test_a_held_slot_whose_merge_is_unread_still_blocks(self):
        self.set_loopmd('ci-max-minutes: 1\n')
        self.hold_slot(9, OTHER_HEAD)
        merge = self.merge_on_main(9, OTHER_HEAD)
        self.checks(merge, [{'id': 1, 'name': 'build', 'status': 'in_progress', 'conclusion': None}])
        self.expect(self.ready(), 'slot-busy', 24)
        self.assertEqual(self.slot()['pr'], 9)
        self.assertEqual([(e['event'], e['outcome']) for e in self.ledger()],
                         [('merged', None), ('verified', 'unread'), ('refused', 'slot-busy')])

    def test_a_stale_slot_whose_pull_request_has_not_merged_is_abandoned(self):
        self.set_pr(9, 'OPEN', OTHER_HEAD)
        long_ago = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=2)
        self.hold_slot(9, OTHER_HEAD, taken=long_ago)
        self.expect(self.ready(), 'go', 0)
        events = self.ledger()
        self.assertEqual([(e['event'], e['outcome'], e['pr']) for e in events],
                         [('abandoned', 'abandoned', 9), ('ready', 'go', 7)])
        self.assertGreaterEqual(events[0]['slot_seconds'], 7000)
        self.assertEqual(self.slot()['pr'], 7)

    def test_a_rerun_for_the_identical_landing_is_slot_busy_while_the_slot_is_fresh(self):
        self.expect(self.ready(), 'go', 0)
        before = self.slot_file.read_text()
        report = self.expect(self.ready(), 'slot-busy', 24)
        self.assertIn('pull request 7', report['detail'])
        self.assertEqual(self.slot_file.read_text(), before)
        self.assertEqual([(e['event'], e['outcome']) for e in self.ledger()],
                         [('ready', 'go'), ('refused', 'slot-busy')])

    def test_ready_never_prints_go_without_taking_the_slot_in_that_invocation(self):
        # The slot for this very landing is stale, so step 2 abandons it. While the checks are read, a
        # `ci-status` command hands the slot to pull request 8: go must not be printed for 7.
        taken = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=2)
        self.hold_slot(7, HEAD, taken=taken)
        other = json.dumps({'v': 1, 'run_dir': str(self.run_dir.resolve()), 'pr': 8, 'head': OTHER_HEAD,
                            'issues': [13], 'taken': datetime.datetime.now(datetime.timezone.utc)
                            .strftime('%Y-%m-%dT%H:%M:%SZ')})
        script = self.sb.root / 'steal.sh'
        script.write_text(f"#!/bin/sh\nprintf '%s' '{other}' > {self.slot_file}\nexit 0\n")
        script.chmod(0o755)
        self.set_loopmd(f'ci-status: {script} {{sha}}\n')
        report = self.expect(self.ready(), 'slot-busy', 24)
        self.assertNotIn('merge_command', report)
        self.assertEqual(self.slot()['pr'], 8)
        self.assertNotIn('go', [e['outcome'] for e in self.ledger()])

    def test_the_same_pull_request_with_a_different_head_is_slot_busy(self):
        self.hold_slot(7, OTHER_HEAD)
        self.expect(self.ready(), 'slot-busy', 24)
        self.assertEqual(self.slot()['head'], OTHER_HEAD)

    def test_the_same_head_from_another_run_directory_is_slot_busy(self):
        other = self.codex / 'loop8'
        other.mkdir()
        self.hold_slot(7, HEAD, run_dir=other)
        self.expect(self.ready(), 'slot-busy', 24)

    def test_the_head_is_confirmed_again_just_before_the_slot_is_taken(self):
        entry = lambda head: {'state': 'OPEN', 'headRefOid': head, 'mergeCommit': None}
        self.gh_file('prs-seq.json', {'7': [entry(HEAD), entry(OTHER_HEAD)]})
        self.expect(self.ready(), 'stale-head', 21)
        self.assertIsNone(self.slot())

    def test_a_pull_request_merged_while_ready_waited_is_reported_as_what_happened(self):
        entry = lambda state: {'state': state, 'headRefOid': HEAD, 'mergeCommit': None}
        self.gh_file('prs-seq.json', {'7': [entry('OPEN'), entry('MERGED')]})
        report = self.expect(self.ready(), 'error', 30)
        self.assertIn('MERGED', report['detail'])
        # No landing was recorded, so verify has nothing to read: the message says what is true.
        self.assertIn('summary --check', report['detail'])
        self.assertNotIn('run verify', report['detail'])
        self.assertIsNone(self.slot())
        self.assertEqual([e['event'] for e in self.ledger()], ['error'])
        self.expect(self.verify(), 'error', 30)

    def test_main_is_read_into_the_remote_tracking_ref_not_fetch_head(self):
        self.sb.git(self.repo, 'config', '--unset-all', 'remote.origin.fetch')
        self.sb.git(self.repo, 'update-ref', '-d', 'refs/remotes/origin/main')
        self.expect(self.ready(), 'go', 0)
        self.assertEqual(self.sb.git(self.repo, 'rev-parse', 'refs/remotes/origin/main'), self.tip())

    def test_two_ready_runs_at_once_land_one_and_refuse_the_other(self):
        self.set_pr(8, 'OPEN', OTHER_HEAD)
        self.head_checks('success', head=OTHER_HEAD)
        common = ('--repo', self.repo, '--run-dir', self.run_dir, '--issues', '11')
        first = self.spawn('ready', *common, '--pr', '7', '--head', HEAD)
        second = self.spawn('ready', *common, '--pr', '8', '--head', OTHER_HEAD)
        outputs = [proc.communicate(timeout=60) for proc in (first, second)]
        codes = sorted((first.returncode, second.returncode))
        self.assertEqual(codes, [0, 24], outputs)
        winner = 7 if first.returncode == 0 else 8
        self.assertEqual(self.slot()['pr'], winner)
        self.assertEqual(sorted(e['outcome'] for e in self.ledger()), ['go', 'slot-busy'])

    def test_the_exclusive_take_refuses_a_second_landing_and_never_replaces_the_slot(self):
        sys.path.insert(0, str(BIN))
        try:
            loader = SourceFileLoader('loop_land_under_test', str(LAND))
            module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
            loader.exec_module(module)
        finally:
            sys.path.remove(str(BIN))
        path = self.slot_file
        self.assertTrue(module.take_slot(path, self.run_dir, 7, HEAD, [1]))
        before = path.read_text()
        self.assertFalse(module.take_slot(path, self.run_dir, 8, OTHER_HEAD, [2]))
        self.assertFalse(module.take_slot(path, self.run_dir, 7, HEAD, [1]))
        self.assertEqual(path.read_text(), before)
        self.assertEqual(list(path.parent.glob('loop-land.slot.json.*')), [])

    def test_stale_head(self):
        self.set_pr(7, 'OPEN', OTHER_HEAD)
        report = self.expect(self.ready(), 'stale-head', 21)
        self.assertIn(OTHER_HEAD, report['detail'])
        self.assertIsNone(self.slot())
        self.assertEqual([(e['event'], e['outcome']) for e in self.ledger()], [('refused', 'stale-head')])

    def test_checks_failed_names_the_check(self):
        self.head_checks('failure')
        report = self.expect(self.ready(), 'checks-failed', 22)
        self.assertEqual(report['checks'], ['build'])
        self.assertIsNone(self.slot())
        self.assertEqual([(e['event'], e['outcome']) for e in self.ledger()], [('refused', 'checks-failed')])

    def test_a_cancelled_check_is_rerun_once_and_then_trusted(self):
        self.head_checks('cancelled', run_id=5)
        self.expect(self.ready(), 'go', 0)
        self.assertEqual(self.reruns(), ['5'])
        post = [c for c in self.calls() if 'POST' in c]
        self.assertEqual(len(post), 1)
        self.assertTrue(post[0][-1].endswith('/actions/jobs/5/rerun'))

    def test_a_check_cancelled_again_after_its_rerun_fails_the_landing(self):
        self.head_checks('cancelled', run_id=5)
        (self.sb.gh_dir / 'rerun-result').write_text('cancelled')
        report = self.expect(self.ready(), 'checks-failed', 22)
        self.assertEqual(report['checks'], ['build'])
        self.assertEqual(self.reruns(), ['5'])

    def test_main_undecided_at_the_deadline(self):
        self.main_checks(None, status='in_progress')
        self.expect(self.ready(7, HEAD, '11', '--timeout', '1'), 'main-undecided', 23)
        self.assertIsNone(self.slot())
        self.assertEqual([(e['event'], e['outcome']) for e in self.ledger()], [('refused', 'main-undecided')])

    def test_head_checks_still_pending_at_the_deadline_is_an_error_not_a_pass(self):
        self.head_checks(None, status='in_progress')
        report = self.expect(self.ready(7, HEAD, '11', '--timeout', '1'), 'error', 30)
        self.assertIn('pending', report['detail'])
        self.assertIsNone(self.slot())
        self.assertEqual([e['event'] for e in self.ledger()], ['error'])

    def test_keys_come_from_main_not_from_the_pull_request_head(self):
        # The head changes LOOP.md to require nothing; main's LOOP.md still requires `build`, and it failed.
        self.sb.git(self.repo, 'checkout', '-q', '-b', 'loop7-land1')
        (self.repo / 'LOOP.md').write_text(LOOPMD.format(extra='').replace('ci-required: build', 'ci-required: none'))
        self.sb.git(self.repo, 'commit', '-q', '-a', '-m', 'weaken the gate')
        head = self.sb.git(self.repo, 'rev-parse', 'HEAD')
        self.sb.git(self.repo, 'push', '-q', 'origin', 'loop7-land1')
        # The pull request branch stays checked out: reading the working tree would see `ci-required: none`.
        self.assertEqual(self.sb.git(self.repo, 'rev-parse', '--abbrev-ref', 'HEAD'), 'loop7-land1')
        self.set_pr(7, 'OPEN', head)
        self.head_checks('failure', head=head)
        self.expect(self.ready(7, head), 'checks-failed', 22)
        self.assertIsNone(self.slot())

    def test_the_standing_file_commands_override_main_loop_md(self):
        standing = self.sb.root / 'standing.md'
        standing.write_text('# Standing\n\n## Commands\n\n- `ci-required`: `none`\n\n## Other\n- ci-required: build\n')
        digest = hashlib.sha256(standing.read_bytes()).hexdigest()
        (self.codex / 'charter-widgets-loop7.md').write_text(
            f'# Charter: widgets loop7\n\nharness:    claude\nstanding:   {standing} sha256={digest}\n\n## First wave\n')
        self.head_checks('failure')
        report = self.expect(self.ready(), 'go', 0)
        self.assertEqual(report['standing'], str(standing))

    def test_with_no_standing_file_the_json_says_so(self):
        self.assertIsNone(self.expect(self.ready(), 'go', 0)['standing'])

    def test_the_newest_charter_by_name_names_the_standing_file(self):
        older, newer = self.sb.root / 'older.md', self.sb.root / 'newer.md'
        older.write_text('## Commands\n- ci-required: none\n')
        newer.write_text('## Commands\n- ci-required: build\n')
        for name, path in (('charter-a-loop7.md', older), ('charter-b-loop7.md', newer)):
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            (self.codex / name).write_text(f'# Charter: widgets loop7\n\nstanding:   {path} sha256={digest}\n')
        self.head_checks('failure')
        report = self.expect(self.ready(), 'checks-failed', 22)
        self.assertEqual(report['standing'], str(newer))

    def test_a_standing_file_that_no_longer_matches_its_hash_is_an_error(self):
        standing = self.sb.root / 'standing.md'
        standing.write_text('## Commands\n- ci-required: none\n')
        (self.codex / 'charter-widgets-loop7.md').write_text(
            f'# Charter: widgets loop7\n\nstanding:   {standing} sha256={"0" * 64}\n\n## First wave\n')
        report = self.expect(self.ready(), 'error', 30)
        self.assertIn('Standing', report['detail'])
        self.assertIsNone(self.slot())

    def test_the_rulesets_name_the_required_checks_when_loop_md_does_not(self):
        sha = self.sb.commit_file(self.repo, 'LOOP.md', LOOPMD.format(extra='').replace('ci-required: build\n', ''), 'no key')
        self.gh_file('required.json', ['lint'])
        self.checks(sha, [{'id': 1, 'name': 'lint', 'conclusion': 'success'}])
        self.checks(HEAD, [{'id': 1, 'name': 'lint', 'conclusion': 'failure'}])
        self.expect(self.ready(), 'checks-failed', 22)

    def test_ci_status_decides_when_loop_md_has_one(self):
        script = self.sb.root / 'ci.sh'
        script.write_text('#!/bin/sh\ncase "$1" in %s) exit 1;; *) exit 0;; esac\n' % HEAD)
        script.chmod(0o755)
        self.set_loopmd(f'ci-status: {script} {{sha}}\n')
        self.expect(self.ready(), 'checks-failed', 22)

    def commands_on_main(self, main_script):
        """Main carries `ci.sh` and the key that runs it; a pull request branch with a different `ci.sh`
        stays checked out in the repository the tool is pointed at."""
        self.sb.commit_file(self.repo, 'ci.sh', main_script, 'main ci')
        self.set_loopmd('ci-status: sh ci.sh {sha}\n')
        self.sb.git(self.repo, 'checkout', '-q', '-b', 'loop7-land1')
        (self.repo / 'ci.sh').write_text('exit 0\n')
        self.sb.git(self.repo, 'commit', '-q', '-a', '-m', 'a script that always passes')

    def test_commands_run_against_main_not_the_checked_out_pull_request_branch(self):
        self.commands_on_main('exit 1\n')
        report = self.expect(self.ready(), 'checks-failed', 22)
        self.assertEqual(report['checks'], ['ci-status'])
        self.assertIsNone(self.slot())
        self.assert_no_worktrees_left()
        self.assertEqual((self.repo / 'ci.sh').read_text(), 'exit 0\n')

    def test_the_worktree_is_removed_when_the_command_cannot_tell(self):
        self.commands_on_main('exit 2\n')
        self.expect(self.ready(), 'error', 30)
        self.assert_no_worktrees_left()

    def interrupt_mid_command(self, number):
        marker = self.sb.root / 'running'
        self.commands_on_main(f'touch {marker}\nexec sleep 60\n')
        proc = self.spawn('ready', '--repo', self.repo, '--run-dir', self.run_dir, '--pr', '7', '--head', HEAD,
                          '--issues', '11')
        deadline = time.monotonic() + 30
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(marker.exists(), 'the command never started')
        proc.send_signal(number)
        out, err = proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, 30, out + err)
        report = json.loads(out.strip().splitlines()[-1])
        self.assertEqual(report['outcome'], 'error')
        self.assertIn('interrupted', report['detail'])
        self.assertEqual([e['event'] for e in self.ledger()], ['error'])
        self.assertIsNone(self.slot())
        self.assert_no_worktrees_left()

    def test_a_sigterm_mid_command_removes_the_worktree_and_reports_an_error(self):
        self.interrupt_mid_command(signal.SIGTERM)

    def test_a_sigint_mid_command_removes_the_worktree_and_reports_an_error(self):
        self.interrupt_mid_command(signal.SIGINT)

    def test_a_signal_after_a_normal_finish_exits_cleanly_wherever_it_lands(self):
        # Each hook between the command's end and the exit sends SIGTERM to the process first, then does its
        # own work: `quiet` (the earlier form, where the reviewer reproduced a traceback and exit 1),
        # `finished` (where the signals stop counting) and `cleanup`. A name the tool no longer has is skipped.
        source = LAND.read_text(encoding='utf-8')
        names = [name for name in ('quiet', 'finished', 'cleanup') if f'\ndef {name}(' in source]
        self.assertIn('cleanup', names)
        for name in names:
            with self.subTest(hook=name):
                script = self.sb.root / f'signal_{name}.py'
                script.write_text(f'''import importlib.util, os, signal, sys
from importlib.machinery import SourceFileLoader
sys.path.insert(0, {str(BIN)!r})
loader = SourceFileLoader('loop_land_signal', {str(LAND)!r})
module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
loader.exec_module(module)
real = getattr(module, {name!r})
def hook(*args):
    os.kill(os.getpid(), signal.SIGTERM)
    return real(*args)
setattr(module, {name!r}, hook)
sys.exit(module.main(['summary', '--repo', {str(self.repo)!r}, '--run-dir', {str(self.run_dir)!r}]))
''')
                done = subprocess.run([sys.executable, str(script)], env=self.env(), text=True,
                                      capture_output=True, timeout=60)
                self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
                self.assertNotIn('Traceback', done.stderr)

    def test_a_corrupt_ledger_does_not_swallow_the_error_event(self):
        (self.run_dir / 'landings.jsonl').write_text('not json\n')
        (self.sb.gh_dir / 'gh.fail').write_text('')
        report = self.expect(self.ready(), 'error', 30)
        self.assertIn('not JSON', report['ledger'])
        lines = (self.run_dir / 'landings.jsonl').read_text().splitlines()
        self.assertEqual(lines[0], 'not json')
        self.assertEqual(json.loads(lines[-1])['event'], 'error')

    def test_a_failing_gh_is_an_error_with_exit_30(self):
        (self.sb.gh_dir / 'gh.fail').write_text('')
        report = self.expect(self.ready(), 'error', 30)
        self.assertIn('last_known', report)
        self.assertIsNone(self.slot())
        self.assertEqual([e['event'] for e in self.ledger()], ['error'])

    def test_usage_errors_exit_2(self):
        self.assertEqual(self.land('ready', '--repo', self.repo, '--run-dir', self.run_dir, '--pr', '7',
                                   '--issues', '11').returncode, 2)
        self.assertEqual(self.ready(7, HEAD, '11', '--fix-forward', ' ').returncode, 2)


class VerifyTest(LandBase):
    def setUp(self):
        super().setUp()
        self.set_pr(7)
        self.head_checks('success')
        self.expect(self.ready(), 'go', 0)

    def landed(self, conclusion='success', path='src.txt'):
        merge = self.merge_on_main(7, path=path)
        self.checks(merge, [{'id': 1, 'name': 'build', 'conclusion': conclusion}])
        return merge

    def deploy_script(self, code):
        marker = self.sb.root / 'deploy.marker'
        script = self.sb.root / 'deploy.sh'
        script.write_text(f'#!/bin/sh\necho "$1" >> {marker}\nexit {code}\n')
        script.chmod(0o755)
        return script, marker

    def test_landed_records_the_merge_and_frees_the_slot(self):
        merge = self.landed()
        report = self.expect(self.verify(), 'landed', 0)
        self.assertEqual((report['main'], report['deploy'], report['merge_sha']), ('green', 'n/a', merge))
        self.assertIsNone(self.slot())
        events = self.ledger()
        self.assertEqual([(e['event'], e['outcome']) for e in events],
                         [('ready', 'go'), ('merged', None), ('verified', 'landed')])
        self.assertEqual((events[1]['merge_sha'], events[1]['issues']), (merge, [11, 12]))
        self.assertEqual((events[2]['main'], events[2]['deploy']), ('green', 'n/a'))
        self.assertIsInstance(events[2]['slot_seconds'], int)
        self.assertNotIn('pending', report['detail'])

    def test_verify_does_not_free_the_slot_of_a_landing_that_took_it_since(self):
        # Scenario B: 7 verified and its slot freed, 8 takes the slot, 7's verify is run again.
        self.landed()
        self.expect(self.verify(), 'landed', 0)
        self.set_pr(8, 'OPEN', OTHER_HEAD)
        self.head_checks('success', head=OTHER_HEAD)
        self.main_checks('success')
        self.expect(self.ready(8, OTHER_HEAD, '13'), 'go', 0)
        self.expect(self.verify(7), 'landed', 0)
        self.assertEqual(self.slot()['pr'], 8)

    def test_a_late_verify_does_not_free_the_slot_of_the_landing_that_abandoned_it(self):
        # Scenario A: 7 goes stale, 8 abandons it and takes the slot, 7 is merged late and verified.
        taken = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=2)
        self.hold_slot(7, HEAD, taken=taken, issues=(11, 12))
        self.set_pr(8, 'OPEN', OTHER_HEAD)
        self.head_checks('success', head=OTHER_HEAD)
        self.expect(self.ready(8, OTHER_HEAD, '13'), 'go', 0)
        self.landed()
        self.expect(self.verify(7), 'landed', 0)
        self.assertEqual(self.slot()['pr'], 8)
        self.assertIn(('abandoned', 7), [(e['event'], e['pr']) for e in self.ledger()])

    def test_an_unexpected_error_exits_30_never_1(self):
        self.gh_file('prs.json', {'7': {'state': 'MERGED', 'headRefOid': HEAD, 'mergeCommit': 'not-an-object'}})
        report = self.expect(self.verify(), 'error', 30)
        self.assertIn('unexpected', report['detail'])
        self.assertEqual(self.ledger()[-1]['event'], 'error')

    def test_main_red_stops_the_line_and_frees_the_slot(self):
        self.landed('failure')
        self.expect(self.verify(), 'main-red', 10)
        self.assertIsNone(self.slot())
        self.assertEqual(self.ledger()[-1]['main'], 'red')

    def test_unread_keeps_the_slot_and_a_rerun_does_not_repeat_the_merge_event(self):
        self.set_loopmd('ci-max-minutes: 1\n')
        merge = self.merge_on_main(7)
        self.checks(merge, [{'id': 1, 'name': 'build', 'status': 'in_progress', 'conclusion': None}])
        self.expect(self.verify(), 'unread', 11)
        self.assertEqual(self.slot()['pr'], 7)
        last = self.ledger()[-1]
        self.assertEqual((last['main'], last['deploy']), ('unread', 'unread'))
        self.checks(merge, [{'id': 1, 'name': 'build', 'conclusion': 'success'}])
        self.expect(self.verify(), 'landed', 0)
        self.assertIsNone(self.slot())
        self.assertEqual([e['event'] for e in self.ledger()], ['ready', 'merged', 'verified', 'verified'])

    def test_deploy_failed_when_main_is_green(self):
        script, marker = self.deploy_script(1)
        self.deploy_on(script)
        merge = self.landed()
        report = self.expect(self.verify(), 'deploy-failed', 12)
        self.assertEqual((report['main'], report['deploy']), ('green', 'failed'))
        self.assertEqual(marker.read_text().split(), [merge])
        self.assertIsNone(self.slot())

    def deploy_on(self, script, paths=None, verify=None):
        extra = f'deploy-verify: {verify or str(script) + " {sha}"}\n' + (f'deploy-paths: {paths}\n' if paths else '')
        text = LOOPMD.format(extra=extra).replace('deploy-on-push: no', 'deploy-on-push: yes')
        self.sb.commit_file(self.repo, 'LOOP.md', text, 'deploys')

    def test_deploy_synced(self):
        script, marker = self.deploy_script(0)
        self.deploy_on(script)
        self.landed()
        report = self.expect(self.verify(), 'landed', 0)
        self.assertEqual(report['deploy'], 'synced')

    def test_the_deploy_read_is_skipped_when_the_merge_touched_no_deploy_path(self):
        script, marker = self.deploy_script(1)
        self.deploy_on(script, paths='deploy/**, infra/*.tf')
        self.landed(path='docs.md')
        report = self.expect(self.verify(), 'landed', 0)
        self.assertEqual(report['deploy'], 'n/a')
        self.assertFalse(marker.exists())

    def test_the_deploy_read_runs_when_the_merge_touched_a_deploy_path(self):
        script, marker = self.deploy_script(1)
        self.deploy_on(script, paths='deploy/**')
        (self.repo / 'deploy').mkdir()
        self.landed(path='deploy/app.yaml')
        self.expect(self.verify(), 'deploy-failed', 12)
        self.assertTrue(marker.exists())

    def assert_deploy_read_required(self, marker, merge):
        report = self.expect(self.verify(), 'unread', 11)
        self.assertEqual(report['deploy'], 'unread')
        self.assertEqual(marker.read_text().splitlines(), [merge])
        self.assertEqual(self.slot()['pr'], 7)

    def verify_deploy_rename(self, source, destination):
        script, marker = self.deploy_script(2)
        self.deploy_on(script, paths='deploy/**')
        self.sb.git(self.repo, 'config', 'diff.renames', 'true')
        (self.repo / source).parent.mkdir()
        (self.repo / destination).parent.mkdir()
        self.sb.commit_file(self.repo, source, 'worker configuration\n', 'add fixture')
        self.sb.git(self.repo, 'mv', '--', source, destination)
        self.sb.git(self.repo, 'commit', '-m', 'rename fixture')
        self.sb.git(self.repo, 'push', 'origin', 'main')
        merge = self.tip()
        self.set_pr(7, 'MERGED', merge=merge)
        self.main_checks('success')
        self.assert_deploy_read_required(marker, merge)

    def test_deploy_read_runs_for_a_rename_out_of_scope(self):
        self.verify_deploy_rename('deploy/app.yml', 'docs/app.yml')

    def test_deploy_read_runs_for_a_rename_into_scope(self):
        self.verify_deploy_rename('docs/app.yml', 'deploy/app.yml')

    def verify_deploy_tree_entry(self, filename, paths='deploy/**'):
        script, marker = self.deploy_script(2)
        self.deploy_on(script, paths=paths)
        base = self.tip()
        base_tree = self.sb.git(self.repo, 'rev-parse', f'{base}^{{tree}}')
        blob = self.sb.git(self.repo, 'rev-parse', f'{base}:README.md').encode()
        git = ['git', '-C', str(self.repo)]

        def tree(entries):
            return subprocess.check_output(git + ['mktree', '-z'], input=entries, env=self.sb.env, timeout=60).strip()

        entries = subprocess.check_output(git + ['ls-tree', '-z', base_tree], env=self.sb.env, timeout=60)
        deploy = tree(b'100644 blob ' + blob + b'\t' + filename + b'\0')
        merged_tree = tree(entries + b'040000 tree ' + deploy + b'\tdeploy\0').decode()
        merge = self.sb.git(self.repo, 'commit-tree', merged_tree, '-p', base, '-m', 'add deploy tree entry')
        # Keep the trusted tip checkoutable even if the filesystem rejects the fixture name.
        main = self.sb.git(self.repo, 'commit-tree', base_tree, '-p', merge, '-m', 'restore checkoutable tip')
        self.sb.git(self.repo, 'push', 'origin', f'{main}:refs/heads/main')
        self.set_pr(7, 'MERGED', merge=merge)
        self.checks(merge, [{'id': 1, 'name': 'build', 'conclusion': 'success'}])
        self.assert_deploy_read_required(marker, merge)

    def test_deploy_read_runs_for_a_non_ascii_path(self):
        self.verify_deploy_tree_entry('café.yml'.encode(), paths='deploy/café.yml')

    def test_deploy_read_runs_for_a_path_with_a_newline(self):
        self.verify_deploy_tree_entry(b'line\nbreak.yml')

    def test_deploy_read_runs_for_a_path_with_an_undecodable_byte(self):
        self.verify_deploy_tree_entry(b'invalid-\xff.yml')

    def test_deploy_read_errors_when_first_parent_is_unavailable(self):
        script, marker = self.deploy_script(2)
        self.deploy_on(script, paths='deploy/**')
        root_commit = self.sb.git(self.repo, 'rev-list', '--max-parents=0', 'HEAD')
        self.set_pr(7, 'MERGED', merge=root_commit)
        self.checks(root_commit, [{'id': 1, 'name': 'build', 'conclusion': 'success'}])
        report = self.expect(self.verify(), 'error', 30)
        self.assertIn('git diff failed', report['detail'])
        self.assertEqual(self.slot()['pr'], 7)
        self.assertFalse(marker.exists())

    def test_deploy_read_errors_when_the_merge_object_is_unavailable(self):
        script, marker = self.deploy_script(2)
        self.deploy_on(script, paths='deploy/**')
        missing = 'a' * 40
        self.set_pr(7, 'MERGED', merge=missing)
        self.checks(missing, [{'id': 1, 'name': 'build', 'conclusion': 'success'}])
        report = self.expect(self.verify(), 'error', 30)
        self.assertIn('git diff failed', report['detail'])
        self.assertEqual(self.slot()['pr'], 7)
        self.assertFalse(marker.exists())

    def test_deploy_verify_none_is_honoured(self):
        script, marker = self.deploy_script(1)
        self.deploy_on(script, verify='none')
        self.landed()
        report = self.expect(self.verify(), 'landed', 0)
        self.assertEqual(report['deploy'], 'n/a')
        self.assertFalse(marker.exists())

    def test_deploy_cannot_tell_is_unread_and_keeps_the_slot(self):
        script, marker = self.deploy_script(2)
        self.deploy_on(script)
        self.landed()
        self.expect(self.verify(), 'unread', 11)
        self.assertEqual(self.slot()['pr'], 7)

    def test_not_merged_keeps_the_slot(self):
        report = self.expect(self.verify(), 'not-merged', 20)
        self.assertIn('OPEN', report['detail'])
        self.assertEqual(self.slot()['pr'], 7)
        self.assertEqual([(e['event'], e['outcome']) for e in self.ledger()],
                         [('ready', 'go'), ('refused', 'not-merged')])

    def test_wrong_head_stops_the_landing(self):
        merge = self.sb.commit_file(self.repo, 'src.txt', 'x', 'squash')
        self.set_pr(7, 'MERGED', OTHER_HEAD, merge)
        report = self.expect(self.verify(), 'wrong-head', 21)
        self.assertIn(OTHER_HEAD, report['detail'])
        self.assertEqual(self.slot()['pr'], 7)
        self.assertEqual([(e['event'], e['outcome']) for e in self.ledger()][-1], ('refused', 'wrong-head'))

    def test_verify_without_a_recorded_landing_is_an_error(self):
        self.slot_file.unlink()
        (self.run_dir / 'landings.jsonl').unlink()
        self.expect(self.verify(), 'error', 30)

    def test_a_failing_gh_is_an_error_with_exit_30(self):
        self.landed()
        (self.sb.gh_dir / 'gh.fail').write_text('')
        self.expect(self.verify(), 'error', 30)
        self.assertEqual(self.ledger()[-1]['event'], 'error')
        self.assertEqual(self.slot()['pr'], 7)

    def test_a_cancelled_main_check_is_rerun_once(self):
        merge = self.merge_on_main(7)
        self.checks(merge, [{'id': 3, 'name': 'build', 'conclusion': 'cancelled'}])
        self.expect(self.verify(), 'landed', 0)
        self.assertEqual(self.reruns(), ['3'])


class DirectModeTest(LandBase):
    def direct(self, command, sha, *extra):
        issues = ['--issues', '11'] if command == 'ready' else []
        return self.land(command, '--repo', self.repo, '--run-dir', self.run_dir, '--sha', sha, *issues, *extra)

    def test_ready_and_verify_take_a_sha_in_place_of_a_pull_request(self):
        sha = self.sb.commit_file(self.repo, 'src.txt', 'x', 'direct push', push=False)
        self.checks(sha, [{'id': 1, 'name': 'build', 'conclusion': 'success'}])
        report = self.expect(self.direct('ready', sha), 'go', 0)
        self.assertNotIn('merge_command', report)
        slot = self.slot()
        self.assertEqual((slot['pr'], slot['head']), (None, sha))
        self.expect(self.direct('verify', sha), 'not-merged', 20)
        self.assertIsNotNone(self.slot())
        self.sb.git(self.repo, 'push', '-q', 'origin', 'main')
        report = self.expect(self.direct('verify', sha), 'landed', 0)
        self.assertEqual((report['main'], report['merge_sha']), ('green', sha))
        self.assertIsNone(self.slot())
        events = self.ledger()
        self.assertTrue(all(e['pr'] is None and e['head'] == sha for e in events))
        self.assertEqual([(e['event'], e['outcome']) for e in events],
                         [('ready', 'go'), ('refused', 'not-merged'), ('merged', None), ('verified', 'landed')])

    def test_direct_ready_holds_on_a_red_main(self):
        self.main_checks('failure')
        self.expect(self.direct('ready', HEAD), 'held', 20)

    def test_direct_ready_is_slot_busy_while_another_direct_landing_holds_it(self):
        held = self.sb.commit_file(self.repo, 'held.txt', 'x', 'unpushed', push=False)
        self.hold_slot(None, held)
        self.expect(self.direct('ready', HEAD), 'slot-busy', 24)


class AbandonAndSummaryTest(LandBase):
    def setUp(self):
        super().setUp()
        self.set_pr(7)
        self.head_checks('success')

    def test_abandon_frees_the_slot_and_records_it(self):
        self.expect(self.ready(), 'go', 0)
        result = self.land('abandon', '--repo', self.repo, '--run-dir', self.run_dir, '--pr', '7',
                           '--reason', 'the review wants changes')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.out(result)['outcome'], 'abandoned')
        self.assertIsNone(self.slot())
        last = self.ledger()[-1]
        self.assertEqual((last['event'], last['outcome'], last['pr'], last['head'], last['issues']),
                         ('abandoned', 'abandoned', 7, HEAD, [11, 12]))
        self.assertIsInstance(last['slot_seconds'], int)
        # The next landing may take the slot.
        self.set_pr(8, 'OPEN', OTHER_HEAD)
        self.head_checks('success', head=OTHER_HEAD)
        self.assertEqual(self.ready(8, OTHER_HEAD).returncode, 0)

    def abandon(self, pr='7', reason='the review wants changes'):
        return self.land('abandon', '--repo', self.repo, '--run-dir', self.run_dir, '--pr', pr, '--reason', reason)

    def test_abandon_writes_its_reason_to_the_ledger_and_an_automatic_one_has_none(self):
        self.expect(self.ready(), 'go', 0)
        self.assertEqual(self.abandon(reason='the review wants changes').returncode, 0)
        last = self.ledger()[-1]
        self.assertEqual((last['event'], last['reason']), ('abandoned', 'the review wants changes'))
        self.assertEqual(list(last)[-1], 'reason')  # additive: every other key keeps its place
        self.assertEqual(list(last)[:-1], LEDGER_KEYS)
        # A stale slot freed by the next `ready` is abandoned with no reason key at all.
        self.set_pr(9, 'OPEN', OTHER_HEAD)
        self.head_checks('success', head=OTHER_HEAD)
        long_ago = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=2)
        self.hold_slot(9, OTHER_HEAD, taken=long_ago)
        self.expect(self.ready(), 'go', 0)
        automatic = [e for e in self.ledger() if e['event'] == 'abandoned'][-1]
        self.assertEqual(automatic['pr'], 9)
        self.assertNotIn('reason', automatic)
        # `summary` reads a ledger with the key and without it.
        result = self.land('summary', '--repo', self.repo, '--run-dir', self.run_dir)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_abandon_refuses_a_pull_request_that_is_already_merged(self):
        self.expect(self.ready(), 'go', 0)
        self.merge_on_main(7)
        before = (self.run_dir / 'landings.jsonl').read_text()
        report = self.expect(self.abandon(), 'error', 30)
        self.assertIn('already merged', report['detail'])
        self.assertIn('run verify', report['detail'])
        self.assertEqual(self.slot()['pr'], 7)
        self.assertEqual([e['event'] for e in self.ledger()], ['ready', 'error'])
        self.assertTrue((self.run_dir / 'landings.jsonl').read_text().startswith(before))

    def test_abandon_leaves_another_landings_slot_alone(self):
        self.hold_slot(9, OTHER_HEAD)
        result = self.land('abandon', '--repo', self.repo, '--run-dir', self.run_dir, '--pr', '7', '--reason', 'x')
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.slot()['pr'], 9)

    def load_module(self):
        sys.path.insert(0, str(BIN))
        try:
            loader = SourceFileLoader('loop_land_under_test', str(LAND))
            module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
            loader.exec_module(module)
        finally:
            sys.path.remove(str(BIN))
        return module

    def test_abandon_whose_release_is_refused_records_nothing_from_the_stale_read(self):
        module = self.load_module()
        self.hold_slot(7, OTHER_HEAD, issues=(21,))
        real = module.release_slot
        other = {'v': 1, 'run_dir': str(self.run_dir.resolve()), 'pr': 8, 'head': HEAD, 'issues': [13],
                 'taken': datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}

        def changed_under_it(path, pr, head):
            path.write_text(json.dumps(other))  # another landing takes the slot after abandon read it
            return real(path, pr, head)

        module.release_slot = changed_under_it
        args = module.argparse.Namespace(repo=str(self.repo), run_dir=str(self.run_dir), pr=7, sha=None,
                                         reason='gave up')
        import contextlib
        import io
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), unittest.mock.patch.dict(os.environ, self.env()):
            self.assertEqual(module.cmd_abandon(args), 0)
        report = json.loads(buffer.getvalue())
        self.assertFalse(report['slot_freed'])
        last = self.ledger()[-1]
        self.assertEqual((last['event'], last['pr'], last['head'], last['slot_seconds']), ('abandoned', 7, None, None))
        self.assertEqual(self.slot()['pr'], 8)

    def summary(self, *extra):
        return self.land('summary', '--repo', self.repo, '--run-dir', self.run_dir, *extra)

    def merged_on_github(self, *numbers):
        self.gh_file('merged.json', [{'number': n, 'headRefName': f'loop7-land{n}', 'mergeCommit': {'oid': 'f' * 40}}
                                     for n in numbers] + [{'number': 99, 'headRefName': 'loop8-land1',
                                                            'mergeCommit': {'oid': 'e' * 40}},
                                                           {'number': 98, 'headRefName': 'feature', 'mergeCommit': None}])

    def test_summary_check_exits_0_with_ok_when_the_ledger_has_every_merge(self):
        self.expect(self.ready(), 'go', 0)
        merge = self.merge_on_main(7)
        self.checks(merge, [{'id': 1, 'name': 'build', 'conclusion': 'success'}])
        self.expect(self.verify(), 'landed', 0)
        self.merged_on_github(7)
        result = self.summary('--check')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('closeout-check: ok', result.stdout)
        self.assertIn('#7', result.stdout)
        self.assertIn('landed', result.stdout)
        last = self.ledger()[-1]
        self.assertEqual((last['event'], last['outcome'], last['missing']), ('closeout-check', 'ok', None))

    def test_summary_check_exits_1_and_names_a_merge_the_ledger_lacks(self):
        self.expect(self.ready(), 'go', 0)
        self.merged_on_github(7, 8)
        result = self.summary('--check')
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('closeout-check: missing #7, #8', result.stdout)
        last = self.ledger()[-1]
        self.assertEqual((last['event'], last['outcome'], last['missing']), ('closeout-check', 'missing', [7, 8]))

    def test_summary_without_check_appends_nothing(self):
        self.expect(self.ready(), 'go', 0)
        before = (self.run_dir / 'landings.jsonl').read_text()
        result = self.summary()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('#7', result.stdout)
        self.assertEqual((self.run_dir / 'landings.jsonl').read_text(), before)

    def test_an_error_event_is_not_a_landings_last_state(self):
        # The Stop hook and loop-pi ignore `error` events when they settle a landing: so does summary.
        self.expect(self.ready(), 'go', 0)
        (self.sb.gh_dir / 'gh.fail').write_text('')
        self.expect(self.verify(), 'error', 30)  # an error event naming pull request 7
        self.assertEqual(self.summary('--check').returncode, 30)  # and one naming no landing at all
        self.assertEqual([e['event'] for e in self.ledger()], ['ready', 'error', 'error'])
        rows = self.summary().stdout.splitlines()
        self.assertEqual(len(rows), 2, rows)
        self.assertEqual(rows[1].split()[:3], ['#7', 'ready', 'go'])
        self.assertNotIn('direct', self.summary().stdout)

    def test_abandon_takes_the_head_and_issues_from_the_last_real_event_not_an_error(self):
        self.expect(self.ready(), 'go', 0)
        (self.sb.gh_dir / 'gh.fail').write_text('')
        self.expect(self.verify(), 'error', 30)
        (self.sb.gh_dir / 'gh.fail').unlink()
        self.slot_file.unlink()
        self.assertEqual(self.abandon().returncode, 0)
        last = self.ledger()[-1]
        self.assertEqual((last['event'], last['head'], last['issues']), ('abandoned', HEAD, [11, 12]))

    def test_summary_of_an_unparseable_ledger_is_an_error(self):
        (self.run_dir / 'landings.jsonl').write_text('not json\n')
        self.assertEqual(self.summary().returncode, 30)

    def test_summary_check_with_a_failing_gh_is_an_error(self):
        (self.sb.gh_dir / 'gh.fail').write_text('')
        result = self.summary('--check')
        self.assertEqual(result.returncode, 30, result.stdout + result.stderr)


class AdoptTest(LandBase):
    """`adopt`: the owner's way out of the two deliberate stop states, with nothing hand-edited."""

    REASON = 'the owner read the merge by hand'

    def adopt(self, pr=7, merged_head=OTHER_HEAD, issues=None, reason=REASON):
        extra = ['--issues', issues] if issues is not None else []
        return self.land('adopt', '--repo', self.repo, '--run-dir', self.run_dir, '--pr', pr,
                         '--merged-head', merged_head, '--reason', reason, *extra)

    def state_a(self):
        """A pull request that merged while ready waited: an error event, no slot, no landing."""
        self.set_pr(7)
        entry = lambda state: {'state': state, 'headRefOid': HEAD, 'mergeCommit': None}
        self.gh_file('prs-seq.json', {'7': [entry('OPEN'), entry('MERGED')]})
        self.head_checks('success')
        self.expect(self.ready(), 'error', 30)
        (self.sb.gh_dir / 'prs-seq.json').unlink()
        return self.merge_on_main(7, head=HEAD)

    def state_b(self):
        """verify stopped on wrong-head: the slot is kept and the ledger has the refusal."""
        self.set_pr(7)
        self.head_checks('success')
        self.expect(self.ready(), 'go', 0)
        merge = self.merge_on_main(7, head=OTHER_HEAD)
        self.expect(self.verify(), 'wrong-head', 21)
        return merge

    def green_main(self, merge):
        self.checks(merge, [{'id': 1, 'name': 'build', 'conclusion': 'success'}])

    def test_after_a_pull_request_merged_while_ready_waited_adopt_lets_verify_land_it(self):
        merge = self.state_a()
        self.green_main(merge)
        before = self.ledger()
        report = self.expect(self.adopt(merged_head=HEAD, issues='11,12'), 'adopted', 0)
        self.assertEqual((report['merge_sha'], report['slot_freed'], report['next']), (merge, False,
                                                                                         'loop-land verify --pr 7'))
        events = self.ledger()
        self.assertEqual(len(events), len(before) + 1, 'adopt writes exactly one event')
        adopted = events[-1]
        self.assertEqual((adopted['event'], adopted['outcome'], adopted['pr'], adopted['head'], adopted['issues'],
                          adopted['merge_sha'], adopted['reason']),
                         ('merged', None, 7, HEAD, [11, 12], merge, self.REASON))
        self.assertFalse([c for c in self.calls() if 'merge' in c[:2]], 'adopt must not call gh pr merge')
        self.assertEqual(self.expect(self.verify(), 'landed', 0)['merge_sha'], merge)
        self.assertEqual([e['event'] for e in self.ledger()], ['error', 'merged', 'verified'])
        self.gh_file('merged.json', [{'number': 7, 'headRefName': 'loop7-land1', 'mergeCommit': {'oid': merge}}])
        result = self.land('summary', '--repo', self.repo, '--run-dir', self.run_dir, '--check')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('closeout-check: ok', result.stdout)

    def test_an_adopt_interrupted_before_its_event_can_be_run_again(self):
        """The slot is freed before the event is written, so no interruption leaves a held slot beside a
        recorded merge (which a retry would call already-recorded, with the slot still naming the old head)."""
        merge = self.state_b()
        self.green_main(merge)
        ledger = self.run_dir / 'landings.jsonl'
        before = ledger.read_text()
        ledger.chmod(0o444)
        try:
            self.assertNotEqual(self.adopt().returncode, 0)
        finally:
            ledger.chmod(0o644)
        self.assertEqual(ledger.read_text(), before)
        self.assertIsNone(self.slot())
        self.expect(self.adopt(), 'adopted', 0)  # the issues now come from the ledger
        self.assertEqual(self.expect(self.verify(), 'landed', 0)['head'], OTHER_HEAD)

    def test_after_wrong_head_adopt_frees_the_slot_and_verify_lands_the_adopted_head(self):
        merge = self.state_b()
        self.green_main(merge)
        self.assertEqual(self.slot()['pr'], 7)
        report = self.expect(self.adopt(), 'adopted', 0)  # the issues come from the slot
        self.assertTrue(report['slot_freed'])
        self.assertIsNone(self.slot())
        adopted = self.ledger()[-1]
        self.assertEqual((adopted['event'], adopted['head'], adopted['issues'], adopted['merge_sha']),
                         ('merged', OTHER_HEAD, [11, 12], merge))
        # The slot is free: a later landing is no longer slot-busy.
        self.set_pr(8, head='3' * 40)
        self.head_checks('success', head='3' * 40)
        self.expect(self.ready(pr=8, head='3' * 40, issues='13'), 'go', 0)
        self.assertEqual(self.slot()['pr'], 8)
        self.assertEqual(self.expect(self.verify(), 'landed', 0)['head'], OTHER_HEAD)
        self.assertEqual(self.slot()['pr'], 8)  # verify of 7 left the other landing's slot alone
        self.assertEqual([e['event'] for e in self.ledger() if e['pr'] == 7],
                         ['ready', 'refused', 'merged', 'verified'])

    def snapshot(self):
        ledger = self.run_dir / 'landings.jsonl'
        return (ledger.read_bytes() if ledger.exists() else None,
                self.slot_file.read_bytes() if self.slot_file.exists() else None)


    def refusal(self, outcome, code, words, *args, **kwargs):
        """Run adopt and show it refused with the ledger and the slot byte-identical."""
        before = self.snapshot()
        report = self.expect(self.adopt(*args, **kwargs), outcome, code)
        for word in words:
            self.assertIn(word, report['detail'])
        self.assertEqual(self.snapshot(), before)

    def test_adopt_refuses_a_pull_request_that_is_still_open(self):
        self.state_b()
        self.set_pr(7, 'OPEN', OTHER_HEAD)
        self.refusal('not-adoptable', 40, ['OPEN'])

    def test_adopt_refuses_a_closed_unmerged_pull_request(self):
        self.state_b()
        self.set_pr(7, 'CLOSED', OTHER_HEAD)
        self.refusal('not-adoptable', 40, ['CLOSED'])

    def test_adopt_refuses_a_head_the_owner_did_not_confirm(self):
        self.state_b()
        self.refusal('not-adoptable', 40, [OTHER_HEAD], merged_head=HEAD)

    def test_adopt_refuses_a_merge_commit_that_is_not_on_main(self):
        self.state_b()
        local = self.sb.commit_file(self.repo, 'local.txt', 'x', 'never pushed', push=False)
        self.set_pr(7, 'MERGED', OTHER_HEAD, local)
        self.refusal('not-adoptable', 40, ["not on origin's main"])

    def test_adopt_refuses_a_merge_commit_this_clone_does_not_have(self):
        self.state_b()
        self.set_pr(7, 'MERGED', OTHER_HEAD, 'c' * 40)
        self.refusal('not-adoptable', 40, ["not on origin's main"])

    def test_adopt_refuses_a_merged_pull_request_with_no_merge_commit(self):
        self.state_b()
        self.set_pr(7, 'MERGED', OTHER_HEAD, None)
        self.refusal('not-adoptable', 40, ['no merge commit'])

    def test_adopt_refuses_a_pull_request_the_ledger_already_has_merged_or_verified(self):
        merge = self.state_b()
        self.green_main(merge)
        self.expect(self.adopt(), 'adopted', 0)
        self.refusal('already-recorded', 41, ['merged event'])
        self.expect(self.verify(), 'landed', 0)
        self.refusal('already-recorded', 41, ['merged event'])  # the merged event is still the first found
        self.slot_file.unlink(missing_ok=True)
        events = [e for e in self.ledger() if e['event'] != 'merged']
        (self.run_dir / 'landings.jsonl').write_text(''.join(json.dumps(e) + '\n' for e in events))
        self.refusal('already-recorded', 41, ['verified event'])

    def test_adopt_refuses_when_the_slot_names_another_landing(self):
        self.state_b()
        self.hold_slot(9, head='3' * 40)
        self.refusal('not-adoptable', 40, ['pull request 9'])

    def test_adopt_refuses_when_the_slot_names_a_direct_landing(self):
        self.state_b()
        self.slot_file.write_text(json.dumps({'v': 1, 'run_dir': str(self.run_dir.resolve()), 'pr': None,
                                              'head': '4' * 40, 'issues': [1], 'taken': '2026-11-19T10:00:00Z'}))
        self.refusal('not-adoptable', 40, ['commit'])

    def test_adopt_refuses_issues_that_contradict_the_recorded_ones(self):
        self.state_b()
        self.refusal('not-adoptable', 40, ['contradicts'], issues='11,99')

    def test_adopt_refuses_when_no_issues_are_known(self):
        self.state_a()
        self.refusal('not-adoptable', 40, ['--issues'])

    def test_adopt_refuses_an_empty_reason_and_a_bad_head_as_usage_errors(self):
        self.state_b()
        before = self.snapshot()
        for result in (self.adopt(reason='   '), self.adopt(merged_head='nope'), self.adopt(issues=',')):
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(self.snapshot(), before)

    def test_adopt_is_listed_with_its_owner_only_warning(self):
        text = self.land('adopt', '--help').stdout
        self.assertIn('Owner only', text)
        self.assertIn('loop root', text)

    def test_the_two_stop_messages_name_the_recovery(self):
        self.set_pr(7)
        self.head_checks('success')
        entry = lambda state: {'state': state, 'headRefOid': HEAD, 'mergeCommit': None}
        self.gh_file('prs-seq.json', {'7': [entry('OPEN'), entry('MERGED')]})
        self.assertIn('loop-land adopt', self.expect(self.ready(), 'error', 30)['detail'])
        (self.sb.gh_dir / 'prs-seq.json').unlink()
        self.set_pr(8, head=HEAD)
        self.head_checks('success')
        self.expect(self.ready(pr=8), 'go', 0)
        merge = self.sb.commit_file(self.repo, 'w.txt', 'x', 'squash')
        self.set_pr(8, 'MERGED', OTHER_HEAD, merge)
        self.assertIn('loop-land adopt', self.expect(self.verify(pr=8), 'wrong-head', 21)['detail'])


class LedgerAndSourceTest(unittest.TestCase):
    def test_the_sample_ledger_validates_against_the_schema(self):
        events = [json.loads(line) for line in FIXTURE.read_text().splitlines()]
        self.assertGreaterEqual(len(events), 10)
        self.assertEqual({e['event'] for e in events}, set(OUTCOMES))
        for event in events:
            self.assertEqual(validate_event(event), [], event)

    def test_the_validator_refuses_a_broken_event(self):
        good = json.loads(FIXTURE.read_text().splitlines()[0])
        self.assertEqual(validate_event(good), [])
        self.assertTrue(validate_event({**good, 'outcome': 'merged'}))
        self.assertTrue(validate_event({k: v for k, v in good.items() if k != 'missing'}))
        self.assertTrue(validate_event({**good, 'ts': '2026-11-10 09:00'}))
        self.assertTrue(validate_event({**good, 'reason': 'only abandoned and merged events carry one'}))
        merged = json.loads(FIXTURE.read_text().splitlines()[1])
        self.assertEqual(validate_event({**merged, 'reason': 'adopted by the owner'}), [])
        self.assertTrue(validate_event({**merged, 'reason': ' '}))

    def source(self):
        return LAND.read_text(encoding='utf-8')

    def strings(self, tree):
        """Every string constant that is not a docstring."""
        docstrings = {id(node.body[0].value) for node in ast.walk(tree)
                      if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef))
                      and node.body and isinstance(node.body[0], ast.Expr)
                      and isinstance(node.body[0].value, ast.Constant) and isinstance(node.body[0].value.value, str)}
        return [(node.value, node) for node in ast.walk(tree)
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings]

    def test_the_tool_contains_no_merge_command(self):
        tree = ast.parse(self.source())
        allowed = {'gh pr merge {pr} --squash --match-head-commit {head}'}
        for value, node in self.strings(tree):
            if value in allowed:
                continue
            self.assertNotEqual(value.strip(), 'merge', f'line {node.lineno}')
            self.assertIsNone(re.search(r'\bpr\s+merge\b|/merge\b|--auto\b|--squash\b|--rebase\b|--admin\b', value),
                              f'line {node.lineno}: {value!r}')
        # The one advised command is printed for the root and never passed to a process or to gh.
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                called = ast.unparse(node.func)
                if called.split('.')[-1] in ('run', 'gh', 'git', 'gh_json', 'call', 'check_call', 'Popen',
                                             'check_output', 'system', 'execv'):
                    self.assertNotIn('MERGE_ADVICE', ast.unparse(node), f'line {node.lineno}')
                    for sub in ast.walk(node):
                        if isinstance(sub, (ast.List, ast.Tuple)):
                            self.assertNotIn('merge', [e.value for e in sub.elts if isinstance(e, ast.Constant)],
                                             f'line {node.lineno}')

    def test_the_merge_advice_is_only_printed(self):
        uses = [n for n in ast.walk(ast.parse(self.source())) if isinstance(n, ast.Name) and n.id == 'MERGE_ADVICE']
        self.assertEqual(len(uses), 2)  # the assignment and the one format() in ready

    def test_loop_lint_imports_the_shared_checks_and_no_longer_defines_them(self):
        lint = (BIN / 'loop-lint').read_text(encoding='utf-8')
        self.assertRegex(lint, r'(?m)^from loop_checks import')
        self.assertNotRegex(lint, r'(?m)^def (required_checks|main_required_state)\b')
        checks = ast.parse((BIN / 'loop_checks.py').read_text(encoding='utf-8'))
        self.assertTrue({'required_checks', 'main_required_state'} <= {
            n.name for n in checks.body if isinstance(n, ast.FunctionDef)})


if __name__ == '__main__':
    unittest.main()
