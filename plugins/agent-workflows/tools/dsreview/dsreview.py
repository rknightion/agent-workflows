#!/usr/bin/env python3
"""dsreview: code review by DeepSeek Flash (Cloudflare AI Gateway or OpenRouter) with read-only repo tools.

Reviews one candidate change against its brief and prints a JSON verdict:
  dsreview --brief brief.md                       # uncommitted worktree in cwd vs HEAD
  dsreview --repo R --base B --head H --brief -   # committed range, brief on stdin
  dsreview --repo R --base B --patch P.patch      # a patch applied to B in a scratch tree
Exit status: 0 PASS, 1 FAIL, 2 no verdict or error.

Configuration (environment):
  DSREVIEW_PROVIDER         cloudflare (default) or openrouter
  DSREVIEW_URL              full chat completions URL; overrides the defaults below
  DSREVIEW_CF_ACCOUNT_ID    Cloudflare account id   } gateway.ai.cloudflare.com/v1/<account>/<gateway>/
  DSREVIEW_CF_GATEWAY       AI Gateway name         } deepseek/chat/completions
                            (openrouter: https://openrouter.ai/api/v1/chat/completions)
  DSREVIEW_TOKEN            AI Gateway token (cf-aig-authorization) or OpenRouter API key, or
  DSREVIEW_TOKEN_FILE       a file holding it
  DSREVIEW_ALLOWED_OWNERS   comma list of repository owners (or host/owner) whose code may be
                            sent to DeepSeek; unset refuses every repository
  DSREVIEW_MODEL            model name (default deepseek-flash; openrouter ~deepseek/deepseek-flash-latest)
  DSREVIEW_OPENROUTER_PROVIDER  openrouter only: JSON provider routing object replacing the default
"""
import argparse
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request

CF_GATEWAY = 'https://gateway.ai.cloudflare.com/v1/{account_id}/{gateway}/deepseek/chat/completions'
OPENROUTER_URL = 'https://openrouter.ai/api/v1/chat/completions'
DEFAULT_MODEL = {'cloudflare': 'deepseek-flash', 'openrouter': '~deepseek/deepseek-flash-latest'}
# OpenRouter hosts: only those that honour every parameter sent (tools, reasoning), keep no copy of
# the code (zero data retention) and never train on it, and serve the model at fp8 or better.
OPENROUTER_ROUTING = {'require_parameters': True, 'data_collection': 'deny', 'zdr': True,
                      'quantizations': ['fp8', 'fp16', 'bf16', 'fp32', 'unknown']}
MAX_DIFF_CHARS = 1_500_000
TOOL_RESULT_CHARS = 40_000
# USD per million tokens, peak rates (off-peak is half); used for the cost estimate only.
PRICE = {'hit': 0.006, 'miss': 0.30, 'out': 1.20}

TOOLS = [
    {'type': 'function', 'function': {
        'name': 'read_file', 'description': 'Read a file in the repository (candidate state) with line numbers, at most 400 lines per call.',
        'parameters': {'type': 'object', 'properties': {'path': {'type': 'string'}, 'start': {'type': 'integer'}, 'end': {'type': 'integer'}},
                       'required': ['path']}}},
    {'type': 'function', 'function': {
        'name': 'grep', 'description': 'Search the repository with a regex. Returns path:line:text, at most 200 lines.',
        'parameters': {'type': 'object', 'properties': {'pattern': {'type': 'string'}, 'path': {'type': 'string', 'description': 'optional subdirectory or file'},
                                                        'glob': {'type': 'string', 'description': 'optional file glob such as *.go'}},
                       'required': ['pattern']}}},
    {'type': 'function', 'function': {
        'name': 'list_files', 'description': 'List files under a directory, recursively, at most 300.',
        'parameters': {'type': 'object', 'properties': {'path': {'type': 'string'}}, 'required': ['path']}}},
]


class ReviewError(Exception):
    pass


def git(repo, *args, binary=False, ok=(0,)):
    r = subprocess.run(['git', '-C', str(repo), *args], capture_output=True)
    if r.returncode not in ok:
        raise ReviewError(f'git {" ".join(args)} failed: {r.stderr.decode(errors="replace").strip()[:300]}')
    return r.stdout if binary else r.stdout.decode('utf-8', 'replace')


def provider(environ):
    name = (environ.get('DSREVIEW_PROVIDER') or 'cloudflare').strip().lower()
    if name not in DEFAULT_MODEL:
        raise ReviewError(f'unknown DSREVIEW_PROVIDER {name!r}: use cloudflare or openrouter')
    return name


def openrouter_routing(environ):
    raw = (environ.get('DSREVIEW_OPENROUTER_PROVIDER') or '').strip()
    if not raw:
        return OPENROUTER_ROUTING
    try:
        routing = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ReviewError(f'DSREVIEW_OPENROUTER_PROVIDER is not JSON: {e}')
    if not isinstance(routing, dict):
        raise ReviewError('DSREVIEW_OPENROUTER_PROVIDER must be a JSON object')
    return routing


def gateway_url(environ):
    """The chat completions URL: DSREVIEW_URL, else OpenRouter's or the stock AI Gateway URL."""
    url = (environ.get('DSREVIEW_URL') or '').strip()
    if url:
        return url
    if provider(environ) == 'openrouter':
        return OPENROUTER_URL
    account, gateway = (environ.get('DSREVIEW_CF_ACCOUNT_ID') or '').strip(), (environ.get('DSREVIEW_CF_GATEWAY') or '').strip()
    if account and gateway:
        return CF_GATEWAY.format(account_id=account, gateway=gateway)
    raise ReviewError('no gateway configured: set DSREVIEW_CF_ACCOUNT_ID and DSREVIEW_CF_GATEWAY, or DSREVIEW_URL')


def gateway_token(environ, token_file=None):
    """The AI Gateway token: the --token-file file, else DSREVIEW_TOKEN, else the DSREVIEW_TOKEN_FILE file."""
    token = None if token_file else environ.get('DSREVIEW_TOKEN')
    where = 'DSREVIEW_TOKEN'
    if not token:
        token_file = token_file or environ.get('DSREVIEW_TOKEN_FILE')
        if not token_file:
            raise ReviewError('no gateway token: set DSREVIEW_TOKEN or DSREVIEW_TOKEN_FILE')
        token, where = Path(token_file).read_text(), token_file
    token = token.strip()
    if not token or any(c.isspace() for c in token):
        raise ReviewError(f'gateway token in {where} is empty or malformed')
    return token


def allowed_owners(environ):
    owners = tuple(o.strip().lower() for o in (environ.get('DSREVIEW_ALLOWED_OWNERS') or '').split(',') if o.strip())
    if not owners:
        raise ReviewError('refusing: DSREVIEW_ALLOWED_OWNERS is not set; list the repository owners whose code '
                          'may be sent to DeepSeek (comma separated, owner or host/owner)')
    return owners


# scp form (git@host:owner/repo) or URL form (scheme://[user[:pass]@]host[:port]/owner/repo).
ORIGIN = re.compile(r'(?:[a-z][a-z0-9+.-]*://)?(?:[^@/]+@)?([^/:@]+)(?::\d+)?[:/]+([^/]+)/', re.I)


def check_origin(repo, owners):
    """Refuse unless origin's owner (on any host) or host/owner is in the allowed list."""
    url = git(repo, 'remote', 'get-url', 'origin', ok=(0, 2)).strip()
    m = ORIGIN.match(url)
    if m:
        host, owner = m.group(1).lower(), m.group(2).lower()
        if owner in owners or f'{host}/{owner}' in owners:
            return
    raise ReviewError(f'refusing: origin {url or "(none)"} is not owned by {", ".join(owners)}')


def confined(root, rel):
    target = (root / (rel or '.')).resolve()
    if not target.is_relative_to(root.resolve()):
        raise ReviewError('path is outside the repository')
    return target


def run_tool(root, name, args):
    try:
        if name == 'read_file':
            path = confined(root, args.get('path'))
            lines = path.read_text(errors='replace').splitlines()
            start = max(1, int(args.get('start') or 1))
            end = min(len(lines), int(args.get('end') or start + 399), start + 399)
            return '\n'.join(f'{i:5d}  {lines[i - 1]}' for i in range(start, end + 1)) or '(empty file or range)'
        if name == 'grep':
            where = confined(root, args.get('path'))
            if shutil.which('rg'):
                cmd = ['rg', '-n', '--no-heading', '--hidden', '-g', '!.git', '-m', '50', '-e', str(args['pattern'])]
                if args.get('glob'):
                    cmd += ['-g', str(args['glob'])]
            else:
                cmd = ['grep', '-rnE', '--exclude-dir=.git', '-m', '50', '-e', str(args['pattern'])]
                if args.get('glob'):
                    cmd += ['--include', str(args['glob'])]
            out = subprocess.run(cmd + [str(where)], capture_output=True, text=True, timeout=60).stdout
            lines = out.replace(str(root.resolve()) + '/', '').replace(str(root) + '/', '').splitlines()
            more = f'\n... {len(lines) - 200} more lines' if len(lines) > 200 else ''
            return '\n'.join(lines[:200]) + more if lines else '(no matches)'
        if name == 'list_files':
            base = confined(root, args.get('path'))
            skip = {'.git', 'node_modules', '.venv', 'vendor'}
            files = sorted(str(p.relative_to(root)) for p in base.rglob('*') if p.is_file() and not skip & set(p.relative_to(root).parts))
            more = f'\n... {len(files) - 300} more files' if len(files) > 300 else ''
            return '\n'.join(files[:300]) + more if files else '(no files)'
    except ReviewError as e:
        return f'error: {e}'
    except Exception as e:  # a tool error is information for the model, not a crash
        return f'error: {type(e).__name__}: {e}'
    return f'error: unknown tool {name}'


def candidate(args, scratch):
    """Return (root of the candidate tree, unified diff of the change)."""
    repo = args.repo
    if args.patch:
        patch = Path(args.patch).read_text()
        tarfile.open(fileobj=io.BytesIO(git(repo, 'archive', args.base, binary=True))).extractall(scratch, filter='data')
        r = subprocess.run(['git', 'apply', '--whitespace=nowarn', '-'], cwd=scratch, input=patch.encode(), capture_output=True)
        if r.returncode:
            raise ReviewError(f'patch does not apply to {args.base}: {r.stderr.decode(errors="replace")[:300]}')
        return scratch, patch
    if args.head:
        tarfile.open(fileobj=io.BytesIO(git(repo, 'archive', args.head, binary=True))).extractall(scratch, filter='data')
        return scratch, git(repo, 'diff', '--no-color', args.base, args.head)
    diff = git(repo, 'diff', '--no-color', args.base)
    for rel in git(repo, 'ls-files', '--others', '--exclude-standard', '-z').split('\0'):
        if rel:
            diff += git(repo, 'diff', '--no-color', '--no-index', '--', '/dev/null', rel, ok=(0, 1))
    # Tools see a copy of tracked and non-ignored files only: never .git or ignored secrets such as .env.
    for rel in git(repo, 'ls-files', '-co', '--exclude-standard', '-z').split('\0'):
        src = repo / rel
        if rel and src.is_file() and not src.is_symlink():
            dst = scratch / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    return scratch, diff


def request_headers(name, token):
    headers = {'content-type': 'application/json', 'user-agent': 'dsreview/1'}
    if name == 'openrouter':
        # Router metadata names the host that served the request, on the stream's final chunk.
        headers.update({'authorization': 'Bearer ' + token, 'x-title': 'dsreview', 'x-openrouter-metadata': 'enabled'})
    else:
        headers.update({'cf-aig-authorization': 'Bearer ' + token, 'cf-aig-skip-cache': 'true',
                        'cf-aig-metadata': json.dumps({'app': 'dsreview'})})
    return headers


def served_by(event):
    """The OpenRouter host named by a stream event's router metadata, if it carries any."""
    routing = event.get('openrouter_metadata') or {}
    for endpoint in (routing.get('endpoints') or {}).get('available') or []:
        if isinstance(endpoint, dict) and endpoint.get('selected'):
            return endpoint.get('provider')
    served = [a for a in routing.get('attempts') or [] if isinstance(a, dict) and a.get('status') == 200]
    return served[-1].get('provider') if served else event.get('provider')


def stream_chat(url, headers, body, timeout):
    """POST a streaming chat completion; return the assembled assistant message and usage."""
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers)
    content, reasoning, calls, usage, meta, finish = [], [], {}, {}, {}, None
    done = False
    with urllib.request.urlopen(req, timeout=timeout) as r:
        for raw in r:
            line = raw.decode('utf-8', 'replace').strip()
            if not line.startswith('data:'):
                continue
            data = line[5:].strip()
            if data == '[DONE]':
                done = True
                break
            try:
                ev = json.loads(data)
            except json.JSONDecodeError:
                raise ConnectionError('truncated stream event')
            if ev.get('error'):  # OpenRouter reports a failure after the 200 as an error event
                raise ConnectionError(f'stream error: {json.dumps(ev["error"])[:300]}')
            meta['model'] = ev.get('model', meta.get('model'))
            meta['host'] = served_by(ev) or meta.get('host')
            meta['fingerprint'] = ev.get('system_fingerprint', meta.get('fingerprint'))
            usage = ev.get('usage') or usage
            for choice in ev.get('choices') or []:
                delta = choice.get('delta') or {}
                content.append(delta.get('content') or '')
                reasoning.append(delta.get('reasoning_content') or delta.get('reasoning') or '')
                for tc in delta.get('tool_calls') or []:
                    slot = calls.setdefault(tc.get('index', 0), {'id': '', 'type': 'function', 'function': {'name': '', 'arguments': ''}})
                    slot['id'] = tc.get('id') or slot['id']
                    fn = tc.get('function') or {}
                    slot['function']['name'] += fn.get('name') or ''
                    slot['function']['arguments'] += fn.get('arguments') or ''
                finish = choice.get('finish_reason') or finish
    if not done:
        raise ConnectionError('stream ended before [DONE]')
    message = {'role': 'assistant', 'content': ''.join(content)}
    if any(reasoning):
        message['reasoning_content'] = ''.join(reasoning)
    if calls:
        message['tool_calls'] = [calls[i] for i in sorted(calls)]
    return message, usage, meta, finish


def call(url, headers, body, timeout):
    last = None
    for attempt in range(3):
        try:
            return stream_chat(url, headers, body, timeout)
        except urllib.error.HTTPError as e:
            last = f'HTTP {e.code}: {e.read()[:300]!r}'
            if e.code in (400, 401, 402, 403, 404):
                break
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            last = str(e)
        time.sleep(5 * (attempt + 1))
    raise ReviewError(f'gateway call failed: {last}')


def parse_review(text):
    s = (text or '').strip()
    i, j = s.find('{'), s.rfind('}')
    if i < 0 or j <= i:
        return None
    try:
        review = json.loads(s[i:j + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(review, dict) or review.get('verdict') not in ('PASS', 'FAIL') or not isinstance(review.get('findings', []), list):
        return None
    review['findings'] = [f for f in review.get('findings', []) if isinstance(f, dict)]
    return review


def anchor(review, root, diff):
    """Mark each finding whose quote is not in the diff or the cited file (unanchored = likely invented)."""
    norm = lambda s: ' '.join((s or '').split())
    corpus = norm(diff.replace('\n+', '\n').replace('\n-', '\n'))
    for f in review.get('findings', []):
        quote = norm(f.get('quote'))
        text = corpus
        try:
            text += ' ' + norm(confined(root, f.get('file')).read_text(errors='replace'))
        except Exception:
            pass
        f['quote_ok'] = bool(quote) and quote in text


def review(args):
    if args.base is None:
        args.base = 'HEAD'
    owners = allowed_owners(os.environ)
    name = provider(os.environ)
    routing = openrouter_routing(os.environ) if name == 'openrouter' else None
    url = args.url or gateway_url(os.environ)
    model = (os.environ.get('DSREVIEW_MODEL') or '').strip() or DEFAULT_MODEL[name]
    args.repo = Path(git(args.repo, 'rev-parse', '--show-toplevel').strip())
    check_origin(args.repo, owners)
    brief = sys.stdin.read() if args.brief == '-' else Path(args.brief).read_text() if args.brief else '(no brief supplied)'
    headers = request_headers(name, gateway_token(os.environ, args.token_file))
    system = Path(args.prompt).read_text()
    scratch = Path(tempfile.mkdtemp(prefix='dsreview-')).resolve()  # /var is a symlink on macOS
    try:
        root, diff = candidate(args, scratch)
        if not diff.strip():
            raise ReviewError('the candidate has no changes against the base')
        if len(diff) > MAX_DIFF_CHARS:
            raise ReviewError(f'diff is {len(diff)} characters; split the review (limit {MAX_DIFF_CHARS})')
        messages = [{'role': 'system', 'content': system},
                    {'role': 'user', 'content': f'<brief>\n{brief}\n</brief>\n\n<diff>\n{diff}\n</diff>\n\n'
                                                'Review the diff against the brief, using the tools as needed, then return the json object.'}]
        totals = {'prompt': 0, 'cache_hit': 0, 'cache_miss': 0, 'output': 0, 'reasoning': 0}
        t0, tool_calls, meta, result, nudged, billed = time.time(), 0, {}, None, False, None
        # One recovery turn is available even after the forced final answer.
        for turn in range(1, args.max_turns + 2):
            body = {'model': model, 'max_tokens': 64000, 'messages': messages,
                    'stream': True, 'stream_options': {'include_usage': True}}
            if name == 'openrouter':
                body.update(reasoning={'effort': args.effort}, provider=routing)
            else:
                body['reasoning_effort'] = args.effort
            if turn < args.max_turns and not nudged:
                body.update(tools=TOOLS, tool_choice='auto')
            message, usage, m, _ = call(url, headers, body, args.timeout)
            meta.update({k: v for k, v in m.items() if v})
            # DeepSeek reports prompt_cache_hit_tokens; OpenRouter reports prompt_tokens_details.cached_tokens.
            hit = usage.get('prompt_cache_hit_tokens', (usage.get('prompt_tokens_details') or {}).get('cached_tokens') or 0)
            totals['prompt'] += usage.get('prompt_tokens', 0)
            totals['cache_hit'] += hit
            totals['cache_miss'] += usage.get('prompt_cache_miss_tokens', usage.get('prompt_tokens', 0) - hit)
            if isinstance(usage.get('cost'), (int, float)):  # OpenRouter's billed amount
                billed = (billed or 0) + usage['cost']
            totals['output'] += usage.get('completion_tokens', 0)
            totals['reasoning'] += (usage.get('completion_tokens_details') or {}).get('reasoning_tokens', 0)
            messages.append(message)  # reasoning_content must travel back when tools are in play
            if message.get('tool_calls') and turn < args.max_turns and not nudged:
                for tc in message['tool_calls']:
                    try:
                        targs = json.loads(tc['function']['arguments'] or '{}')
                    except json.JSONDecodeError:
                        targs = {}
                    messages.append({'role': 'tool', 'tool_call_id': tc['id'],
                                     'content': run_tool(root, tc['function']['name'], targs)[:TOOL_RESULT_CHARS]})
                    tool_calls += 1
                if args.verbose:
                    print(f'dsreview: turn {turn}, {tool_calls} tool calls', file=sys.stderr)
                if turn == args.max_turns - 6:
                    messages.append({'role': 'user', 'content': 'Few tool calls remain. Finish verifying, then return the json object.'})
                continue
            # Never trust a verdict bundled with an unexpected tool request.
            result = None if message.get('tool_calls') else parse_review(message.get('content'))
            if result and (usage.get('completion_tokens_details') or {}).get('reasoning_tokens', 0) > 0:
                break
            if nudged:
                break
            # Recover once from prose, leaked tool markup, or a reply that skips thinking.
            nudged = True
            messages.append({'role': 'user', 'content': 'Tools are disabled. Return only the json object described in your instructions. '
                                                        'Do not emit tool calls, markup, or prose.'})
        if result:
            anchor(result, root, diff)
        cost = (totals['cache_hit'] * PRICE['hit'] + totals['cache_miss'] * PRICE['miss'] + totals['output'] * PRICE['out']) / 1e6
        return {
            'verdict': result.get('verdict') if result else None,
            'summary': result.get('summary') if result else None,
            'findings': result.get('findings', []) if result else [],
            'reviewer': 'dsreview', 'provider': name, 'host': meta.get('host'),
            'model': meta.get('model'), 'fingerprint': meta.get('fingerprint'),
            'effort': args.effort, 'turns': turn, 'tool_calls': tool_calls, 'elapsed_s': round(time.time() - t0, 1),
            'usage': totals, 'cost_usd_peak': round(cost, 4),
            'cost_usd': round(billed, 4) if billed is not None else None,
            'error': None if result else 'no_parseable_verdict: JSON-only recovery failed: ' + (messages[-1].get('content') or '')[-300:],
        }
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def main(argv=None):
    here = Path(__file__).resolve().parent
    ap =argparse.ArgumentParser(prog='dsreview', description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--repo', type=Path, default=Path.cwd(), help='repository (default: cwd)')
    ap.add_argument('--base', help='base revision the change applies to (default: HEAD)')
    group = ap.add_mutually_exclusive_group()
    group.add_argument('--head', help='review the committed range base..head')
    group.add_argument('--patch', help='review a patch file applied to base in a scratch tree')
    ap.add_argument('--brief', help='file holding the lane brief, or - for stdin')
    ap.add_argument('--effort', default='high', choices=['low', 'high', 'max'])
    ap.add_argument('--max-turns', type=int, default=50, help='review turns (default: 50), plus at most one JSON-only recovery turn')
    ap.add_argument('--timeout', type=int, default=900, help='seconds per model call')
    ap.add_argument('--out', type=Path, help='also write the JSON here')
    ap.add_argument('--verbose', action='store_true')
    ap.add_argument('--url', help=argparse.SUPPRESS)  # default: DSREVIEW_URL, else the provider's URL
    ap.add_argument('--token-file', help=argparse.SUPPRESS)  # default: DSREVIEW_TOKEN, then DSREVIEW_TOKEN_FILE
    ap.add_argument('--prompt', default=str(here / 'system.md'), help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    if args.head and not args.base:
        ap.error('--head needs --base')
    if args.patch and not args.base:
        ap.error('--patch needs --base')
    try:
        out = review(args)
    except (ReviewError, OSError) as e:
        out = {'verdict': None, 'reviewer': 'dsreview', 'error': str(e), 'findings': []}
    except Exception as e:  # any crash is "no verdict" (exit 2), never FAIL
        out = {'verdict': None, 'reviewer': 'dsreview', 'error': f'{type(e).__name__}: {e}', 'findings': []}
    text = json.dumps(out, indent=2)
    if args.out:
        try:
            args.out.write_text(text + '\n')
        except OSError as e:  # an unwritten verdict is no verdict (exit 2), never FAIL
            out = {**out, 'verdict': None, 'error': f'could not write --out {args.out}: {e}'}
            text = json.dumps(out, indent=2)
    print(text)
    return {'PASS': 0, 'FAIL': 1}.get(out.get('verdict'), 2)


if __name__ == '__main__':
    sys.exit(main())
