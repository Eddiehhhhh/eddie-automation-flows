"""Fixed public result projection. No paths, records, titles or caller strings."""
CONTRACT='get-private-runtime-report-2'
STATUSES=frozenset(('failed','prepared','queue_empty','queue_partial','executed','captured','applied','blocked',
                   'recurring_candidate_verified','recurring_transaction_verified','recurring_failed','notion_probe'))
PREPARE={'contract':CONTRACT,'status':'recurring_candidate_verified','main_updated':False,'cas_attempts':0,
         'local_proposal_only':True,'readback_verified':True,'business_health_asserted':False}

FAILURE_KINDS=frozenset(('missing_key','invalid_type','invalid_value','io_error','guard_or_runtime'))

def public(result,allowed_codes=(),*,allowed_stages=(),allowed_execution_reasons=()):
    if not isinstance(result,dict):result={}
    if result.get('status')=='recurring_candidate_verified':
        required={'main_updated':False,'cas_attempts':0,'local_proposal_only':True,'apply_ready':False,
                  'schedule_enabled':False,'asset_downloads':0,'notion_writes':0,'confirmed_content_ids':0,
                  'opaque_source_fields_rendered':False,'complete_source_rendering_claimed':False,
                  'readback_verified':True,'sink_status':'not_requested'}
        if all(type(result.get(k)) is type(v) and result[k]==v for k,v in required.items()):return dict(PREPARE)
    out={'contract':CONTRACT,'status':result.get('status') if isinstance(result.get('status'),str) and result.get('status') in STATUSES else 'blocked',
         'business_health_asserted':False,'complete_library_claimed':False}
    if result.get('execution_status') in ('verified','partial','blocked'):
        out['execution_status']=result['execution_status']
        reason=result.get('execution_reason')
        out['execution_reason']=reason if reason is None or isinstance(reason,str) and reason in allowed_execution_reasons else 'other'
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
    diagnostic_codes = frozenset(('rate_budget', 'rate_stop', 'response_byte_budget', 'invalid_source_version',
        'invalid_response_length', 'incomplete_response_body', 'transport_failed',
        'redirect_blocked', 'request_failed', 'api_error', 'http_error', 'other', 'unavailable'))
    code = result.get('source_diagnostic_code')
    if isinstance(code, str) and code in diagnostic_codes and out['status'] == 'recurring_failed':
        out['source_diagnostic_code'] = code
        if code in ('rate_budget', 'rate_stop'):
            reason = result.get('source_rate_reason')
            if reason in ('qps_global', 'qps_bucket', 'quota_day', 'quota_month', 'not_member', 'unknown'):
                out['source_rate_reason'] = reason
            for key in ('source_rate_budget', 'source_rate_http_429'):
                if type(result.get(key)) is bool:
                    out[key] = result[key]
        phase = result.get('source_phase')
        if phase in ('not_started', 'list', 'detail'):
            out['source_phase'] = phase
        for key in ('source_operation_active', 'source_counters_valid', 'source_deadline_reached'):
            if type(result.get(key)) is bool:
                out[key] = result[key]
        limits = {'source_api_requests': 2073, 'source_http_attempts': 10365,
                  'source_detail_requests': 1924, 'source_list_pages': 150,
                  'source_response_bytes': 130 * 1024 * 1024}
        counts = {key: result.get(key) for key in limits}
        valid = result.get('source_counters_valid') is True and all(type(v) is int and 0 <= v <= limits[k] for k, v in counts.items())
        valid = valid and counts['source_http_attempts'] <= 5 * counts['source_api_requests']
        if valid:
            out.update(counts)
        elif result.get('source_counters_valid') is True:
            out['source_counters_valid'] = False
        elapsed = result.get('source_elapsed_ms')
        if type(elapsed) is int and 0 <= elapsed <= 3600000:
            out['source_elapsed_ms'] = elapsed
            delay = result.get('source_first_operation_delay_ms')
            if type(delay) is int and 0 <= delay <= elapsed:
                out['source_first_operation_delay_ms'] = delay
    # Fixed stage names only. Parent and child values are inclusive, not additive.
    preparation_stages = frozenset(('policy', 'execution_provenance', 'remote_guard',
        'checkout', 'capture_evidence', 'parent_history', 'bootstrap_history',
        'bootstrap_inventory', 'current_inventory', 'receipt_provenance',
        'trusted_inputs', 'trusted_states', 'legacy_baselines', 'new_baselines',
        'preview_inputs', 'canonical_prepare', 'candidate_report'))
    timings = result.get('preparation_inclusive_timings')
    if out['status'] == 'recurring_failed' and type(timings) is dict and len(timings) <= len(preparation_stages):
        safe = {}
        for stage, value in timings.items():
            if (type(stage) is str and stage in preparation_stages and type(value) is dict
                    and set(value) == {'ms', 'calls'} and type(value['ms']) is int
                    and 0 <= value['ms'] <= 3600000 and type(value['calls']) is int
                    and 1 <= value['calls'] <= 1024):
                safe[stage] = {'ms': value['ms'], 'calls': value['calls']}
        if safe:
            out['preparation_inclusive_timings'] = safe
    return out
