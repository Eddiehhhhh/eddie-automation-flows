import hashlib
from pathlib import Path
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import get_canonical_assets as a

class CanonicalAssets(unittest.TestCase):
 def test_suffix_preserves_formal_path_original_and_hash(self):
  raw=b'original source body\n';binary=b'image';path='_assets/images/'+hashlib.sha256(binary).hexdigest()+'.png'
  result=a.append('99 录音卡笔记/old.md',raw,{path:binary})
  self.assertTrue(result.startswith(raw));self.assertEqual(a.references({'99 录音卡笔记/old.md':result}),{path})
  a.verify(path,binary)
 def test_forged_hash_escape_and_double_append_rejected(self):
  with self.assertRaises(ValueError):a.verify('_assets/images/'+'0'*64+'.png',b'x')
  with self.assertRaises(ValueError):a.references({'old.md':(a.MARKER+'\n[x](../../secret)').encode()})
  with self.assertRaises(ValueError):a.append('old.md',a.MARKER.encode(),{})

 def test_source_wrapper_only_downloads_disposable_assets(self):
  import tempfile
  from pathlib import Path
  with tempfile.TemporaryDirectory() as d:
   class Source:
    def enumerate(self):return ['unchanged-protocol']
   source=a.SourceWithAssets(Source(),d,fetch=lambda url,max_bytes:(b'image','image/png'))
   note={'content':'![x](https://example.com/a.png)'}
   self.assertEqual(source.enumerate(),['unchanged-protocol'])
   files=source.attachment_files(note,'03 纯文本记录/old.md')
   self.assertEqual(len(files),1)
   for path,data in files.items():a.verify(path,data)
   self.assertEqual(note,{'content':'![x](https://example.com/a.png)'})

 def test_download_failure_does_not_modify_source(self):
  import tempfile
  from source_sync_assets import AssetError
  note={'content':'![x](https://example.com/a.png)'}
  def failure(url,max_bytes):raise AssetError('network failed')
  with tempfile.TemporaryDirectory() as d:
   source=a.SourceWithAssets(object(),d,fetch=failure)
   with self.assertRaises(AssetError):source.attachment_files(note,'03 纯文本记录/old.md')
   self.assertEqual(note,{'content':'![x](https://example.com/a.png)'})
 def test_failed_held_and_audio_status_never_advance_candidate(self):
  from copy import deepcopy
  from unittest.mock import Mock
  import tempfile
  registry={'newById':{'1':{'filePath':'03 纯文本记录/a.md','mirror_sha256':'old','baseline_blob':'old'}}}
  changes={'03 纯文本记录/a.md':b'original'}
  before=deepcopy(registry)
  with tempfile.TemporaryDirectory() as d:
   source=a.SourceWithAssets(object(),d)
   for status,kind in [('failed','image'),('held','file'),('not-downloaded','audio'),('not-downloaded','video')]:
    source.store=Mock();source.store.materialize.return_value=[{'status':status,'kind':kind}]
    with self.assertRaisesRegex(ValueError,'asset_materialization_required'):
     a.enrich(registry,changes,{'1':{'content':'text'}},source.attachment_files)
    self.assertEqual(registry,before);self.assertEqual(changes,{'03 纯文本记录/a.md':b'original'})
 def test_update_rebuilds_suffix_and_baseline_from_new_source_body(self):
  binary=b'new image';asset='_assets/images/'+hashlib.sha256(binary).hexdigest()+'.png'
  registry={'newById':{'1':{'filePath':'03 纯文本记录/a.md','mirror_sha256':'old','baseline_blob':'old'}}}
  outputs={'03 纯文本记录/a.md':b'new source version'}
  updated,files=a.enrich(registry,outputs,{'1':{}},lambda note,path:{asset:binary})
  raw=files['03 纯文本记录/a.md']
  self.assertEqual(raw.count(a.MARKER.encode()),1);self.assertTrue(raw.startswith(b'new source version'))
  self.assertEqual(updated['newById']['1']['mirror_sha256'],hashlib.sha256(raw).hexdigest())
  self.assertEqual(registry['newById']['1']['mirror_sha256'],'old')
