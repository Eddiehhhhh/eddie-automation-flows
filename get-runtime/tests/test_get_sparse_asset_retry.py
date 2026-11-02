"""Cold second-run Git objects; exact selected fetch only, no real network."""
import hashlib,json,subprocess,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import get_canonical_assets as assets
import get_canonical_manual as engine
from get_legacy_adapter import OfflineGit

class SparseRetry(unittest.TestCase):
 def fixture(self,d):
  root=Path(d)/'git';root.mkdir()
  def git(*args):return subprocess.check_output(['git','-C',str(root),*args],stderr=subprocess.PIPE)
  git('init','-q');git('config','user.name','test');git('config','user.email','test@example.invalid')
  note={'id':'1','updated_at':1,'attachments':[{'type':'audio','url':'https://example.com/'+str(i)+'.mp3'} for i in range(3)]+[{'type':'image','url':'https://example.com/x.png'}]}
  def fetch(url,max_bytes):return (b'image','image/png') if '.png' in url else (url.encode(),'audio/mpeg')
  initial=assets.SourceWithAssets(object(),Path(d)/'first',fetch=fetch).attachment_files(note,'a.md')
  body=assets.append('a.md',b'original',initial);values={**initial,'a.md':body}
  for path,data in values.items():
   p=root/'Raw/03 Get'/path;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data)
  git('add','--','Raw/03 Get');git('commit','-qm','synthetic stored partial assets');parent=git('rev-parse','HEAD').decode().strip();reader=OfflineGit(root.resolve());meta={}
  for path,(mode,kind,blob) in reader.tree(parent).items():meta[path]=(mode,kind,blob,len(values[path[len('Raw/03 Get/'):]]))
  for path in initial:
   oid=meta['Raw/03 Get/'+path][2];(root/'.git/objects'/oid[:2]/oid[2:]).unlink()
  container=Path(d)/'cold';container.mkdir();engine.bind_asset_reader(reader,object(),meta,'synthetic-only',container,parent)
  registry={'newById':{'1':{'filePath':'a.md','mirror_sha256':hashlib.sha256(body).hexdigest(),'source_version':{'type':'integer','value':1}}}}
  return reader,parent,note,initial,body,registry,meta,values
 def test_cold_second_run_restores_exact_images_and_audio_once_per_oid(self):
  with tempfile.TemporaryDirectory() as d:
   reader,parent,note,initial,body,registry,meta,values=self.fixture(d);calls=[]
   def selected(remote,rows,token,directory):
    calls.extend(row[2] for row in rows.values());return {p:values[p[len('Raw/03 Get/'):]] for p in rows}
   downloads=[]
   def fetch(url,max_bytes):downloads.append(url);return url.encode(),'audio/mpeg'
   source=assets.SourceWithAssets(object(),Path(d)/'second',fetch=fetch)
   with patch.object(engine.m,'fetch_selected',side_effect=selected):
    _,outputs=assets.enrich(registry,{}, {'1':note},source.attachment_files,current={'a.md':body},retry=['1'],read_asset=lambda p:engine.read_saved_asset(reader,parent,p))
   self.assertEqual(downloads,['https://example.com/2.mp3'])
   self.assertEqual(len(calls),len(set(calls)))
   self.assertFalse(any(p.endswith('.pending.json') for p in outputs))
   for path,data in initial.items():
    if not path.endswith(('.pending.json','.media.json')):self.assertEqual(outputs[path],data)
   self.assertEqual(reader.local.run('rev-parse','HEAD').decode().strip(),parent)
 def test_missing_transport_or_corrupt_return_fails_before_source_or_ref_change(self):
  for corrupt in (False,True):
   with tempfile.TemporaryDirectory() as d:
    reader,parent,note,initial,body,registry,meta,values=self.fixture(d)
    def failure(remote,rows,token,directory):
     if corrupt:return {p:b'corrupt' for p in rows}
     raise OSError('synthetic offline')
    source=assets.SourceWithAssets(object(),Path(d)/'second',fetch=lambda *a,**k:(_ for _ in ()).throw(AssertionError('must fail before CDN')))
    with patch.object(engine.m,'fetch_selected',side_effect=failure),self.assertRaises(Exception):
     assets.enrich(registry,{}, {'1':note},source.attachment_files,current={'a.md':body},retry=['1'],read_asset=lambda p:engine.read_saved_asset(reader,parent,p))
    self.assertEqual(reader.read(parent,'Raw/03 Get/a.md',100000),body)
    self.assertEqual(reader.local.run('rev-parse','HEAD').decode().strip(),parent)
    self.assertEqual(registry['newById']['1']['mirror_sha256'],hashlib.sha256(body).hexdigest())

 def test_cumulative_download_budget_counts_unique_oids_and_parent_is_pinned(self):
  with tempfile.TemporaryDirectory() as d:
   reader,parent,note,initial,body,registry,meta,values=self.fixture(d)
   binaries=[p for p in initial if p.endswith('.bin')];self.assertEqual(len(binaries),2)
   calls=[]
   def selected(remote,rows,token,directory):
    calls.extend(rows);return {p:values[p[len('Raw/03 Get/'):]] for p in rows}
   first,second=binaries;cap=len(initial[first])+len(initial[second])-1
   with patch.object(engine.m,'fetch_selected',side_effect=selected),patch.object(engine.m,'MAX_CHANGED_BYTES',cap):
    self.assertEqual(engine.read_saved_asset(reader,parent,first),initial[first])
    self.assertEqual(engine.read_saved_asset(reader,parent,first),initial[first])
    with self.assertRaisesRegex(engine.m.ManualFailure,'manual_budget'):engine.read_saved_asset(reader,parent,second)
   self.assertEqual(calls,['Raw/03 Get/'+first])
   # A different commit with a different asset entry cannot use old metadata.
   for saved,data in initial.items():engine.m.import_blob(reader.local,data,meta['Raw/03 Get/'+saved][2])
   full='Raw/03 Get/'+second
   (reader.local.vault/full).write_bytes(b'foreign bytes')
   reader.local.run('add','--',full);reader.local.run('commit','-qm','synthetic foreign tree')
   newer=reader.local.run('rev-parse','HEAD').decode().strip()
   engine.bind_asset_reader(reader,object(),meta,'synthetic-only',Path(d)/'other',newer)
   with patch.object(engine.m,'fetch_selected',side_effect=AssertionError('must reject wrong parent mapping')):
    with self.assertRaisesRegex(engine.m.ManualFailure,'manual_readback'):engine.read_saved_asset(reader,newer,second)
   self.assertEqual(reader.local.run('rev-parse','HEAD').decode().strip(),newer)
 def test_corrupt_local_object_is_not_treated_as_downloadable_missing_object(self):
  with tempfile.TemporaryDirectory() as d:
   reader,parent,note,initial,body,registry,meta,values=self.fixture(d)
   path=next(p for p in initial if p.endswith('.bin'));oid=meta['Raw/03 Get/'+path][2]
   loose=reader.local.vault/'.git/objects'/oid[:2]/oid[2:];loose.parent.mkdir(exist_ok=True);loose.write_bytes(b'not a git object')
   with patch.object(engine.m,'fetch_selected',side_effect=AssertionError('do not overwrite corrupt object')):
    with self.assertRaisesRegex(engine.m.ManualFailure,'manual_readback'):engine.read_saved_asset(reader,parent,path)
   self.assertEqual(loose.read_bytes(),b'not a git object')
