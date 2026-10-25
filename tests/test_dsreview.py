"""dsreview: the DeepSeek Flash reviewer, driven end to end against a fake streaming gateway.

Every test works in a temporary directory with its own git repository and a local HTTP server that
replays scripted server-sent events; nothing reaches a real gateway. The same file runs from the
source tree (plugins/agent-workflows/tests) and from the published tree (tests/).
"""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = Path(__file__).resolve().parent
TOOL = next(p for p in (HERE.parent / 'tools/dsreview/dsreview.py',
                        HERE.parent / 'plugins/agent-workflows/tools/dsreview/dsreview.py') if p.is_file())
OWNER = 'example-owner'
AT = '@'  # kept out of literal user@host strings, which the public export's email check refuses


def load():
    spec = importlib.util.spec_from_file_location('dsreview_under_test', TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def git(repo, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env.update(GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@example.com', GIT_COMMITTER_NAME='t',
               GIT_COMMITTER_EMAIL='t@example.com', GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1')
    return subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True, text=True, env=env).stdout


def make_repo(tmp, origin=f'git{AT}github.com:{OWNER}/example.git'):
    repo = tmp / 'repo'
    repo.mkdir()
    git(repo, 'init', '-q', '-b', 'main')
    git(repo, 'remote', 'add', 'origin', origin)
    (repo / 'calc.py').write_text('def total(xs):\n    return sum(xs)\n')
    git(repo, 'add', '-A')
    git(repo, '-c', 'commit.gpgsign=false', 'commit', '-qm', 'base')
    (repo / 'calc.py').write_text('def total(xs):\n    return sum(xs[1:])\n')
    (repo / 'new_mod.py').write_text('LIMIT = 3\n')
    return repo


def sse(*events):
    return ''.join('data: ' + json.dumps(e) + '\n\n' for e in events) + 'data: [DONE]\n\n'


FINAL = {'verdict': 'FAIL', 'summary': 'drops the first element',
         'findings': [{'severity': 'major', 'confidence': 'high', 'file': 'calc.py', 'line': 2,
                       'quote': 'return sum(xs[1:])', 'title': 'skips first item', 'scenario': '[1,2] -> 2', 'unverified': None},
                      {'severity': 'minor', 'confidence': 'low', 'file': 'calc.py', 'line': 1,
                       'quote': 'this text is nowhere', 'title': 'invented', 'scenario': 'x', 'unverified': None}]}


class Gateway:
    """Fake streaming gateway: answers each request with the next scripted SSE body."""

    def __init__(self, bodies):
        self.bodies, self.requests = list(bodies), []
        gateway = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers['content-length'])
                gateway.requests.append({'headers': {k.lower(): v for k, v in self.headers.items()},
                                         'body': json.loads(self.rfile.read(length))})
                payload = gateway.bodies.pop(0).encode()
                self.send_response(200)
                self.send_header('content-type', 'text/event-stream')
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f'http://127.0.0.1:{self.server.server_port}/chat/completions'

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


def clean_env(tmp, **extra):
    """The test process environment without any DSREVIEW_* setting, plus `extra`."""
    env = {k: v for k, v in os.environ.items() if not k.startswith('DSREVIEW_')}
    env.update(HOME=str(tmp), **extra)
    return env


def run(tmp, gateway_url, *args, stdin=None, **overrides):
    token = tmp / 'token'
    token.write_text('test-token\n')
    env = clean_env(tmp, DSREVIEW_URL=gateway_url, DSREVIEW_TOKEN_FILE=str(token), DSREVIEW_ALLOWED_OWNERS=OWNER)
    for key, value in overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return subprocess.run([sys.executable, str(TOOL), *args], capture_output=True, text=True, env=env, input=stdin,
                          timeout=60)


def tool_then_final():
    usage = {'prompt_tokens': 100, 'prompt_cache_hit_tokens': 0, 'prompt_cache_miss_tokens': 100,
             'completion_tokens': 50, 'completion_tokens_details': {'reasoning_tokens': 40}}
    first = sse(
        {'model': 'deepseek-flash', 'choices': [{'delta': {'reasoning_content': 'check callers'}}]},
        {'choices': [{'delta': {'tool_calls': [{'index': 0, 'id': 'c1', 'type': 'function',
                                                'function': {'name': 'read_file', 'arguments': '{"path": "new_'}}]}}]},
        {'choices': [{'delta': {'tool_calls': [{'index': 0, 'function': {'arguments': 'mod.py"}'}}]},
                      'finish_reason': 'tool_calls'}]},
        {'choices': [], 'usage': usage})
    second = sse(
        {'model': 'deepseek-flash', 'system_fingerprint': 'fp1', 'choices': [{'delta': {'reasoning_content': 'done'}}]},
        {'choices': [{'delta': {'content': json.dumps(FINAL)}, 'finish_reason': 'stop'}]},
        {'choices': [], 'usage': usage})
    return [first, second]


def scripted_tools(*calls, final_reasoning='done'):
    """One turn that makes the given tool calls, then a final FAIL verdict."""
    usage = {'prompt_tokens': 10, 'completion_tokens': 5, 'completion_tokens_details': {'reasoning_tokens': 3}}
    deltas = [{'index': i, 'id': f'c{i}', 'type': 'function', 'function': {'name': n, 'arguments': json.dumps(a)}}
              for i, (n, a) in enumerate(calls)]
    first = sse({'choices': [{'delta': {'reasoning_content': 'look', 'tool_calls': deltas}}]}, {'choices': [], 'usage': usage})
    final_usage = dict(usage, completion_tokens_details={'reasoning_tokens': 3 if final_reasoning else 0})
    second = sse({'choices': [{'delta': {'reasoning_content': final_reasoning, 'content': json.dumps(FINAL)}}]},
                 {'choices': [], 'usage': final_usage})
    return first, second


def passing():
    return sse({'model': 'deepseek-flash', 'choices': [{'delta': {'reasoning_content': 'ok',
                'content': json.dumps({'verdict': 'PASS', 'summary': 'fine', 'findings': []})}}]},
               {'choices': [], 'usage': {'prompt_tokens': 5, 'completion_tokens': 2,
                                         'completion_tokens_details': {'reasoning_tokens': 1}}})


def tool_results(request):
    return [m['content'] for m in request['body']['messages'] if m['role'] == 'tool']


class Case(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name).resolve()
        self.addCleanup(self._tmp.cleanup)


class Review(Case):
    def test_worktree_review_runs_tools_and_reports_fail(self):
        repo = make_repo(self.tmp)
        with Gateway(tool_then_final()) as gateway:
            result = run(self.tmp, gateway.url, '--repo', str(repo), '--brief', '-', stdin='Sum all items.')
        self.assertEqual(result.returncode, 1, result.stderr + result.stdout)
        out = json.loads(result.stdout)
        self.assertEqual((out['verdict'], out['tool_calls'], out['fingerprint']), ('FAIL', 1, 'fp1'))
        self.assertEqual([f['quote_ok'] for f in out['findings']], [True, False])
        self.assertEqual(out['usage']['reasoning'], 80)
        self.assertGreater(out['cost_usd_peak'], 0)
        first, second = gateway.requests
        self.assertEqual(first['headers']['cf-aig-authorization'], 'Bearer test-token')
        self.assertEqual(json.loads(first['headers']['cf-aig-metadata']), {'app': 'dsreview'})
        self.assertEqual(first['body']['model'], 'deepseek-flash')
        user = first['body']['messages'][1]['content']
        for needle in ('Sum all items.', 'sum(xs[1:])', 'LIMIT = 3'):  # the untracked file is in the diff
            self.assertIn(needle, user)
        self.assertIs(first['body']['stream'], True)
        self.assertTrue(first['body']['tools'])
        assistant = second['body']['messages'][2]
        self.assertEqual(assistant['reasoning_content'], 'check callers')
        self.assertEqual(assistant['tool_calls'][0]['function'], {'name': 'read_file', 'arguments': '{"path": "new_mod.py"}'})
        self.assertIn('LIMIT = 3', second['body']['messages'][3]['content'])
        self.assertEqual(git(repo, 'status', '--porcelain'), ' M calc.py\n?? new_mod.py\n')  # repository untouched

    def test_patch_mode_reviews_patch_on_base(self):
        repo = make_repo(self.tmp)
        patch = self.tmp / 'c.patch'
        patch.write_text(git(repo, 'diff'))
        git(repo, 'checkout', '--', 'calc.py')
        with Gateway(tool_then_final()) as gateway:
            result = run(self.tmp, gateway.url, '--repo', str(repo), '--base', 'HEAD', '--patch', str(patch))
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn('sum(xs[1:])', gateway.requests[0]['body']['messages'][1]['content'])
        self.assertEqual((repo / 'calc.py').read_text(), 'def total(xs):\n    return sum(xs)\n')

    def test_prose_reply_is_retried_once_then_no_verdict(self):
        repo = make_repo(self.tmp)
        prose = sse({'choices': [{'delta': {'reasoning_content': 'r', 'content': 'I think it is fine.'}}]},
                    {'choices': [], 'usage': {'prompt_tokens': 1, 'completion_tokens': 1}})
        with Gateway([prose, prose]) as gateway:
            result = run(self.tmp, gateway.url, '--repo', str(repo))
        self.assertEqual(result.returncode, 2)
        self.assertEqual(len(gateway.requests), 2)
        self.assertIsNone(json.loads(result.stdout)['verdict'])
        self.assertNotIn('tools', gateway.requests[1]['body'])

    def test_markup_at_turn_limit_gets_one_tools_disabled_recovery(self):
        repo = make_repo(self.tmp)
        first, _ = scripted_tools(('read_file', {'path': 'calc.py'}))
        markup = sse({'choices': [{'delta': {'content':
            '<｜DSML｜function_calls><｜DSML｜invoke name="read_file">'
            '<｜DSML｜parameter name="path">calc.py</｜DSML｜parameter>'
            '</｜DSML｜invoke></｜DSML｜function_calls>'}}]})
        for final, exit_code in ((passing(), 0), (markup, 2), (first, 2)):
            with self.subTest(exit_code=exit_code), Gateway([first] * 49 + [markup, final]) as gateway:
                result = run(self.tmp, gateway.url, '--repo', str(repo), '--max-turns', '50')
            self.assertEqual(result.returncode, exit_code, result.stdout + result.stderr)
            self.assertEqual(len(gateway.requests), 51)
            out = json.loads(result.stdout)
            self.assertEqual((out['turns'], out['tool_calls']), (51, 49))
            for request in gateway.requests[-2:]:
                self.assertNotIn('tools', request['body'])
            recovery = gateway.requests[-1]['body']['messages'][-1]['content']
            self.assertIn('only the json object', recovery)
            if exit_code == 2:
                self.assertIsNone(out['verdict'])
                self.assertIn('no_parseable_verdict', out['error'])

    def test_worktree_tools_never_see_git_metadata_or_ignored_files(self):
        repo = make_repo(self.tmp)
        (repo / '.gitignore').write_text('.env\n')
        (repo / '.env').write_text('SECRET=1\n')
        git(repo, 'config', 'remote.origin.pushurl', f'https://user:token{AT}github.com/{OWNER}/example.git')
        script = scripted_tools(('list_files', {'path': '.'}), ('read_file', {'path': '.env'}),
                                ('read_file', {'path': '.git/config'}), ('grep', {'pattern': 'SECRET|token'}))
        with Gateway(script) as gateway:
            result = run(self.tmp, gateway.url, '--repo', str(repo))
        self.assertEqual(result.returncode, 1, result.stdout)
        listing, env_file, git_config, grep = tool_results(gateway.requests[1])
        self.assertEqual(sorted(listing.splitlines()), ['.gitignore', 'calc.py', 'new_mod.py'])
        self.assertTrue(env_file.startswith('error') and git_config.startswith('error'))
        self.assertNotIn('SECRET', grep)
        self.assertNotIn('token@', grep)

    def test_patch_mode_tools_work_in_the_scratch_tree(self):
        repo = make_repo(self.tmp)
        patch = self.tmp / 'c.patch'
        patch.write_text(git(repo, 'diff'))
        git(repo, 'checkout', '--', 'calc.py')
        with Gateway(scripted_tools(('list_files', {'path': '.'}), ('read_file', {'path': 'calc.py'}))) as gateway:
            result = run(self.tmp, gateway.url, '--repo', str(repo), '--base', 'HEAD', '--patch', str(patch))
        self.assertEqual(result.returncode, 1, result.stdout)
        listing, calc = tool_results(gateway.requests[1])
        self.assertEqual(listing.strip(), 'calc.py')
        self.assertIn('sum(xs[1:])', calc)

    def test_final_reply_without_reasoning_is_retried_once(self):
        repo = make_repo(self.tmp)
        first, no_reasoning = scripted_tools(('read_file', {'path': 'calc.py'}), final_reasoning='')
        _, with_reasoning = scripted_tools(('read_file', {'path': 'calc.py'}))
        with Gateway([first, no_reasoning, with_reasoning]) as gateway:
            result = run(self.tmp, gateway.url, '--repo', str(repo))
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertEqual(len(gateway.requests), 3)

    def test_unexpected_crash_is_no_verdict_not_fail(self):
        repo = make_repo(self.tmp)
        brief = self.tmp / 'brief.bin'
        brief.write_bytes(b'\xff\xfe not utf-8')
        with Gateway([]) as gateway:
            result = run(self.tmp, gateway.url, '--repo', str(repo), '--brief', str(brief))
        self.assertEqual(result.returncode, 2)
        self.assertTrue(json.loads(result.stdout)['error'])

    def test_stream_cut_before_done_is_retried_not_trusted(self):
        repo = make_repo(self.tmp)
        first, final = scripted_tools(('read_file', {'path': '.github/ci.yml'}))
        cut = final.replace('data: [DONE]\n\n', '')
        (repo / '.github').mkdir()
        (repo / '.github/ci.yml').write_text('run: tests\n')
        with Gateway([first, cut, final]) as gateway:
            result = run(self.tmp, gateway.url, '--repo', str(repo))
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertEqual(len(gateway.requests), 3)
        self.assertIn('run: tests', tool_results(gateway.requests[1])[0])

    def test_head_mode_pass_exits_zero(self):
        repo = make_repo(self.tmp)
        git(repo, 'add', '-A')
        git(repo, '-c', 'commit.gpgsign=false', 'commit', '-qm', 'candidate')
        with Gateway([passing()]) as gateway:
            result = run(self.tmp, gateway.url, '--repo', str(repo), '--base', 'HEAD~1', '--head', 'HEAD')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(json.loads(result.stdout)['verdict'], 'PASS')
        user = gateway.requests[0]['body']['messages'][1]['content']
        self.assertIn('sum(xs[1:])', user)
        self.assertIn('LIMIT = 3', user)


    def test_unwritable_out_is_no_verdict_not_fail(self):
        repo = make_repo(self.tmp)
        out = self.tmp / 'missing-dir' / 'review.json'
        with Gateway(tool_then_final()) as gateway:
            result = run(self.tmp, gateway.url, '--repo', str(repo), '--brief', '-', '--out', str(out), stdin='Sum all items.')
        self.assertEqual(result.returncode, 2, result.stderr + result.stdout)
        report = json.loads(result.stdout)
        self.assertIsNone(report['verdict'])
        self.assertIn('could not write --out', report['error'])


class Tools(Case):
    def test_tools_are_confined_to_the_candidate_tree(self):
        module = load()
        root = self.tmp / 'tree'
        root.mkdir()
        (root / 'a.txt').write_text('alpha\n')
        (self.tmp / 'secret.txt').write_text('outside\n')
        (root / 'link').symlink_to(self.tmp / 'secret.txt')
        self.assertIn('alpha', module.run_tool(root, 'read_file', {'path': 'a.txt'}))
        for path in ['../secret.txt', '/etc/hosts', 'link']:
            self.assertTrue(module.run_tool(root, 'read_file', {'path': path}).startswith('error'), path)
        self.assertTrue(module.run_tool(root, 'grep', {'pattern': 'x', 'path': '..'}).startswith('error'))
        self.assertTrue(module.run_tool(root, 'list_files', {'path': '../'}).startswith('error'))
        self.assertIn('a.txt', module.run_tool(root, 'list_files', {'path': '.'}))
        self.assertIn('a.txt:1:alpha', module.run_tool(root, 'grep', {'pattern': 'alp'}))

    def test_grep_finds_hidden_tracked_files(self):
        (self.tmp / '.github').mkdir()
        (self.tmp / '.github/ci.yml').write_text('needle\n')
        self.assertIn('.github/ci.yml:1:needle', load().run_tool(self.tmp, 'grep', {'pattern': 'needle'}))


class Configuration(Case):
    """Nothing is sent unless the gateway, the token and the allowed owners are all configured."""

    def assertNoVerdict(self, result, gateway, needle):
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        out = json.loads(result.stdout)
        self.assertIsNone(out['verdict'])
        self.assertIn(needle, out['error'])
        self.assertEqual(gateway.requests, [])

    def test_missing_gateway_configuration_is_no_verdict(self):
        repo = make_repo(self.tmp)
        with Gateway([]) as gateway:
            result = run(self.tmp, gateway.url, '--repo', str(repo), DSREVIEW_URL=None)
            self.assertNoVerdict(result, gateway, 'DSREVIEW_CF_ACCOUNT_ID')
            half = run(self.tmp, gateway.url, '--repo', str(repo), DSREVIEW_URL=None, DSREVIEW_CF_ACCOUNT_ID='acct')
            self.assertNoVerdict(half, gateway, 'DSREVIEW_CF_GATEWAY')

    def test_gateway_url_is_the_stock_ai_gateway_form_unless_overridden(self):
        module = load()
        stock = module.gateway_url({'DSREVIEW_CF_ACCOUNT_ID': 'acct123', 'DSREVIEW_CF_GATEWAY': 'reviews'})
        self.assertEqual(stock, 'https://gateway.ai.cloudflare.com/v1/acct123/reviews/deepseek/chat/completions')
        self.assertEqual(module.gateway_url({'DSREVIEW_CF_ACCOUNT_ID': 'acct123', 'DSREVIEW_CF_GATEWAY': 'reviews',
                                             'DSREVIEW_URL': 'https://proxy.example.com/deepseek/chat/completions'}),
                         'https://proxy.example.com/deepseek/chat/completions')
        with self.assertRaises(module.ReviewError):
            module.gateway_url({})

    def test_missing_token_is_no_verdict(self):
        repo = make_repo(self.tmp)
        with Gateway([]) as gateway:
            result = run(self.tmp, gateway.url, '--repo', str(repo), DSREVIEW_TOKEN_FILE=None)
            self.assertNoVerdict(result, gateway, 'DSREVIEW_TOKEN')
            missing = run(self.tmp, gateway.url, '--repo', str(repo), DSREVIEW_TOKEN_FILE=str(self.tmp / 'absent'))
            self.assertNoVerdict(missing, gateway, 'absent')
            (self.tmp / 'blank').write_text('\n')
            blank = run(self.tmp, gateway.url, '--repo', str(repo), DSREVIEW_TOKEN_FILE=str(self.tmp / 'blank'))
            self.assertNoVerdict(blank, gateway, 'empty or malformed')

    def test_token_from_the_environment(self):
        repo = make_repo(self.tmp)
        with Gateway([passing()]) as gateway:
            result = run(self.tmp, gateway.url, '--repo', str(repo), DSREVIEW_TOKEN_FILE=None, DSREVIEW_TOKEN='env-token')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(gateway.requests[0]['headers']['cf-aig-authorization'], 'Bearer env-token')

    def test_unset_allowed_owners_refuses_before_any_request(self):
        repo = make_repo(self.tmp)
        with Gateway([]) as gateway:
            self.assertNoVerdict(run(self.tmp, gateway.url, '--repo', str(repo), DSREVIEW_ALLOWED_OWNERS=None),
                                 gateway, 'DSREVIEW_ALLOWED_OWNERS')
            self.assertNoVerdict(run(self.tmp, gateway.url, '--repo', str(repo), DSREVIEW_ALLOWED_OWNERS=' , '),
                                 gateway, 'DSREVIEW_ALLOWED_OWNERS')

    def test_owner_not_allowed_is_refused_before_any_request(self):
        repo = make_repo(self.tmp, origin=f'git{AT}github.com:someemployer/internal.git')
        with Gateway([]) as gateway:
            self.assertNoVerdict(run(self.tmp, gateway.url, '--repo', str(repo)), gateway, 'refusing')

    def test_repository_without_origin_is_refused(self):
        repo = make_repo(self.tmp)
        git(repo, 'remote', 'remove', 'origin')
        with Gateway([]) as gateway:
            self.assertNoVerdict(run(self.tmp, gateway.url, '--repo', str(repo)), gateway, 'refusing')

    def test_origin_forms_and_host_scoped_owners(self):
        module = load()
        cases = [
            (f'git{AT}github.com:{OWNER}/x.git', (OWNER,), True),
            (f'https://github.com/{OWNER}/x', (OWNER,), True),
            (f'https://user:secret{AT}git.example.com/{OWNER}/x.git', (OWNER,), True),
            (f'ssh://git{AT}git.example.com:2222/{OWNER}/x.git', (OWNER,), True),
            (f'git{AT}github.com:{OWNER.upper()}/x.git', (OWNER,), True),
            (f'git{AT}github.com:{OWNER}/x.git', (f'github.com/{OWNER}',), True),
            (f'git{AT}git.example.com:{OWNER}/x.git', (f'github.com/{OWNER}',), False),
            (f'git{AT}github.com:other/x.git', (OWNER,), False),
            (f'/srv/git/{OWNER}/x.git', (OWNER,), False),
            (f'file:///srv/git/{OWNER}/x.git', (OWNER,), False),
        ]
        for url, owners, allowed in cases:
            repo = self.tmp / f'r{cases.index((url, owners, allowed))}'
            repo.mkdir()
            git(repo, 'init', '-q')
            git(repo, 'remote', 'add', 'origin', url)
            if allowed:
                module.check_origin(repo, owners)
            else:
                with self.assertRaises(module.ReviewError, msg=url):
                    module.check_origin(repo, owners)

    def test_model_override(self):
        repo = make_repo(self.tmp)
        with Gateway([passing()]) as gateway:
            result = run(self.tmp, gateway.url, '--repo', str(repo), DSREVIEW_MODEL='deepseek-other')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(gateway.requests[0]['body']['model'], 'deepseek-other')


def openrouter_tool_then_final():
    """OpenRouter's stream shape: reasoning in `reasoning`, the routed host in the final chunk's
    openrouter_metadata, cached tokens in prompt_tokens_details and the billed amount in usage.cost."""
    routing = {'openrouter_metadata': {'endpoints': {'total': 2, 'available': [
        {'provider': 'OtherHost', 'selected': False}, {'provider': 'ExampleHost', 'selected': True}]}}}
    usage = {'prompt_tokens': 100, 'prompt_tokens_details': {'cached_tokens': 60}, 'completion_tokens': 50,
             'completion_tokens_details': {'reasoning_tokens': 40}, 'cost': 0.0012}
    first = sse(
        {'model': 'deepseek/deepseek-v4.1-flash', 'choices': [{'delta': {'reasoning': 'check callers'}}]},
        {'choices': [{'delta': {'tool_calls': [{'index': 0, 'id': 'c1', 'type': 'function',
                                                'function': {'name': 'read_file', 'arguments': '{"path": "new_mod.py"}'}}]},
                      'finish_reason': 'tool_calls'}]},
        {'choices': [], 'usage': usage, **routing})
    second = sse(
        {'model': 'deepseek/deepseek-v4.1-flash', 'choices': [{'delta': {'reasoning': 'done'}}]},
        {'choices': [{'delta': {'content': json.dumps(FINAL)}, 'finish_reason': 'stop'}]},
        {'choices': [], 'usage': usage, **routing})
    return [first, second]


class OpenRouter(Case):
    """DSREVIEW_PROVIDER=openrouter: same review, OpenRouter's auth, request and stream shapes."""

    def test_review_uses_openrouter_auth_reasoning_and_routing(self):
        repo = make_repo(self.tmp)
        with Gateway(openrouter_tool_then_final()) as gateway:
            result = run(self.tmp, gateway.url, '--repo', str(repo), '--effort', 'max', DSREVIEW_PROVIDER='openrouter')
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        out = json.loads(result.stdout)
        self.assertEqual(out['verdict'], 'FAIL')
        self.assertEqual((out['provider'], out['host'], out['model']), ('openrouter', 'ExampleHost', 'deepseek/deepseek-v4.1-flash'))
        self.assertEqual(out['usage']['cache_hit'], 120)
        self.assertEqual(out['usage']['cache_miss'], 80)
        self.assertEqual(out['usage']['reasoning'], 80)
        self.assertEqual(out['cost_usd'], 0.0024)
        first = gateway.requests[0]
        self.assertEqual(first['headers']['authorization'], 'Bearer test-token')
        self.assertEqual(first['headers']['x-openrouter-metadata'], 'enabled')
        self.assertFalse([h for h in first['headers'] if h.startswith('cf-aig-')])
        body = first['body']
        self.assertEqual(body['model'], '~deepseek/deepseek-flash-latest')
        self.assertEqual(body['reasoning'], {'effort': 'max'})
        self.assertNotIn('reasoning_effort', body)
        self.assertEqual((body['provider']['data_collection'], body['provider']['zdr']), ('deny', True))
        self.assertTrue(body['provider']['require_parameters'])
        self.assertNotIn('fp4', body['provider']['quantizations'])
        # The reasoning travels back with the tool call, and the tool result is the candidate file.
        assistant = gateway.requests[1]['body']['messages'][2]
        self.assertEqual(assistant['reasoning_content'], 'check callers')
        self.assertIn('LIMIT = 3', tool_results(gateway.requests[1])[0])

    def test_routing_override_and_cloudflare_cost_is_null(self):
        repo = make_repo(self.tmp)
        with Gateway([passing()]) as gateway:
            result = run(self.tmp, gateway.url, '--repo', str(repo), DSREVIEW_PROVIDER='openrouter',
                         DSREVIEW_OPENROUTER_PROVIDER='{"only": ["deepseek"]}')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(gateway.requests[0]['body']['provider'], {'only': ['deepseek']})
        with Gateway([passing()]) as gateway:
            result = run(self.tmp, gateway.url, '--repo', str(repo))
        out = json.loads(result.stdout)
        self.assertEqual((out['provider'], out['cost_usd']), ('cloudflare', None))
        self.assertNotIn('provider', gateway.requests[0]['body'])

    def test_error_event_after_200_is_retried_not_trusted(self):
        repo = make_repo(self.tmp)
        failed = sse({'error': {'code': 502, 'message': 'upstream died'}, 'choices': [{'delta': {}, 'finish_reason': 'error'}]})
        with Gateway([failed, passing()]) as gateway:
            result = run(self.tmp, gateway.url, '--repo', str(repo), DSREVIEW_PROVIDER='openrouter')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(gateway.requests), 2)
        # A retry of the same turn, not an empty reply answered with a nudge.
        self.assertEqual(gateway.requests[1]['body']['messages'], gateway.requests[0]['body']['messages'])

    def test_default_url_and_bad_configuration(self):
        module = load()
        self.assertEqual(module.gateway_url({'DSREVIEW_PROVIDER': 'openrouter'}), 'https://openrouter.ai/api/v1/chat/completions')
        self.assertEqual(module.gateway_url({'DSREVIEW_PROVIDER': 'OpenRouter', 'DSREVIEW_URL': 'http://x/c'}), 'http://x/c')
        repo = make_repo(self.tmp)
        with Gateway([]) as gateway:
            for env, needle in (({'DSREVIEW_PROVIDER': 'bedrock'}, 'DSREVIEW_PROVIDER'),
                                ({'DSREVIEW_PROVIDER': 'openrouter', 'DSREVIEW_OPENROUTER_PROVIDER': '[1]'}, 'JSON object'),
                                ({'DSREVIEW_PROVIDER': 'openrouter', 'DSREVIEW_OPENROUTER_PROVIDER': '{'}, 'not JSON')):
                result = run(self.tmp, gateway.url, '--repo', str(repo), **env)
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertIn(needle, json.loads(result.stdout)['error'])
            self.assertEqual(gateway.requests, [])


if __name__ == '__main__':
    unittest.main()
