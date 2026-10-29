import copy
import hashlib
import io
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import source_sync_pack_seed as seed


def blob(data):
    return hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()


class PackSeedTests(unittest.TestCase):
    def test_protocol_pack_framing_caps_and_failure_are_strict(self):
        content = b'PACK' + struct.pack('>II', 2, 1) + b'0'*20
        wire = seed.pkt(b'packfile\n') + seed.pkt(b'\1'+content) + b'00000002'
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d)/'source.pack'
            self.assertEqual(seed.decode_pack(io.BytesIO(wire), dest, 1, 64), 32)
            self.assertEqual(dest.read_bytes(), content)
            cases = [(wire,2,64), (wire,1,20), (wire[:-2],1,64), (wire+b'X',1,64),
                     (seed.pkt(b'packfile-uris\n')+b'0000',1,64),
                     (seed.pkt(b'packfile\n')+seed.pkt(b'\3SECRET')+b'0000',1,64)]
            for value,count,maximum in cases:
                with self.assertRaises(ValueError): seed.decode_pack(io.BytesIO(value),dest,count,maximum)
        for raw in (b'zzzz', b'0003', b'ffff'):
            with self.assertRaises(ValueError): seed.Wire(io.BytesIO(raw),4).packet()

    def test_offline_real_git_pack_contains_exact_blobs_and_reproduces_bytes(self):
        with tempfile.TemporaryDirectory() as d:
            work=Path(d); source=work/'source.git'; source.mkdir()
            seed.git_run(source,['init','--bare','--template=',str(source)])
            data=[b'---\nsource_system: getnote\nnote_id: "1001"\n---\nPRIVATE\n', b'\x00PRIVATE_BINARY\xff']
            expected={}
            for value in data:
                with (work/'input').open('wb') as f: f.write(value)
                with (work/'input').open('rb') as f: oid=seed.git_run(source,['hash-object','-w','--stdin'],stdin=f).decode().strip()
                expected[oid]=len(value)
            request=work/'oids'; request.write_text(''.join(i+'\n' for i in expected))
            pack=work/'source.pack'
            with request.open('rb') as ins, pack.open('wb') as out:
                seed.git_run(source,['pack-objects','--stdout'],stdin=ins,stdout=out)
            paths={'notes/PRIVATE.md':{'sha':blob(data[0]),'size':len(data[0])},
                   '_assets/image.bin':{'sha':blob(data[1]),'size':len(data[1])},
                   '_assets/same.bin':{'sha':blob(data[1]),'size':len(data[1])}}
            root=work/'vault'; root.mkdir()
            seed.verify_materialize(pack,work/'verified.git',expected,paths,root)
            self.assertEqual((root/'notes/PRIVATE.md').read_bytes(),data[0])
            self.assertEqual((root/'_assets/same.bin').read_bytes(),data[1])
            report={}; seed.audit_actual(root,{'1001':'notes/PRIVATE.md'},report)
            self.assertTrue(report['all_actual_headers_checked'])
            with self.assertRaises(ValueError):
                seed.verify_materialize(pack,work/'rejected.git',{blob(data[0]):len(data[0])},paths,root)

    def test_expected_object_and_total_limits(self):
        paths={'one.md':{'sha':'a'*40,'size':2}, 'two.md':{'sha':'a'*40,'size':2}}
        self.assertEqual(seed.expected_blobs(paths),{'a'*40:2})
        for change in ('size', 'conflict', 'path', 'count'):
            value=copy.deepcopy(paths)
            if change=='size': value['one.md']['size']=seed.MAX_BLOB+1
            if change=='conflict': value['two.md']['size']=3
            if change=='path': value['../BAD']=value.pop('one.md')
            if change=='count': value={str(i)+'.md':paths['one.md'] for i in range(seed.MAX_FILES+1)}
            with self.assertRaises(ValueError): seed.expected_blobs(value)

    def test_exact_git_host_and_no_credential_in_child_environment(self):
        req=seed.request('SECRET','/git-upload-pack',b'0000')
        self.assertEqual(req.full_url,seed.GIT_URL+'/git-upload-pack')
        self.assertEqual(req.method,'POST')
        with self.assertRaises(ValueError): seed.request('SECRET','https://example.com')
        with patch.dict(seed.os.environ,{'PRIVATE_VAULT_TOKEN':'SECRET','GETNOTE_API_KEY':'SECRET'}), \
             patch.object(seed.subprocess,'run',return_value=subprocess.CompletedProcess([],0,b'',b'')) as run:
            seed.git_run(Path('/tmp/fixture'),['cat-file','--batch-check'])
            environment=run.call_args.kwargs['env']
            self.assertNotIn('PRIVATE_VAULT_TOKEN',environment)
            self.assertNotIn('GETNOTE_API_KEY',environment)
            self.assertEqual(environment['GIT_NO_LAZY_FETCH'],'1')
            self.assertNotIn('SECRET',str(run.call_args))

    def test_safe_report_never_exports_identity_or_claims_sync(self):
        value=dict.fromkeys(seed.FLAGS,True)
        value.update(status='real_seed_ok', source_complete=True,published=True,PRIVATE='SECRET',commit='a'*40)
        result=seed.safe_report(value)
        self.assertEqual(result['status'],'real_seed_ok')
        self.assertNotIn('SECRET',str(result)); self.assertNotIn('commit',result)
        self.assertFalse(result['source_complete']); self.assertFalse(result['published'])

    def test_protocol_v2_implicit_sha1_is_valid_and_unknown_format_rejected(self):
        class Response(io.BytesIO):
            status = 200
        for format_line in (b'', seed.pkt(b'object-format=sha1\n')):
            response=Response(seed.pkt(b'version 2\n')+seed.pkt(b'fetch=shallow filter\n')+format_line+b'0000')
            report={}
            with patch.object(seed,'opener',return_value=Mock(open=Mock(return_value=response))):
                seed.capabilities('SECRET',report)
            self.assertTrue(report['git_v2_advertised']); self.assertTrue(report['git_fetch_advertised'])
            self.assertEqual(report['git_sha1_implicit'],not bool(format_line))
        response=Response(seed.pkt(b'version 2\n')+seed.pkt(b'fetch\n')+seed.pkt(b'object-format=sha256\n')+b'0000')
        with patch.object(seed,'opener',return_value=Mock(open=Mock(return_value=response))), self.assertRaises(ValueError):
            seed.capabilities('SECRET')

    def test_smart_service_preamble_and_explicit_legacy_reachable_permission(self):
        class Response(io.BytesIO):
            status = 200
        preamble=seed.pkt(b'# service=git-upload-pack\n')+b'0000'
        for wire, protocol in ((preamble+seed.pkt(b'version 2\n')+seed.pkt(b'fetch\n')+b'0000',2),
                (preamble+seed.pkt(b'a'*40+b' HEAD\0allow-reachable-sha1-in-want side-band-64k no-progress\n')+b'0000',0)):
            report={}
            with patch.object(seed,'opener',return_value=Mock(open=Mock(return_value=Response(wire)))):
                self.assertEqual(seed.capabilities('SECRET',report),protocol)
            self.assertTrue(report['git_smart_service_preamble'])
        denied=preamble+seed.pkt(b'a'*40+b' HEAD\0side-band-64k no-progress\n')+b'0000'
        with patch.object(seed,'opener',return_value=Mock(open=Mock(return_value=Response(denied)))), self.assertRaisesRegex(ValueError,'reachable blob wants unavailable'):
            seed.capabilities('SECRET')
        pack=b'PACK'+struct.pack('>II',2,1)+b'0'*20
        wire=seed.pkt(b'NAK\n')+seed.pkt(b'\1'+pack)+b'0000'
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(seed.decode_pack(io.BytesIO(wire),Path(d)/'data.pack',1,64,0),32)


if __name__=='__main__': unittest.main()
