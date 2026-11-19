"""Binary delivery with durable Notion intents and byte-verified readback.

Transports never retry writes. ``send_file`` targets the fixed Notion API;
``download(url, sha256, size)`` must use an unauthenticated, bounded downloader
that rejects redirects and unapproved hosts (for example AssetArchive).
"""
import base64
import hashlib
import mimetypes
import os
import re
import secrets
import stat
from urllib.parse import urlencode

from assets import asset_references, manifest_coverage, open_regular, private_directory, safe_mime
from core import GitHubSink, Hold, canonical
from notion_sink import Hold as NotionHold, NotionSink, plain, rich
from render import render_text
import notion_native as native
from retry import HTTPFailure

GIT_UPLOAD_THRESHOLD = 20 * 1024 * 1024
MAX_GITHUB_BYTES = 100 * 1024 * 1024
PART_BYTES = 10 * 1024 * 1024


class _PrivateTransport:
    """Do not let adapter exceptions expose URLs, headers, or response bodies."""
    def __init__(self, transport):
        self.transport = transport

    def request(self, *args):
        try:
            return self.transport.request(*args)
        except HTTPFailure as error:
            raise Hold('media_http_' + str(error.status)) from None
        except Exception:
            raise Hold('media_transport_uncertain') from None

    def send_file(self, *args, **kwargs):
        try:
            return self.transport.send_file(*args, **kwargs)
        except HTTPFailure as error:
            raise Hold('media_http_' + str(error.status)) from None
        except Exception:
            raise Hold('media_transport_uncertain') from None


def asset_identity(asset):
    sha, size = asset.get('sha256'), asset.get('bytes')
    if (not isinstance(sha, str) or not re.fullmatch('[0-9a-f]{64}', sha)
            or type(size) is not int or size <= 0 or size > MAX_GITHUB_BYTES):
        raise Hold('media_asset_invalid_or_too_large')
    return sha, size


def checked_asset(asset, data):
    sha, size = asset_identity(asset)
    if not isinstance(data, bytes) or len(data) != size or hashlib.sha256(data).hexdigest() != sha:
        raise Hold('media_local_bytes_mismatch')
    return sha, size


def read_asset_bytes(asset, archive_root):
    root = private_directory(archive_root)
    if asset.get('local_name') != asset.get('sha256', '') + '.bin':
        raise Hold('media_local_name_invalid')
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fd = open_regular(asset['local_name'], directory)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077
                    or info.st_size > MAX_GITHUB_BYTES):
                raise Hold('media_local_file_not_private_or_too_large')
            data = stream.read(MAX_GITHUB_BYTES + 1)
    finally:
        os.close(directory)
    checked_asset(asset, data)
    return data


def manifest_assets(snapshot, manifest):
    refs = asset_references(snapshot['note'])
    if (not isinstance(manifest, dict) or manifest.get('source_id') != snapshot['id']
            or manifest.get('source_version') != snapshot['version']
            or manifest.get('coverage') != manifest_coverage(refs)):
        raise Hold('media_manifest_identity_or_coverage')
    assets = manifest.get('assets')
    if (not isinstance(assets, list) or any(not isinstance(a, dict) for a in assets) or len(assets) != len(refs)
            or [a.get('source_field') for a in assets] != [r['field'] for r in refs]):
        raise Hold('media_manifest_incomplete')
    for asset, ref in zip(assets, refs):
        if asset.get('source_url_sha256') != hashlib.sha256(ref['url'].encode()).hexdigest():
            raise Hold('media_manifest_source_mismatch')
    return assets


class GitHubMedia:
    def __init__(self, http, repo, branch, namespace, *, publish_large=None):
        self.publish_large = publish_large
        self.sink = GitHubSink(_PrivateTransport(http), repo, branch, namespace)

    def sync(self, asset, data):
        sha, size = checked_asset(asset, data)
        sink = self.sink
        repo = sink.http.request('GET', '/repos/' + sink.repo)
        if not repo or repo.get('private') is not True:
            raise Hold('private_repository_required')
        target = 'get-direct/' + sink.namespace + '/assets/' + sha + '.bin'
        path = '/repos/' + sink.repo + '/contents/' + target
        query = '?' + urlencode({'ref': sink.branch})
        old = sink.http.request('GET', path + query)
        if old:
            if sink.content(old) != data:
                raise Hold('media_github_content_conflict')
            observed = old
        elif self.publish_large and size >= GIT_UPLOAD_THRESHOLD:
            expected = hashlib.sha1(b'blob ' + str(size).encode() + b'\0' + data).hexdigest()
            commit = self.publish_large(sink.http, sink.repo, sink.branch, target, data, expected)
            observed = sink.http.request('GET', path + '?' + urlencode({'ref': commit}))
            if not observed or observed.get('sha') != expected:
                raise Hold('media_github_readback_mismatch')
        else:
            observed = sink.put_verified(path, {
                'message': 'Archive Get binary asset',
                'content': base64.b64encode(data).decode(), 'branch': sink.branch}, data,
                mismatch_reason='media_github_readback_mismatch')
        if not observed or sink.content(observed) != data:
            raise Hold('media_github_readback_mismatch')
        return {'sha256': sha, 'bytes': size, 'target': path, 'blob_sha': observed['sha'],
                'binary_assets_verified': True}

    def restore(self, asset, archive_root):
        """Read only from the private target; publish verified cache bytes without replacement."""
        sha, _ = asset_identity(asset)
        if asset.get('local_name') != sha + '.bin':
            raise Hold('media_local_name_invalid')
        root = private_directory(archive_root)
        if os.path.lexists(root / asset['local_name']):
            read_asset_bytes(asset, root)
            return True
        sink = self.sink
        repo = sink.http.request('GET', '/repos/' + sink.repo)
        if not repo or repo.get('private') is not True:
            raise Hold('private_repository_required')
        path = '/repos/' + sink.repo + '/contents/get-direct/' + sink.namespace + '/assets/' + sha + '.bin'
        observed = sink.http.request('GET', path + '?' + urlencode({'ref': sink.branch}))
        if not observed:
            return False
        data = sink.content(observed)
        checked_asset(asset, data)
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        temporary = '.restore-' + secrets.token_hex(16)
        created = False
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=directory)
            created = True
            with os.fdopen(fd, 'wb') as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            try:
                os.link(temporary, asset['local_name'], src_dir_fd=directory,
                        dst_dir_fd=directory, follow_symlinks=False)
            except FileExistsError:
                read_asset_bytes(asset, root)
            os.fsync(directory)
        finally:
            try:
                if created:
                    os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass
            os.close(directory)
        read_asset_bytes(asset, root)
        return True

    def sync_manifest(self, snapshot, manifest, archive_root):
        assets = manifest_assets(snapshot, manifest)
        receipts = [self.sync(a, read_asset_bytes(a, archive_root)) for a in assets]
        return {'version': snapshot['version'], 'assets': receipts,
                'binary_assets_verified': True, 'media_coverage': manifest['coverage']}


class NotionMedia:
    def __init__(self, http, database_id, journal, *, namespace, download,
                 id_property='Get ID', owner_property='Sync Owner', title_property='Name', source_base=None):
        self.sink = NotionSink(_PrivateTransport(http), database_id, journal, namespace=namespace,
                               id_property=id_property, owner_property=owner_property,
                               title_property=title_property, source_base=source_base)
        self.download = download

    def _state(self, key, state, phase, **fields):
        state = dict(state, phase=phase, **fields)
        self.sink.journal.set(key, state)
        return state

    def _locate(self, page_id, header, state):
        matches = [b for b in self.sink.children(page_id)
                   if b.get('type') == 'toggle' and native.matches_title(b['toggle']['rich_text'], header)]
        if len(matches) > 1:
            raise Hold('media_duplicate_toggle')
        if state.get('block_id') and (not matches or matches[0]['id'] != state['block_id']):
            raise Hold('media_managed_toggle_modified')
        return matches[0] if matches else None

    def _verify_block(self, block, asset, *, kind='file', caption=None):
        children = self.sink.children(block['id'])
        if len(children) != 1 or children[0].get('type') != kind or children[0].get('has_children'):
            raise Hold('media_managed_block_modified')
        media = children[0][kind]
        if plain(media.get('caption', [])) != (caption if caption is not None else 'sha256:' + asset['sha256']):
            raise Hold('media_managed_caption_modified')
        for item in media.get('caption', []):
            annotations = item.get('annotations', {})
            if (item.get('href') or item.get('text', {}).get('link')
                    or any(annotations.get(k) for k in ('bold', 'italic', 'underline', 'strikethrough', 'code'))
                    or annotations.get('color', 'default') != 'default'):
                raise Hold('media_managed_caption_modified')
        url = media.get('file', {}).get('url') if media.get('type') == 'file' else None
        if not isinstance(url, str):
            raise Hold('media_hosted_file_unavailable')
        try:
            observed = self.download(url, asset['sha256'], asset['bytes'])
        except HTTPFailure as error:
            raise Hold('media_http_' + str(error.status)) from None
        except Exception:
            raise Hold('media_download_uncertain') from None
        if isinstance(observed, bytes):
            checked_asset(asset, observed)
        elif (not isinstance(observed, dict) or observed.get('sha256') != asset['sha256']
                or observed.get('bytes') != asset['bytes']
                or observed.get('verification') != 'local_bytes_readback'):
            raise Hold('media_notion_readback_mismatch')
        return {'sha256': asset['sha256'], 'bytes': asset['bytes'],
                'block_id': children[0]['id'], 'toggle_id': block['id'],
                'binary_assets_verified': True}

    def _upload(self, key, state, asset, data):
        http = self.sink.http
        sha, size = checked_asset(asset, data)
        mime = safe_mime(asset.get('content_type'))
        extension = {'audio/wav':'.wav','audio/x-wav':'.wav','audio/ogg':'.ogg',
                     'audio/mpeg':'.mp3','audio/mp4':'.m4a'}.get(mime) or mimetypes.guess_extension(mime) or '.bin'
        if not re.fullmatch(r'\.[a-zA-Z0-9]{1,10}', extension):
            extension = '.bin'
        if mime == 'application/octet-stream' or extension == '.bin':
            raise Hold('media_notion_unsupported_mime')
        filename = sha + extension
        parts = (size + PART_BYTES - 1) // PART_BYTES if size > 20 * 1024 * 1024 else 1
        if not state.get('upload_id'):
            if state.get('phase') == 'upload_create_intent':
                raise Hold('media_upload_create_requires_reconcile')
            state = self._state(key, state, 'upload_create_intent')
            body = {'mode': 'multi_part' if parts > 1 else 'single_part',
                    'filename': filename, 'content_type': mime}
            if parts > 1:
                body['number_of_parts'] = parts
            upload = http.request('POST', '/v1/file_uploads', body)
            if not upload or not isinstance(upload.get('id'), str):
                raise Hold('media_upload_create_uncertain')
            state = self._state(key, state, 'upload_known', upload_id=upload['id'], completed_parts=0)
        upload_id = state['upload_id']
        if not re.fullmatch(r'[0-9a-fA-F-]{32,36}', upload_id):
            raise Hold('media_upload_identity_invalid')
        status = http.request('GET', '/v1/file_uploads/' + upload_id)
        if not status or status.get('id') != upload_id or status.get('in_trash'):
            raise Hold('media_upload_identity_invalid')
        if status.get('status') == 'uploaded':
            return self._state(key, state, 'upload_ready')
        if status.get('status') != 'pending':
            raise Hold('media_upload_not_pending')
        if state.get('phase') in ('part_send_intent', 'upload_complete_intent'):
            raise Hold('media_upload_send_requires_reconcile')
        completed = state.get('completed_parts', 0)
        if type(completed) is not int or not 0 <= completed <= parts:
            raise Hold('media_upload_parts_invalid')
        for index in range(completed, parts):
            state = self._state(key, state, 'part_send_intent', part_number=index + 1)
            piece = data[index * PART_BYTES:(index + 1) * PART_BYTES] if parts > 1 else data
            http.send_file(upload_id, filename, mime, piece,
                           part_number=index + 1 if parts > 1 else None)
            state = self._state(key, state, 'part_sent', completed_parts=index + 1)
        if parts > 1:
            state = self._state(key, state, 'upload_complete_intent')
            http.request('POST', '/v1/file_uploads/' + upload_id + '/complete', {})
        status = http.request('GET', '/v1/file_uploads/' + upload_id)
        if not status or status.get('status') != 'uploaded':
            raise Hold('media_upload_not_uploaded')
        return self._state(key, state, 'upload_ready')

    def sync(self, snapshot, text_receipt, manifest, archive_root):
        assets = manifest_assets(snapshot, manifest)
        sink = self.sink
        if (text_receipt.get('version') != snapshot['version']
                or text_receipt.get('content_sha256') != hashlib.sha256(render_text(snapshot).encode()).hexdigest()):
            raise Hold('media_text_receipt_mismatch')
        sink.preflight()
        pages = sink.pages(snapshot['id'])
        if len(pages) != 1 or pages[0]['id'] != text_receipt.get('page_id'):
            raise Hold('media_page_identity_mismatch')
        page = sink.request('GET', '/pages/' + pages[0]['id'])
        props = (page or {}).get('properties', {})
        if (not page or page.get('archived') or page.get('in_trash')
                or page.get('parent', {}).get('database_id', '').replace('-', '') != sink.database.replace('-', '')
                or plain(props.get(sink.idprop, {}).get('rich_text', [])) != snapshot['id']
                or plain(props.get(sink.ownerprop, {}).get('rich_text', [])) != sink.owner
                or plain(props.get(sink.titleprop, {}).get('title', [])) != text_receipt.get('title')):
            raise Hold('media_page_ownership_modified')
        try:
            sink.verify_text_receipt(sink.children(page['id']), snapshot['id'], text_receipt)
        except (Hold, NotionHold):
            raise Hold('media_managed_text_modified') from None
        native_media = text_receipt.get('format') == native.FORMAT
        receipts = []
        for index, asset in enumerate(assets):
            data = read_asset_bytes(asset, archive_root)
            label = sink.owner + ':media:' + snapshot['id'] + ':' + snapshot['version'] + ':' + asset['sha256'] + ':' + str(index)
            key = 'media:' + sink.database + ':' + label
            state = sink.journal.get(key) or {}
            kind, caption, header = 'file', None, rich(label)
            if native_media:
                mime = safe_mime(asset.get('content_type'))
                group = mime.split('/', 1)[0]
                kind = group if group in ('image', 'audio', 'video') else 'file'
                name = {'image': '图片', 'audio': '录音', 'video': '视频', 'file': '附件'}[kind]
                caption = name + ' ' + str(index + 1)
                header = sink.display_title(caption, sink.origin(snapshot['id'], snapshot['version']))
            block = self._locate(page['id'], header, state)
            if block and not state:
                raise Hold('media_unknown_existing_toggle')
            if not block:
                if state.get('phase') in ('attach_intent', 'verified'):
                    raise Hold('media_attach_requires_reconcile')
                state = self._upload(key, state, asset, data)
                state = self._state(key, state, 'attach_intent')
                sink.request('PATCH', '/blocks/' + page['id'] + '/children', {'children': [{
                    'object': 'block', 'type': 'toggle', 'toggle': {'rich_text': header, 'children': [{
                        'object': 'block', 'type': kind, kind: {'type': 'file_upload',
                        'file_upload': {'id': state['upload_id']},
                        'caption': rich(caption if caption is not None else 'sha256:' + asset['sha256'])}}]}}]})
                block = self._locate(page['id'], header, state)
                if not block:
                    raise Hold('media_attach_readback_missing')
            receipt = self._verify_block(block, asset, kind=kind, caption=caption)
            if native_media:
                receipt.update(kind=kind, caption=caption)
            self._state(key, state, 'verified', block_id=block['id'], receipt=receipt)
            receipts.append(receipt)
        return {'version': snapshot['version'], 'page_id': page['id'], 'assets': receipts,
                'binary_assets_verified': True, 'media_coverage': manifest['coverage']}
