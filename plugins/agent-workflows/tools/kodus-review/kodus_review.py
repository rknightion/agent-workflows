#!/usr/bin/env python3
"""Authenticated Kodus component review, with an immutable candidate for independent local review."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

VERSION = '1'
MAX_TREE_BYTES = 64 * 1024 * 1024
MAX_PATCH_BYTES = 1500000
MAX_RESPONSE_BYTES = 8 * 1024 * 1024

class Refusal(Exception):
    pass

def digest(data):
    return hashlib.sha256(data).hexdigest()

def encoded(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=True, indent=2) + '\n').encode()

def git(repo, *args, index=None):
    env = {k:v for k,v in os.environ.items() if not k.startswith('GIT_')}
    env['GIT_TERMINAL_PROMPT'] = '0'
    if index:
        env['GIT_INDEX_FILE'] = str(index)
    run = subprocess.run(['git', '-C', str(repo), *args], env=env, capture_output=True, timeout=30)
    if run.returncode:
        raise Refusal('Git candidate operation failed: ' + args[0])
    return run.stdout

def identity(repo):
    if os.environ.get('KODUS_REVIEW_CONTEXT') != 'personal':
        raise Refusal('KODUS_REVIEW_CONTEXT must explicitly be personal')
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

def worktree_tree(repo):
    with tempfile.TemporaryDirectory(prefix='kodus-index-') as tmp:
        index = Path(tmp) / 'index'
        git(repo, 'read-tree', 'HEAD', index=index)
        names = git(repo, 'ls-files', '-z', '--cached', '--others', '--exclude-standard').split(b'\0')
        names += git(repo, 'ls-tree', '-r', '--name-only', '-z', 'HEAD').split(b'\0')
        paths = sorted({os.fsdecode(n) for n in names if n})
        if any((repo / name).is_dir() for name in paths):
            raise Refusal('Tracked file replaced by directory; refusing recursive force-add')
        for start in range(0, len(paths), 100):
            # Force only the enumerated tracked/nonignored paths, never an ignored directory
            # sweep. Literal pathspecs keep Git metacharacters from broadening this inventory.
            git(repo, '--literal-pathspecs', 'add', '--force', '--', *paths[start:start+100], index=index)
        return git(repo, 'write-tree', index=index).decode().strip()

def snapshot_tree(repo, tree, destination):
    manifest, total = [], 0
    destination.mkdir()
    for entry in git(repo, 'ls-tree', '-r', '-z', tree).split(b'\0'):
        if not entry:
            continue
        meta, raw_path = entry.split(b'\t', 1)
        mode, kind, blob = meta.decode().split()
        name = raw_path.decode('utf-8', errors='strict')
        rel = PurePosixPath(name)
        if mode not in ('100644', '100755') or kind != 'blob':
            raise Refusal('Symlinks and submodules are unsupported in either snapshot')
        if rel.is_absolute() or '..' in rel.parts or '\\' in name or any(ord(c)<32 for c in name):
            raise Refusal('Unsupported candidate path')
        size = int(git(repo, 'cat-file', '-s', blob))
        total += size
        if total > MAX_TREE_BYTES:
            raise Refusal('Oversized candidate tree is unsupported')
        data = git(repo, 'cat-file', 'blob', blob)
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(0o555 if mode == '100755' else 0o444)
        manifest.append({'path':name, 'mode':mode, 'blob':blob, 'sha256':digest(data)})
    return encoded(manifest)

def capture(repo, base, head, directory, brief):
    tree = git(repo, 'rev-parse', head + '^{tree}').decode().strip() if head else worktree_tree(repo)
    patch = git(repo, 'diff', '--no-color', '--no-relative', '--no-ext-diff', '--no-textconv', '--binary',
                '--no-renames', '--src-prefix=a/', '--dst-prefix=b/', '--line-prefix=',
                base, tree, '--')
    if not patch:
        raise Refusal('No candidate changes; no review verdict')
    # Binary markers are Git header lines, never prefixed unified-hunk content.
    if len(patch) > MAX_PATCH_BYTES or re.search(
            rb'^(?:GIT binary patch|Binary files [^\n]* differ)$', patch, re.MULTILINE):
        raise Refusal('Binary or oversized patch is unsupported')
    patch.decode('utf-8', errors='strict')
    changed = git(repo, 'diff', '--name-only', '--no-renames', '-z', base, tree, '--').split(b'\0')
    changed = [p.decode('utf-8') for p in changed if p]
    # Pinned vendor parser accepts ordinary spaces, but trims names, splits at
    # 'diff --git', and uses a greedy ' b/' delimiter for deleted paths. Keep
    # these ambiguous forms and all quoted/escaped names outside the grammar.
    if any(not re.fullmatch(r'[A-Za-z0-9_./@+ -]+', p)
           or any(part != part.strip() for part in p.split('/'))
           or ' b/' in p or 'diff --git' in p for p in changed):
        raise Refusal('Changed path is outside the supported raw-diff grammar')
    statuses = git(repo, 'diff', '--name-status', '--no-renames', '-z', base, tree, '--').split(b'\0')
    pairs = list(zip(statuses[0:-1:2], statuses[1:-1:2]))
    if len(pairs) != len(changed) or [p.decode('utf-8') for _, p in pairs] != changed or len(pairs) > 500:
        raise Refusal('Structured file inventory differs from candidate paths or exceeds API limit')
    files = []
    for status, raw_path in pairs:
        name = raw_path.decode('utf-8')
        kind = {b'A': 'added', b'M': 'modified', b'D': 'deleted'}.get(status)
        if not kind or len(name) > 500:
            raise Refusal('Unsupported structured file status or path length')
        # Even a literal Git pathspec includes descendants when a file becomes a
        # directory (or vice versa). Exclude that subtree explicitly. The grammar
        # above admits no glob metacharacters, so only the appended /** is a glob.
        file_patch = git(repo, 'diff', '--no-color', '--no-relative', '--no-ext-diff', '--no-textconv',
                         '--binary', '--no-renames', '--src-prefix=a/', '--dst-prefix=b/',
                         '--line-prefix=', base, tree, '--', ':(top,literal)' + name,
                         ':(top,exclude,glob)' + name + '/**').decode('utf-8')
        if not file_patch or len(file_patch.encode('utf-8')) > 500000:
            raise Refusal('Empty or oversized structured file patch')
        # content is required by the DTO; empty sends no source beyond the existing diff.
        files.append({'path': name, 'status': kind, 'diff': file_patch, 'content': ''})
    if ''.join(f['diff'] for f in files).encode('utf-8') != patch:
        raise Refusal('Structured patches do not exactly cover the aggregate candidate patch')
    files_bytes = encoded(files)
    snapshot = directory/'snapshot'; base_snapshot = directory/'base-snapshot'
    manifest_bytes = snapshot_tree(repo, tree, snapshot)
    base_manifest = snapshot_tree(repo, base, base_snapshot)
    for name,data in [('manifest.json',manifest_bytes),('base-manifest.json',base_manifest),
                      ('candidate.patch',patch),('brief.txt',brief),('files.json',files_bytes)]:
        path=directory/name;path.write_bytes(data);path.chmod(0o444)
    if not head and worktree_tree(repo) != tree:
        raise Refusal('Working candidate changed during capture')
    return {'base':base, 'head':head, 'tree':tree, 'patch_sha256':digest(patch),
            'brief_sha256':digest(brief), 'files_sha256':digest(files_bytes),
            'files_path':str(directory/'files.json'), 'snapshot_manifest_sha256':digest(manifest_bytes),
            'base_manifest_sha256':digest(base_manifest), 'changed_paths':changed,
            'snapshot_path':str(snapshot), 'base_snapshot_path':str(base_snapshot),
            'manifest_path':str(directory/'manifest.json'),
            'base_manifest_path':str(directory/'base-manifest.json'),
            'patch_path':str(directory/'candidate.patch'), 'brief_path':str(directory/'brief.txt')}, patch

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

def review(candidate, patch, audit, persist):
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
    deadline = time.monotonic() + positive_env('KODUS_REVIEW_TIMEOUT', '900', 1800)
    interval = positive_env('KODUS_REVIEW_POLL_INTERVAL', '1', 5)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    def request(path, body=None):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise Refusal('Review deadline exceeded')
        headers={'X-Team-Key':token, 'Accept':'application/json', 'User-Agent':'kodus-review/'+VERSION}
        if body is not None:
            headers.update({'X-Kodus-Async':'1', 'Content-Type':'application/json'})
        req=urllib.request.Request(endpoint+path, data=body, headers=headers)
        try:
            with opener.open(req, timeout=min(remaining,30)) as response:
                data=response.read(MAX_RESPONSE_BYTES+1)
                if len(data)>MAX_RESPONSE_BYTES:
                    raise Refusal('Oversized API response')
                value=json.loads(data.replace(token.encode(), b'[REDACTED]'))
        except urllib.error.HTTPError as exc:
            raise Refusal('Authenticated API HTTP '+str(exc.code)) from None
        except (urllib.error.URLError, TimeoutError, ValueError):
            raise Refusal('API transport or JSON failure') from None
        if not isinstance(value,dict):
            raise Refusal('Malformed API envelope')
        if 'data' in value:
            value=value['data']
        if not isinstance(value,dict):
            raise Refusal('Malformed API data envelope')
        return value
    # The service rejects config.focus; intent is retained only for the independent local agent.
    files_bytes=Path(candidate['files_path']).read_bytes()
    if digest(files_bytes) != candidate['files_sha256']:
        raise Refusal('Structured file artifact changed before submission')
    files=json.loads(files_bytes)
    if ''.join(f['diff'] for f in files).encode('utf-8') != patch:
        raise Refusal('Structured request differs from captured aggregate patch')
    body=encoded({'diff':patch.decode(), 'config':{'files':files}, 'gitRemote':'https://github.com/'+candidate['repository']+'.git',
                  'mergeBaseSha':candidate['base'], 'commitSha':candidate['head'] or candidate['base'],
                  'cliVersion':'kodus-review/'+VERSION})
    queued=request('/cli/review', body)
    audit['enqueue_response']=queued
    persist()
    job=queued.get('jobId')
    if not isinstance(job,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,200}',job):
        raise Refusal('Missing async job identity; legacy synchronous fallback is refused')
    audit['job_id']=job
    persist()
    while True:
        if time.monotonic()+interval>=deadline:
            raise Refusal('Review deadline exceeded')
        time.sleep(interval)
        polled=request('/cli/review/jobs/'+urllib.parse.quote(job,safe=''))
        state=polled.get('status')
        if state not in ('PENDING','PROCESSING'):
            audit['terminal_response']=polled
            persist()
        if state=='COMPLETED':
            result=polled.get('result')
            break
        if state not in ('PENDING','PROCESSING'):
            raise Refusal('Review job failed or has unknown status')
    audit['result']=result
    persist()
    if not isinstance(result,dict) or not isinstance(result.get('issues'),list):
        raise Refusal('Malformed review result')
    if type(result.get('filesAnalyzed')) is not int or result['filesAnalyzed'] != len(candidate['changed_paths']):
        raise Refusal('Reported file count differs from submitted paths; no complete component verdict')
    blocking=False
    for finding in result['issues']:
        if not isinstance(finding,dict) or finding.get('severity') not in ('critical','high','major','error','medium','warning','low','minor','info'):
            raise Refusal('Unknown or malformed finding severity')
        if (not isinstance(finding.get('file'),str) or not finding['file'].strip()
                or type(finding.get('line')) is not int or finding['line']<0
                or not isinstance(finding.get('message'),str) or not finding['message'].strip()):
            raise Refusal('Malformed finding location or message')
        blocking |= finding['severity'] in ('critical','high','major','error')
    return {**audit, 'verdict':'FAIL' if blocking else 'PASS', 'context':'partial-or-unknown',
            'job_id':job, 'result':result}, 1 if blocking else 0

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo',type=Path,default=Path.cwd())
    parser.add_argument('--base',default='HEAD')
    parser.add_argument('--head')
    parser.add_argument('--brief',type=Path,required=True)
    parser.add_argument('--out',type=Path,required=True)
    args=parser.parse_args()
    evidence={'schema_version':1, 'candidate':None, 'kodus':{'verdict':'NO_VERDICT','context':'partial-or-unknown'},
              'local_review_required':True, 'overall_verdict':'PENDING'}
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
            run=subprocess.run(['git','-C',str(repo),'check-ignore','-q','--',str(out),str(directory)],capture_output=True,timeout=10)
            if run.returncode:
                raise Refusal('Evidence within a repository must be Git-ignored')
        base=revision(repo,args.base);head=revision(repo,args.head) if args.head else None
        brief=args.brief.read_bytes()
        if len(brief)>100000 or b'\0' in brief:
            raise Refusal('Brief must be bounded text')
        brief.decode('utf-8',errors='strict')
        directory.mkdir(mode=0o700)
        candidate,patch=capture(repo,base,head,directory,brief)
        candidate['repository']=name;evidence['candidate']=candidate
        persist()
        evidence['kodus'],code=review(candidate,patch,evidence['kodus'],persist)
        if not head and worktree_tree(repo) != candidate['tree']:
            code=2
            evidence['kodus']['verdict']='NO_VERDICT'
            raise Refusal('Working candidate changed while review was running')
        if code==1:evidence['overall_verdict']='FAIL'
    except (Refusal,OSError,UnicodeError,subprocess.SubprocessError) as exc:
        code=2
        evidence['kodus']['verdict']='NO_VERDICT'
        # Never render server bodies, credentials or command stderr into evidence.
        evidence['kodus']['error']=str(exc) if isinstance(exc,Refusal) else 'Candidate or local configuration operation failed'
    evidence['evidence_path']=str(out)
    persist();os.fchmod(stream.fileno(),0o400)
    stream.close()
    print('Kodus component: '+evidence['kodus']['verdict']+'; independent local review required')
    return code

if __name__=='__main__':
    sys.exit(main())
