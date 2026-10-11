import io
import json
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import source_sync_assets as a

PNG = b'\x89PNG\r\n\x1a\nsynthetic-image'


class Response(io.BytesIO):
    def __init__(self, data=PNG, status=200, headers=None):
        super().__init__(data)
        self.status = status
        self.headers = headers or {'Content-Type':'image/png'}
    def getheader(self, name): return self.headers.get(name)


class AssetTests(unittest.TestCase):
    def test_plugin_asset_fixture_equivalence_and_idempotence(self):
        fixture = json.loads((Path(__file__).parent/'fixtures/getnote-plugin-synthetic.json').read_text())
        with tempfile.TemporaryDirectory() as temp:
            fetch = lambda url, **kw: (PNG,'image/png') if 'image' in url else (b'%PDF-synthetic','application/pdf')
            store = a.AssetStore(temp,fetch=fetch)
            manifest = store.materialize(fixture['note'])
            self.assertEqual([m['status'] for m in manifest],fixture['expected_asset_statuses'])
            self.assertEqual([m['kind'] for m in manifest],fixture['expected_asset_kinds'])
            self.assertEqual(len(list(Path(temp).rglob('*.*'))),2)
            before={p:p.stat().st_mtime_ns for p in Path(temp).rglob('*') if p.is_file()}
            self.assertEqual(manifest,a.AssetStore(temp,fetch=fetch).materialize(fixture['note']))
            self.assertEqual(before,{p:p.stat().st_mtime_ns for p in Path(temp).rglob('*') if p.is_file()})
            for item in manifest:
                if item['status']=='downloaded':
                    import hashlib
                    self.assertEqual(item['sha256'],hashlib.sha256((Path(temp)/item['path']).read_bytes()).hexdigest())
                    self.assertNotIn('..',item['path'])

    def test_private_dns_scheme_credentials_and_mixed_dns_rejected(self):
        for url in ['file:///etc/passwd','http://example.com/x','https://user:pass@example.com/x','https://example.com:8443/x']:
            with self.assertRaises(a.AssetError): a.public_endpoint(url)
        for addresses in [['127.0.0.1'],['169.254.169.254'],['::1'],['8.8.8.8','10.0.0.1']]:
            with patch.object(socket,'getaddrinfo',return_value=[(2,1,6,'',(ip,443)) for ip in addresses]):
                with self.assertRaises(a.AssetError): a.public_endpoint('https://example.com/x')

    def test_redirect_is_revalidated_without_credentials(self):
        with patch.object(a,'public_endpoint',side_effect=[(__import__('urllib.parse',fromlist=['urlsplit']).urlsplit('https://example.com/a'),'8.8.8.8'),a.AssetError('private redirect')]) as endpoint, patch.object(a,'PinnedHTTPSConnection') as conn:
            conn.return_value.getresponse.return_value=Response(status=302,headers={'Location':'https://127.0.0.1/secret'})
            with self.assertRaises(a.AssetError): a.fetch_once('https://example.com/a')
            self.assertEqual(endpoint.call_args_list[1].args[0],'https://127.0.0.1/secret')
            headers=conn.return_value.request.call_args.kwargs['headers']
            self.assertNotIn('Authorization',headers)
            self.assertNotIn('Cookie',headers)

    def test_stream_size_declared_size_short_read_and_mime(self):
        from urllib.parse import urlsplit
        for response in [Response(b'x'*11),Response(headers={'Content-Length':'100'}),Response(b'x',headers={'Content-Length':'2'}),Response(headers={'Content-Type':'audio/mpeg'})]:
            with patch.object(a,'public_endpoint',return_value=(urlsplit('https://example.com/a'),'8.8.8.8')),patch.object(a,'PinnedHTTPSConnection') as conn:
                conn.return_value.getresponse.return_value=response
                with self.assertRaises(a.AssetError): a.fetch_once('https://example.com/a',max_bytes=10)

    def test_timeout_retry_and_permanent_failure(self):
        with patch.object(a,'fetch_once',side_effect=[TimeoutError(),a.RetryableAssetError(),(PNG,'image/png')]) as fetch,patch.object(a.time,'sleep'):
            self.assertEqual(a.fetch_asset('https://example.com/a'),(PNG,'image/png'))
            self.assertEqual(fetch.call_count,3)
        with patch.object(a,'fetch_once',side_effect=a.AssetError('401')) as fetch:
            with self.assertRaises(a.AssetError): a.fetch_asset('https://example.com/a')
            self.assertEqual(fetch.call_count,1)
        with patch.object(a,'fetch_once',side_effect=TimeoutError()),patch.object(a.time,'sleep'):
            with self.assertRaises(a.AssetError): a.fetch_asset('https://example.com/a')

    def test_path_symlink_and_total_limits(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)/'root';root.mkdir()
            (root/'_assets').symlink_to(Path(temp),target_is_directory=True)
            with self.assertRaises(a.AssetError): a.write_binary(root,Path('_assets/x'),PNG)
            with self.assertRaises(a.AssetError): a.write_binary(root,Path('../escape'),PNG)
            (root/'_assets').unlink()
            note={'attachments':[{'type':'image','url':'https://example.com/image'}]}
            for kwargs in [{'max_file':1},{'max_run':1},{'max_assets':0}]:
                with self.assertRaises(a.AssetError): a.AssetStore(root,fetch=lambda *args,**kw:(PNG,'image/png'),**kwargs).materialize(note)
            with self.assertRaises(a.AssetError): a.AssetStore(root,fetch=lambda *args,**kw:(b'html','text/html')).materialize(note)


if __name__=='__main__': unittest.main()
