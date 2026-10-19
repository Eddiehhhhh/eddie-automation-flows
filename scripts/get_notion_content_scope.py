"""Fixed reviewed Markdown scope; no transmission, classifier or credentials.

Hashes identify the exact reviewed records without publishing note identities.
A review of one recording version is not permission for changed private speech.
Audio assets and opaque registry-only text are never transmitted by this gate.
"""
import json
import get_notion_sink_plan as p

REVIEWED = {'d7de22f28801dd7d506f338525ec634b18468173462564552c252d391553cfc9': '128d84c953d6b6e4c2240cc01db9d850eb1cda5ecd7b4c85251a223cdfaa7df9', '26662b200631a2942386a88380c113f4473d46034dda085c8b1f97e5a6b792b4': 'cf987551267546994e6aa21c50dbfe192651c60aff0fd4f4280d8fb0348c7c3f', 'c94333d6d358f1b6955cc0be47556c7d19f2316fb83767d60e3d5c84b21a6788': 'ac5dcc72aa989a63a7c10bdcf7fadf1f1b0136dbadadcbacba503dbe99533d80', '1dfb18a82c9131c0d5a396b479722301c7b146861a646b1418cdd40e44c232b1': '351ba62fb31a8b57bdcbd66497682c11e8275ff45a9895753ea0fa891ac1189d', 'a3867c93b71c441112cbf8040801103f5641721f5a2c81f40d8ed69ded30b5dd': 'b7f5c7ce71d62928146175338f2da7abab256d94a4914c801845d68d22cb6019', '1d020802de0351f0aed76c36519559b0dcde5c914892dc778d037e39e9a811fd': '6822e61842a7b43dc59b474d66677a2d5869dbdcdc7753f707915b02275bf861'}
# Exact versions reviewed for the existing destination; credential-bearing
# body spans are separately projected before rendering, never passed through.
NEWLY_REVIEWED = {'54ac79236d8986640d3444d956de24c7a58570459d688aaf5727f5ffec69d3a7': 'c2dc7f7ea6cee459fd1ccdbf7e359252ace45b5c211f315c6d625f6987fced84', 'a5f7c5452508fd67ac64c5bed5159a401d8d92d2c4f612b1900855c04624b0e6': '4903796f8fb086ea752eebcc33ca4e6dcd37201548bd59d2ac49ad798d752a17', 'af2797ebad9902bb27aba6e892167c37ed13f9c2a74e02359ea6d92119df8bb9': 'f021ae16a264b1e609ec77506e4bcf8e1ef50fbf0cf67d5b27df1a7a59db0895'}
REVIEWED.update(NEWLY_REVIEWED)
HELD = {'cbd721afcf6bd2749c54f7e52b46efef88c20ac802eb5823e08a592b1668b6b0': 'empty_source', '4354606a64f0a1cf6cabce8b78532ef0ac6bb0e249a32a75f91b91110df64fa7': 'privacy_scope_required', 'b4faf23305a45b952fa5a5ab1d214add63a4ff0c2f00693d2c25f38f95b21595': 'privacy_scope_required', 'f4130cce1a112f7db0c25c9535f0358d4db34c86cb081f9ae7543690ca713981': 'privacy_scope_required', 'd3eb7bf2d00b6594454d06f6a4923dcf67ef862a2c86cadaf5e031f76c435cb2': 'prior_denial'}
EMPTY_SHA256 = '09f048a709dad7bfeee7cf31d534aa7e4ec42bf52d94eaec9f909c46ca3ca75e'
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
