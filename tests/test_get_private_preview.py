import sys,json,subprocess,tempfile,unittest
from pathlib import Path
from copy import deepcopy
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from get_mirror_incremental_replay import prepare_private_preview,snapshot,ParityFailure
from get_mirror_render import CompatibleWriter
from source_sync_assets import AssetStore

class PreviewTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.work=Path(self.temp.name);self.vault=self.work/'vault';self.vault.mkdir();self.root=self.vault/'Raw'/'03 Get';(self.root/'03 State').mkdir(parents=True)
  (self.root/'03 State'/'manifest.json').write_text('{"notes":[]}')
  self.git('init','-q');self.git('config','user.email','fixture@example.test');self.git('config','user.name','Fixture');self.git('add','.');self.git('commit','-qm','seed');self.head=self.git('rev-parse','HEAD').strip()
  self.note={'id':'123','updated_at':'2026-10-13 08:00:00','created_at':'2026-10-13 08:00:00','title':'Readable','content':'## Heading\n\n- First\n- Second','note_type':'plain_text'};self.calls=[]
 def git(self,*args):return subprocess.check_output(['git','-C',str(self.vault),*args],text=True)
 def factory(self,root):
  def fetch(*a,**k):raise AssertionError('no expected asset')
  return CompatibleWriter(root,AssetStore(root,fetch=fetch))
 def run_preview(self,folder='preview',fetch=None):
  def detail(i):self.calls.append(i);return deepcopy(self.note)
  return prepare_private_preview(self.vault,self.work/folder,self.head,lambda:([deepcopy(self.note)],True),fetch or detail,self.factory)
 def test_complete_candidate_exact_diff_and_original_unchanged(self):
  before=snapshot(self.root);result,counts=self.run_preview();self.assertEqual(snapshot(self.root),before);self.assertEqual(self.git('rev-parse','HEAD').strip(),self.head)
  self.assertEqual((self.work/'preview').stat().st_mode & 0o777,0o700)
  plan=json.loads((self.work/'preview'/'plan.json').read_text());self.assertFalse(plan['published']);self.assertFalse(plan['apply_ready']);self.assertEqual(plan['source_commit'],self.head)
  paths={x['path'] for x in plan['changes']};self.assertIn('Raw/03 Get/03 纯文本记录/Readable.md',paths);self.assertIn('Raw/03 Get/03 State/cloud-sync-state.json',paths)
  self.assertEqual(counts['changed_paths'],len(paths));self.assertEqual(result['second']['fresh_details'],0);self.assertEqual(self.calls,['123'])
 def test_second_independent_preview_uses_committed_candidate_state(self):
  self.run_preview();import shutil
  shutil.copytree(self.work/'preview'/'candidate',self.root,dirs_exist_ok=True);self.git('add','.');self.git('commit','-qm','synthetic approved candidate');self.head=self.git('rev-parse','HEAD').strip();self.calls=[]
  result,counts=self.run_preview('preview2');self.assertEqual(self.calls,[]);self.assertEqual(result['first']['cached_skipped'],1);self.assertEqual(counts['changed_paths'],0)
 def test_partial_preserves_pending_and_never_success_state(self):
  self.note['note_type']='recorder_audio';before=snapshot(self.root);result,counts=self.run_preview();self.assertEqual(counts['pending'],1);self.assertEqual(snapshot(self.root),before)
  root=self.work/'preview'/'candidate';state=json.loads((root/'03 State'/'cloud-sync-state.json').read_text());self.assertEqual(state['notesByCanonicalId'],{});self.assertIn('123',state['pendingById']);self.assertFalse(json.loads((root/'03 State'/'receipt.json').read_text())['complete'])
 def test_failures_leave_source_and_no_candidate(self):
  before=snapshot(self.root)
  for failure in ('wrong_head','source_change','fetch_error','ignored'):
   with self.subTest(failure=failure):
    original=self.head
    def detail(i):
     if failure=='source_change':(self.root/'outside.txt').write_text('concurrent')
     if failure=='fetch_error':raise RuntimeError('private source error')
     return deepcopy(self.note)
    if failure=='wrong_head':self.head='0'*40
    if failure=='ignored':
     (self.vault/'.gitignore').write_text('Raw/03 Get/03 纯文本记录/*\n');self.git('add','.gitignore');self.git('commit','-qm','ignore fixture');self.head=self.git('rev-parse','HEAD').strip()
    with self.assertRaises(Exception):self.run_preview('failed-'+failure,detail)
    self.assertFalse((self.work/('failed-'+failure)).exists())
    extra=self.root/'outside.txt'
    if extra.exists():extra.unlink()
    self.head=self.git('rev-parse','HEAD').strip()
    self.assertEqual(snapshot(self.root),before)
 def test_output_inside_source_and_existing_output_rejected(self):
  for output in (self.root/'bad',self.vault,self.work):
   with self.assertRaises(ParityFailure):prepare_private_preview(self.vault,output,self.head,lambda:([self.note],True),lambda _:self.note,self.factory)

 def test_assume_unchanged_and_ignored_untracked_not_trusted(self):
  rel='Raw/03 Get/03 State/manifest.json';self.git('update-index','--assume-unchanged',rel)
  (self.root/'03 State'/'manifest.json').write_text('{"notes":[],"extra":"hidden edit"}')
  with self.assertRaisesRegex(ParityFailure,'dirty_preview_source'):self.run_preview()
  self.git('update-index','--no-assume-unchanged',rel);self.git('checkout','--',rel)
  (self.vault/'.gitignore').write_text('Raw/03 Get/hidden.txt\n');self.git('add','.gitignore');self.git('commit','-qm','fixture ignore');self.head=self.git('rev-parse','HEAD').strip();(self.root/'hidden.txt').write_text('not trusted')
  with self.assertRaisesRegex(ParityFailure,'untracked_preview_source'):self.run_preview()
 def test_get_root_symlink_rejected(self):
  import shutil
  other=self.work/'other';shutil.move(self.root,other);self.root.symlink_to(other,target_is_directory=True)
  with self.assertRaises(ParityFailure):self.run_preview()

 def test_cli_conflicting_modes_reject_before_credentials(self):
  script=Path(__file__).resolve().parents[1]/'scripts'/'source_sync_mirror_replay.py'
  for extra in (['--pending-diagnostic'],['--publish-report','does-not-exist']):
   result=subprocess.run([sys.executable,str(script),'--private-preview-vault',str(self.vault),*extra],capture_output=True,text=True)
   self.assertEqual(result.returncode,1);self.assertEqual(json.loads(result.stdout)['error'],'conflicting_modes');self.assertEqual(result.stderr,'')

 def test_preview_report_two_filters_and_no_private_paths(self):
  from source_sync_mirror_replay import safe_preview_report
  result,counts=self.run_preview();value={**result,**counts,'mode':'private_preview','status':'private_preview_ready_for_review','private_path':'secret','changes':[{'path':'secret'}]}
  first=safe_preview_report(value);self.assertEqual(first['status'],'private_preview_ready_for_review');self.assertEqual(safe_preview_report(first),first);self.assertNotIn('secret',json.dumps(first));self.assertFalse(first['apply_ready'])

 def test_publish_report_non_object_never_traceback(self):
  script=Path(__file__).resolve().parents[1]/'scripts'/'source_sync_mirror_replay.py';report=self.work/'report.json'
  for value in ([],None):
   report.write_text(json.dumps(value));result=subprocess.run([sys.executable,str(script),'--publish-report',str(report)],capture_output=True,text=True)
   self.assertEqual(result.returncode,0);self.assertEqual(result.stderr,'');self.assertEqual(json.loads(result.stdout)['status'],'failed')
 def test_preview_cli_filtered_failure_returns_failure(self):
  from unittest.mock import patch
  from types import SimpleNamespace
  import source_sync_mirror_replay as cli
  args=SimpleNamespace(private_preview_vault=self.vault,private_preview_output=self.work/'candidate',expected_source_commit=self.head)
  with patch('sync_getnote_to_raw.load_credentials',return_value=('synthetic','synthetic')),patch('get_mirror_incremental_replay.prepare_private_preview',return_value=({'first':{},'second':{}},{'pending':0,'changed_paths':0,'published':False,'preview_only':True})),patch('builtins.print'):
   self.assertEqual(cli.private_preview_main(args),1)

class PreviewWorkflowTests(unittest.TestCase):
 def test_retention_explicit_scope_and_partial_preserved(self):
  p=Path(__file__).resolve().parents[1]/'.github/workflows/source-sync-shadow.yml'
  text=p.read_text()
  self.assertIn('private_preview, verify_retained, legacy_capture, legacy_verify, full]',text)
  self.assertIn('get_private_preview_retention.py checkout',text)
  self.assertIn('--publish-report "$RETENTION_RESULT"',text)
  self.assertNotIn('upload-artifact',text)
  self.assertNotIn('schedule:',text)
  self.assertIn('get_private_preview_retention.py prepare',text)
  self.assertIn('get_private_preview_retention.py retain',text)
  self.assertLess(text.index('get_private_preview_retention.py prepare'),text.index("--private-preview-vault"))
  self.assertIn('exit "$preview_rc"',text)
  self.assertIn('[ "$preview_rc" != 2 ]',text)
  self.assertIn('persist-credentials: false',text)

 def test_verify_retained_mode_has_no_source_credentials_or_writer(self):
  text=(Path(__file__).resolve().parents[1]/'.github/workflows/source-sync-shadow.yml').read_text()
  step=text.split('      - name: Verify existing retained private preview without Get reads or writes')[1].split('      - name:')[0]
  self.assertIn("inputs.mode == 'verify_retained'",step)
  self.assertIn(' verify-retained ',step)
  self.assertIn('--preview-commit "$RETAINED_PREVIEW"',step)
  for forbidden in ('GETNOTE_API_KEY','GETNOTE_CLIENT_ID',' prepare ',' retain ','source_sync_mirror_replay.py'):
   self.assertNotIn(forbidden,step)

class LegacyVerifyWorkflowTests(unittest.TestCase):
 def test_legacy_verify_is_secret_minimal_and_read_only(self):
  text=(Path(__file__).resolve().parents[1]/'.github/workflows/source-sync-shadow.yml').read_text()
  step=text.split('      - name: Verify retained legacy observations without source reads or writes')[1].split('      - name:')[0]
  self.assertIn("inputs.mode == 'legacy_verify'",step)
  self.assertIn('get_legacy_capture.py --verify-only',step)
  self.assertIn('PRIVATE_VAULT_TOKEN',step)
  self.assertNotIn('GETNOTE_API_KEY',step);self.assertNotIn('GETNOTE_CLIENT_ID',step)
  self.assertNotIn('schedule:',text);self.assertNotIn('upload-artifact',text)
