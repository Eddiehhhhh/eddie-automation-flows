"""Fixed reviewed Markdown scope; no transmission, classifier or credentials.

Hashes identify the exact reviewed records without publishing note identities.
A review of one recording version is not permission for changed private speech.
Audio assets and opaque registry-only text are never transmitted by this gate.
"""
import get_private_runtime_bindings as _private_bindings
import json
import get_notion_sink_plan as p

REVIEWED = _private_bindings.value('get_notion_content_scope', 'REVIEWED')
# Exact versions reviewed for the existing destination; credential-bearing
# body spans are separately projected before rendering, never passed through.
NEWLY_REVIEWED = _private_bindings.value('get_notion_content_scope', 'NEWLY_REVIEWED')
REVIEWED.update(NEWLY_REVIEWED)
HELD = _private_bindings.value('get_notion_content_scope', 'HELD')
EMPTY_SHA256 = _private_bindings.value('get_notion_content_scope', 'EMPTY_SHA256')
RECORDINGS = frozenset(('audio','recorder_audio','recorder_flash_audio','recording'))


def reason(ident,data):
    key=p.digest(ident)
    if key in HELD:
        reason=HELD[key]
        return 'content_review_required' if reason=='empty_source' and p.digest(data)!=EMPTY_SHA256 else reason
    if key in REVIEWED:return None if p.digest(data)==REVIEWED[key] else 'content_review_required'
    try:
        header=data.decode('utf-8').split('\n---\n',1)[0]
        fields=[x.partition(':')[2].strip() for x in header.splitlines() if x.startswith('note_type:')]
        if len(fields)>1:return 'content_review_required'
        if fields:
            raw=fields[0]
            kind=json.loads(raw) if raw.startswith('"') else raw
            if kind in RECORDINGS:return 'content_review_required'
    except Exception:return 'content_review_required'
    return None


def retry_reviewed_failure(ident, source, failed):
    """Only these exact newly reviewed versions may retry their old review gate.

    Existing privacy/empty/ownership/unknown-operation failures are unaffected.
    Source identity and full raw bytes are still checked during preparation.
    """
    return bool(isinstance(source, dict) and isinstance(failed, dict)
        and failed.get('reason') == 'content_review_required'
        and source.get('blob') and failed.get('md_blob') == source['blob']
        and NEWLY_REVIEWED.get(p.digest(ident)) == source.get('sha256')
        and p.digest(ident) in NEWLY_REVIEWED)
