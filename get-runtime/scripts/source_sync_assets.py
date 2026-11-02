"""Bounded, credential-free HTTPS assets in content-addressed private storage."""
import hashlib
import http.client
import ipaddress
import os
from pathlib import Path
import socket
import ssl
import tempfile
import threading
import queue
import time
from urllib.parse import urljoin, urlsplit

MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_MEDIA_BYTES = 48 * 1024 * 1024
MAX_RUN_BYTES = 200 * 1024 * 1024
MAX_ASSETS = 128
AUDIO_EXTENSIONS = {'.mp3', '.m4a', '.wav', '.aac', '.amr', '.ogg', '.flac', '.opus'}


def valid_ogg_page(data):
    if len(data) < 27 or data[:5] != b'OggS\x00':
        return False
    count = data[26]
    if len(data) < 27 + count:
        return False
    size = 27 + count + sum(data[27:27 + count])
    if size > len(data):
        return False
    expected = int.from_bytes(data[22:26], 'little')
    crc = 0
    for index, byte in enumerate(data[:size]):
        crc ^= (0 if 22 <= index < 26 else byte) << 24
        for _ in range(8):
            crc = ((crc << 1) ^ (0x04c11db7 if crc & 0x80000000 else 0)) & 0xffffffff
    return crc == expected


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
        # Expose the transport before connect/handshake so the attempt watchdog
        # can interrupt either phase, rather than granting each a fresh timeout.
        sock = socket.socket(socket.AF_INET6 if ':' in self.address else socket.AF_INET, socket.SOCK_STREAM)
        self.sock = sock
        sock.settimeout(self.timeout)
        try:
            sock.connect((self.address, 443))
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host, do_handshake_on_connect=False)
            self.sock.do_handshake()
        except Exception:
            self.sock.close()
            raise


def remaining(deadline):
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise TimeoutError("asset transfer deadline exceeded")
    return seconds


def resolve_before(url, deadline):
    # DNS has no socket timeout in Python. Bound the caller's wait; at most one
    # daemon resolver remains per failed attempt, and retries are bounded to 3.
    result = queue.Queue(maxsize=1)
    def resolve():
        try:
            result.put((public_endpoint(url), None))
        except Exception as exc:
            result.put((None, exc))
    threading.Thread(target=resolve, daemon=True).start()
    try:
        value, error = result.get(timeout=remaining(deadline))
    except queue.Empty:
        raise TimeoutError("asset DNS deadline exceeded") from None
    if error:
        raise error
    remaining(deadline)
    return value


def fetch_once(url, max_bytes=MAX_FILE_BYTES, timeout=30, transfer_budget=None, allow_media=False):
    deadline = time.monotonic() + timeout
    for _ in range(6):
        parsed, address = resolve_before(url, deadline)
        connection = PinnedHTTPSConnection(parsed.hostname, address, remaining(deadline))
        active_socket = [None]
        def abort():
            # HTTPResponse can detach connection.sock (Connection: close), so
            # retain the transport before reading headers. This also interrupts
            # slow-drip headers/chunk framing inside http.client.
            sock = active_socket[0] or connection.sock
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        watchdog = threading.Timer(remaining(deadline), abort)
        watchdog.daemon = True
        watchdog.start()
        try:
            target = parsed.path or '/'
            if parsed.query:
                target += '?' + parsed.query
            # No Authorization, cookies, or local proxy credentials are forwarded.
            connection.request('GET', target, headers={'Accept-Encoding': 'identity', 'User-Agent': 'source-sync-assets/1'})
            active_socket[0] = connection.sock
            if active_socket[0] is not None:
                active_socket[0].settimeout(remaining(deadline))
            response = connection.getresponse()
            remaining(deadline)
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
            if mime.startswith(('audio/', 'video/')) and not allow_media:
                raise AssetError('audio/video body download is disabled')
            length = response.getheader('Content-Length')
            try:
                expected = int(length) if length is not None else None
            except ValueError:
                raise AssetError('invalid asset length') from None
            if expected is not None and (expected < 0 or expected > max_bytes):
                raise AssetError('asset exceeds byte limit')
            chunks, size = [], 0
            while True:
                wait = remaining(deadline)
                if active_socket[0] is not None:
                    active_socket[0].settimeout(wait)
                # read(n) may wait for n bytes across an arbitrary number of
                # successful tiny receives. read1 returns after one raw read.
                read_limit = min(65536, max_bytes - size + 1)
                if transfer_budget is not None:
                    read_limit = transfer_budget.read_limit(read_limit)
                try:
                    chunk = response.read1(read_limit)
                except http.client.IncompleteRead as error:
                    if transfer_budget is not None:
                        transfer_budget.consume(len(error.partial))
                    raise
                if transfer_budget is not None:
                    transfer_budget.consume(len(chunk))
                remaining(deadline)
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
            watchdog.cancel()
            watchdog.join()
            connection.close()
    raise AssetError('asset redirect limit exceeded')


def fetch_asset(url, max_bytes=MAX_FILE_BYTES, transfer_budget=None, allow_media=False):
    for attempt in range(3):
        try:
            options = {} if transfer_budget is None else {'transfer_budget': transfer_budget}
            if allow_media: options['allow_media'] = True
            return fetch_once(url, max_bytes=max_bytes, **options)
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
    def __init__(self, root, fetch=fetch_asset, max_file=MAX_FILE_BYTES, max_run=MAX_RUN_BYTES, max_assets=MAX_ASSETS, allow_media=False):
        self.allow_media = allow_media
        if allow_media and fetch is fetch_asset:
            from functools import partial
            fetch = partial(fetch_asset, allow_media=True)
        self.root, self.fetch = Path(root), fetch
        self.max_file, self.max_run, self.max_assets = max_file, max_run, max_assets
        self.bytes = 0
        self.media_attempts = 0
        self.media_bytes = 0
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
            media = kind in ('audio', 'video') or suffix in AUDIO_EXTENSIONS | {'.mp4','.webm','.mov','.m3u8'}
            if kind == 'link' or suffix == '.m3u8' or (media and not self.allow_media):
                result.append({'url': url, 'kind': kind, 'status': 'not-downloaded', 'reason':'unsupported_stream' if suffix == '.m3u8' else 'download_disabled'})
                continue
            if url not in self.memo:
                if media:
                    if self.media_attempts >= 2:
                        result.append({'url':url,'kind':kind,'status':'not-downloaded','reason':'media_attempt_budget'})
                        continue
                    self.media_attempts += 1
                try:
                    if len(self.memo) >= self.max_assets:
                        raise AssetError('asset count limit reached')
                    remaining = min(MAX_MEDIA_BYTES if media and self.allow_media and self.max_file == MAX_FILE_BYTES else self.max_file, self.max_run - self.bytes)
                    if media: remaining = min(remaining, MAX_MEDIA_BYTES - self.media_bytes)
                    if remaining <= 0:
                        raise AssetError('run byte limit reached')
                    content, mime = self.fetch(url, max_bytes=remaining)
                    if not content or len(content) > remaining:
                        raise AssetError('asset byte limit violated')
                    if self.allow_media and kind == 'audio' and mime == 'application/octet-stream' and valid_ogg_page(content):
                        mime = 'audio/ogg'
                    if mime.startswith(('audio/', 'video/')) and not self.allow_media:
                        raise AssetError('audio/video body download is disabled')
                    if media and not mime.startswith(('audio/', 'video/')):
                        raise AssetError('unsupported media MIME')
                    if kind == 'image' and not mime.startswith('image/'):
                        raise AssetError('image MIME mismatch')
                    digest = hashlib.sha256(content).hexdigest()
                    # Fixed safe extensions; never use title, Content-Disposition, or URL filenames.
                    extension = {'image/png':'.png', 'image/jpeg':'.jpg', 'image/webp':'.webp', 'image/gif':'.gif', 'application/pdf':'.pdf', 'audio/ogg':'.ogg'}.get(mime, '.bin')
                    relative = Path('_assets') / ('images' if kind == 'image' else 'files') / (digest + extension)
                    write_binary(self.root, relative, content)
                    self.bytes += len(content)
                    if media: self.media_bytes += len(content)
                    self.memo[url] = {'url':url, 'status':'downloaded', 'path':relative.as_posix(), 'sha256':digest, 'bytes':len(content), 'mime':mime}
                except (AssetError, TimeoutError, OSError, http.client.HTTPException) as error:
                    if not media: raise
                    reason='media_byte_budget' if 'byte' in str(error) else 'unsupported_format' if 'MIME' in str(error) else 'media_download_failed'
                    result.append({'url':url,'kind':kind,'status':'not-downloaded','reason':reason})
                    continue
            result.append(dict(self.memo[url], kind=kind))
        return result
