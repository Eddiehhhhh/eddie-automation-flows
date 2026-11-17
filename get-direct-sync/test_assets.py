import hashlib,io,json,os,pathlib,socket,tempfile,unittest
from assets import *
from core import snapshot
from test_core import note
class Response:
    status=200
    def __init__(self,data=b'binary\x00payload',headers=None,fail=False):self.stream=io.BytesIO(data);self.headers={'Content-Length':str(len(data)),**(headers or {})};self.fail=fail
    def getheader(self,k,default=None):return self.headers.get(k,default)
    def read(self,n):
        if self.fail:raise OSError('do not log private URL')
        return self.stream.read(n)
class Conn:
    def __init__(self,*a,response=None):self.response=response or Response();self.closed=False;self.requests=[]
    def request(self,*a,**kw):self.requests.append((a,kw))
    def getresponse(self):return self.response
    def close(self):self.closed=True
class Transport:
    def __init__(self,response=None):self.response=response;self.calls=0
    def open(self,url):self.calls+=1;self.conn=Conn(response=self.response);return self.conn,self.conn.response
class Tests(unittest.TestCase):
    def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=pathlib.Path(self.tmp.name)
    def test_full_download_retry_cleans_first_attempt(self):
        class Once(Transport):
            def open(self,url):
                self.response=Response(fail=self.calls==0)
                return super().open(url)
        t=Once();a=AssetArchive(self.root,t)
        r=a.fetch_with_retry('https://cdn.example.test/x',sleep=lambda _:None,random_value=lambda:0)
        self.assertEqual(t.calls,2);self.assertEqual((self.root/r['local_name']).read_bytes(),b'binary\x00payload')
        self.assertEqual(list(self.root.glob('.partial-*')),[])
    def test_mime_cannot_echo_signed_url(self):
        r=AssetArchive(self.root,Transport(Response(headers={'Content-Type':'application/octet-stream; private=https://cdn.example.test/x?signature=SECRET'}))).fetch('https://cdn.example.test/x')
        self.assertEqual(r['content_type'],'application/octet-stream');self.assertNotIn('SECRET',json.dumps(r))
    def test_slow_eof_cannot_publish(self):
        now=[0]
        class Slow(Response):
            def read(self,n):now[0]=1000;return b''
        with self.assertRaisesRegex(Hold,'time_limit'):AssetArchive(self.root,Transport(Slow(b'')),max_seconds=1,clock=lambda:now[0]).fetch('https://cdn.example.test/x')
        self.assertEqual(list(self.root.iterdir()),[])
    def test_audio_provenance_field(self):
        self.assertEqual(asset_references({'audio':{'url':'https://cdn.example.test/a'}})[0]['field'],'audio/url')
    def test_nonfinite_limits_rejected(self):
        for options in ({'max_bytes':float('nan')},{'max_bytes':True},{'max_seconds':float('inf')},{'max_seconds':float('nan')},{'max_seconds':0}):
            with self.assertRaises(ValueError):AssetArchive(self.root,Transport(),**options)
    def test_manifest_fifo_rejected_without_block(self):
        s=snapshot(note());name='manifest-'+hashlib.sha256((s['id']+':'+s['version']).encode()).hexdigest()+'.json';os.mkfifo(self.root/name)
        with self.assertRaisesRegex(Hold,'file_invalid'):AssetArchive(self.root,Transport()).archive_snapshot(s)
    def test_file_digest_rejects_symlink_fd(self):
        target=self.root/'target';target.write_bytes(b'private');link=self.root/'link';link.symlink_to(target)
        with self.assertRaisesRegex(Hold,'file_invalid'):file_digest(link)
    def test_directory_fd_remains_bound(self):
        directory=self.root/'store';directory.mkdir(mode=0o700);a=AssetArchive(directory,Transport());renamed=self.root/'original';directory.rename(renamed);directory.mkdir(mode=0o700)
        r=a.fetch('https://cdn.example.test/x')
        self.assertTrue((renamed/r['local_name']).exists());self.assertEqual(list(directory.iterdir()),[])
    def test_unknown_length_is_only_eof_evidence(self):
        r=AssetArchive(self.root,Transport(Response(headers={'Content-Length':None}))).fetch('https://cdn.example.test/x')
        self.assertEqual(r['source_integrity'],'eof_only');self.assertEqual(r['verification'],'local_bytes_readback')
    def test_exact_bytes_readback(self):
        a=AssetArchive(self.root,Transport());r=a.fetch('https://cdn.example.test/x?secret=not-logged')
        self.assertEqual((self.root/r['local_name']).read_bytes(),b'binary\x00payload');self.assertNotIn('secret',json.dumps(r))
        self.assertEqual(r['sha256'],hashlib.sha256(b'binary\x00payload').hexdigest())
    def test_idempotent_content_addressed(self):
        a=AssetArchive(self.root,Transport());r=a.fetch('https://cdn.example.test/x');r2=a.fetch('https://cdn.example.test/x?new-signature=1')
        self.assertEqual(r['local_name'],r2['local_name']);self.assertEqual(len(list(self.root.glob('*.bin'))),1)
    def test_wrong_length_cleans_partial(self):
        a=AssetArchive(self.root,Transport(Response(headers={'Content-Length':'999'})))
        with self.assertRaisesRegex(Hold,'length'):a.fetch('https://cdn.example.test/x')
        self.assertEqual(list(self.root.iterdir()),[])
    def test_stream_limit_cleans_partial(self):
        r=Response(headers={'Content-Length':None});a=AssetArchive(self.root,Transport(r),max_bytes=2)
        with self.assertRaisesRegex(Hold,'size_limit'):a.fetch('https://cdn.example.test/x')
        self.assertEqual(list(self.root.iterdir()),[])
    def test_network_failure_cleans_partial(self):
        t=Transport(Response(fail=True));a=AssetArchive(self.root,t)
        with self.assertRaises(TransportFailure):a.fetch('https://cdn.example.test/x')
        self.assertTrue(t.conn.closed);self.assertEqual(list(self.root.iterdir()),[])
    def test_expected_hash_mismatch(self):
        with self.assertRaisesRegex(Hold,'hash'):AssetArchive(self.root,Transport()).fetch('https://cdn.example.test/x',expected_sha256='0'*64)
        self.assertEqual(list(self.root.iterdir()),[])
    def test_corrupt_existing_never_overwritten(self):
        sha=hashlib.sha256(b'binary\x00payload').hexdigest();p=self.root/(sha+'.bin');p.write_bytes(b'human')
        with self.assertRaisesRegex(Hold,'corrupt'):AssetArchive(self.root,Transport()).fetch('https://cdn.example.test/x')
        self.assertEqual(p.read_bytes(),b'human')
    def test_symlink_never_followed(self):
        target=self.root/'target';target.write_bytes(b'human');sha=hashlib.sha256(b'binary\x00payload').hexdigest();(self.root/(sha+'.bin')).symlink_to(target)
        with self.assertRaises(Hold):AssetArchive(self.root,Transport()).fetch('https://cdn.example.test/x')
        self.assertEqual(target.read_bytes(),b'human')
    def test_private_dir_required(self):
        p=self.root/'wide';p.mkdir(mode=0o755)
        with self.assertRaisesRegex(Hold,'private'):AssetArchive(p,Transport())
    def test_manifest_only_complete(self):
        n=note();n['attachments']=[{'original_url':'https://cdn.example.test/x'},{'url':'https://cdn.example.test/y'}]
        s=snapshot(n);a=AssetArchive(self.root,Transport());m=a.archive_snapshot(s)
        self.assertEqual(len(m['assets']),2);self.assertEqual(m['destination_delivery'],'not_verified')
        self.assertEqual(len(list(self.root.glob('manifest-*'))),1)
    def test_failed_second_attachment_no_manifest(self):
        class F(Transport):
            def open(self,url):
                if self.calls:raise Hold('fail_second')
                return super().open(url)
        n=note();n['attachments']=[{'url':'https://cdn.example.test/x'},{'url':'https://cdn.example.test/y'}]
        with self.assertRaises(Hold):AssetArchive(self.root,F()).archive_snapshot(snapshot(n))
        self.assertEqual(list(self.root.glob('manifest-*')),[]);self.assertEqual(len(list(self.root.glob('*.bin'))),1)
    def test_only_declared_assets(self):
        n=note();n['attachments']=[{'url':'https://cdn.example.test/x'}];n['webpage']={'url':'https://example.test/page'};n['audio']={'play_url':'https://cdn.example.test/a','original':'transcript'}
        refs=asset_references(n);self.assertEqual(len(refs),2);self.assertNotIn('webpage',json.dumps(refs))
    def test_recording_uses_declared_audio_attachment(self):
        n=note();n['note_type']='recorder_audio';n['audio']={'original':'synthetic transcript'}
        n['attachments']=[{'type':'audio','url':'https://cdn.example.test/recording.mp3'}]
        self.assertEqual(asset_references(n),[{'field':'attachments/0','url':'https://cdn.example.test/recording.mp3'}])

    def test_binary_header_uses_verified_ogg_or_png_signature_for_upload_mime(self):
        for data,mime in [(b'OggS'+b'\x00'*32,'audio/ogg'),(b'\x89PNG\r\n\x1a\n'+b'\x00'*32,'image/png')]:
            result=AssetArchive(self.root,Transport(Response(data,{'Content-Type':'application/octet-stream'}))).fetch('https://cdn.example.test/x')
            self.assertEqual(result['content_type'],mime)

    def test_link_pointer_is_preserved_as_source_metadata_not_downloaded_as_binary(self):
        refs=asset_references({'note_type':'link','attachments':[{'type':'link','url':'https://article.example.test/post'}]})
        self.assertEqual(refs,[])

    def test_audio_kinds_require_audio_bytes_reference(self):
        for kind in ('audio','local_audio'):
            with self.assertRaisesRegex(Hold,'recording_url_missing'):
                asset_references({'note_type':kind,'audio':{'original':'synthetic transcript'},'attachments':[]})

    def test_missing_recording_hold(self):
        n=note();n['note_type']='recorder_audio';n['audio']={'original':'transcript'}
        with self.assertRaisesRegex(Hold,'recording'):asset_references(n)
    def test_transport_public_pinned_no_auth(self):
        made=[]
        def factory(host,ip,timeout):c=Conn();made.append((host,ip,c));return c
        resolver=lambda *a,**k:[(socket.AF_INET,socket.SOCK_STREAM,6,'',('93.184.216.34',443))]
        conn,r=AssetTransport({'cdn.example.test'},resolver=resolver,connection_factory=factory).open('https://cdn.example.test/a?signature=secret')
        self.assertEqual(made[0][:2],('cdn.example.test','93.184.216.34'))
        headers=conn.requests[0][1]['headers'];self.assertNotIn('Authorization',headers);self.assertNotIn('Cookie',headers)
    def test_private_dns_rejected_before_connect(self):
        for ip in ['127.0.0.1','10.0.0.1','169.254.169.254','::1']:
            resolver=lambda *a,**k:[(socket.AF_INET,socket.SOCK_STREAM,6,'',(ip,443))]
            with self.assertRaisesRegex(Hold,'not_public'):AssetTransport({'cdn.example.test'},resolver=resolver,connection_factory=lambda *a:self.fail('connected')).open('https://cdn.example.test/x')
    def test_forbidden_urls(self):
        for u in ['http://cdn.example.test/x','https://evil.example/x','https://user:pass@cdn.example.test/x','https://cdn.example.test:444/x','https://cdn.example.test/x#fragment']:
            with self.assertRaisesRegex(Hold,'not_allowed'):AssetTransport({'cdn.example.test'}).open(u)
    def test_redirect_rejected(self):
        c=Conn();c.response.status=302
        resolver=lambda *a,**k:[(socket.AF_INET,socket.SOCK_STREAM,6,'',('93.184.216.34',443))]
        with self.assertRaises(HTTPFailure):AssetTransport({'cdn.example.test'},resolver=resolver,connection_factory=lambda *a:c).open('https://cdn.example.test/x')
        self.assertTrue(c.closed)
if __name__=='__main__':unittest.main()
