#!/usr/bin/env python3
"""Authenticated Kodus component review, with an immutable candidate for independent local review."""
import argparse
import bisect
import calendar
import codecs
import hashlib
import http.client
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

VERSION = '2'
MIN_GIT = (2, 40)
# The launcher pins an absolute Git (Homebrew on macOS, PATH Git on Linux) as KODUS_REVIEW_GIT.
GIT = os.environ.get('KODUS_REVIEW_GIT') or 'git'
MAX_PATCH_BYTES = 1500000
MAX_FILE_PATCH_BYTES = 500000
MAX_SENT_FILES = 500
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_IGNORES_BYTES = 1000000
# One constant per artifact bound, enforced by capture where it writes the artifact and by verify
# where it reads it, so nothing capture accepts can fail verify on size.
MAX_BRIEF_BYTES = 100000
MAX_FILES_JSON_BYTES = 64 * 1024 * 1024
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_SCOPE_JSON_BYTES = 32 * 1024 * 1024
MAX_SNAPSHOT_ENTRIES = 200000
MAX_EVIDENCE_BYTES = 64 * 1024 * 1024
# Room kept in the evidence for the Kodus enqueue, terminal and result payloads.
EVIDENCE_RESPONSE_RESERVE = 40 * 1024 * 1024
MAX_PATTERNS = 500
MAX_FULL_PATCH_BYTES = 64 * 1024 * 1024
MAX_RENDER_BYTES = 1024 * 1024
MAX_CONTENT_CHARS = 2000000
RENDER_TIMEOUT = 30
EMPTY_BLOB = 'e69de29bb2d1d6434b8b29ae775ad8c2e48c5391'
NULL_BLOB = '0' * 40
IGNORES_SCHEMA = 'kodus-review-ignores/2'
IGNORES_KEYS = ('schema', 'source', 'commit', 'repository', 'patterns', 'context_include', 'same_directory',
                'generated_markers', 'send_changed_content', 'secret_add', 'max_context_file_bytes',
                'snapshot_budget_bytes', 'kodus_content_bytes')
SCOPE_CEILINGS = {'max_context_file_bytes': 2 * 1024 * 1024, 'snapshot_budget_bytes': 48 * 1024 * 1024,
                  'kodus_content_bytes': 4 * 1024 * 1024}
SKIP_ALL_IGNORED = 'all-changed-paths-ignored'
GLOB_CHARS = '*?[]'
# Copy detection threshold (Git's own default): a changed file at least this similar to any
# secret-pattern blob in either full tree is withheld as a secret copy.
SECRET_COPY_SIMILARITY = 50
# Explicit bounds on copy detection (secret blobs x size-window candidates, and Git's -l limit);
# exceeding either is no verdict, never an unchecked pass.
MAX_COPY_PAIRS = 2000000
COPY_BLOB_LIMIT = 30000
COPY_RENAME_LIMIT = 30000
COPY_TIMEOUT = 300
# Retry policy for idempotent GETs only (never the enqueue POST).
RETRY_STATUSES = (429, 502, 503, 504, 524)
RETRY_DELAYS = (2, 4, 8, 16, 30)
# Secret FILE shapes, matched case-insensitively. Never sent to Kodus (no diff, no content), never
# copied into a snapshot, never context. The ignores document can only add to this list.
SECRET_PATTERNS = (
    '**/.env', '**/.env.*', '**/*.env', '**/*.pem', '**/*.key', '**/*.p12', '**/*.pfx', '**/*.ppk',
    '**/*.jks', '**/*.keystore', '**/*.asc', '**/id_rsa*', '**/id_ed25519*', '**/id_ecdsa*', '**/id_dsa*',
    '**/credentials', '**/credentials/**', '**/credentials.json', '**/service-account*.json',
    '**/secrets', '**/secrets/**', '**/.netrc', '**/.git-credentials', '**/.npmrc', '**/.pypirc',
    '**/.pgpass', '**/.htpasswd', '**/kubeconfig', '**/.kube/config', '**/.docker/config.json',
    '**/*.tfvars', '**/*.tfstate', '**/*.tfstate.backup', '**/*.kdbx', '**/secring.gpg',
    '**/*.p8', '**/.envrc', '**/.terraformrc', '**/terraform.rc', '**/*.age',
    # Secret-named config/data files only; source code named after secrets is reviewed.
    *('**/*secret*.' + ext for ext in ('json', 'yaml', 'yml', 'env', 'txt', 'ini', 'toml', 'conf', 'properties')))
GENERATED_HEADER = re.compile(rb'Code generated .{0,200}DO NOT EDIT')

class Refusal(Exception):
    pass

def digest(data):
    return hashlib.sha256(data).hexdigest()

def encoded(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=True, indent=2) + '\n').encode()

def clean_env():
    env = {k:v for k,v in os.environ.items() if not k.startswith('GIT_')}
    env['GIT_TERMINAL_PROMPT'] = '0'
    return env

class Private:
    """A throwaway bare Git directory that borrows the candidate repository's objects through
    alternates. Reads through it see no repository config, info/attributes, attributesFile,
    textconv drivers, replace refs or grafts, so nothing the caller controls in .git can change
    a diff, a disposition or a content decision."""
    def __init__(self, gitdir):
        self.gitdir = gitdir

PRIVATE_CONFIG = (('core.attributesFile', os.devnull), ('core.bigFileThreshold', '100g'),
                  ('core.ignorecase', 'false'), ('diff.noprefix', 'false'))

def private_env(gitdir):
    env = clean_env()
    env.update({'GIT_DIR': str(gitdir), 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull,
                # No GIT_GRAFT_FILE: the private directory has no info/grafts and alternates do not
                # carry grafts; pointing it at /dev/null only makes Git print a deprecation hint.
                'GIT_NO_REPLACE_OBJECTS': '1', 'GIT_ATTR_NOSYSTEM': '1',
                'GIT_CONFIG_COUNT': str(len(PRIVATE_CONFIG))})
    for i, (key, value) in enumerate(PRIVATE_CONFIG):
        env['GIT_CONFIG_KEY_%d' % i] = key; env['GIT_CONFIG_VALUE_%d' % i] = value
    return env

def private_view(repo, holder):
    """Create the private view inside holder (a temporary directory) and return it."""
    objects = Path(os.fsdecode(git(repo, 'rev-parse', '--path-format=absolute', '--git-path', 'objects').strip()))
    if not objects.is_absolute() or not objects.is_dir():
        raise Refusal('Candidate object database could not be located')
    gitdir = Path(holder) / 'private.git'
    env = private_env(gitdir); env.pop('GIT_DIR')
    if subprocess.run([GIT, 'init', '-q', '--bare', '--template=', str(gitdir)], env=env, capture_output=True,
                      timeout=30, cwd=holder).returncode:
        raise Refusal('Private Git view could not be created')
    (gitdir / 'objects/info').mkdir(parents=True, exist_ok=True)
    (gitdir / 'objects/info/alternates').write_text(str(objects) + '\n')
    return Private(gitdir)

def git(repo, *args, index=None, stdin=None):
    if isinstance(repo, Private):
        env, command, cwd = private_env(repo.gitdir), [GIT, *args], str(repo.gitdir)
    else:
        env, command, cwd = clean_env(), [GIT, '-C', str(repo), *args], None
    if index:
        env['GIT_INDEX_FILE'] = str(index)
    run = subprocess.run(command, env=env, capture_output=True, timeout=30, input=stdin, cwd=cwd)
    if run.returncode:
        raise Refusal('Git candidate operation failed: ' + args[0])
    return run.stdout

def git_version():
    # The launcher pins an absolute Git. No silent PATH fallback: unset is allowed only in the
    # synthetic localhost fixture.
    if not os.environ.get('KODUS_REVIEW_GIT'):
        if not synthetic_fixture():
            raise Refusal('KODUS_REVIEW_GIT is required (the launcher pins an absolute Git)')
    elif not Path(GIT).is_absolute() or not Path(GIT).is_file() or not os.access(GIT, os.X_OK):
        raise Refusal('KODUS_REVIEW_GIT must be an absolute path to an executable')
    try:
        out = subprocess.run([GIT, '--version'], capture_output=True, text=True, timeout=10, env=clean_env()).stdout
    except OSError:
        out = ''
    match = re.match(r'git version (\d+)\.(\d+)', out)
    if not match or (int(match[1]), int(match[2])) < MIN_GIT:
        raise Refusal('Git 2.40 or later is required (check-attr --source); found: ' + (out.strip()[:80] or 'none'))

def identity(repo):
    if os.environ.get('KODUS_REVIEW_CONTEXT') != 'personal':
        raise Refusal('KODUS_REVIEW_CONTEXT must explicitly be personal')
    git_version()
    home = Path.home().resolve()
    denied = os.environ.get('KODUS_REVIEW_DENIED_ROOTS', '').split(os.pathsep)
    denied_file = os.environ.get('KODUS_REVIEW_DENIED_ROOTS_FILE')
    if denied_file:
        denied += Path(denied_file).read_text().splitlines()
    denied = [p for p in denied if p.strip()]
    if not denied or any(not Path(p).is_absolute() for p in denied):
        raise Refusal('Explicit absolute backup/runtime denied roots are required')
    forbidden = [Path(p).resolve() for p in denied]
    forbidden += [home/n for n in ('.claude', '.codex', '.config')]
    if repo == home or any(repo == p or p in repo.parents for p in forbidden):
        raise Refusal('Backup or runtime repositories cannot be externally reviewed')
    if git(repo, 'rev-parse', '--is-bare-repository').strip() != b'false':
        raise Refusal('Bare repositories cannot be reviewed')
    if Path(os.fsdecode(git(repo, 'rev-parse', '--show-toplevel').strip())).resolve() != repo:
        raise Refusal('--repo must name the repository root')
    remote = os.fsdecode(git(repo, 'remote', 'get-url', 'origin').strip())
    match = re.fullmatch(r'(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?', remote)
    if not match:
        raise Refusal('Origin must identify an exact GitHub repository without URL credentials')
    name = match[1].lower()
    raw = os.environ.get('KODUS_REVIEW_ALLOWED_REPOS', '')
    config = os.environ.get('KODUS_REVIEW_ALLOWED_REPOS_FILE')
    if config:
        raw += '\n' + Path(config).read_text()
    allowed = {v.lower() for v in re.split(r'[\s,]+', raw.strip()) if v}
    if not allowed or any(not re.fullmatch(r'[a-z0-9_.-]+/[a-z0-9_.-]+', v) for v in allowed):
        raise Refusal('An explicit exact-repository allowlist is required')
    if name not in allowed:
        raise Refusal('Repository is outside the explicit allowlist')
    return name

def revision(repo, ref):
    if ref.startswith('-'):
        raise Refusal('Invalid revision')
    return git(repo, 'rev-parse', '--verify', ref + '^{commit}').decode().strip()

def valid_pattern(pattern):
    # Exactly the central source's rule: plain Git glob pathspecs, no negation, braces, character
    # classes, extglobs or escapes, normalised segments, and never a pattern that matches every path.
    if not isinstance(pattern, str) or not 0 < len(pattern) <= 200 or pattern != pattern.strip():
        return False
    if pattern.startswith(('/', '!', './')) or '\\' in pattern:
        return False
    if any(ord(c) < 32 or ord(c) == 127 for c in pattern) or any(c in pattern for c in '{}[]'):
        return False
    if any(form in pattern for form in ('!(', '@(', '+(', '?(', '*(')):
        return False
    segments = pattern.split('/')
    if '..' in segments or '.' in segments or '' in segments:
        return False
    return not all(set(segment) <= set('*?') for segment in segments)

def pattern_list(value, label, empty=True):
    if not isinstance(value, list) or len(value) > MAX_PATTERNS or (not empty and not value):
        raise Refusal('Ignores ' + label + ' must be a bounded list')
    if not all(valid_pattern(p) for p in value):
        raise Refusal('Ignores pattern outside the supported glob grammar')
    if len(set(value)) != len(value):
        raise Refusal('Ignores patterns must be unique')
    return value

def load_ignores(path, name):
    """The rendered kodus-review-ignores/2 document: ignore patterns plus the effective scope."""
    if path is None:
        raise Refusal('--ignores is required; render it with the central `ignores` command')
    if not path.is_absolute():
        raise Refusal('--ignores must be an absolute path')
    with path.open('rb') as handle:
        data = handle.read(MAX_IGNORES_BYTES + 1)
    if len(data) > MAX_IGNORES_BYTES:
        raise Refusal('Ignores document is oversized')
    def unique(pairs):
        keys = [k for k, _ in pairs]
        if len(set(keys)) != len(keys):
            raise Refusal('Ignores document has duplicate keys')
        return dict(pairs)
    try:
        document = json.loads(data.decode('utf-8', errors='strict'), object_pairs_hook=unique)
    except (ValueError, RecursionError):
        raise Refusal('Ignores document is not valid UTF-8 JSON') from None
    if not isinstance(document, dict) or set(document) != set(IGNORES_KEYS):
        raise Refusal('Ignores document must have exactly the rendered key set')
    if document['schema'] != IGNORES_SCHEMA:
        raise Refusal('Unsupported ignores schema')
    if not isinstance(document['source'], str) or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', document['source']):
        raise Refusal('Ignores source must name an owner/repository')
    if not isinstance(document['commit'], str) or not re.fullmatch(r'[0-9a-f]{40}', document['commit']):
        raise Refusal('Ignores commit must be a full lowercase SHA-1')
    if not isinstance(document['repository'], str) or document['repository'].lower() != name:
        raise Refusal('Ignores document names a different repository')
    pattern_list(document['patterns'], 'patterns')
    pattern_list(document['context_include'], 'context_include')
    # Additions only: the built-in secret list is always applied and cannot be removed or replaced.
    pattern_list(document['secret_add'], 'secret_add')
    for key in ('same_directory', 'generated_markers', 'send_changed_content'):
        if type(document[key]) is not bool:
            raise Refusal('Ignores scope flag must be boolean: ' + key)
    for key, ceiling in SCOPE_CEILINGS.items():
        if type(document[key]) is not int or not 0 < document[key] <= ceiling:
            raise Refusal('Scope bound outside the adapter ceiling: ' + key)
    return document, data

def synthetic_fixture():
    # The same localhost fixture review() accepts: plain HTTP at 127.0.0.1 with a test key.
    url = urllib.parse.urlsplit(os.environ.get('KODUS_REVIEW_API_URL', ''))
    token_path = os.environ.get('KODUS_REVIEW_TOKEN_FILE')
    if url.scheme != 'http' or url.hostname != '127.0.0.1' or not token_path:
        return False
    try:
        return Path(token_path).read_text().strip().startswith('kodus_test_')
    except OSError:
        return False

def bind_ignores(document, data):
    """Require the trusted launcher's renderer to reproduce the --ignores bytes exactly."""
    renderer = os.environ.get('KODUS_REVIEW_IGNORES_RENDERER', '')
    if not renderer:
        if synthetic_fixture():
            return False
        raise Refusal('KODUS_REVIEW_IGNORES_RENDERER is required to bind the ignores document')
    path = Path(renderer)
    if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
        raise Refusal('Ignores renderer must be an absolute executable regular file')
    proc = subprocess.Popen([str(path), '--repo', document['repository'], '--commit', document['commit']],
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            env=clean_env())
    output = []
    reader = threading.Thread(target=lambda: output.append(proc.stdout.read(MAX_RENDER_BYTES + 1)), daemon=True)
    deadline = time.monotonic() + RENDER_TIMEOUT
    reader.start(); reader.join(RENDER_TIMEOUT)
    try:
        if reader.is_alive():
            raise Refusal('Ignores renderer timed out')
        if not output:
            raise Refusal('Ignores renderer output could not be read')
        if len(output[0]) > MAX_RENDER_BYTES:
            raise Refusal('Ignores renderer output is oversized')
        try:
            code = proc.wait(timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            raise Refusal('Ignores renderer timed out') from None
        if code:
            raise Refusal('Ignores renderer failed')
        if output[0] != data:
            raise Refusal('Ignores document differs from the pinned central render')
    finally:
        if proc.poll() is None:
            proc.kill(); proc.wait()
        proc.stdout.close()
    return True

# One matcher: every path decision is a Git glob pathspec rooted at the top level. `*` and `?`
# stay within a segment, `**/` matches any leading directories including none, `/**` matches
# everything below. Secret patterns add the `icase` magic.
def specs(patterns, exclude=False, icase=False):
    magic = 'top,glob' + (',icase' if icase else '') + (',exclude' if exclude else '')
    return [':(' + magic + ')' + p for p in patterns]

def ignored_by(lister, patterns, universe):
    """Paths among `universe` (path bytes) that the ignore patterns match. A path is ignored when a
    wildcard pattern (with `*` or `?`) matches it as a Git glob pathspec, or when it equals a
    wildcard-free pattern exactly. A wildcard-free pattern never covers paths below it, even when
    it names a directory; write `dir/**` for that. lister(pathspec) -> paths."""
    wild = [p for p in patterns if '*' in p or '?' in p]
    free = {p.encode('utf-8') for p in patterns if '*' not in p and '?' not in p}
    found = set(lister(specs(wild))) & set(universe) if wild else set()
    return found | (set(universe) & free)

def literal_excludes(paths):
    return [':(top,literal,exclude)' + p for p in paths]

def tree_matches(repo, tree, pathspec):
    """{path bytes: blob} for every tree entry a pathspec selects, through a temporary index."""
    with tempfile.TemporaryDirectory(prefix='kodus-match-') as tmp:
        index = Path(tmp) / 'index'
        git(repo, 'read-tree', tree, index=index)
        listed = git(repo, 'ls-files', '-z', '--stage', '--', *pathspec, index=index)
    found = {}
    for entry in listed.split(b'\0'):
        if entry:
            meta, raw_path = entry.split(b'\t', 1)
            found[raw_path] = meta.decode().split()[1]
    return found

def worktree_tree(repo):
    with tempfile.TemporaryDirectory(prefix='kodus-index-') as tmp:
        index = Path(tmp) / 'index'
        git(repo, 'read-tree', 'HEAD', index=index)
        # Case folding from repository config must not rename what the working tree holds.
        names = git(repo, '-c', 'core.ignorecase=false', 'ls-files', '-z', '--cached', '--others',
                    '--exclude-standard').split(b'\0')
        names += git(repo, 'ls-tree', '-r', '--name-only', '-z', 'HEAD').split(b'\0')
        paths = sorted({os.fsdecode(n) for n in names if n})
        if any((repo / name).is_dir() for name in paths):
            raise Refusal('Tracked file replaced by directory; refusing recursive force-add')
        for start in range(0, len(paths), 100):
            # Force only the enumerated tracked/nonignored paths, never an ignored directory
            # sweep. Literal pathspecs keep Git metacharacters from broadening this inventory.
            git(repo, '-c', 'core.ignorecase=false', '--literal-pathspecs', 'add', '--force', '--',
                *paths[start:start+100], index=index)
        return git(repo, 'write-tree', index=index).decode().strip()

def inventory(repo, tree):
    """Full-tree metadata from Git; no blob is read here."""
    files = {}
    for entry in git(repo, 'ls-tree', '-r', '-l', '-z', tree).split(b'\0'):
        if entry:
            meta, raw_path = entry.split(b'\t', 1)
            mode, kind, blob, size = meta.decode().split()
            files[raw_path] = {'mode':mode, 'kind':kind, 'blob':blob, 'bytes':0 if size == '-' else int(size)}
    return files

def representable(name):
    rel = PurePosixPath(name)
    return not (rel.is_absolute() or '..' in rel.parts or '\\' in name or any(ord(c) < 32 for c in name))

# --full-index: Git scales abbreviated object names with the object count, which changes between
# calls as the private view writes objects; full names keep every patch byte-identical.
DIFF = ('diff', '--no-color', '--no-relative', '--no-ext-diff', '--no-textconv', '--binary',
        '--no-renames', '--full-index', '--src-prefix=a/', '--dst-prefix=b/', '--line-prefix=')
# Binary markers are Git header lines, never prefixed unified-hunk content.
BINARY_MARKER = re.compile(rb'^(?:GIT binary patch|Binary files [^\n]* differ)$', re.MULTILINE)

def in_raw_grammar(path):
    # Pinned vendor parser accepts ordinary spaces, but trims names, splits at
    # 'diff --git', and uses a greedy ' b/' delimiter for deleted paths. Keep
    # these ambiguous forms and all quoted/escaped names outside the grammar.
    return (re.fullmatch(r'[A-Za-z0-9_./@+ -]+', path) is not None
            and all(part == part.strip() for part in path.split('/'))
            and ' b/' not in path and 'diff --git' not in path)

def names(repo, base, tree, *pathspec):
    out = git(repo, 'diff', '--name-only', '--no-relative', '--no-renames', '-z', base, tree, '--', *pathspec)
    return [p for p in out.split(b'\0') if p]

def raw_changes(repo, base, tree):
    """[(path bytes, status, base blob, candidate blob, base mode, candidate mode)] for every change."""
    fields = git(repo, 'diff', '--raw', '-z', '--no-abbrev', '--no-relative', '--no-renames', base, tree, '--').split(b'\0')
    rows = []
    for meta, path in zip(fields[0:-1:2], fields[1:-1:2]):
        old_mode, new_mode, old, new, status = meta.decode().lstrip(':').split()
        rows.append((path, status, old, new, old_mode, new_mode))
    return rows

def scan_patch(path):
    """Hash, UTF-8 and binary-marker scan of a patch file, read line by line from disk."""
    hasher, decoder, utf8, binary = hashlib.sha256(), codecs.getincrementaldecoder('utf-8')('strict'), True, False
    with path.open('rb') as handle:
        for line in handle:
            hasher.update(line)
            binary = binary or BINARY_MARKER.fullmatch(line.rstrip(b'\n')) is not None
            if utf8:
                try:
                    decoder.decode(line)
                except UnicodeDecodeError:
                    utf8 = False
    if utf8:
        try:
            decoder.decode(b'', final=True)
        except UnicodeDecodeError:
            utf8 = False
    return hasher.hexdigest(), utf8, binary

def blob_sizes(repo, blobs):
    out = git(repo, 'cat-file', '--batch-check=%(objectname) %(objectsize)', stdin=''.join(b + '\n' for b in blobs).encode())
    sizes = {}
    for line in out.decode().splitlines():
        name, size = line.split()[:2]
        if size.isdigit():
            sizes[name] = int(size)
    if set(sizes) != set(blobs):
        raise Refusal('Secret copy detection could not find every blob; no review verdict')
    return sizes

def secret_copies(repo, secret_blobs, candidates):
    """Blobs among candidates that equal, or that Git copy detection derives from, a secret blob.
    Fails closed: any bound exceeded, timeout or skipped detection is a refusal, never a pass."""
    exact = {b for b in candidates if b in secret_blobs}
    rest = sorted(set(candidates) - exact - {NULL_BLOB, EMPTY_BLOB})
    if not secret_blobs or not rest:
        return exact
    # Git scores at most min/max size, so only blobs within a 2x size window of some secret blob
    # can reach 50% similarity; others are never paired.
    sources = sorted(secret_blobs)
    # Bounds first, on the unfiltered counts, so no unbounded work runs before a refusal.
    if len(sources) * len(rest) > MAX_COPY_PAIRS or len(sources) + len(rest) > COPY_BLOB_LIMIT:
        raise Refusal('Secret copy detection exceeds its bound; no review verdict')
    sizes = blob_sizes(repo, sources + rest)
    secret_sizes = sorted(sizes[b] for b in sources)
    def in_window(size):
        # Some secret size s with size/2 <= s <= 2*size, found by bisection over sorted sizes.
        i = bisect.bisect_left(secret_sizes, (size + 1) // 2)
        return i < len(secret_sizes) and secret_sizes[i] <= 2 * size
    rest = [b for b in rest if in_window(sizes[b])]
    if not rest:
        return exact
    with tempfile.TemporaryDirectory(prefix='kodus-secret-') as tmp:
        index = Path(tmp) / 'index'
        def tree(entries):
            if index.exists():
                index.unlink()
            git(repo, 'update-index', '--add', '--index-info', index=index,
                stdin=''.join('100644 %s\t%s\n' % entry for entry in entries).encode())
            return git(repo, 'write-tree', index=index).decode().strip()
        secret_entries = [(blob, 's/%06d' % i) for i, blob in enumerate(sources)]
        before = tree(secret_entries)
        after = tree(secret_entries + [(blob, 'c/%06d' % i) for i, blob in enumerate(rest)])
        try:
            run = subprocess.run([GIT, 'diff-tree', '-r', '-z', '--name-status', '--find-copies-harder',
                                  '--find-copies=%d%%' % SECRET_COPY_SIMILARITY, '-l%d' % COPY_RENAME_LIMIT,
                                  before, after], env=private_env(repo.gitdir), cwd=str(repo.gitdir),
                                 capture_output=True, timeout=COPY_TIMEOUT)
        except subprocess.TimeoutExpired:
            raise Refusal('Secret copy detection did not finish; no review verdict') from None
        # Git degrades detection past its limit with only a warning ("only found copies from
        # modified paths"); any diagnostic at all is treated as incomplete detection.
        if run.returncode or run.stderr.strip():
            raise Refusal('Secret copy detection did not run completely; no review verdict')
        out = run.stdout.split(b'\0')
    found, position = set(exact), 0
    while position < len(out) and out[position]:
        status = out[position].decode()
        if status[0] in 'CR':
            destination = out[position + 2].decode()
            found.add(rest[int(destination[2:])])
            position += 3
        else:
            position += 2
    return found

def generated_attributes(repo, tree, paths):
    """Paths Git attributes in the candidate tree mark generated, vendored or non-diffable."""
    if not paths:
        return set()
    out = git(repo, 'check-attr', '--source=' + tree, '-z', '--stdin', 'linguist-generated',
              'linguist-vendored', 'diff', stdin=b'\0'.join(p.encode() for p in paths) + b'\0')
    fields = out.split(b'\0')
    marked = set()
    for start in range(0, len(fields) - 2, 3):
        path, attribute, value = (f.decode('utf-8') for f in fields[start:start+3])
        if (attribute in ('linguist-generated', 'linguist-vendored') and value in ('set', 'true')) or \
                (attribute == 'diff' and value == 'unset'):
            marked.add(path)
    return marked

def stream_blob(view, sha):
    proc = subprocess.Popen([GIT, 'cat-file', 'blob', sha], env=private_env(view.gitdir), cwd=str(view.gitdir),
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
    return proc

def finish(proc, complete):
    proc.stdout.close()
    if proc.poll() is None:
        proc.kill()
    code = proc.wait()
    if complete and code:
        raise Refusal('Git candidate operation failed: cat-file')

def blob_head(view, sha, limit):
    """At most `limit` leading bytes of a blob; the rest is never read."""
    proc = stream_blob(view, sha)
    head = b''
    try:
        while len(head) < limit:
            chunk = proc.stdout.read(limit - len(head))
            if not chunk:
                break
            head += chunk
    finally:
        finish(proc, len(head) < limit)
    return head

def nul_in_head(view, sha):
    """Git's binary rule and the only one: a NUL byte in the first 8000 bytes. Bounded read."""
    return b'\0' in blob_head(view, sha, 8000)

def valid_utf8(view, sha):
    """Streamed strict UTF-8 check of a whole blob; nothing is kept."""
    proc = stream_blob(view, sha)
    decoder, ok = codecs.getincrementaldecoder('utf-8')('strict'), True
    try:
        while True:
            chunk = proc.stdout.read(65536)
            if not chunk:
                decoder.decode(b'', final=True)
                break
            decoder.decode(chunk)
    except UnicodeDecodeError:
        ok = False
    finally:
        finish(proc, ok)
    return ok

def ondisk_secret_blobs(local, view, secret_in):
    """Blobs of Git-ignored files on disk that match the secret patterns (never committed, so
    absent from both trees), hashed into the throwaway private view's own object store for copy
    detection; nothing is written to the candidate repository. verify re-reads and re-hashes them."""
    listed = git(local, 'ls-files', '-z', '--others', '--ignored', '--exclude-standard', '--', *secret_in)
    paths = []
    for raw in listed.split(b'\0'):
        if raw and any(c.encode() in raw for c in GLOB_CHARS):
            raise Refusal('On-disk secret path contains a glob character and cannot be matched soundly: '
                          + os.fsdecode(raw))
        path = local / os.fsdecode(raw) if raw else None
        if path and path.is_file() and not path.is_symlink() and path.stat().st_size <= MAX_FULL_PATCH_BYTES:
            paths.append(str(path))
    if not paths:
        return set()
    out = git(view, 'hash-object', '-w', '--no-filters', '--stdin-paths', stdin=''.join(p + '\n' for p in paths).encode())
    return {line for line in out.decode().split()}

def addable_under(repo, *paths):
    """Paths Git would add under the given paths (untracked and not ignored), with the same
    clean Git environment as every other call in the candidate repository."""
    out = git(repo, 'ls-files', '-z', '--others', '--exclude-standard', '--', *(str(p) for p in paths))
    return [os.fsdecode(p) for p in out.split(b'\0') if p]

def require_unaddable(repo, evidence, directory):
    """Evidence inside a repository: the evidence file must be ignored and Git must report nothing
    addable anywhere under its artifacts directory (snapshots and patches included)."""
    run = subprocess.run([GIT, '-C', str(repo), 'check-ignore', '-q', '--', str(evidence)],
                         capture_output=True, timeout=10, env=clean_env())
    if run.returncode:
        raise Refusal('Evidence within a repository must be Git-ignored: ' + evidence.name)
    addable = addable_under(repo, directory, evidence)
    if addable:
        raise Refusal('Evidence artifact would be committable (not Git-ignored): ' + addable[0])

WITHHELD_CLASS = {'ignored': 'ignored by the repository review scope', 'binary': 'binary',
                  'secret-pattern': 'secret file', 'secret-copy': 'copy of a secret file'}

def withheld_stub(path, status, disposition, entry):
    """A config.files entry telling Kodus a changed path exists without any of its content. Secret
    paths carry only their name and class; others also their size. No blob id: for a small file it
    can be brute-forced back to the content."""
    detail = ''
    if not disposition.startswith('secret') and entry:
        detail = '; %d bytes' % entry['bytes']
    line = ('[kodus-review: this changed file is part of the change but withheld from this review (%s); '
            'its content is not shown%s]' % (WITHHELD_CLASS[disposition], detail))
    diff = ('@@ -1 +0,0 @@\n-' if status == 'deleted' else '@@ -0,0 +1 @@\n+') + line + '\n'
    return {'path': path, 'status': status, 'diff': diff, 'content': ''}

def with_grouped_line(stub, further, counts):
    """The last notice also states how many further withheld paths are not listed individually."""
    grouped = ('[kodus-review: %d further changed paths are withheld from this review and not listed '
               'individually: ignored %d, binary %d, secret %d]'
               % (further, counts['ignored'], counts['binary'], counts['secret']))
    line = stub['diff'].split('\n')[1][1:]
    if stub['status'] == 'deleted':
        # Both lines on the old side: a deletion never claims the file still exists.
        diff = '@@ -1,2 +0,0 @@\n-' + line + '\n-' + grouped + '\n'
    else:
        diff = '@@ -0,0 +1,2 @@\n+' + line + '\n+' + grouped + '\n'
    return {**stub, 'diff': diff}

def within_bound(artifact, data, limit):
    if len(data) > limit:
        raise Refusal('Artifact %s exceeds its %d-byte bound; no review verdict' % (artifact, limit))
    return data

def capture(repo, base, head, directory, brief, ignores, tree=None, ondisk=None, guard=None):
    with tempfile.TemporaryDirectory(prefix='kodus-view-') as holder:
        return capture_view(repo, private_view(repo, holder), base, head, directory, brief, ignores, tree, ondisk,
                            guard)

def capture_view(local, view, base, head, directory, brief, ignores, recorded_tree, recorded_ondisk, guard=None):
    """Every read below goes through the private view `view`; only the working-tree snapshot and
    the on-disk secret scan touch the caller's repository `local`."""
    document, ignores_bytes = ignores
    captured_at = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    secret_patterns = list(dict.fromkeys([*SECRET_PATTERNS, *document['secret_add']]))
    secret_in = specs(secret_patterns, icase=True)
    secret_out = specs(secret_patterns, exclude=True, icase=True)
    if recorded_tree:
        tree = recorded_tree
    else:
        tree = git(view, 'rev-parse', head + '^{tree}').decode().strip() if head else worktree_tree(local)
    base_tree = git(view, 'rev-parse', base + '^{tree}').decode().strip()
    # The full candidate patch (every change, secrets included) is local evidence for the
    # independent reviewer and is never sent. Streamed to disk: ignored bulk files may be large.
    patch_path = directory/'candidate.patch'
    git(view, *DIFF, '--output=' + str(patch_path), base, tree, '--')
    patch_size = patch_path.stat().st_size
    if not patch_size:
        raise Refusal('No candidate changes; no review verdict')
    if patch_size > MAX_FULL_PATCH_BYTES:
        raise Refusal('Oversized full candidate patch is unsupported')
    patch_sha256, patch_utf8, patch_binary = scan_patch(patch_path)
    rows = raw_changes(view, base, tree)
    raw_changed = [row[0] for row in rows]
    if raw_changed != names(view, base, tree):
        raise Refusal('Changed path inventory is inconsistent')
    # Local representability applies to every changed path: the mandatory reviewer must see it.
    changed = [p.decode('utf-8') for p in raw_changed]
    if not all(representable(p) for p in changed):
        raise Refusal('Unsupported candidate path')
    # Git matches a glob pathspec literally too, so a path whose name contains the pattern text
    # (`docs/*/y.go` against `docs/*`) would be ignored. Such names cannot be matched soundly.
    for p in changed:
        if any(c in p for c in GLOB_CHARS):
            raise Refusal('Changed path contains a glob character and cannot be matched soundly: ' + p)
    if any(mode in ('120000', '160000') for row in rows for mode in row[4:]):
        raise Refusal('Symlinks and submodules are unsupported in either snapshot')
    status_of = {row[0]: {'A':'added', 'M':'modified', 'D':'deleted'}.get(row[1]) for row in rows}
    if not all(status_of.values()):
        raise Refusal('Unsupported structured file status')
    candidate_files, base_files = inventory(view, tree), inventory(view, base)
    # Secret-pattern blobs in BOTH full trees seed copy detection, so a copy of an unchanged
    # secret, or a renamed secret with an edit, is withheld too.
    secret_blobs = {b for side in (base, tree) for b in tree_matches(view, side, secret_in).values()}
    ondisk = ondisk_secret_blobs(local, view, secret_in)
    if recorded_tree is not None and ondisk != set(recorded_ondisk or ()):
        # verify: the gitignored secret files must still be on disk, byte for byte.
        raise Refusal('On-disk secret files differ from those recorded at capture (missing or changed)')
    secret_blobs |= ondisk
    secret_blobs -= {EMPTY_BLOB}
    blobs = {}
    def read_blob(sha):
        if sha not in blobs:
            blobs[sha] = git(view, 'cat-file', 'blob', sha)
        return blobs[sha]
    secret_matched = set(names(view, base, tree, *secret_in))
    ignored_changed = ignored_by(lambda spec: names(view, base, tree, *spec), document['patterns'], raw_changed)
    not_ignored = set(raw_changed) - ignored_changed
    copies = secret_copies(view, secret_blobs, [b for row in rows if row[0] not in secret_matched for b in row[2:4]])
    candidates = [p for p in raw_changed if p in not_ignored and p not in secret_matched]
    candidate_set = set(candidates)
    # Count bound before any per-blob probe: a candidate set that could never be sent refuses
    # without spawning one process per blob. Non-ignored binaries count toward it.
    if len(candidates) > MAX_SENT_FILES:
        raise Refusal('More changed paths outside the ignores (%d) than the API limit of %d; no review verdict'
                      % (len(candidates), MAX_SENT_FILES))
    to_classify = sorted({b for row in rows if row[0] in candidate_set and row[2] not in copies
                          and row[3] not in copies for b in row[2:4] if b != NULL_BLOB})
    binary_blobs = {b for b in to_classify if nul_in_head(view, b)}
    binary = {row[0] for row in rows if row[0] in candidate_set and row[2] not in copies and row[3] not in copies
              and (row[2] in binary_blobs or row[3] in binary_blobs)}
    dispositions = []
    for path, status, old, new, _, _ in rows:
        if path in secret_matched:
            disposition = 'secret-pattern'
        elif old in copies or new in copies:
            disposition = 'secret-copy'
        elif path not in not_ignored:
            disposition = 'ignored'
        elif path in binary:
            disposition = 'binary'
        else:
            disposition = 'sent'
        dispositions.append({'path':path.decode(), 'status':status_of[path], 'disposition':disposition})
    by_path = {d['path']: d['disposition'] for d in dispositions}
    sent = [p for p in changed if by_path[p] == 'sent']
    ignored = [p for p in changed if by_path[p] == 'ignored']
    withheld = [p for p in changed if by_path[p] not in ('sent', 'ignored')]
    secret = {p for p in changed if by_path[p].startswith('secret')}
    all_ignored = not not_ignored
    if not sent and not all_ignored:
        raise Refusal('No reviewable text changes: every changed path outside the ignores is binary or secret')
    # The submitted aggregate: ignore and secret patterns plus literal excludes for the
    # path-level withholdings (secret copies and binaries outside the ignores).
    # The submitted aggregate is an inclusion list of exactly the sent paths. A literal pathspec
    # also covers a same-named directory, so unsent changed paths below a sent path (a file
    # replaced by a directory) are excluded one by one; nothing above a sent path is excluded.
    sent_dirs = tuple(p + '/' for p in sent)
    aggregate_spec = [*(':(top,literal)' + p for p in sent),
                      *literal_excludes([p for p in changed if by_path[p] != 'sent' and p.startswith(sent_dirs)])]
    submitted = git(view, *DIFF, '--text', base, tree, '--', *aggregate_spec) if sent else b''
    # An empty pathspec would mean every path, so the inventory check runs only when something is sent.
    if sent and [p.decode() for p in names(view, base, tree, *aggregate_spec)] != sent:
        raise Refusal('Submitted path inventory differs from the sent paths')
    if bool(sent) != bool(submitted) or (len(sent) == len(changed) and (
            len(submitted) != patch_size or digest(submitted) != patch_sha256)):
        raise Refusal('Submitted patch differs from the candidate patch')
    # Kodus transport limits apply to the sent subset only. A sent path is never silently
    # withheld: any path that cannot be submitted whole makes the run NO_VERDICT, naming it.
    for name in sent:
        if not in_raw_grammar(name):
            raise Refusal('Changed path is outside the supported raw-diff grammar: ' + name)
        if len(name) > 500:
            raise Refusal('Changed path is longer than the API limit: ' + name)
    if len(sent) > MAX_SENT_FILES:
        raise Refusal('More changed paths to send than the API limit of %d' % MAX_SENT_FILES)
    blob_of = {row[0].decode(): row[2:4] for row in rows}
    for name in sent:
        if not all(valid_utf8(view, b) for b in blob_of[name] if b != NULL_BLOB):
            raise Refusal('Changed text file is not valid UTF-8 and cannot be sent whole: ' + name)
    files = []
    for name in sent:
        # Even a literal Git pathspec includes descendants when a file becomes a
        # directory (or vice versa). Exclude that subtree explicitly. The grammar
        # above admits no glob metacharacters, so only the appended /** is a glob.
        file_patch = git(view, *DIFF, '--text', base, tree, '--', ':(top,literal)' + name,
                         ':(top,exclude,glob)' + name + '/**').decode('utf-8')
        if not file_patch or len(file_patch.encode('utf-8')) > MAX_FILE_PATCH_BYTES:
            raise Refusal('Changed file diff is empty or over the per-file limit: ' + name)
        files.append({'path':name, 'status':status_of[name.encode()], 'diff':file_patch, 'content':''})
    if len(submitted) > MAX_PATCH_BYTES:
        raise Refusal('Changed text diffs exceed the %d-byte submission limit; largest: %s' % (
            MAX_PATCH_BYTES, max(files, key=lambda f: len(f['diff']))['path']))
    if BINARY_MARKER.search(submitted):
        raise Refusal('Submitted patch carries a binary marker')
    submitted.decode('utf-8', errors='strict')
    if ''.join(f['diff'] for f in files).encode('utf-8') != submitted:
        raise Refusal('Structured patches do not exactly cover the submitted patch')

    # Scoped snapshots: changed files (except secrets) always, then context_include matches, then
    # same-directory siblings, each sorted by path, as an ordered prefix under the combined budget.
    def sides(raw):
        return [entry for entry in (candidate_files.get(raw), base_files.get(raw)) if entry]
    budget = document['snapshot_budget_bytes']
    chosen = [(p.encode(), 'changed') for p in changed if p not in secret]
    used = sum(e['bytes'] for raw, _ in chosen for e in sides(raw))
    if used > budget:
        raise Refusal('Changed files exceed the scope snapshot budget; no review verdict')
    # Snapshot entries (files plus directories, counted over both sides) share verify's bound.
    snapshot_dirs = {str(d) for raw, _ in chosen for d in PurePosixPath(raw.decode('utf-8')).parents if str(d) != '.'}
    def entries_with(raw):
        new = {str(d) for d in PurePosixPath(raw.decode('utf-8')).parents if str(d) != '.'} - snapshot_dirs
        return len(chosen) + 1 + len(snapshot_dirs) + len(new), new
    if len(chosen) + len(snapshot_dirs) > MAX_SNAPSHOT_ENTRIES:
        raise Refusal('Changed files exceed the snapshot entry bound of %d; no review verdict' % MAX_SNAPSHOT_ENTRIES)
    in_scope = set(tree_matches(view, tree, ['.', *secret_out])) - ignored_by(
        lambda spec: tree_matches(view, tree, spec), document['patterns'], candidate_files)
    included = tree_matches(view, tree, specs(document['context_include'])) if document['context_include'] else {}
    ordered = [(p, 'include') for p in sorted(included)]
    if document['same_directory']:
        folders = {str(PurePosixPath(p).parent) for p in changed if p not in secret}
        ordered += [(p, 'same-directory') for p in sorted(candidate_files)
                    if str(PurePosixPath(p.decode('utf-8', errors='replace')).parent) in folders]
    seen = {raw for raw, _ in chosen}
    pending = []
    for raw, reason in ordered:
        if raw not in seen:
            seen.add(raw); pending.append((raw, reason))
    names_for_attributes = [r.decode('utf-8', errors='replace') for r, _ in chosen + pending]
    marked = generated_attributes(view, tree, names_for_attributes) if document['generated_markers'] else set()
    def blob(entry):
        return read_blob(entry['blob'])
    def generated(name, entry):
        return document['generated_markers'] and (name in marked or bool(
            entry and GENERATED_HEADER.search(blob_head(view, entry['blob'], 1024))))
    omitted, excluded_count, truncated = [], 0, False
    for raw, reason in pending:
        entry = candidate_files[raw]
        try:
            name = raw.decode('utf-8')
        except UnicodeDecodeError:
            excluded_count += 1
            continue
        if (raw not in in_scope or entry['blob'] in secret_blobs or entry['kind'] != 'blob'
                or entry['mode'] not in ('100644', '100755') or not representable(name)
                or name in marked):
            excluded_count += 1
        elif entry['bytes'] > document['max_context_file_bytes']:
            omitted.append({'path':name, 'reason':'file-size'})
        elif (truncated or used + sum(e['bytes'] for e in sides(raw)) > budget
              or entries_with(raw)[0] > MAX_SNAPSHOT_ENTRIES):
            # Budget (bytes and entries) is decided before any blob read, so a spent budget reads
            # nothing further.
            truncated = True
            omitted.append({'path':name, 'reason':'budget'})
        elif generated(name, entry):
            excluded_count += 1
        else:
            used += sum(e['bytes'] for e in sides(raw))
            snapshot_dirs |= entries_with(raw)[1]
            chosen.append((raw, reason))
    # Copy detection runs on the admitted, budget-bounded context only: near-copies of a secret are
    # dropped (recorded, not refilled), and the copy bounds still fail closed on this bounded set.
    admitted = [(raw, reason) for raw, reason in chosen if reason != 'changed']
    near_secret = secret_copies(view, secret_blobs, [candidate_files[raw]['blob'] for raw, _ in admitted])
    if near_secret:
        dropped = [(raw, reason) for raw, reason in admitted if candidate_files[raw]['blob'] in near_secret]
        for raw, reason in dropped:
            chosen.remove((raw, reason))
            used -= sum(e['bytes'] for e in sides(raw))
            omitted.append({'path':raw.decode('utf-8'), 'reason':'secret-copy'})
    def materialise(files, destination):
        destination.mkdir()
        manifest, written_bytes = [], 0
        for raw, _ in sorted(chosen):
            entry = files.get(raw)
            if not entry:
                continue
            name = raw.decode('utf-8')
            data = blob(entry)
            written_bytes += len(data)
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            target.chmod(0o555 if entry['mode'] == '100755' else 0o444)
            manifest.append({'path':name, 'mode':entry['mode'], 'blob':entry['blob'], 'sha256':digest(data)})
        written = sum(1 for _ in destination.rglob('*'))
        if written > MAX_SNAPSHOT_ENTRIES or written_bytes > SCOPE_CEILINGS['snapshot_budget_bytes']:
            raise Refusal('Snapshot exceeds its entry or byte bound: ' + destination.name)
        return within_bound('manifest', encoded(manifest), MAX_MANIFEST_BYTES)

    # Changed-file content for Kodus, within the content budget; the diff is always sent.
    content, content_withheld, content_used, exhausted = [], [], 0, False
    for item in files:
        if item['status'] == 'deleted':
            continue
        entry = candidate_files[item['path'].encode()]
        if not document['send_changed_content']:
            reason = 'disabled'
        elif generated(item['path'], entry):
            reason = 'generated'
        elif exhausted or content_used + entry['bytes'] > document['kodus_content_bytes']:
            # Decided from the recorded size: an over-budget file is never read for content.
            exhausted, reason = True, 'budget'
        else:
            try:
                text = blob(entry).decode('utf-8', errors='strict')
            except UnicodeDecodeError:
                text = None
            size = 0 if text is None else len(text.encode('utf-8'))
            if text is None:
                reason = 'non-utf8'
            elif len(text) > MAX_CONTENT_CHARS:
                reason = 'file-size'
            elif exhausted or content_used + size > document['kodus_content_bytes']:
                exhausted, reason = True, 'budget'
            else:
                content_used += size
                item['content'] = text
                content.append({'path':item['path'], 'sha256':digest(text.encode('utf-8')), 'bytes':size})
                continue
        content_withheld.append({'path':item['path'], 'reason':reason})
    # Kodus is told that each withheld changed path exists, never its content: one stub entry per
    # path in config.files (not in the raw diff, which the service `git apply`s in its sandbox).
    # Individual notices fill the request up to its file limit (secret, then binary, then ignored,
    # each by path); the last one also carries a grouped line counting the rest.
    notified, notice_of = [], {}
    if not all_ignored:
        order = {'secret-pattern': 0, 'secret-copy': 0, 'binary': 1, 'ignored': 2}
        withheld_rows = sorted((row for row in rows if by_path[row[0].decode()] != 'sent'),
                               key=lambda row: (order[by_path[row[0].decode()]], row[0]))
        capacity = max(0, MAX_SENT_FILES - len(files))
        # A path over the API's 500-character limit cannot have its own entry: it is only counted.
        listable = [row for row in withheld_rows if len(row[0].decode()) <= 500]
        too_long = [row for row in withheld_rows if len(row[0].decode()) > 500]
        individual, grouped = listable[:capacity], listable[capacity:] + too_long
        for row in individual:
            name = row[0].decode()
            entry = base_files.get(row[0]) if status_of[row[0]] == 'deleted' else candidate_files.get(row[0])
            notified.append(withheld_stub(name, status_of[row[0]], by_path[name], entry))
            notice_of[name] = 'individual'
        if grouped and notified:
            counts = {'ignored': 0, 'binary': 0, 'secret': 0}
            for row in grouped:
                disposition = by_path[row[0].decode()]
                counts['secret' if disposition.startswith('secret') else disposition] += 1
                notice_of[row[0].decode()] = 'grouped'
            # The grouped line rides on the last notice that has a new side; a deletion notice keeps
            # zero new lines, so it is used only when every notice is a deletion.
            carriers = [i for i, stub in enumerate(notified) if stub['status'] != 'deleted']
            carrier = carriers[-1] if carriers else len(notified) - 1
            notified[carrier] = with_grouped_line(notified[carrier], len(grouped), counts)
    for item in dispositions:
        if item['disposition'] != 'sent':
            item['notified'] = notice_of.get(item['path'], False)
            if item['notified'] is False:
                # skipped: no request was made; no-slot: no request entry remained to carry a notice
                # (sent paths filled the limit, or only unlistable paths were withheld).
                item['notified_reason'] = 'skipped' if all_ignored else 'no-slot'
    files = files + notified
    files_bytes = within_bound('files.json', encoded(files), MAX_FILES_JSON_BYTES)
    snapshot = directory/'snapshot'; base_snapshot = directory/'base-snapshot'
    manifest_bytes = materialise(candidate_files, snapshot)
    base_manifest = materialise(base_files, base_snapshot)
    def side(entry):
        return None if entry is None else {'blob':entry['blob'], 'bytes':entry['bytes']}
    withheld_files = []
    for row in rows:
        name = row[0].decode()
        if by_path[name] in ('secret-pattern', 'secret-copy', 'binary'):
            withheld_files.append({'path':name, 'status':status_of[row[0]], 'reason':by_path[name],
                                   'base':side(base_files.get(row[0])), 'candidate':side(candidate_files.get(row[0]))})
    record = {'schema_version':2, 'effective':{**{k:document[k] for k in IGNORES_KEYS[4:]},
                                               'secret_patterns':secret_patterns,
                                               'secret_copy_similarity':SECRET_COPY_SIMILARITY},
              'tree':{'files':len(candidate_files), 'bytes':sum(e['bytes'] for e in candidate_files.values())},
              'base_tree':{'files':len(base_files), 'bytes':sum(e['bytes'] for e in base_files.values())},
              'materialised':[{'path':raw.decode(), 'reason':reason, 'candidate':side(candidate_files.get(raw)),
                               'base':side(base_files.get(raw))} for raw, reason in chosen],
              'omitted':omitted, 'excluded_context_files':excluded_count,
              'ondisk_secret_blobs':sorted(ondisk),
              'budget':{'snapshot_bytes':budget, 'snapshot_bytes_used':used,
                        'kodus_content_bytes':document['kodus_content_bytes'],
                        'kodus_content_bytes_used':content_used},
              'kodus':{'diff_paths':sent, 'withheld_files':withheld_files, 'content':content,
                       'notified':[{'path':f['path'], 'status':f['status'], 'diff':f['diff']} for f in notified],
                       'content_withheld':content_withheld, 'patch_bytes':len(submitted),
                       'patch_sha256':digest(submitted)}}
    scope_bytes = within_bound('scope.json', encoded(record), MAX_SCOPE_JSON_BYTES)
    for name,data in [('manifest.json',manifest_bytes),('base-manifest.json',base_manifest),
                      ('submitted.patch',submitted),('brief.txt',brief),('files.json',files_bytes),
                      ('ignores.json',ignores_bytes),('scope.json',scope_bytes)]:
        path=directory/name;path.write_bytes(data);path.chmod(0o444)
    patch_path.chmod(0o444)
    if scan_patch(patch_path)[0] != patch_sha256:
        raise Refusal('Full candidate patch changed during capture')
    if guard:
        guard()  # e.g. nothing under an in-repository artifacts directory may be committable
    if not head and not recorded_tree and worktree_tree(local) != tree:
        raise Refusal('Working candidate changed during capture')
    return {'base':base, 'head':head, 'tree':tree, 'base_tree':base_tree, 'captured_at':captured_at,
            'patch_sha256':patch_sha256,
            'brief_sha256':digest(brief), 'files_sha256':digest(files_bytes),
            'files_path':str(directory/'files.json'), 'snapshot_manifest_sha256':digest(manifest_bytes),
            'base_manifest_sha256':digest(base_manifest), 'changed_paths':changed,
            'dispositions':dispositions, 'submitted_paths':sent, 'notified_paths':[f['path'] for f in notified],
            'ignored_paths':ignored,
            'withheld_paths':withheld, 'all_changed_paths_ignored':all_ignored,
            'unsent_paths_outside_grammar':[p for p in changed if by_path[p] != 'sent' and not in_raw_grammar(p)],
            'snapshot_path':str(snapshot), 'base_snapshot_path':str(base_snapshot),
            'manifest_path':str(directory/'manifest.json'),
            'base_manifest_path':str(directory/'base-manifest.json'),
            'patch_path':str(patch_path), 'brief_path':str(directory/'brief.txt'),
            'submitted_patch_sha256':digest(submitted), 'submitted_patch_path':str(directory/'submitted.patch'),
            'candidate_patch_bytes':patch_size, 'candidate_patch_utf8':patch_utf8,
            'candidate_patch_has_binary':patch_binary,
            'scope_path':str(directory/'scope.json'), 'scope_sha256':digest(scope_bytes),
            'ignores_sha256':digest(ignores_bytes), 'ignores_commit':document['commit'],
            'ignores_source':document['source'], 'ignores_path':str(directory/'ignores.json')}, submitted

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise Refusal('API redirects are refused')

def positive_env(name, default, maximum):
    try:
        value = float(os.environ.get(name, default))
    except ValueError:
        raise Refusal('Invalid timeout configuration') from None
    if not 0 < value <= maximum:
        raise Refusal('Invalid timeout configuration')
    return value

def kodus_client():
    """Authenticated Kodus API client shared by review and verify: pinned token file, HTTPS origin
    (synthetic localhost fixture excepted), no redirects or proxies, bounded responses."""
    token_path = os.environ.get('KODUS_REVIEW_TOKEN_FILE')
    if not token_path:
        raise Refusal('KODUS_REVIEW_TOKEN_FILE is required')
    token = Path(token_path).read_text().strip()
    if not re.fullmatch(r'kodus_[A-Za-z0-9_-]+', token):
        raise Refusal('A Kodus team key is required')
    endpoint = os.environ.get('KODUS_REVIEW_API_URL', 'https://api.kodus.io').rstrip('/')
    url = urllib.parse.urlsplit(endpoint)
    test_local = url.scheme == 'http' and url.hostname == '127.0.0.1' and token.startswith('kodus_test_')
    if (url.scheme != 'https' and not test_local) or not url.hostname or url.username or url.password or url.query or url.fragment or url.path:
        raise Refusal('API URL must be an HTTPS origin (synthetic localhost fixture excepted)')
    deadline = time.monotonic() + positive_env('KODUS_REVIEW_TIMEOUT', '900', 3600)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    retries = []
    def attempt(path, body):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise Refusal('Review deadline exceeded')
        headers={'X-Team-Key':token, 'Accept':'application/json', 'User-Agent':'kodus-review/'+VERSION}
        if body is not None:
            headers.update({'X-Kodus-Async':'1', 'Content-Type':'application/json'})
        req=urllib.request.Request(endpoint+path, data=body, headers=headers)
        with opener.open(req, timeout=min(remaining,30)) as response:
            data=response.read(MAX_RESPONSE_BYTES+1)
            if len(data)>MAX_RESPONSE_BYTES:
                raise Refusal('Oversized API response')
            return json.loads(data.replace(token.encode(), b'[REDACTED]'))
    def request(path, body=None):
        # Only idempotent GETs (job polls, verify's re-fetch) are retried, on gateway errors,
        # 429 and transport failures, with bounded backoff inside the deadline. The enqueue POST
        # is never retried: no automatic paid resubmission.
        tries = 0
        while True:
            wait = None
            try:
                value = attempt(path, body)
                break
            except urllib.error.HTTPError as exc:
                if body is not None or exc.code not in RETRY_STATUSES:
                    raise Refusal('Authenticated API HTTP '+str(exc.code)) from None
                status = exc.code
                if exc.code == 429:
                    after = (exc.headers or {}).get('Retry-After', '')
                    wait = float(after) if re.fullmatch(r'\d{1,5}', after or '') else None
            except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException) as exc:
                if body is not None:
                    raise Refusal('API transport or JSON failure') from None
                status = 'transport'
            except (ValueError, RecursionError, OSError):
                raise Refusal('API transport or JSON failure') from None
            if wait is None:
                wait = RETRY_DELAYS[min(tries, len(RETRY_DELAYS) - 1)]
            tries += 1
            retries.append({'status': status, 'attempt': tries, 'wait': wait})
            if time.monotonic() + wait >= deadline:
                raise Refusal('Review deadline exceeded after %d retried request(s); last: %s' % (tries, status))
            time.sleep(wait)
        if not isinstance(value,dict):
            raise Refusal('Malformed API envelope')
        if 'data' in value:
            value=value['data']
        if not isinstance(value,dict):
            raise Refusal('Malformed API data envelope')
        return value
    request.retries = retries
    request.origin = endpoint
    request.synthetic = test_local
    return request, deadline

def job_path(job):
    if not isinstance(job,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,200}',job):
        raise Refusal('Missing async job identity; legacy synchronous fallback is refused')
    return '/cli/review/jobs/'+urllib.parse.quote(job,safe='')

def review(candidate, patch, audit, persist):
    request, deadline = kodus_client()
    audit['api_origin'] = request.origin
    audit['synthetic_fixture'] = request.synthetic
    interval = positive_env('KODUS_REVIEW_POLL_INTERVAL', '1', 5)
    # The service rejects config.focus; intent is retained only for the independent local agent.
    files_bytes=Path(candidate['files_path']).read_bytes()
    if digest(files_bytes) != candidate['files_sha256']:
        raise Refusal('Structured file artifact changed before submission')
    files=json.loads(files_bytes)
    sent_count=len(candidate['submitted_paths'])
    if (''.join(f['diff'] for f in files[:sent_count]).encode('utf-8') != patch
            or [f['path'] for f in files[:sent_count]] != candidate['submitted_paths']
            or [f['path'] for f in files[sent_count:]] != candidate['notified_paths']):
        raise Refusal('Structured request differs from captured submitted patch and notices')
    body=request_body(candidate, files_bytes, patch, audit.get('fast') is True)
    audit['request_sha256']=digest(body);audit['request_bytes']=len(body)
    persist()
    queued=request('/cli/review', body)
    audit['enqueue_response']=queued
    persist()
    job=queued.get('jobId')
    path=job_path(job)
    audit['job_id']=job
    audit['retries']=request.retries
    persist()
    while True:
        if time.monotonic()+interval>=deadline:
            raise Refusal('Review deadline exceeded')
        time.sleep(interval)
        polled=request(path)
        state=polled.get('status')
        if state not in ('PENDING','PROCESSING'):
            audit['terminal_response']=polled
            persist()
        if state=='COMPLETED':
            result=polled.get('result')
            audit['job_created_at']=polled.get('createdAt')
            break
        if state not in ('PENDING','PROCESSING'):
            raise Refusal('Review job failed or has unknown status')
    audit['result']=result
    persist()
    if not isinstance(result,dict) or not isinstance(result.get('issues'),list):
        raise Refusal('Malformed review result')
    if (type(result.get('filesAnalyzed')) is not int
            or result['filesAnalyzed'] != len(candidate['submitted_paths']) + len(candidate['notified_paths'])):
        raise Refusal('Reported file count differs from submitted paths and notices; no complete component verdict')
    blocking=False
    for finding in result['issues']:
        if not isinstance(finding,dict) or finding.get('severity') not in ('critical','high','major','error','medium','warning','low','minor','info'):
            raise Refusal('Unknown or malformed finding severity')
        if (not isinstance(finding.get('file'),str) or not finding['file'].strip()
                or type(finding.get('line')) is not int or finding['line']<0
                or not isinstance(finding.get('message'),str) or not finding['message'].strip()):
            raise Refusal('Malformed finding location or message')
        blocking |= finding['severity'] in BLOCKING
    return {**audit, 'verdict':'FAIL' if blocking else 'PASS', 'context':'partial-or-unknown',
            'job_id':job, 'result':result}, 1 if blocking else 0

def request_body(candidate, files_bytes, patch, fast=False):
    config={'files':json.loads(files_bytes)}
    if fast:
        config['fast']=True
    return encoded({'diff':patch.decode(), 'config':config,
                    'gitRemote':'https://github.com/'+candidate['repository']+'.git',
                    'mergeBaseSha':candidate['base'], 'commitSha':candidate['head'] or candidate['base'],
                    'cliVersion':'kodus-review/'+VERSION})

BLOCKING = ('critical','high','major','error')
VERIFIED_KEYS = ('tree', 'base_tree', 'patch_sha256', 'submitted_patch_sha256', 'files_sha256',
                 'snapshot_manifest_sha256', 'base_manifest_sha256', 'scope_sha256', 'brief_sha256',
                 'changed_paths', 'dispositions', 'submitted_paths', 'notified_paths', 'ignored_paths', 'withheld_paths',
                 'all_changed_paths_ignored', 'ignores_sha256', 'ignores_commit', 'ignores_source')
ARTIFACT_HASHES = (('patch_path','patch_sha256'), ('submitted_patch_path','submitted_patch_sha256'),
                   ('files_path','files_sha256'), ('manifest_path','snapshot_manifest_sha256'),
                   ('base_manifest_path','base_manifest_sha256'), ('scope_path','scope_sha256'),
                   ('ignores_path','ignores_sha256'), ('brief_path','brief_sha256'))

# Allowed difference between this host's clock (capture time) and Kodus's (job createdAt).
JOB_CLOCK_SKEW = 120

def timestamp(value):
    """Seconds since the epoch for an ISO-8601 UTC time such as 2026-11-07T11:58:08.382Z."""
    match = re.fullmatch(r'(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d{1,9})?Z', value or '')
    if not match:
        raise Refusal('Missing or malformed UTC timestamp')
    return calendar.timegm(tuple(int(g) for g in match.groups()) + (0, 0, 0))

# verify never trusts a path or a size from the evidence: every artifact lives at a fixed name in
# the evidence file's own .artifacts directory and every read is bounded.
def artifact_files():
    """{evidence key: (fixed name, bound)}, read at call time from the shared constants."""
    return {'patch_path': ('candidate.patch', MAX_FULL_PATCH_BYTES),
            'submitted_patch_path': ('submitted.patch', MAX_PATCH_BYTES),
            'files_path': ('files.json', MAX_FILES_JSON_BYTES),
            'manifest_path': ('manifest.json', MAX_MANIFEST_BYTES),
            'base_manifest_path': ('base-manifest.json', MAX_MANIFEST_BYTES),
            'scope_path': ('scope.json', MAX_SCOPE_JSON_BYTES),
            'ignores_path': ('ignores.json', MAX_IGNORES_BYTES),
            'brief_path': ('brief.txt', MAX_BRIEF_BYTES)}
ARTIFACT_DIRS = {'snapshot_path': 'snapshot', 'base_snapshot_path': 'base-snapshot'}

def bounded_read(path, limit):
    """A regular file, opened without following a symlink, refused above `limit` bytes."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    except OSError:
        raise Refusal('Artifact is missing or a symlink: ' + path.name) from None
    with os.fdopen(fd, 'rb') as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise Refusal('Artifact is not a regular file: ' + path.name)
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise Refusal('Artifact exceeds its size bound: ' + path.name)
    return data

def artifact_layout(evidence_path, candidate):
    directory = Path(str(evidence_path) + '.artifacts')
    # The evidence location is the root's own choice; nothing inside the artifacts directory may
    # be a link (files are opened without following one; snapshot walks refuse links).
    if directory.is_symlink() or not directory.is_dir():
        raise Refusal('Evidence artifacts directory is missing or is a symlink')
    paths = {key: directory / name for key, (name, _) in artifact_files().items()}
    paths.update({key: directory / name for key, name in ARTIFACT_DIRS.items()})
    for key, path in paths.items():
        if candidate.get(key) != str(path):
            raise Refusal('Evidence names an artifact outside its own artifacts directory: ' + key)
    return paths

def snapshot_files(root):
    """{relative path: sha256} of a snapshot directory, refusing links and bounded in entries and bytes."""
    if root.is_symlink() or not root.is_dir():
        raise Refusal('Snapshot is missing or is a symlink: ' + root.name)
    found, entries, total = {}, 0, 0
    for current, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            entries += 1
            if entries > MAX_SNAPSHOT_ENTRIES:
                raise Refusal('Snapshot exceeds its entry bound: ' + root.name)
            if os.path.islink(os.path.join(current, name)):
                raise Refusal('Snapshot contains a symlink: ' + root.name)
        for name in files:
            path = Path(current) / name
            data = bounded_read(path, SCOPE_CEILINGS['snapshot_budget_bytes'] - total)
            total += len(data)
            found[str(path.relative_to(root))] = digest(data)
    return found

def verify(evidence_path, repo):
    """Recompute a review's candidate from Git and the launcher-pinned render, and compare."""
    if not evidence_path.is_absolute() or not repo.is_absolute():
        raise Refusal('verify needs absolute --evidence and --repo paths')
    if not os.environ.get('KODUS_REVIEW_IGNORES_RENDERER'):
        raise Refusal('verify runs only through the launcher (KODUS_REVIEW_IGNORES_RENDERER)')
    evidence_bytes = bounded_read(evidence_path, MAX_EVIDENCE_BYTES)
    evidence = json.loads(evidence_bytes)
    if not isinstance(evidence, dict):
        raise Refusal('Evidence is not a JSON object')
    candidate, kodus = evidence.get('candidate'), evidence.get('kodus')
    if evidence.get('schema_version') != 2 or not isinstance(candidate, dict) or not isinstance(kodus, dict):
        raise Refusal('Evidence is not a schema 2 adapter result with a candidate')
    repo = repo.resolve()
    name = identity(repo)
    if candidate.get('repository') != name:
        raise Refusal('Evidence names a different repository')
    if candidate.get('ignores_renderer_verified') is not True:
        raise Refusal('Evidence was not bound to the launcher renderer')
    layout = artifact_layout(evidence_path, candidate)
    if repo in evidence_path.absolute().parents:
        require_unaddable(repo, evidence_path.absolute(), Path(str(evidence_path) + '.artifacts'))
    artifacts = {key: bounded_read(layout[key], limit) for key, (_, limit) in artifact_files().items()}
    for path_key, hash_key in ARTIFACT_HASHES:
        if digest(artifacts[path_key]) != candidate[hash_key]:
            raise Refusal('Artifact differs from its recorded hash: ' + path_key)
    ignores = load_ignores(layout['ignores_path'], name)
    bind_ignores(*ignores)
    base, head, tree = candidate['base'], candidate['head'], candidate['tree']
    for value in (base, head, tree):
        if value is not None and (not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{40}', value)):
            raise Refusal('Evidence revision identities are malformed')
    if head and git(repo, 'rev-parse', head + '^{tree}').decode().strip() != tree:
        raise Refusal('Evidence tree differs from its head')
    if not head and worktree_tree(repo) != tree:
        # The recorded tree is the evidence author's claim; recompute it exactly as capture does.
        raise Refusal('Working tree differs from the evidence tree; if it has moved on, re-run the review')
    brief = artifacts['brief_path']
    recorded_scope = json.loads(artifacts['scope_path'])
    with tempfile.TemporaryDirectory(prefix='kodus-verify-') as tmp:
        directory = Path(tmp) / 'artifacts'; directory.mkdir()
        fresh, patch = capture(repo, base, head, directory, brief, ignores, tree=tree,
                               ondisk=recorded_scope.get('ondisk_secret_blobs') or [])
        for key in VERIFIED_KEYS:
            if fresh[key] != candidate[key]:
                raise Refusal('Recomputed candidate differs: ' + key)
        manifest = json.loads(Path(fresh['manifest_path']).read_bytes())
        base_manifest = json.loads(Path(fresh['base_manifest_path']).read_bytes())
        files_bytes = Path(fresh['files_path']).read_bytes()
    for key, entries in (('snapshot_path', manifest), ('base_snapshot_path', base_manifest)):
        found = snapshot_files(layout[key])
        if found != {e['path']: e['sha256'] for e in entries}:
            raise Refusal('Snapshot files differ from the recomputed manifest: ' + layout[key].name)
    fast = kodus.get('fast', False)
    if not isinstance(fast, bool):
        raise Refusal('Evidence kodus.fast is not a boolean')
    verdict = kodus.get('verdict')
    if verdict == 'SKIPPED':
        if not fresh['all_changed_paths_ignored'] or kodus.get('skip_reason') != SKIP_ALL_IGNORED:
            raise Refusal('SKIPPED is not eligible: not every changed path is ignored')
    elif verdict in ('PASS', 'FAIL'):
        if fresh['all_changed_paths_ignored']:
            raise Refusal('A Kodus verdict on a candidate that must be SKIPPED')
        if kodus.get('request_sha256') != digest(request_body(candidate, files_bytes, patch, fast)):
            raise Refusal('Recorded request differs from the recomputed submission')
        # The stored result is the lane's copy: fetch the job from Kodus and require it to match.
        # Always the launcher-pinned endpoint and token; evidence never chooses them. A capture made
        # against the localhost fixture is only verifiable from inside that fixture.
        request, _ = kodus_client()
        if kodus.get('api_origin') != request.origin or kodus.get('synthetic_fixture') is not request.synthetic:
            raise Refusal('Evidence was captured against a different Kodus endpoint or key class')
        fetched = request(job_path(kodus.get('job_id')))
        if fetched.get('status') != 'COMPLETED':
            raise Refusal('Kodus job is not COMPLETED')
        result = fetched.get('result')
        if encoded(result) != encoded(kodus.get('result')):
            raise Refusal('Kodus job result differs from the evidence')
        if (not isinstance(result, dict) or not isinstance(result.get('issues'), list)
                or type(result.get('filesAnalyzed')) is not int
                or result['filesAnalyzed'] != len(fresh['submitted_paths']) + len(fresh['notified_paths'])):
            raise Refusal('Kodus result does not cover the recomputed submission')
        created = fetched.get('createdAt')
        if created != kodus.get('job_created_at'):
            raise Refusal('Kodus job creation time differs from the evidence')
        if timestamp(created) < timestamp(candidate.get('captured_at')) - JOB_CLOCK_SKEW:
            raise Refusal('Kodus job was created before the candidate was captured')
        blocking = any(isinstance(i, dict) and i.get('severity') in BLOCKING for i in result['issues'])
        if verdict != ('FAIL' if blocking else 'PASS'):
            raise Refusal('Kodus verdict differs from its findings')
    else:
        raise Refusal('Evidence has no Kodus verdict to verify')
    return {'verdict':'VERIFIED', 'kodus_verdict':verdict, 'repository':name, 'base':base, 'head':head,
            'tree':tree, 'base_tree':fresh['base_tree'], 'job_id':kodus.get('job_id'),
            'submitted_paths':fresh['submitted_paths'], 'fast':fast, 'evidence_sha256':digest(evidence_bytes)}

def verify_main(argv):
    parser=argparse.ArgumentParser(prog='kodus-review verify', description='Recompute and check a review result.')
    parser.add_argument('--evidence',type=Path,required=True)
    parser.add_argument('--repo',type=Path,required=True)
    args=parser.parse_args(argv)
    try:
        report=verify(args.evidence,args.repo);code=0
    except (Refusal,OSError,UnicodeError,MemoryError,RecursionError,ValueError,KeyError,TypeError,
            subprocess.SubprocessError) as exc:
        report={'verdict':'MISMATCH','error':str(exc) if isinstance(exc,Refusal) else 'Evidence could not be verified'};code=2
    except Exception:
        report={'verdict':'MISMATCH','error':'Unexpected verification failure'};code=2
    print(json.dumps(report,sort_keys=True))
    return code

def main():
    if sys.argv[1:2] == ['verify']:
        return verify_main(sys.argv[2:])
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo',type=Path,default=Path.cwd())
    parser.add_argument('--base',default='HEAD')
    parser.add_argument('--head')
    parser.add_argument('--brief',type=Path,required=True)
    parser.add_argument('--out',type=Path,required=True)
    # Not argparse-required: a missing document must still leave a refusal in the evidence.
    parser.add_argument('--ignores',type=Path)
    parser.add_argument('--fast',action='store_true')
    args=parser.parse_args()
    evidence={'schema_version':2, 'candidate':None, 'kodus':{'verdict':'NO_VERDICT','context':'partial-or-unknown'},
              'local_review_required':True, 'overall_verdict':'PENDING'}
    if args.fast:
        evidence['kodus']['fast']=True
    out=args.out.absolute(); directory=Path(str(out)+'.artifacts')
    # Exclusive reservation prevents a retry from replacing earlier review evidence.
    try:
        out.parent.mkdir(parents=True,exist_ok=True)
        stream=os.fdopen(os.open(out,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'wb')
        stream.write(encoded(evidence));stream.flush()
    except OSError:
        print('kodus-review: output already exists or cannot be created',file=sys.stderr)
        return 2
    def persist():
        stream.seek(0);stream.truncate();stream.write(encoded(evidence));stream.flush()
        os.fsync(stream.fileno())
    code=2
    try:
        repo=args.repo.resolve()
        name=identity(repo)
        if repo in out.parents:
            # check-ignore exits 0 when ANY listed path is ignored, so each path is checked on its
            # own. A file inside the artifacts directory also matches directory-only patterns.
            for path in (out, directory/'candidate.patch'):
                run=subprocess.run([GIT,'-C',str(repo),'check-ignore','-q','--',str(path)],
                                   capture_output=True,timeout=10,env=clean_env())
                if run.returncode:
                    raise Refusal('Evidence and its artifacts within a repository must both be Git-ignored')
        base=revision(repo,args.base);head=revision(repo,args.head) if args.head else None
        brief=args.brief.read_bytes()
        if len(brief)>MAX_BRIEF_BYTES or b'\0' in brief:
            raise Refusal('Brief must be bounded text')
        brief.decode('utf-8',errors='strict')
        ignores=load_ignores(args.ignores,name)
        verified=bind_ignores(*ignores)
        directory.mkdir(mode=0o700)
        # Once every artifact exists, nothing under an in-repository directory may be addable.
        guard=(lambda: require_unaddable(repo,out,directory)) if repo in out.parents else None
        candidate,patch=capture(repo,base,head,directory,brief,ignores,guard=guard)
        candidate['repository']=name;candidate['ignores_renderer_verified']=verified
        evidence['candidate']=candidate
        if len(encoded(evidence)) > MAX_EVIDENCE_BYTES - EVIDENCE_RESPONSE_RESERVE:
            evidence['candidate']=None
            raise Refusal('Evidence exceeds its bound before submission; no review verdict')
        persist()
        if candidate['all_changed_paths_ignored']:
            # Nothing in Kodus scope: no token read and no request. The local review still
            # receives the full candidate patch and snapshots.
            evidence['kodus']={'verdict':'SKIPPED','skip_reason':SKIP_ALL_IGNORED,'context':'partial-or-unknown',
                               **({'fast':True} if args.fast else {})}
            code=0
        else:
            evidence['kodus'],code=review(candidate,patch,evidence['kodus'],persist)
            if not head and worktree_tree(repo) != candidate['tree']:
                code=2
                evidence['kodus']['verdict']='NO_VERDICT'
                raise Refusal('Working candidate changed while review was running')
        if code==1:evidence['overall_verdict']='FAIL'
    except (Refusal,OSError,UnicodeError,MemoryError,RecursionError,ValueError,subprocess.SubprocessError) as exc:
        code=2
        evidence['kodus']['verdict']='NO_VERDICT'
        # Never render server bodies, credentials or command stderr into evidence.
        evidence['kodus']['error']=str(exc) if isinstance(exc,Refusal) else 'Candidate or local configuration operation failed'
    except Exception:
        # Exit 1 is the Kodus FAIL code; an unexpected crash must never look like a verdict.
        code=2
        evidence['kodus']['verdict']='NO_VERDICT'
        evidence['kodus']['error']='Unexpected adapter failure'
    evidence['evidence_path']=str(out)
    if len(encoded(evidence)) > MAX_EVIDENCE_BYTES:
        # Keep the evidence verifiable-in-size: drop the bulky payloads and give no verdict.
        code=2
        evidence['kodus']={'verdict':'NO_VERDICT','context':'partial-or-unknown','job_id':evidence['kodus'].get('job_id'),
                           **({'fast':True} if args.fast else {}),
                           'error':'Evidence exceeds its %d-byte bound; no review verdict' % MAX_EVIDENCE_BYTES}
        evidence['overall_verdict']='PENDING'
    persist();os.fchmod(stream.fileno(),0o400)
    stream.close()
    print('Kodus component: '+evidence['kodus']['verdict']+'; independent local review required')
    return code

if __name__=='__main__':
    sys.exit(main())
