"""Fixed public result projection. No paths, records, titles or caller strings."""
CONTRACT='get-private-runtime-report-2'
STATUSES=frozenset(('failed','prepared','queue_empty','queue_partial','executed','captured','applied','blocked',
                   'recurring_candidate_verified','recurring_transaction_verified','recurring_failed','notion_probe'))
PREPARE={'contract':CONTRACT,'status':'recurring_candidate_verified','main_updated':False,'cas_attempts':0,
         'local_proposal_only':True,'readback_verified':True,'business_health_asserted':False}

FAILURE_KINDS=frozenset(('missing_key','invalid_type','invalid_value','io_error','guard_or_runtime'))

def public(result,allowed_codes=(),*,allowed_stages=()):
    if not isinstance(result,dict):result={}
    if result.get('status')=='recurring_candidate_verified':
        required={'main_updated':False,'cas_attempts':0,'local_proposal_only':True,'apply_ready':False,
                  'schedule_enabled':False,'asset_downloads':0,'notion_writes':0,'confirmed_content_ids':0,
                  'opaque_source_fields_rendered':False,'complete_source_rendering_claimed':False,
                  'readback_verified':True,'sink_status':'not_requested'}
        if all(type(result.get(k)) is type(v) and result[k]==v for k,v in required.items()):return dict(PREPARE)
    out={'contract':CONTRACT,'status':result.get('status') if isinstance(result.get('status'),str) and result.get('status') in STATUSES else 'blocked',
         'business_health_asserted':False,'complete_library_claimed':False}
    for key in ('write_state','journal_write_state'):
        if result.get(key) in ('not_attempted','confirmed','unknown','uncertain'):out[key]=result[key]
    for key in ('reason','error'):
        if isinstance(result.get(key),str) and result[key] in allowed_codes:out[key]=result[key]
    stage=result.get('stage')
    if isinstance(stage,str) and stage in allowed_stages:out['stage']=stage
    kind=result.get('failure_kind')
    if isinstance(kind,str) and kind in FAILURE_KINDS:out['failure_kind']=kind
    for key in ('external_write_attempts','writes','asset_downloads','notion_writes','confirmed_content_ids','new_selected','notes_changed'):
        value=result.get(key)
        if type(value) is int and 0<=value<=1000000 or key in result and value is None:out[key]=value
    values=result.get('http_statuses')
    if isinstance(values,list) and len(values)<=20 and all(type(v) is int and 100<=v<=599 for v in values):out['http_statuses']=values
    return out
