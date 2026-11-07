"""Retain still-useful OSS V1 links without hiding source or attachment edits.

Only the observed OSSAccessKeyId/Expires/Signature triplet is recognized.
No query is removed and no URL is reconstructed. Downloads use fresh source URLs.
"""
import copy
import json
import stat
from pathlib import Path
import time
from urllib.parse import parse_qsl, urlsplit

from source_sync_store import attachment_manifest, validate_tree_path, json_text

REFRESH_BEFORE_SECONDS = 12 * 60 * 60
MAX_CACHE_BYTES = 2 * 1024 * 1024
FIELDS = ('attachments', 'images', 'files', 'resources')
URL_FIELDS = ('url', 'original_url', 'download_url')
SIGNING_KEYS = frozenset(('OSSAccessKeyId', 'Expires', 'Signature'))


def oss_v1_url(value):
    """Return a strict parse or None; never log source URL/query values."""
    if not isinstance(value, str) or len(value) > 16384 or any(ord(c) <= 32 for c in value):
        return None
    try:
        parts = urlsplit(value)
        if (parts.scheme != 'https' or not parts.hostname or parts.username is not None
                or parts.password is not None or parts.port not in (None, 443)):
            return None
        pairs = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=True,
                          max_num_fields=3)
        params = dict(pairs)
        if (len(pairs) != 3 or set(params) != SIGNING_KEYS
                or any(not value for value in params.values())):
            return None
        expiry = params['Expires']
        if not expiry.isascii() or not expiry.isdigit() or len(expiry) > 12:
            return None
        return (parts.scheme, parts.netloc, parts.path, parts.fragment), params, int(expiry)
    except ValueError:
        return None


def reusable_url(old, new, now):
    left, right = oss_v1_url(old), oss_v1_url(new)
    if left is None or right is None:
        return False
    old_location, a, old_expiry = left
    new_location, b, new_expiry = right
    return (old_location == new_location and a['OSSAccessKeyId'] == b['OSSAccessKeyId']
            and old_expiry > now + REFRESH_BEFORE_SECONDS
            and new_expiry >= old_expiry)


def read_previous(path, root):
    """Bound private cache reads. Missing/oversized caches simply cannot be reused."""
    path = Path(path)
    validate_tree_path(root, path)
    if not path.exists():
        return None
    if not stat.S_ISREG(path.stat().st_mode):
        raise ValueError('non-regular source cache')
    with path.open('rb') as stream:
        value = stream.read(MAX_CACHE_BYTES + 1)
    if len(value) > MAX_CACHE_BYTES:
        return None
    try:
        value = json.loads(value)
    except (ValueError, UnicodeError):
        return None
    return value if isinstance(value, dict) else None


def stabilize_attachment_urls(note, assets, previous, previous_manifest, now=None):
    """Reuse URLs only if the entire source and acquired asset bytes are unchanged.

    Any other source field, descriptor, order, query, signer or content hash change
    returns the complete fresh source response. Expiring links are refreshed in
    every output together. Persisted raw-cache is a retained source snapshot, not
    a promise that transient signing metadata is the newest API response.
    """
    unchanged = (note, assets, 0)
    if not isinstance(previous, dict) or not isinstance(previous_manifest, dict):
        return unchanged
    if json_text({k: v for k, v in previous_manifest.items() if k != 'assets'}) != json_text(attachment_manifest(previous)):
        return unchanged
    now = time.time() if now is None else now
    retained = copy.deepcopy(note)
    replacements = {}
    count = 0
    for field in FIELDS:
        left, right = previous.get(field), retained.get(field)
        if left == right:
            continue
        if not isinstance(left, list) or not isinstance(right, list) or len(left) != len(right):
            return unchanged
        for old, current in zip(left, right):
            if not isinstance(old, dict) or not isinstance(current, dict):
                return unchanged
            for key in URL_FIELDS:
                a, b = old.get(key), current.get(key)
                if a == b:
                    continue
                if not reusable_url(a, b, now):
                    return unchanged
                if b in replacements and replacements[b] != a:
                    return unchanged
                replacements[b] = a
                current[key] = a
                count += 1
    # This also validates exact note identity, title, timestamps, transcript,
    # every unknown field, attachment order and all non-signing metadata.
    if not count or json_text(retained) != json_text(previous):
        return unchanged
    retained_assets = copy.deepcopy(assets)
    if not isinstance(retained_assets, list):
        return unchanged
    for asset in retained_assets:
        if not isinstance(asset, dict):
            return unchanged
        url = asset.get('url')
        if isinstance(url, str) and url in replacements:
            asset['url'] = replacements[url]
    # Images/files have already been fetched using fresh URLs. An in-place
    # binary change therefore differs in SHA/size/path and cannot be suppressed.
    if json_text(retained_assets) != json_text(previous_manifest.get('assets')):
        return unchanged
    return retained, retained_assets, count
