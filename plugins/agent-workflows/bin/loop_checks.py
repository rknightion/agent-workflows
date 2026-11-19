"""Shared reads for the loop tools (`loop-lint` and `loop-land`): the command keys of a repository's
`LOOP.md` and of a Standing file's `## Commands` section (one reader, so the lint and the tool see the
same keys), the repository's required checks and the state of those checks on a commit or branch head.

Everything GitHub-facing here decides on HTTP status codes and check conclusions, never on message text. A read that
cannot be made raises GhError (NotOnGitHub for an origin that is not github.com). Only `success` is
green; pending, missing, skipped or neutral decide nothing.
"""
import csv
import json
import re
import subprocess
from urllib.parse import quote

# A github.com remote in any of its usual spellings: https, git and ssh URLs (ssh.github.com on a port
# too), the scp-like form (a user name, an at sign, the host, a colon, then owner and repository) and a
# bare host followed by owner and repository.
GITHUB_REMOTE = re.compile(r'^(?:(?:https?|ssh|git)://(?:[^@/]+@)?(?:ssh\.)?github\.com(?::\d+)?/'
                           r'|[^@/:]+@(?:ssh\.)?github\.com:|github\.com[:/])'
                           r'([^/\s]+/[^/\s]+?)(?:\.git)?/?$', re.IGNORECASE)
COMMAND_KEYS = ('ci-required', 'ci-status', 'ci-max-minutes', 'deploy-on-push', 'deploy-paths', 'deploy-verify')
STANDING_VALUE = re.compile(r'^(/\S+)\s+sha256=([0-9a-f]{64})$')
FAILURE_CONCLUSIONS = ('failure', 'timed_out', 'startup_failure')
CANCELLED_CONCLUSIONS = ('cancelled', 'stale')


class GhError(Exception):
    pass


class NotOnGitHub(GhError):
    pass


# --- command keys ----------------------------------------------------------------------------

def unwrap(value):
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '`':
        value = value[1:-1].strip()
    return value


def key_lines(numbered):
    """{key: (value, line number, raw value)} for the `key: value` lines of `numbered`, a list of
    (number, line); the first of each key counts and fenced lines are ignored. A list bullet and
    backticks round the key are tolerated; the value is everything after `key:`, unwrapped when it is
    wholly wrapped in backticks. The raw value keeps its backticks, so a lint can tell a wrapped value
    from one with text after it. An indented line is never a key."""
    keys, fence = {}, None
    for number, line in numbered:
        stripped = line.lstrip()
        marker = stripped[:3] if stripped[:3] in ('```', '~~~') else None
        if fence is None and marker:
            fence = marker
            continue
        if fence is not None:
            if marker == fence:
                fence = None
            continue
        match = re.match(r'^(?:[-*+]\s+)?`?([a-z][a-z0-9-]*)`?:\s*(.*?)\s*$', line)
        if match and match.group(1) not in keys:
            keys[match.group(1)] = (unwrap(match.group(2)), number, match.group(2))
    return keys


def key_block(lines):
    """{key: value} of `lines`, as key_lines reads them."""
    return {k: v for k, (v, _, _) in key_lines(list(enumerate(lines, 1))).items()}


def opening_entries(text):
    """{key: (value, line, raw value)}: every key in the opening block of a LOOP.md or a charter,
    before its first `## ` heading outside a fence."""
    head, fence = [], None
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.lstrip()
        marker = stripped[:3] if stripped[:3] in ('```', '~~~') else None
        if fence is None and marker:
            fence = marker
        elif fence is not None and marker == fence:
            fence = None
        elif fence is None and line.startswith('## '):
            break
        head.append((number, line))
    return key_lines(head)


def opening_keys(text):
    """({key: value}, {key: line}) of opening_entries."""
    found = opening_entries(text)
    return {k: v for k, (v, _, _) in found.items()}, {k: n for k, (_, n, _) in found.items()}


def commands_body(text):
    """[(line number, line)] of a Standing file's `## Commands` section, fence lines included."""
    body, inside, fence = [], False, None
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.lstrip()
        marker = stripped[:3] if stripped[:3] in ('```', '~~~') else None
        if fence is None and marker:
            fence = marker
        elif fence is not None and marker == fence:
            fence = None
        if fence is None and not marker and line.startswith('## '):
            inside = line[3:].strip() == 'Commands'
            continue
        if inside:
            body.append((number, line))
    return body


def commands_entries(text):
    """{key: (value, line, raw value)} of a Standing file's `## Commands` section."""
    return key_lines(commands_body(text))


def commands_keys(text):
    """({key: value}, {key: line}): every key in a Standing file's `## Commands` section, not only the
    command keys, so a caller can see what else the section holds."""
    found = commands_entries(text)
    return {k: v for k, (v, _, _) in found.items()}, {k: n for k, (_, n, _) in found.items()}


def loopmd_commands(text):
    """The command keys in the opening block of a LOOP.md."""
    return {k: v for k, v in opening_keys(text)[0].items() if k in COMMAND_KEYS}


def standing_commands(text):
    """The command keys in a Standing file's `## Commands` section."""
    return {k: v for k, v in commands_keys(text)[0].items() if k in COMMAND_KEYS}


def split_globs(value):
    """Comma-separated globs; a comma inside `{...}` belongs to the glob."""
    items, depth, start = [], 0, 0
    for i in range(len(value) + 1):
        c = value[i] if i < len(value) else None
        if c == '{':
            depth += 1
        elif c == '}':
            depth = max(0, depth - 1)
        elif (c == ',' and depth == 0) or c is None:
            item = unwrap(value[start:i])
            if item:
                items.append(item)
            start = i + 1
    return items


# --- git and gh ------------------------------------------------------------------------------

def run(argv, cwd=None, *, text=True):
    try:
        return subprocess.run(argv, cwd=cwd, capture_output=True, text=text, timeout=120)
    except (OSError, subprocess.SubprocessError) as error:
        raise GhError(f'{argv[0]} failed to run: {error}')


def origin_slug(repo):
    """owner/repo of a github.com origin. NotOnGitHub when origin is anywhere else, since GitHub then
    holds no branch protection to read and the required checks are unknowable."""
    result = run(['git', '-C', str(repo), 'config', '--get', 'remote.origin.url'])
    if result.returncode != 0:
        raise GhError('no git remote named origin')
    url = result.stdout.strip()
    match = GITHUB_REMOTE.match(url)
    if not match:
        raise NotOnGitHub(f'origin {url!r} is not a github.com remote')
    return match.group(1)


def gh_api_status(endpoint):
    """(HTTP status, parsed body, message) for `gh api -i <endpoint>`. Decided on the status code,
    never on text in the output; no status line at all (gh failed before a response) is an error."""
    result = run(['gh', 'api', '-i', endpoint])
    head, _, body = result.stdout.replace('\r\n', '\n').partition('\n\n')
    status = re.match(r'^HTTP/\S+ (\d{3})', head)
    if not status:
        raise GhError(f'gh api {endpoint} gave no HTTP response: '
                      f'{(result.stderr or result.stdout).strip()[:200]}')
    try:
        data = json.loads(body) if body.strip() else None
    except ValueError:
        raise GhError(f'gh api {endpoint} returned invalid JSON')
    message = data.get('message', '') if isinstance(data, dict) else ''
    return int(status.group(1)), data, message


def ci_required_names(value):
    """The check names a `ci-required` value declares: comma-separated, a name holding a comma in
    double quotes, or `none`."""
    if value.strip().lower() == 'none':
        return set()
    return {c.strip() for c in next(csv.reader([value], skipinitialspace=True), []) if c.strip()}


def required_checks(repo, branch="main"):
    """Required status checks for the landing branch: active rulesets plus classic
    branch protection. The rulesets endpoint answers 200 for any repository gh can see, so anything
    else is an error; the only 404 read as empty is classic protection's `Branch not protected`."""
    slug = origin_slug(repo)
    ref = quote(branch, safe="")
    names = set()
    status, data, message = gh_api_status(f'repos/{slug}/rules/branches/{ref}?per_page=100')
    if status != 200 or not isinstance(data, list):
        raise GhError(f'rulesets for {slug} {branch}: HTTP {status} {message}'.rstrip()
                      + ' (gh cannot see the repository or branch)')
    for rule in data:
        if isinstance(rule, dict) and rule.get('type') == 'required_status_checks':
            checks = (rule.get('parameters') or {}).get('required_status_checks') or []
            names.update(c.get('context') for c in checks if c.get('context'))
    status, data, message = gh_api_status(f'repos/{slug}/branches/{ref}/protection/required_status_checks')
    if status == 200 and isinstance(data, dict):
        names.update(data.get('contexts') or [])
        names.update(c.get('context') for c in data.get('checks') or [] if c.get('context'))
    elif not (status == 404 and message == 'Branch not protected'):
        raise GhError(f'branch protection for {slug} {branch}: HTTP {status} {message}'.rstrip())
    return names


def read_check_inputs(repo, required, ref):
    """(latest run per required name, legacy statuses) on `ref`, a branch or a commit. GhError when
    either cannot be read."""
    slug = origin_slug(repo)
    quoted = quote(ref, safe='')
    result = run(['gh', 'api', '--paginate', f'repos/{slug}/commits/{quoted}/check-runs?per_page=100',
                  '--jq', '.check_runs[] | {id, name, status, conclusion}'], cwd=str(repo))
    if result.returncode != 0:
        raise GhError(f'check runs on {ref}: {(result.stderr or result.stdout).strip()[:200]}')
    latest = {}
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except ValueError:
            raise GhError('check runs on main returned invalid JSON')
        name = item.get('name') if isinstance(item, dict) else None
        if name in required and (name not in latest or (item.get('id') or 0) > (latest[name].get('id') or 0)):
            latest[name] = item
    # Legacy commit statuses: a required name may report only here, or here as well as a check run.
    result = run(['gh', 'api', f'repos/{slug}/commits/{quoted}/status?per_page=100'], cwd=str(repo))
    if result.returncode != 0:
        raise GhError(f'commit statuses on {ref}: {(result.stderr or result.stdout).strip()[:200]}')
    try:
        statuses = json.loads(result.stdout or '{}').get('statuses') or []
    except (ValueError, AttributeError):
        raise GhError('commit statuses on main returned invalid JSON')
    return latest, statuses


def main_required_state(repo, required, branch="main"):
    """('red'|'green'|'undecided', detail) for the latest run of each required check on the landing head,
    read from the check runs and the legacy commit statuses; a name red in either is red. Only `success` is green; pending, missing, skipped or neutral decide
    nothing. GhError when the check runs cannot be read."""
    latest, statuses = read_check_inputs(repo, required, branch)
    outcomes = {}
    for name, r in latest.items():
        done = r.get('status') == 'completed'
        outcomes.setdefault(name, []).append(
            'red' if done and r.get('conclusion') in FAILURE_CONCLUSIONS
            else 'green' if done and r.get('conclusion') == 'success' else 'undecided')
    for item in statuses:
        name = item.get('context') if isinstance(item, dict) else None
        if name in required:
            state = item.get('state')
            outcomes.setdefault(name, []).append(
                'red' if state in ('failure', 'error') else 'green' if state == 'success' else 'undecided')
    red = sorted(n for n, o in outcomes.items() if 'red' in o)
    if red:
        return 'red', ', '.join(red)
    if set(outcomes) == required and all(set(o) == {'green'} for o in outcomes.values()):
        return 'green', 'every required check succeeded'
    return 'undecided', 'required checks pending, missing or not a pass'


def required_check_states(repo, required, ref):
    """{name: {'state': 'green'|'red'|'cancelled'|'undecided', 'id': check run id or None}} for each
    required check that has reported on `ref`, a branch or a commit. A check run that was cancelled or
    superseded (`cancelled`, `stale`) is its own state so a caller can rerun it; a name red in a legacy
    status is red. For a check run the id is the id of the latest run, and for GitHub Actions that is
    the job id a rerun needs. GhError when the reads fail."""
    latest, statuses = read_check_inputs(repo, required, ref)
    states = {}
    for name, r in latest.items():
        done = r.get('status') == 'completed'
        conclusion = r.get('conclusion')
        states[name] = {
            'state': 'red' if done and conclusion in FAILURE_CONCLUSIONS
            else 'green' if done and conclusion == 'success'
            else 'cancelled' if done and conclusion in CANCELLED_CONCLUSIONS else 'undecided',
            'id': r.get('id')}
    for item in statuses:
        name = item.get('context') if isinstance(item, dict) else None
        if name in required:
            state = item.get('state')
            legacy = 'red' if state in ('failure', 'error') else 'green' if state == 'success' else 'undecided'
            current = states.get(name)
            if current is None:
                states[name] = {'state': legacy, 'id': None}
            elif legacy == 'red' or (current['state'] == 'green' and legacy != 'green'):
                current['state'] = legacy if legacy == 'red' else 'undecided'
    return states
