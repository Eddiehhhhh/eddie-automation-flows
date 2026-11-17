"""Fixed-origin JSON/multipart transport; credentials never reach media downloads."""
import hashlib,json,os,secrets,urllib.error,urllib.parse,urllib.request
from core import NoRedirect,Hold,canonical
from retry import HTTPFailure,TransportFailure
from assets import AssetTransport,AssetArchive

class HTTP:
    def __init__(self,host,headers,*,max_response=144*1024*1024,use_proxy=False):
        if host not in ('https://openapi.biji.com','https://api.github.com','https://api.notion.com'):
            raise Hold('unapproved_host')
        self.host,self.headers,self.max_response=host,headers,max_response
        self.opener=urllib.request.build_opener(*(([NoRedirect()]) if use_proxy else [urllib.request.ProxyHandler({}),NoRedirect()]))
    def _send(self,method,path,data=None,content_type='application/json'):
        if not isinstance(path,str) or not path.startswith('/') or path.startswith('//') or any(ord(c)<32 for c in path) or '#' in path:
            raise Hold('invalid_path')
        req=urllib.request.Request(self.host+path,data=data,method=method,headers={**self.headers,'Content-Type':content_type})
        try:
            with self.opener.open(req,timeout=60) as response:
                raw=response.read(self.max_response+1)
                if len(raw)>self.max_response:raise Hold('response_too_large')
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            status=exc.code;delay=exc.headers.get('Retry-After') if exc.headers else None;exc.close()
            if status==404 and method=='GET':return None
            raise HTTPFailure(status,delay) from None
        except (urllib.error.URLError,TimeoutError,OSError):raise TransportFailure() from None
    def request(self,method,path,body=None):
        return self._send(method,path,None if body is None else canonical(body).encode())
    def send_file(self,upload_id,filename,mime,data,part_number=None):
        if self.host!='https://api.notion.com' or any(c not in '0123456789abcdef-' for c in upload_id) or not upload_id:
            raise Hold('invalid_upload_identity')
        if any(c in filename for c in '\r\n"\\/') or any(c in mime for c in '\r\n'):
            raise Hold('invalid_upload_metadata')
        boundary='get-sync-'+secrets.token_hex(16)
        parts=[]
        if part_number is not None:
            if type(part_number) is not int or part_number<1:raise Hold('invalid_upload_part')
            parts.append(('--'+boundary+'\r\nContent-Disposition: form-data; name="part_number"\r\n\r\n'+str(part_number)+'\r\n').encode())
        parts.extend([('--'+boundary+'\r\nContent-Disposition: form-data; name="file"; filename="'+filename+'"\r\nContent-Type: '+mime+'\r\n\r\n').encode(),data,('\r\n--'+boundary+'--\r\n').encode()])
        return self._send('POST','/v1/file_uploads/'+upload_id+'/send',b''.join(parts),'multipart/form-data; boundary='+boundary)

class NotionDownload:
    def __init__(self,root,*,use_local_proxy=False):self.root,self.use_local_proxy=root,use_local_proxy
    def __call__(self,url,sha256,size):
        host=(urllib.parse.urlsplit(url).hostname or '').lower()
        # Known Notion-managed delivery domains; source URLs cannot extend this list.
        allowed=('prod-files-secure.s3.us-west-2.amazonaws.com','s3.us-west-2.amazonaws.com','file.notion.so')
        if host not in allowed:raise Hold('unapproved_notion_download_host')
        from public_media_transport import media_transport
        archive=AssetArchive(self.root,media_transport([host],use_local_proxy=self.use_local_proxy),max_bytes=max(size,1))
        try:
            result=archive.fetch_with_retry(url,expected_size=size,expected_sha256=sha256)
            return result
        finally:archive.close()

from retry import RetryTransport

class PacedHTTP(RetryTransport):
    """Reuse read retries; file uploads remain one-shot writes."""
    def send_file(self,*args,**kwargs):
        with self._lock:
            now=self.monotonic()
            pace=0 if self._last_start is None else max(0,self._last_start+self.min_interval-now)
            if pace:self.sleep(pace)
            self._last_start=self.monotonic()
            return self.transport.send_file(*args,**kwargs)
