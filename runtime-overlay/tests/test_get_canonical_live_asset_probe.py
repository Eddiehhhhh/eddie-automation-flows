"""Opt-in live source/asset probe; writes only isolated candidate output."""
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from test_get_canonical_asset_transaction import Reader
import get_canonical_writer as n
import get_canonical_assets as a
import get_recurring_entry as entry
from sync_getnote_to_raw import load_credentials, fetch_note_detail

@unittest.skipUnless(os.environ.get('GET_ASSET_SAMPLE_FILE'),'live private sample not selected')
class LiveAssets(unittest.TestCase):
 def test_source_and_assets_share_verified_candidate(self):
  identity=json.loads(Path(os.environ['GET_ASSET_SAMPLE_FILE']).read_text())['note_id']
  key,client=load_credentials();note=fetch_note_detail(identity,key,client)
  note=n.source_note(note,note)
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);repo=root/'repo';repo.mkdir();reader=Reader(repo)
   for args in [('init','-q'),('config','user.name','test'),('config','user.email','test@example.invalid')]:reader.local.run(*args)
   rel=n.new_path(note,identity);raw=n.render_exact(note,rel);full='Raw/03 Get/'+rel
   path=repo/full;path.parent.mkdir(parents=True);path.write_bytes(raw)
   reader.local.run('add','--',full);reader.local.run('commit','-qm','base');parent=reader.local.run('rev-parse','HEAD').decode().strip()
   old=n.record(note,note,rel,raw);legacy={'legacyById':{},'adoption_ids':[]};registry={'newById':{identity:old}}
   source=a.SourceWithAssets(object(),root/'assets')
   updated,changes=a.enrich(registry,{rel:raw},{identity:note},source.attachment_files)
   self.assertGreater(len(changes),1)
   plan=n.git_candidate(reader,parent,legacy,updated,changes,{rel:raw})
   candidate=plan['candidate_commit'];self.assertEqual(reader.local.run('rev-parse','HEAD').decode().strip(),parent)
   if entry.transition(reader,None,parent,candidate,({rel:raw},legacy,registry),({rel:changes[rel]},legacy,updated)) != [identity]:
    raise RuntimeError('live_candidate_identity_mismatch')
   output=Path(os.environ['CANDIDATE_OUTPUT']);output.mkdir(parents=True,exist_ok=True)
   for row in plan['changes']:
    data=reader.read(candidate,'Raw/03 Get/'+row['path'],a.MAX_ASSET)
    dest=output/row['path'];dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(data)
   (output/'probe-result.json').write_text(json.dumps({'note_path':rel,'note_id':identity,'asset_paths':sorted(p for p in changes if p!=rel),'source_contract_verified':True,'atomic_candidate_verified':True}))
   print('Live source/attachment atomic candidate verified; asset count',len(changes)-1)
