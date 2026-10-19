"""Local-only private checkpoint validation; emit hashes/counts, never content.

The input path is supplied by the operator. This never writes the input or the
transitioned private state, performs no network I/O and creates no remote effect.
"""
from copy import deepcopy
from pathlib import Path
import hashlib
import json
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import get_notion_run32_recovery as r
import get_notion_outbox as o
import get_recurring_policy_recovery as policy


def check(path):
    raw=Path(path).read_bytes();state=json.loads(raw)
    o.check_state(state);sha=o.digest(state)
    if (sha!=hashlib.sha256(raw).hexdigest() or sha!=r.ANCHOR['state_sha256']
            or len(raw)!=r.CHECKPOINT['state_bytes']
            or hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()!=r.ANCHOR['state_blob']):
        raise ValueError('private_checkpoint_identity_mismatch')
    original=deepcopy(state);candidate=r.transition_outer(state,sha,policy.current_get_policy())
    kept=lambda value:{k:v for k,v in value.items() if k!='policy_sha256'}
    if (o.encoded(kept(state))!=o.encoded(kept(candidate)) or state!=original
            or candidate['engine_state']['journal'] is not None):
        raise ValueError('private_transition_changed_preserved_state')
    tampered=deepcopy(state);tampered['rotation_order']=list(reversed(tampered['rotation_order']))
    if tampered==state:tampered['policy_sha256']='f'*64
    try:r.transition_outer(tampered,o.digest(tampered),policy.current_get_policy())
    except o.Blocked:wrong_state_rejected=True
    else:raise ValueError('private_transition_accepted_changed_state')
    return {'actual_state_schema_valid':True,'input_bytes':len(raw),'input_sha256':sha,
        'git_blob_matches_public_anchor':True,'output_sha256':o.digest(candidate),
        'only_outer_policy_changed':True,'original_not_mutated':True,'journal':None,
        'receipts':len(state['engine_state']['receipts']),'pending':len(state['pending_by_id']),
        'unknown_holds':len(state['engine_state']['unresolved_by_id']),
        'unknown_holds_sha256':o.digest(state['engine_state']['unresolved_by_id']),
        'candidate_get_policy':policy.current_get_policy(),'wrong_state_rejected':wrong_state_rejected,
        'all_preserved_outer_fields_sha256':o.digest(kept(state)),
        'preserved_fields':{k:{'sha256':o.digest(v),'count':len(v) if isinstance(v,(list,dict)) else None}
                            for k,v in kept(state).items()},
        'local_only':True,'network_calls':0,'remote_state_mutations':0,'private_content_exported':False}


if __name__=='__main__':
    print(json.dumps(check(sys.argv[1]),sort_keys=True,indent=2))
