"""Pinned private-Git blobs -> native Notion uploads; the caller owns attachment.

No CLI, secret discovery, scheduling, persistence, page writes or implicit retries.
All error strings are fixed codes. Receipts and returned links are private data.
"""
from __future__ import annotations
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import datetime as dt
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
import uuid

import cloud_private_checkout as private_checkout

NOTION_VERSION = '2026-03-11'
SINGLE_PART_BYTES = 20 * 1024 * 1024
SOURCE_READ_BYTES = 64 * 1024 * 1024
CREDENTIAL_SHAPE = re.compile(
    rb'(?<![A-Za-z0-9_])(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}'
    rb'|sk-[A-Za-z0-9_-]{20,})(?![A-Za-z0-9_])'
    rb'|-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----')


class AssetHeld(ValueError):
    def __init__(self, code): self.code = code; super().__init__(code)


class UnknownAssetWrite(AssetHeld): pass


@dataclass(frozen=True, repr=False)
class GitBlobAsset:
    commit: str
    path: str
    blob: str
    sha256: str
    data: bytes
    content_type: str
    filename: str
    block_type: str
    fallback_url: str | None

    def __repr__(self): return 'GitBlobAsset(<private>)'


def _git(root, *args):
    try:
        result = subprocess.run(['git', '-C', str(root), *args], capture_output=True, timeout=60)
        if result.returncode: raise AssetHeld('source_git_unreadable')
        return result.stdout
    except (OSError, subprocess.TimeoutExpired): raise AssetHeld('source_git_unreadable') from None


def _path(value):
    if not isinstance(value, str) or not value or '\\' in value or '\0' in value:
        raise AssetHeld('source_path_invalid')
    path = PurePosixPath(value)
    if path.is_absolute() or '..' in path.parts or path.as_posix() != value:
        raise AssetHeld('source_path_invalid')
    return path


def _origin_repository(root):
    expected = (r'(?:https://github\.com/|git@github\.com:|ssh://git@(?:github\.com|github-eddie-llm-wiki)/)'
                + re.escape(private_checkout.REPOSITORY) + r'(?:\.git)?')
    for args in [('remote','get-url','--all','origin'),('remote','get-url','--push','--all','origin')]:
        urls=_git(root,*args).decode().splitlines()
        if len(urls)!=1 or not re.fullmatch(expected,urls[0]):return None
    return private_checkout.REPOSITORY


def private_git_link(root, commit, relative_path):
    """Caller must have independently verified this checkout is the approved private repo."""
    path = _path(relative_path)
    if not re.fullmatch('[0-9a-f]{40}', commit): raise AssetHeld('source_commit_not_fixed')
    repo = _origin_repository(root)
    if not repo: return None
    rows=[row for row in _git(root,'ls-tree','-z',commit,'--',path.as_posix()).split(b'\0') if row]
    if len(rows)!=1:return None
    metadata,found=rows[0].split(b'\t',1)
    mode,kind,_=metadata.decode().split()
    if mode not in ('100644','100755') or kind!='blob' or found.decode()!=path.as_posix():return None
    return 'https://github.com/' + repo + '/blob/' + commit + '/' + urllib.parse.quote(path.as_posix(), safe='/')


def prepare(root, fixed_commit, relative_path):
    try: return _prepare(root, fixed_commit, relative_path)
    except AssetHeld: raise
    except Exception: raise AssetHeld('source_unreadable') from None


def _prepare(root, fixed_commit, relative_path):
    root = Path(root)
    path = _path(relative_path)
    if not isinstance(fixed_commit, str) or not re.fullmatch('[0-9a-f]{40}', fixed_commit):
        raise AssetHeld('source_commit_not_fixed')
    if root.is_symlink(): raise AssetHeld('source_symlink')
    current = root
    for part in path.parts:
        current = current / part
        if current.is_symlink(): raise AssetHeld('source_symlink')
    if not current.resolve().is_relative_to(root.resolve()): raise AssetHeld('source_outside_root')
    if _git(root, 'rev-parse', '--verify', fixed_commit + '^{commit}').decode().strip() != fixed_commit:
        raise AssetHeld('source_commit_not_fixed')
    raw = _git(root, 'ls-tree', '-z', fixed_commit, '--', path.as_posix())
    rows = [row for row in raw.split(b'\0') if row]
    if len(rows) != 1: raise AssetHeld('source_not_tracked_in_commit')
    metadata, found = rows[0].split(b'\t', 1)
    mode, kind, blob = metadata.decode().split()
    if found.decode() != path.as_posix() or kind != 'blob' or mode not in ('100644', '100755'):
        raise AssetHeld('source_not_regular_blob')
    size = int(_git(root, 'cat-file', '-s', blob).strip())
    if size > SOURCE_READ_BYTES: raise AssetHeld('source_read_budget')
    data = _git(root, 'cat-file', 'blob', blob)
    if CREDENTIAL_SHAPE.search(data): raise AssetHeld('potential_credential_in_asset')
    if data.startswith(b'version https://git-lfs.github.com/spec/v1\n'):
        raise AssetHeld('source_lfs_payload_unavailable')
    if len(data) != size or hashlib.sha1(b'blob ' + str(size).encode() + b'\0' + data).hexdigest() != blob:
        raise AssetHeld('source_blob_mismatch')
    try:
        probe = subprocess.run(['file', '--brief', '--mime-type', '-'], input=data,
                               capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired): raise AssetHeld('source_mime_probe_unavailable') from None
    mime = probe.stdout.decode(errors='strict').strip()
    if probe.returncode or not re.fullmatch(r'[a-z0-9.+-]+/[a-z0-9.+-]+', mime):
        raise AssetHeld('source_mime_probe_failed')
    sha = hashlib.sha256(data).hexdigest()
    # Use byte-detected type, never a private filename extension, to choose a block.
    block_type = 'image' if mime.startswith('image/') else 'pdf' if mime == 'application/pdf' else 'file'
    extensions = {'image/png':'.png', 'image/jpeg':'.jpg', 'image/gif':'.gif', 'image/webp':'.webp',
                  'image/svg+xml':'.svg', 'application/pdf':'.pdf', 'text/plain':'.txt',
                  'application/json':'.json', 'application/zip':'.zip'}
    filename = 'asset-' + sha + extensions.get(mime, '')
    return GitBlobAsset(fixed_commit, path.as_posix(), blob, sha, data, mime, filename,
                        block_type, private_git_link(root, fixed_commit, path.as_posix()))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs): return None


def _request(api_key, method, path, body=None, content_type='application/json'):
    if not re.fullmatch(r'/(?:users/me|file_uploads(?:/[0-9a-f-]{36}(?:/send)?)?)', path):
        raise AssetHeld('asset_api_path_invalid')
    allowed = ((method=='GET' and (path=='/users/me' or re.fullmatch(r'/file_uploads/[0-9a-f-]{36}',path)))
               or (method=='POST' and (path=='/file_uploads' or re.fullmatch(r'/file_uploads/[0-9a-f-]{36}/send',path))))
    if not allowed:raise AssetHeld('asset_api_method_invalid')
    data = json.dumps(body).encode() if isinstance(body, dict) else body
    request = urllib.request.Request('https://api.notion.com/v1' + path, data=data,
        headers={'Authorization':'Bearer ' + api_key, 'Notion-Version':NOTION_VERSION,
                 'Content-Type':content_type}, method=method)
    try:
        with urllib.request.build_opener(_NoRedirect()).open(request, timeout=60) as response:
            raw = response.read(1024 * 1024 + 1)
            if response.status != 200 or len(raw) > 1024 * 1024:
                raise ValueError('invalid response')
            length = response.headers.get('Content-Length')
            if length is not None and int(length) != len(raw): raise ValueError('short response')
            result = json.loads(raw)
            if not isinstance(result, dict): raise ValueError('invalid response')
            return result
    except urllib.error.HTTPError as exc:
        if method != 'GET' and (exc.code >= 500 or exc.code in (408, 409)):
            raise UnknownAssetWrite('asset_write_unknown') from None
        reason = {400:'asset_validation_rejected',403:'asset_access_or_limit_denied',
                  404:'asset_not_accessible',429:'asset_rate_limited'}.get(exc.code,'asset_api_rejected')
        raise AssetHeld(reason) from None
    except (OSError, ValueError):
        if method != 'GET': raise UnknownAssetWrite('asset_write_unknown') from None
        raise AssetHeld('asset_readback_failed') from None


def _uuid(value):
    try: return str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError): raise AssetHeld('asset_upload_identity_invalid') from None


def _uploaded(value, asset, ident):
    expires = value.get('expiry_time')
    if expires:
        try:
            if dt.datetime.fromisoformat(expires.replace('Z','+00:00')) <= dt.datetime.now(dt.timezone.utc):
                return False
        except (ValueError, TypeError): return False
    return (value.get('object') == 'file_upload' and value.get('id') == ident
            and value.get('status') == 'uploaded' and not value.get('archived') and not value.get('in_trash')
            and type(value.get('content_length')) is int and value.get('content_length') == len(asset.data)
            and value.get('filename') == asset.filename and value.get('content_type') == asset.content_type)


def publish(asset, api_key, checkpoint, prior_receipt=None):
    """Return ready native block or held; checkpoint must durably publish each receipt.

    An exception from checkpoint is not success. The caller must preserve its
    prior durable dispatched receipt and must never append a returned held block.
    """
    receipt = deepcopy(prior_receipt) if prior_receipt else {
        'version':1, 'sha256':asset.sha256, 'size':len(asset.data),
        'content_type':asset.content_type, 'filename':asset.filename,
        'source_commit':asset.commit, 'source_path':asset.path, 'source_blob':asset.blob, 'phase':'new'}
    def held(reason):
        return {'status':'held', 'block':None, 'receipt':deepcopy(receipt), 'reason':reason,
                'fallback_url':asset.fallback_url, 'fallback_requires_private_github_access':True}
    def save():
        try: checkpoint(deepcopy(receipt))
        except Exception: raise AssetHeld('asset_checkpoint_failed') from None
    def ready(ident):
        receipt.setdefault('sources',{}).setdefault(asset.commit,{})[asset.path]=asset.blob
        receipt.update({'phase':'verified', 'no_retry':False, 'upload_id':ident})
        save()
        block = {'object':'block', 'type':asset.block_type, asset.block_type:{
            'type':'file_upload', 'file_upload':{'id':ident}, 'caption':[]}}
        return {'status':'ready', 'block':block, 'receipt':deepcopy(receipt), 'reason':None}
    try:
        if CREDENTIAL_SHAPE.search(asset.data): return held('potential_credential_in_asset')
        if (not isinstance(asset.data,bytes) or hashlib.sha256(asset.data).hexdigest()!=asset.sha256
                or hashlib.sha1(b'blob '+str(len(asset.data)).encode()+b'\0'+asset.data).hexdigest()!=asset.blob):
            return held('asset_source_binding_mismatch')
        if any(receipt.get(key) != value for key,value in {
            'version':1,'sha256':asset.sha256,'size':len(asset.data),
            'content_type':asset.content_type,'filename':asset.filename}.items()):
            return held('asset_receipt_binding_mismatch')
        ident = receipt.get('upload_id')
        if ident:
            ident = _uuid(ident)
            current = _request(api_key, 'GET', '/file_uploads/' + ident)
            if _uploaded(current, asset, ident): return ready(ident)
            # Recovery is GET-only even if an earlier send was actually rejected.
            if receipt.get('phase') != 'created' or receipt.get('no_retry'):
                return held('asset_upload_not_verified')
            if (current.get('object')!='file_upload' or current.get('id') != ident
                    or current.get('status') != 'pending'
                    or current.get('filename')!=asset.filename or current.get('content_type')!=asset.content_type):
                return held('asset_upload_not_pending')
        elif receipt.get('phase') != 'new' or receipt.get('no_retry'):
            return held('asset_create_unknown_no_retry')
        if len(asset.data) > SINGLE_PART_BYTES: return held('asset_multipart_required')
        limits = _request(api_key, 'GET', '/users/me').get('bot', {}).get('workspace_limits', {})
        maximum = limits.get('max_file_upload_size_in_bytes')
        if type(maximum) is not int or maximum <= 0: return held('asset_workspace_limit_unknown')
        if len(asset.data) > maximum: return held('asset_workspace_limit_exceeded')
        if not ident:
            receipt.update({'phase':'prepared','no_retry':True}); save()
            try:
                created = _request(api_key,'POST','/file_uploads',{
                    'mode':'single_part','filename':asset.filename,'content_type':asset.content_type})
                ident = _uuid(created.get('id'))
                receipt['upload_id'] = ident
                if created.get('object') != 'file_upload' or created.get('status') != 'pending':
                    raise UnknownAssetWrite('asset_create_response_unknown')
            except AssetHeld as exc:
                receipt.update({'phase':'create_unknown' if isinstance(exc,UnknownAssetWrite) else 'create_rejected',
                                'no_retry':True}); save()
                return held('asset_create_unknown_no_retry' if isinstance(exc,UnknownAssetWrite) else exc.code)
            except Exception:
                receipt.update({'phase':'create_unknown','no_retry':True}); save()
                return held('asset_create_unknown_no_retry')
            receipt.update({'phase':'created','upload_id':ident,'no_retry':False}); save()
        boundary = 'notion-asset-' + uuid.uuid4().hex
        prefix = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{asset.filename}"\r\n'
                  f'Content-Type: {asset.content_type}\r\n\r\n').encode()
        body = prefix + asset.data + f'\r\n--{boundary}--\r\n'.encode()
        receipt.update({'phase':'dispatched','no_retry':True}); save()
        try: _request(api_key,'POST','/file_uploads/' + ident + '/send',body,
                      'multipart/form-data; boundary=' + boundary)
        except AssetHeld as exc:
            receipt.update({'phase':'send_unknown' if isinstance(exc,UnknownAssetWrite) else 'send_rejected',
                            'no_retry':True}); save()
            return held('asset_send_unknown_readback_required' if isinstance(exc,UnknownAssetWrite) else exc.code)
        except Exception:
            receipt.update({'phase':'send_unknown','no_retry':True}); save()
            return held('asset_send_unknown_readback_required')
        receipt.update({'phase':'sent','no_retry':True}); save()
        current = _request(api_key,'GET','/file_uploads/' + ident)
        if not _uploaded(current,asset,ident): return held('asset_upload_metadata_mismatch')
        return ready(ident)
    except AssetHeld as exc: return held(exc.code)
    except Exception: return held('asset_operation_failed')


NOTION_DOWNLOAD_HOSTS = {
    'prod-files-secure.s3.us-west-2.amazonaws.com',
    's3.us-west-2.amazonaws.com',
    'secure.notion-static.com',
    'file.notion.so',
}


def validate_download_url(value):
    if not isinstance(value,str): raise AssetHeld('asset_download_url_invalid')
    parsed=urllib.parse.urlsplit(value)
    if (parsed.scheme!='https' or parsed.username or parsed.password or parsed.port not in (None,443)
            or not parsed.hostname or not (parsed.hostname in NOTION_DOWNLOAD_HOSTS
            or parsed.hostname.endswith('.notionusercontent.com'))):
        raise AssetHeld('asset_download_host_not_allowed')
    return value


def _download_returned_asset(url):
    # Shared public-address-pinned TLS transport; never receives the Notion token.
    from sync_notion_flomo_to_raw import download_asset
    return download_asset(url)


def _native_bytes(block):
    kind=block.get('type')
    if kind not in {'image','file','pdf','audio','video'} or block.get('archived') or block.get('in_trash'):
        raise AssetHeld('asset_target_block_mismatch')
    value=block.get(kind,{})
    if value.get('type')!='file': raise AssetHeld('asset_target_not_native_file')
    url=validate_download_url(value.get('file',{}).get('url'))
    try:return _download_returned_asset(url)
    except urllib.error.HTTPError as exc:
        raise AssetHeld({403:'asset_target_access_denied',429:'asset_target_rate_limited'}.get(
            exc.code,'asset_target_binary_unreadable')) from None
    except ValueError as exc:
        raise AssetHeld('asset_target_read_budget' if str(exc)=='asset_file_budget' else
                        'asset_target_binary_unreadable') from None
    except Exception: raise AssetHeld('asset_target_binary_unreadable') from None


def canonical_native_file(block):
    """Stable actual-byte identity for a human-managed native file, not Git proof."""
    data=_native_bytes(block)
    return {'type':block['type'],'sha256':hashlib.sha256(data).hexdigest(),'size':len(data)}


def verify_uploaded_block(asset,block):
    """Read actual target bytes; temporary URL text alone is never identity proof."""
    if block.get('type')!=asset.block_type:raise AssetHeld('asset_target_block_mismatch')
    data=_native_bytes(block)
    if len(data)!=len(asset.data) or hashlib.sha256(data).hexdigest()!=asset.sha256:
        raise AssetHeld('asset_target_binary_mismatch')
    return asset.sha256


def canonical_asset_identity(asset,block):
    return {'type':asset.block_type,'sha256':verify_uploaded_block(asset,block),
            'size':len(asset.data),'content_type':asset.content_type}
