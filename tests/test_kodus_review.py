"""Public CLI proof with real Git and synthetic authenticated HTTP responses; no paid API."""
import hashlib
import http.server
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

HERE = Path(__file__).resolve().parent
PLUGIN = HERE.parent if (HERE.parent / 'tools').is_dir() else HERE.parent / 'plugins/agent-workflows'
CLI = PLUGIN / 'tools/kodus-review/kodus_review.py'

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
        self.calls=[]; self.response={'summary':'done','issues':[],'filesAnalyzed':1,'duration':1}
        self.http_status=200; self.job_status='COMPLETED'; self.mutate_on_poll=False; self.move_on_poll=False
        owner=self
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_POST(self):
                body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                owner.calls.append((self.path,dict(self.headers),body))
                self.send_response(owner.http_status); self.end_headers()
                self.wfile.write(json.dumps({'data':{'jobId':'synthetic-job','status':'PENDING'}}).encode())
            def do_GET(self):
                if owner.move_on_poll:
                    owner.repo.rename(owner.root/'moved-repo')
                if owner.mutate_on_poll:
                    (owner.repo/'a.py').write_text('raced = True\n')
                owner.calls.append((self.path,dict(self.headers),None))
                self.send_response(200); self.end_headers()
                self.wfile.write(json.dumps({'data':{'status':owner.job_status,'result':owner.response}}).encode())
        self.server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=self.server.serve_forever,daemon=True);thread.start()
        self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)
        self.env['KODUS_REVIEW_API_URL']=f'http://127.0.0.1:{self.server.server_port}'

    def git(self,*args):
        return subprocess.check_output(['git','-C',str(self.repo),*args],env=self.env,text=True,stderr=subprocess.PIPE)

    def run_review(self,changes=None,*args):
        env={**self.env,**(changes or {})}
        out=self.root/('review-'+str(len(list(self.root.glob('review-*.json'))))+'.json')
        run=subprocess.run([sys.executable,str(CLI),'--repo',str(self.repo),'--base',self.base,
                            '--brief',str(self.brief),'--out',str(out),*args],env=env,
                           capture_output=True,text=True,timeout=8)
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

    def test_unchanged_binary_context_is_retained_but_changed_binary_is_refused(self):
        binary=b'\x89PNG\x00\xfffixture'
        (self.repo/'image.png').write_bytes(binary)
        self.git('add','--','image.png');self.git('-c','commit.gpgsign=false','commit','-qm','asset')
        self.base=self.git('rev-parse','HEAD').strip()
        (self.repo/'a.py').write_text('value = 7\n')
        run,e=self.run_review()
        self.assertEqual(run.returncode,0,run.stderr)
        self.assertEqual((Path(e['candidate']['snapshot_path'])/'image.png').read_bytes(),binary)
        self.assertEqual((Path(e['candidate']['base_snapshot_path'])/'image.png').read_bytes(),binary)
        calls=len(self.calls);(self.repo/'image.png').write_bytes(binary+b'changed')
        run,e=self.run_review();self.assertEqual(run.returncode,2,run.stderr)
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
        self.response['filesAnalyzed']=0
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
                               '--out',e['evidence_path']],env=self.env,capture_output=True,timeout=8)
        self.assertEqual(repeat.returncode,2)
        self.assertEqual(Path(e['evidence_path']).read_bytes(),before)
        self.assertEqual(len(self.calls),calls)

if __name__=='__main__': unittest.main()
