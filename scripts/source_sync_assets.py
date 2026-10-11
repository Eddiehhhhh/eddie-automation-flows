"""Bounded, credential-free HTTPS assets in content-addressed private storage."""
import hashlib
import http.client
import ipaddress
import os
from pathlib import Path
import socket
import ssl
import tempfile
import time
from urllib.parse import urljoin, urlsplit

MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_RUN_BYTES = 200 * 1024 * 1024
MAX_ASSETS = 128
AUDIO_EXTENSIONS = {'.mp3', '.m4a', '.wav', '.aac', '.amr', '.ogg', '.flac', '.opus'}


class AssetError(RuntimeError):
    pass


class RetryableAssetError(AssetError):
    pass


def public_endpoint(url):
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise AssetError('asset URL must be credential-free HTTPS on port 443')
    host = parsed.hostname
    addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
        raise AssetError('asset destination must resolve only to public addresses')
    return parsed, addresses[0][4][0]


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Connect to the validated IP while retaining hostname verification and SNI."""
    def __init__(self, host, address, timeout):
        super().__init__(host, timeout=timeout, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        sock = socket.create_connection((self.address, 443), self.timeout)
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except Exception:
            sock.close()
            raise


def fetch_once(url, max_bytes=MAX_FILE_BYTES, timeout=30):
    for _ in range(6):
        parsed, address = public_endpoint(url)
        connection = PinnedHTTPSConnection(parsed.hostname, address, timeout)
        try:
            target = parsed.path or '/'
            if parsed.query:
                target += '?' + parsed.query
            # No Authorization, cookies, or local proxy credentials are forwarded.
            connection.request('GET', target, headers={'Accept-Encoding': 'identity', 'User-Agent': 'source-sync-assets/1'})
            response = connection.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader('Location')
                if not location:
                    raise AssetError('redirect missing destination')
                url = urljoin(url, location)
                continue  # Validate DNS/scheme/IP anew on every redirect.
            if response.status == 429 or 500 <= response.status < 600:
                raise RetryableAssetError('temporary asset HTTP failure')
            if response.status != 200:
                raise AssetError('asset HTTP request rejected')
            mime = (response.getheader('Content-Type') or 'application/octet-stream').split(';')[0].lower().strip()
            if mime.startswith(('audio/', 'video/')):
                raise AssetError('audio/video body download is disabled')
            length = response.getheader('Content-Length')
            try:
                expected = int(length) if length is not None else None
            except ValueError:
                raise AssetError('invalid asset length') from None
            if expected is not None and (expected < 0 or expected > max_bytes):
                raise AssetError('asset exceeds byte limit')
            chunks, size = [], 0
            deadline = time.monotonic() + timeout
            while True:
                if time.monotonic() > deadline:
                    raise TimeoutError('asset transfer deadline exceeded')
                chunk = response.read(min(65536, max_bytes - size + 1))
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise AssetError('asset exceeds byte limit')
                chunks.append(chunk)
            if expected is not None and size != expected:
                raise RetryableAssetError('incomplete asset body')
            if not size:
                raise AssetError('empty asset body')
            return b''.join(chunks), mime
        finally:
            connection.close()
    raise AssetError('asset redirect limit exceeded')


def fetch_asset(url, max_bytes=MAX_FILE_BYTES):
    for attempt in range(3):
        try:
            return fetch_once(url, max_bytes=max_bytes)
        except (RetryableAssetError, TimeoutError, ConnectionError, OSError, http.client.HTTPException):
            if attempt == 2:
                raise AssetError('asset transfer failed after bounded retries') from None
            time.sleep(2 ** attempt)


def write_binary(root, relative, content):
    root = Path(root)
    path = root / relative
    # Never trust upstream names or existing symlink paths.
    if not path.resolve().is_relative_to(root.resolve()):
        raise AssetError('asset path escapes root')
    for part in [path, *path.parents]:
        if part.is_symlink():
            raise AssetError('symlink in asset path')
        if part == root:
            break
    if path.is_symlink():
        raise AssetError('symlink asset')
    if path.exists() and path.read_bytes() == content:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.asset-')
    try:
        with os.fdopen(fd, 'wb') as out:
            out.write(content)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class AssetStore:
    def __init__(self, root, fetch=fetch_asset, max_file=MAX_FILE_BYTES, max_run=MAX_RUN_BYTES, max_assets=MAX_ASSETS):
        self.root, self.fetch = Path(root), fetch
        self.max_file, self.max_run, self.max_assets = max_file, max_run, max_assets
        self.bytes = 0
        self.memo = {}

    def materialize(self, note):
        attachments = note.get('attachments') or []
        if not isinstance(attachments, list):
            raise AssetError('invalid attachment list')
        result = []
        for attachment in attachments:
            if not isinstance(attachment, dict):
                raise AssetError('invalid attachment descriptor')
            url = attachment.get('original_url') or attachment.get('url')
            kind = attachment.get('type') or 'file'
            if not isinstance(url, str) or not url:
                raise AssetError('attachment URL missing')
            suffix = Path(urlsplit(url).path).suffix.lower()
            if kind in ('audio', 'video', 'link') or suffix in AUDIO_EXTENSIONS:
                result.append({'url': url, 'kind': kind, 'status': 'not-downloaded'})
                continue
            if url not in self.memo:
                if len(self.memo) >= self.max_assets:
                    raise AssetError('asset count limit reached')
                remaining = min(self.max_file, self.max_run - self.bytes)
                if remaining <= 0:
                    raise AssetError('run byte limit reached')
                content, mime = self.fetch(url, max_bytes=remaining)
                if not content or len(content) > remaining:
                    raise AssetError('asset byte limit violated')
                if mime.startswith(('audio/', 'video/')):
                    raise AssetError('audio/video body download is disabled')
                if kind == 'image' and not mime.startswith('image/'):
                    raise AssetError('image MIME mismatch')
                digest = hashlib.sha256(content).hexdigest()
                # Fixed safe extensions; never use title, Content-Disposition, or URL filenames.
                extension = {'image/png':'.png', 'image/jpeg':'.jpg', 'image/webp':'.webp', 'image/gif':'.gif', 'application/pdf':'.pdf'}.get(mime, '.bin')
                relative = Path('_assets') / ('images' if kind == 'image' else 'files') / (digest + extension)
                write_binary(self.root, relative, content)
                self.bytes += len(content)
                self.memo[url] = {'url':url, 'status':'downloaded', 'path':relative.as_posix(), 'sha256':digest, 'bytes':len(content), 'mime':mime}
            result.append(dict(self.memo[url], kind=kind))
        return result
