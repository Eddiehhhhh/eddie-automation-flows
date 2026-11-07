import unittest
import source_sync_pack_seed as pack

class ReadbackBudget(unittest.TestCase):
 def test_default_preserves_32_mib_and_get_allows_48_mib(self):
  data={'objects/'+'a'*40:{'sha':'a'*40,'size':46384699}}
  with self.assertRaisesRegex(ValueError,'invalid blob size'):pack.expected_blobs(data)
  self.assertEqual(pack.expected_blobs(data,max_blob=48*pack.MIB),{'a'*40:46384699})
  data['objects/'+'a'*40]['size']=48*pack.MIB+1
  with self.assertRaisesRegex(ValueError,'invalid blob size'):pack.expected_blobs(data,max_blob=48*pack.MIB)
 def test_global_and_conflicting_identity_limits_still_apply(self):
  with self.assertRaisesRegex(ValueError,'invalid blob limit'):pack.expected_blobs({},max_blob=49*pack.MIB)
  data={'objects/a':{'sha':'a'*40,'size':1},'objects/b':{'sha':'a'*40,'size':2}}
  with self.assertRaisesRegex(ValueError,'invalid blob size'):pack.expected_blobs(data,max_blob=48*pack.MIB)
  data={'objects/'+str(i):{'sha':format(i,'040x'),'size':48*pack.MIB} for i in range(6)}
  with self.assertRaisesRegex(ValueError,'seed size limit'):pack.expected_blobs(data,max_blob=48*pack.MIB)
 def test_large_exact_object_round_trip(self):
  import hashlib,subprocess,tempfile
  from pathlib import Path
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);repo=root/'source.git';subprocess.run(['git','init','--bare','-q',str(repo)],check=True)
   payload=b'o'*46384699
   oid=subprocess.run(['git','--git-dir',str(repo),'hash-object','-w','--stdin'],input=payload,capture_output=True,check=True).stdout.decode().strip()
   packed=subprocess.run(['git','--git-dir',str(repo),'pack-objects','--stdout'],input=(oid+'\n').encode(),capture_output=True,check=True).stdout
   packfile=root/'readback.pack';packfile.write_bytes(packed);files={'objects/'+oid:{'sha':oid,'size':len(payload)}};expected=pack.expected_blobs(files,max_blob=48*pack.MIB)
   from unittest.mock import patch
   with patch.object(pack,'child_limits',lambda:None):
    pack.verify_materialize(packfile,root/'readback.git',expected,files,root/'data')
   output=(root/'data'/'objects'/oid).read_bytes();self.assertEqual(len(output),len(payload));self.assertEqual(hashlib.sha256(output).digest(),hashlib.sha256(payload).digest())
