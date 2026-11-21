import hashlib, json, sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from stage_tencent_complex import stage, index_digest

class StageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root=Path(self.temp.name)
        self.capture=self.root/'capture';self.vault=self.root/'vault';self.vault.mkdir()
        p=self.capture/'excel/xl/workbook.xml';p.parent.mkdir(parents=True)
        data=b'<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheets><sheet name="A"/></sheets></workbook>';p.write_bytes(data)
        self.snapshot={'file_id':'test','sheets':[{'metadata':{'sheet_name':'A','sheet_id':'s','sheet_type':'worksheet'},'cells':[]}]}
        (self.capture/'snapshot.json').write_text(json.dumps(self.snapshot))
        (self.capture/'excel-manifest.json').write_text(json.dumps({'entries':[{'path':'xl/workbook.xml','size':len(data),'sha256':hashlib.sha256(data).hexdigest()}]}))
        self.dest='Raw/06 TencentDocs/Test'
    def tearDown(self):self.temp.cleanup()
    def test_repeat_and_preserve_old_version(self):
        a=stage(self.capture,self.vault,self.dest,'absent'); index=self.vault/self.dest/'索引.md'
        b=stage(self.capture,self.vault,self.dest,index_digest(index)); self.assertEqual(a,b)
        self.snapshot['sheets'][0]['cells']=[{'row':0,'col':0,'value_type':'STRING','string_value':'test'}]
        (self.capture/'snapshot.json').write_text(json.dumps(self.snapshot));c=stage(self.capture,self.vault,self.dest,index_digest(index))
        self.assertNotEqual(a['version'],c['version']);self.assertTrue((index.parent/a['version']).is_dir())
    def test_conflict_and_tamper_stop(self):
        with self.assertRaises(ValueError):stage(self.capture,self.vault,self.dest,'0'*64)
        (self.capture/'excel/xl/workbook.xml').write_text('modified')
        with self.assertRaises(ValueError):stage(self.capture,self.vault,self.dest,'absent')
        self.assertFalse((self.vault/self.dest/'索引.md').exists())
    def test_reject_traversal(self):
        with self.assertRaises(ValueError):stage(self.capture,self.vault,'Raw/06 TencentDocs/../bad','absent')
    def test_interrupted_index_write_reuses_intact_version(self):
        from unittest.mock import patch
        with patch('stage_tencent_complex.os.replace', side_effect=OSError('interrupted')):
            with self.assertRaises(OSError):stage(self.capture,self.vault,self.dest,'absent')
        index=self.vault/self.dest/'索引.md';self.assertFalse(index.exists())
        result=stage(self.capture,self.vault,self.dest,'absent')
        self.assertTrue(index.exists());self.assertTrue((index.parent/result['version']).is_dir())
    def test_rebuild_checks_payload_and_preserves_existing_output(self):
        import zipfile
        from rebuild_tencent_excel import rebuild
        dest=self.root/'result.xlsx';rebuild(self.capture,dest)
        with zipfile.ZipFile(dest) as z:self.assertEqual(z.read('xl/workbook.xml'),(self.capture/'excel/xl/workbook.xml').read_bytes())
        prior=dest.read_bytes()
        with self.assertRaises(ValueError):rebuild(self.capture,dest)
        self.assertEqual(dest.read_bytes(),prior)
    def test_unmanaged_index_is_never_overwritten(self):
        folder=self.vault/self.dest;folder.mkdir(parents=True);index=folder/'索引.md';index.write_text('human content')
        with self.assertRaises(ValueError):stage(self.capture,self.vault,self.dest,index_digest(index))
        self.assertEqual(index.read_text(),'human content')
