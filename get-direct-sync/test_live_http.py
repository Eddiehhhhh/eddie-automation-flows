import io,json,socket,unittest
from unittest.mock import patch,MagicMock
from live_http import HTTP,Hold
from public_media_transport import PublicDNS,ProxyPinnedHTTPS,media_transport

class Response(io.BytesIO):
    def __enter__(self):return self
    def __exit__(self,*_):self.close()
class Tests(unittest.TestCase):
    def test_multipart_is_bound_to_notion_and_preserves_bytes(self):
        h=HTTP('https://api.notion.com',{'Authorization':'Bearer synthetic'})
        h.opener.open=MagicMock(return_value=Response(b'{"status":"uploaded"}'))
        h.send_file('abc-def','asset.bin','application/octet-stream',b'\x00\xff',2)
        req=h.opener.open.call_args[0][0]
        self.assertEqual(req.full_url,'https://api.notion.com/v1/file_uploads/abc-def/send')
        self.assertIn(b'\x00\xff',req.data);self.assertIn(b'name="part_number"',req.data)
    def test_bad_upload_filename_never_requests(self):
        h=HTTP('https://api.notion.com',{})
        with self.assertRaises(Hold):h.send_file('abc','x\r\nAuthorization: injected','text/plain',b'x')
        with self.assertRaises(Hold):HTTP('https://api.github.com',{}).send_file('abc','x','text/plain',b'x')
    def test_public_dns_never_accepts_private_addresses(self):
        opener=MagicMock();opener.open.return_value=Response(json.dumps({'Status':0,'Answer':[{'type':1,'data':'127.0.0.1'}]}).encode())
        with patch('urllib.request.build_opener',return_value=opener):
            with self.assertRaises(Hold):PublicDNS()('cdn.example.test',443)
    def test_public_dns_keeps_actual_ip(self):
        opener=MagicMock();opener.open.return_value=Response(json.dumps({'Status':0,'Answer':[{'type':1,'data':'8.8.8.8'}]}).encode())
        with patch('urllib.request.build_opener',return_value=opener):self.assertEqual(PublicDNS()('cdn.example.test',443)[0][4],('8.8.8.8',443))
    def test_proxy_connect_pins_public_ip_and_original_certificate(self):
        c=ProxyPinnedHTTPS('cdn.example.test','8.8.8.8',10,('127.0.0.1',7890));c._context=MagicMock();c._tunnel=MagicMock()
        with patch.object(socket,'create_connection',return_value=MagicMock()) as connect:c.connect()
        self.assertEqual(c._tunnel_host,'8.8.8.8');self.assertEqual(c._context.wrap_socket.call_args.kwargs['server_hostname'],'cdn.example.test')
        self.assertEqual(connect.call_args[0][0],('127.0.0.1',7890))
    def test_untrusted_proxy_is_rejected(self):
        with patch('urllib.request.getproxies',return_value={'https':'http://untrusted.example:8080'}):
            with self.assertRaises(Hold):media_transport(['cdn.example.test'],use_local_proxy=True)
