import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import get_canonical_assets as assets
import get_canonical_writer as n
import get_recurring_entry as entry

class Local:
 def __init__(self,path):self.path=path;self.env=os.environ.copy()
 def run(self,*args,data=None,env=None):
  return subprocess.check_output(['git','-C',str(self.path),*args],input=data,env=env or self.env,stderr=subprocess.PIPE)
 def tree(self,commit):
  result={}
  for row in self.run('ls-tree','-rz','--full-tree',commit).split(b'\0'):
   if row:
    header,path=row.split(b'\t');mode,kind,oid=header.decode().split();result[path.decode()]=(mode,kind,oid)
  return result
 def blob(self,oid):return self.run('cat-file','blob',oid)
class Reader:
 def __init__(self,path):self.local=Local(path)
 def tree(self,commit):return self.local.tree(commit)
 def read(self,commit,path,cap):
  raw=self.local.run('show',commit+':'+path);assert len(raw)<=cap;return raw

class Transaction(unittest.TestCase):
 def test_atomic_note_asset_and_baseline_scope(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);r=Reader(root);r.local.run('init','-q');r.local.run('config','user.name','test');r.local.run('config','user.email','test@example.invalid')
   rel='03 纯文本记录/old.md';raw=b'---\nnote_type: "plain_text"\n---\noriginal\n';full='Raw/03 Get/'+rel
   p=root/full;p.parent.mkdir(parents=True);p.write_bytes(raw)
   r.local.run('add','--',full);r.local.run('commit','-qm','base');parent=r.local.run('rev-parse','HEAD').decode().strip()
   binary=b'image';asset='_assets/images/'+hashlib.sha256(binary).hexdigest()+'.png';enriched=assets.append(rel,raw,{asset:binary})
   old={'filePath':rel,'mirror_sha256':hashlib.sha256(raw).hexdigest(),'baseline_blob':n.legacy.retention.oid(raw),'created_at':'x','preview_anchor':None,'unknown_sha256':'x','text_fields':[],'source_metadata':{}}
   new={**old,'mirror_sha256':hashlib.sha256(enriched).hexdigest(),'baseline_blob':n.legacy.retention.oid(enriched)}
   legacy={'legacyById':{},'adoption_ids':[]};registry={'newById':{'123':new}}
   plan=n.git_candidate(r,parent,legacy,registry,{rel:enriched,asset:binary},{rel:raw})
   commit=plan['candidate_commit'];self.assertEqual(r.read(commit,'Raw/03 Get/'+asset,100),binary)
   self.assertEqual(r.local.run('rev-parse','HEAD').decode().strip(),parent)
   self.assertEqual(entry.transition(r,None,parent,commit,({rel:raw},legacy,{'newById':{'123':old}}),({rel:enriched},legacy,registry)),['123'])
   with self.assertRaises(Exception):n.git_candidate(r,parent,legacy,registry,{rel:enriched,'_assets/files/unowned.bin':b'x'},{rel:raw})
