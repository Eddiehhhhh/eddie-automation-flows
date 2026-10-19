"""Exact reviewed text-only exclusions; no source mutation or credential data.

Only the explicitly reviewed immutable version is projected. Coordinates are
Unicode code points in the already validated Markdown body. Source evidence,
asset bindings and receipts continue to identify the untouched original bytes.
"""
import hashlib

NOTE_SHA256 = '71792a29a0805cdf36ddce70f401faeb69401f13292412527559a004d58a82f3'
SOURCE_SHA256 = '4903796f8fb086ea752eebcc33ca4e6dcd37201548bd59d2ac49ad798d752a17'
BODY_SHA256 = '0ce9517f194f15200e3b2a8d55d812d77bb482661a172dda5528ff373dc42c34'
PROJECTED_BODY_SHA256 = 'c04c6ebb1cd1fccf4b2ce300a4f0f50a8c8d957c870560c9fb4e19127803db28'
SPANS = ((50346, 50358), (51904, 51907), (51910, 51916), (51942, 51948))
REPLACEMENT = '〔验证码已隐去〕'


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def render_body(data, ident, body):
    """Return an exact credential-excluding rendering, or fail closed.

    No general numeric classifier is used: prices, dates, timestamps and all
    other content remain byte-for-byte identical outside the pinned spans.
    Frontmatter and original audio are never exported by this function.
    """
    if not isinstance(ident, str):
        raise ValueError('content_review_required')
    if _sha(ident.encode()) != NOTE_SHA256:
        return body
    if (not isinstance(data, bytes) or not isinstance(body, str)
            or _sha(data) != SOURCE_SHA256
            or _sha(body.encode()) != BODY_SHA256):
        raise ValueError('content_review_required')
    previous = 0
    for start, end in SPANS:
        if not previous <= start < end <= len(body) or not body[start:end].isascii() or not body[start:end].isdigit():
            raise ValueError('content_review_required')
        previous = end
    projected = body
    for start, end in reversed(SPANS):
        projected = projected[:start] + REPLACEMENT + projected[end:]
    if _sha(projected.encode()) != PROJECTED_BODY_SHA256:
        raise ValueError('content_review_required')
    return projected
