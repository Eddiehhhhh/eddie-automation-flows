"""Private content-addressed binary archival. Disabled without explicit allowed hosts.
No bearer/cookie headers, proxies, redirects, executable loading, or URL logging.
This archives bytes locally; it does not claim GitHub/Notion media delivery.
"""
import hashlib,http.client,ipaddress,json,math,os,pathlib,re,secrets,signal,socket,ssl,stat,threading,time,urllib.parse
from core import Hold,canonical
from retry import HTTPFailure,TransportFailure,RetryTransport

class AssetDeadline:
    """POSIX main-thread guard; refuse unsupported/concurrent alarm contexts."""
    def __init__(self,seconds):self.seconds=seconds
    def __enter__(self):
        if not hasattr(signal,'setitimer') or threading.current_thread() is not threading.main_thread():raise Hold('asset_deadline_context_unsupported')
        if signal.getitimer(signal.ITIMER_REAL)!=(0.0,0.0):raise Hold('asset_deadline_already_active')
        self.previous=signal.getsignal(signal.SIGALRM)
        def expired(*_):raise Hold('asset_time_limit')
        signal.signal(signal.SIGALRM,expired);signal.setitimer(signal.ITIMER_REAL,self.seconds)
        return self
    def __exit__(self,*_):
        signal.setitimer(signal.ITIMER_REAL,0);signal.signal(signal.SIGALRM,self.previous)

def safe_mime(value):
    token=value.split(';',1)[0].strip().lower() if isinstance(value,str) else ''
    return token if len(token)<=127 and re.fullmatch(r'[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+',token) else 'application/octet-stream'

def binary_mime(header,head):
    mime=safe_mime(header)
    if mime!='application/octet-stream':return mime
    for signature,detected in ((b'OggS','audio/ogg'),(b'fLaC','audio/flac'),(b'ID3','audio/mpeg'),
                               (b'#!AMR','audio/amr'),(b'\x89PNG\r\n\x1a\n','image/png'),
                               (b'\xff\xd8\xff','image/jpeg'),(b'GIF87a','image/gif'),
                               (b'GIF89a','image/gif'),(b'%PDF-','application/pdf')):
        if head.startswith(signature):return detected
    if head.startswith(b'RIFF') and head[8:12]==b'WAVE':return 'audio/wav'
    if head.startswith(b'RIFF') and head[8:12]==b'WEBP':return 'image/webp'
    if len(head)>=12 and head[4:8]==b'ftyp':return 'video/mp4'
    if len(head)>=2 and head[0]==255 and head[1]&224==224:return 'audio/mpeg'
    return mime

class PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self,host,address,timeout):
        super().__init__(host,443,timeout=timeout,context=ssl.create_default_context())
        self.address=address
    def connect(self):
        raw=socket.create_connection((self.address,443),timeout=self.timeout)
        try:self.sock=self._context.wrap_socket(raw,server_hostname=self.host)
        except BaseException:raw.close();raise

class AssetTransport:
    def __init__(self,allowed_hosts,*,resolver=None,connection_factory=PinnedHTTPS,timeout=30):
        if isinstance(timeout,bool) or not isinstance(timeout,(int,float)) or not math.isfinite(timeout) or timeout<=0:raise ValueError('invalid_asset_timeout')
        self.allowed=frozenset(allowed_hosts)
        self.resolver=resolver or socket.getaddrinfo
        self.factory,self.timeout=connection_factory,timeout
    def open(self,url):
        try:
            p=urllib.parse.urlsplit(url)
            host=(p.hostname or '').encode('idna').decode().lower()
            if p.scheme!='https' or p.username or p.password or p.fragment or p.port not in (None,443) or host not in self.allowed:raise Hold('asset_url_not_allowed')
            if any(ord(c)<32 or ord(c)==127 for c in url):raise Hold('asset_url_not_allowed')
            addresses={row[4][0] for row in self.resolver(host,443,type=socket.SOCK_STREAM)}
            if not addresses or any(not ipaddress.ip_address(a).is_global for a in addresses):raise Hold('asset_address_not_public')
            # Connect to an already-validated address, preserving TLS hostname check.
            connection=self.factory(host,sorted(addresses)[0],self.timeout)
            target=urllib.parse.urlunsplit(('', '',p.path or '/',p.query,''))
            try:
                connection.request('GET',target,headers={'Accept-Encoding':'identity','User-Agent':'get-direct-asset-archive/1'})
                response=connection.getresponse()
                if response.status!=200:raise HTTPFailure(response.status,response.getheader('Retry-After'))
                if response.getheader('Content-Encoding','identity').lower() not in ('','identity'):raise Hold('asset_encoded_response')
                return connection,response
            except BaseException:connection.close();raise
        except (Hold,HTTPFailure):raise
        except (OSError,http.client.HTTPException):raise TransportFailure() from None
        except Exception:raise Hold('asset_transport_failed') from None

def private_directory(path):
    root=pathlib.Path(path)
    if not root.exists():raise Hold('asset_directory_required')
    if root.is_symlink() or not root.is_dir() or root.stat().st_uid!=os.getuid() or stat.S_IMODE(root.stat().st_mode)&0o077:raise Hold('asset_directory_not_private')
    # Reject ancestor aliases, which could redirect the durable store unexpectedly.
    if root.absolute()!=root.resolve():raise Hold('asset_directory_alias')
    return root

def open_regular(path,dir_fd=None):
    try:fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=dir_fd)
    except OSError:raise Hold('asset_file_invalid') from None
    if not stat.S_ISREG(os.fstat(fd).st_mode):os.close(fd);raise Hold('asset_file_invalid')
    return fd

def file_digest(path,*,dir_fd=None,max_bytes=128*1024*1024):
    fd=open_regular(path,dir_fd);h=hashlib.sha256();size=0
    with os.fdopen(fd,'rb') as f:
        if os.fstat(f.fileno()).st_size>max_bytes:raise Hold('asset_existing_size_limit')
        while True:
            b=f.read(65536)
            if not b:break
            size+=len(b)
            if size>max_bytes:raise Hold('asset_existing_size_limit')
            h.update(b)
    return h.hexdigest(),size

class AssetArchive:
    def __init__(self,root,transport,*,max_bytes=128*1024*1024,max_seconds=300,clock=time.monotonic):
        if type(max_bytes) is not int or max_bytes<=0 or isinstance(max_seconds,bool) or not isinstance(max_seconds,(int,float)) or not math.isfinite(max_seconds) or max_seconds<=0:raise ValueError('invalid_asset_limits')
        self.root=private_directory(root);self.transport=transport
        self.dir_fd=os.open(self.root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        st=os.fstat(self.dir_fd)
        if st.st_uid!=os.getuid() or stat.S_IMODE(st.st_mode)&0o077:self.close();raise Hold('asset_directory_not_private')
        self.max_bytes,self.max_seconds,self.clock=max_bytes,max_seconds,clock
    def close(self):
        if getattr(self,'dir_fd',None) is not None:os.close(self.dir_fd);self.dir_fd=None
    def __del__(self):self.close()
    def _exists(self,name):
        try:os.stat(name,dir_fd=self.dir_fd,follow_symlinks=False);return True
        except FileNotFoundError:return False
    def _temporary(self,prefix):
        name=prefix+secrets.token_hex(16)
        return os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=self.dir_fd),name
    def _manifest_matches(self,name,data):
        fd=open_regular(name,self.dir_fd)
        with os.fdopen(fd,'rb') as f:
            return os.fstat(f.fileno()).st_size==len(data) and f.read(len(data)+1)==data
    def fetch(self,url,*,expected_size=None,expected_sha256=None):
        with AssetDeadline(self.max_seconds):
            return self._fetch(url,expected_size=expected_size,expected_sha256=expected_sha256)
    def _fetch(self,url,*,expected_size=None,expected_sha256=None):
        # Signed URL remains only in memory. The snapshot already retains source fields.
        start=self.clock()
        conn,response=self.transport.open(url)
        path=None
        try:
            value=response.getheader('Content-Length')
            try:length=int(value) if value is not None else None
            except (ValueError,TypeError):raise Hold('asset_invalid_length') from None
            if length is not None and (length<0 or length>self.max_bytes):raise Hold('asset_size_limit')
            if expected_size is not None and (type(expected_size) is not int or expected_size<0 or expected_size>self.max_bytes):raise Hold('asset_invalid_expected_size')
            if expected_size is not None and length is not None and expected_size!=length:raise Hold('asset_length_mismatch')
            if expected_sha256 is not None and (not isinstance(expected_sha256,str) or not re.fullmatch('[0-9a-f]{64}',expected_sha256)):raise Hold('asset_invalid_expected_hash')
            fd,path=self._temporary('.partial-')
            digest=hashlib.sha256();size=0;head=b''
            with os.fdopen(fd,'wb') as f:
                while True:
                    if self.clock()-start>self.max_seconds:raise Hold('asset_time_limit')
                    try:b=(response.read1(65536) if hasattr(response,'read1') else response.read(65536))
                    except (OSError,http.client.HTTPException):raise TransportFailure() from None
                    if self.clock()-start>self.max_seconds:raise Hold('asset_time_limit')
                    if not b:break
                    if not head:head=b[:32]
                    size+=len(b)
                    if size>self.max_bytes:raise Hold('asset_size_limit')
                    digest.update(b);f.write(b)
                f.flush();os.fsync(f.fileno())
            if length is not None and size!=length or expected_size is not None and size!=expected_size:raise Hold('asset_length_mismatch')
            sha=digest.hexdigest()
            if expected_sha256 is not None and sha!=expected_sha256:raise Hold('asset_hash_mismatch')
            dest=sha+'.bin'
            if self._exists(dest):
                if file_digest(dest,dir_fd=self.dir_fd,max_bytes=self.max_bytes)!=(sha,size):raise Hold('asset_existing_corrupt')
            else:
                # Atomic no-overwrite publish, even if another process wins the race.
                try:os.link(path,dest,src_dir_fd=self.dir_fd,dst_dir_fd=self.dir_fd,follow_symlinks=False)
                except FileExistsError:
                    if file_digest(dest,dir_fd=self.dir_fd,max_bytes=self.max_bytes)!=(sha,size):raise Hold('asset_existing_corrupt')
            os.unlink(path,dir_fd=self.dir_fd);path=None
            os.fsync(self.dir_fd)
            if file_digest(dest,dir_fd=self.dir_fd,max_bytes=self.max_bytes)!=(sha,size):raise Hold('asset_readback_mismatch')
            return {'sha256':sha,'bytes':size,'local_name':dest,'source_url_sha256':hashlib.sha256(url.encode()).hexdigest(),'content_type':binary_mime(response.getheader('Content-Type','application/octet-stream'),head),'verification':'local_bytes_readback','source_integrity':('provided_expected_sha256_matched' if expected_sha256 is not None else 'provided_expected_size_matched' if expected_size is not None else 'http_length_matched' if length is not None else 'eof_only')}
        except (Hold,TransportFailure):raise
        except Exception:raise Hold('asset_read_failed') from None
        finally:
            conn.close()
            if path is not None:
                try:os.unlink(path,dir_fd=self.dir_fd)
                except FileNotFoundError:pass
    def fetch_with_retry(self,url,*,expected_size=None,expected_sha256=None,**retry_options):
        archive=self
        class FullDownload:
            def request(self,method,path,body=None):
                return archive.fetch(path,expected_size=expected_size,expected_sha256=expected_sha256)
        # Full GET stream may retry; each failed attempt removes only its own temp file.
        return RetryTransport(FullDownload(),**retry_options).request('GET',url)
    def archive_snapshot(self,snapshot):
        refs=asset_references(snapshot['note'])
        assets=[]
        for ref in refs:
            result=self.fetch_with_retry(ref['url'],expected_size=ref.get('expected_size'),expected_sha256=ref.get('expected_sha256'))
            if ref['field'].startswith('content/image/'):
                with os.fdopen(open_regular(result['local_name'], self.dir_fd), 'rb') as stream:
                    mime = binary_mime('application/octet-stream', stream.read(32))
                if not mime.startswith('image/'):
                    raise Hold('media_embedded_image_unverified')
                result['content_type'] = mime
            assets.append({**result,'source_field':ref['field']})
        manifest={'source_id':snapshot['id'],'source_version':snapshot['version'],'assets':assets,'coverage':manifest_coverage(refs),'destination_delivery':'not_verified'}
        # Only publish complete manifest. Partial downloads are reusable by content hash.
        data=canonical(manifest).encode();name='manifest-'+hashlib.sha256((snapshot['id']+':'+snapshot['version']).encode()).hexdigest()+'.json'
        final=name
        if self._exists(final):
            if not self._manifest_matches(final,data):raise Hold('asset_manifest_conflict')
            return manifest
        fd,tmp=self._temporary('.manifest-')
        try:
            with os.fdopen(fd,'wb') as f:f.write(data);f.flush();os.fsync(f.fileno())
            try:os.link(tmp,final,src_dir_fd=self.dir_fd,dst_dir_fd=self.dir_fd,follow_symlinks=False)
            except FileExistsError:
                if not self._manifest_matches(final,data):raise Hold('asset_manifest_conflict')
            os.fsync(self.dir_fd)
        finally:os.unlink(tmp,dir_fd=self.dir_fd)
        return manifest

def manifest_coverage(refs):
    if any(ref['field'].startswith('content/image/') for ref in refs):
        return 'declared_attachments_audio_and_standalone_images'
    return 'declared_attachments_and_audio_only'


def recording_images(note):
    content = note.get('content', '')
    if '<img' in content.lower():
        raise Hold('media_embedded_coverage_unverified')
    if '![' not in content:
        return []
    # shortcut: support the recording export's standalone image lines; hold ambiguous Markdown.
    if (note.get('note_type') != 'recorder_audio' or '<!--' in content
            or re.search(r'(?m)^\s*(?:`{3}|~{3})', content)):
        raise Hold('media_embedded_coverage_unverified')
    refs = []
    for line in content.splitlines():
        if '![' not in line:
            continue
        match = re.fullmatch(r'!\[\]\((https://[^\s()<>\\\[\]"]+)\)', line)
        if not match:
            raise Hold('media_embedded_coverage_unverified')
        refs.append({'field': 'content/image/' + str(len(refs)), 'url': match[1]})
    return refs


def asset_references(note):
    """Known API fields only. No arbitrary recursive URLs or webpage downloads."""
    refs=[]
    attachments=note.get('attachments',[])
    if not isinstance(attachments,list):raise Hold('asset_attachments_shape')
    for i,item in enumerate(attachments):
        if not isinstance(item,dict):raise Hold('asset_attachment_shape')
        if item.get('type')=='link':continue
        url=item.get('original_url') or item.get('url')
        if not isinstance(url,str) or not url:raise Hold('asset_url_missing')
        refs.append({'field':'attachments/'+str(i),'url':url})
    attachment_audio=any(item.get('type')=='audio' for item in attachments)
    audio=note.get('audio')
    if audio is not None:
        if not isinstance(audio,dict):raise Hold('asset_audio_shape')
        url=audio.get('play_url') or audio.get('url')
        if url:
            if not isinstance(url,str):raise Hold('asset_audio_shape')
            refs.append({'field':'audio/'+('play_url' if audio.get('play_url') else 'url'),'url':url})
        elif note.get('note_type') in ('audio','local_audio','recorder_audio','recorder_flash_audio') and not attachment_audio:
            raise Hold('asset_recording_url_missing')
    elif note.get('note_type') in ('audio','local_audio','recorder_audio','recorder_flash_audio') and not attachment_audio:raise Hold('asset_recording_url_missing')
    return refs + recording_images(note)
