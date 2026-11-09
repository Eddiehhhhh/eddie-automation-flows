"""Fixed public result projection. No paths, records, titles or caller strings."""
import re

CAS_GIT_WORDS=frozenset(('auth','branch','connection','curl','denied','disk','error','failed','fatal',
    'fetch','hook','http','index','large','lock','memory','missing','object','pack','permission',
    'protocol','push','quota','ref','reject','rejected','remote','rpc','shallow','sideband','signal',
    'size','space','stale','timeout','unpack','write'))

def git_word_hits(raw):
    if not isinstance(raw,bytes):return []
    words=set(re.findall(rb'[a-z]+',raw.lower()))
    return sorted(word for word in CAS_GIT_WORDS if word.encode('ascii') in words)
CONTRACT='get-private-runtime-report-2'
STATUSES=frozenset(('failed','prepared','queue_empty','queue_partial','executed','captured','applied','blocked',
                   'recurring_candidate_verified','recurring_transaction_verified','recurring_failed','notion_probe'))
PREPARE={'contract':CONTRACT,'status':'recurring_candidate_verified','main_updated':False,'cas_attempts':0,
         'local_proposal_only':True,'readback_verified':True,'business_health_asserted':False}

FAILURE_KINDS=frozenset(('missing_key','invalid_type','invalid_value','io_error','guard_or_runtime'))
HISTORY_CHECKS=frozenset(('merge_first_parent_tree_mismatch','pilot_start_disallowed',
    'pilot_migration_unconfigured','pilot_migration_malformed','pilot_tree_pair_unmatched',
    'pilot_tree_diverged','pilot_parent_mismatch','pilot_migration_unclosed'))
HISTORY_INTERVALS=frozenset(('previous_receipt_to_parent','trusted_anchor_to_parent'))

def public(result,allowed_codes=(),*,allowed_stages=(),allowed_execution_reasons=()):
    if not isinstance(result,dict):result={}
    if result.get('status')=='recurring_candidate_verified':
        required={'main_updated':False,'cas_attempts':0,'local_proposal_only':True,'apply_ready':False,
                  'schedule_enabled':False,'asset_downloads':0,'notion_writes':0,'confirmed_content_ids':0,
                  'opaque_source_fields_rendered':False,'complete_source_rendering_claimed':False,
                  'readback_verified':True,'sink_status':'not_requested'}
        if all(type(result.get(k)) is type(v) and result[k]==v for k,v in required.items()):
            scope=result.get('source_scope')
            complete=result.get('catalog_complete')
            if 'source_scope' not in result and 'catalog_complete' not in result:return dict(PREPARE)
            if type(complete) is bool and (scope,complete) in (('complete_catalog',True),('single_note',False)):
                return {**PREPARE,'source_scope':scope,'catalog_complete':complete}
            return {'contract':CONTRACT,'status':'blocked','business_health_asserted':False,
                    'complete_library_claimed':False}
    out={'contract':CONTRACT,'status':result.get('status') if isinstance(result.get('status'),str) and result.get('status') in STATUSES else 'blocked',
         'business_health_asserted':False,'complete_library_claimed':False}
    if out['status'] == 'recurring_transaction_verified' and result.get('stage_ref_cleanup') in ('removed', 'retained_or_unknown'):
        out['stage_ref_cleanup'] = result['stage_ref_cleanup']
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
    gitdata_diagnostic=result.get('gitdata_api_diagnostic')
    if (out['status']=='recurring_failed' and result.get('error')=='gitdata_staging_failed'
            and isinstance(gitdata_diagnostic,dict)):
        operation=gitdata_diagnostic.get('operation')
        failure=gitdata_diagnostic.get('failure')
        operations=('pending_ref','stage_refs','ancestry_compare','blob_create','tree_create',
                    'commit_create','stage_ref_create','stage_validation','stage_ref_precondition',
                    'candidate_commit_validation','blob_readback','tree_readback','commit_readback',
                    'stage_ref_readback','commit_object_readback','source_tree_readback','content_readback')
        failures=('unexpected_status','paginated','http_error','timeout','transport_error',
                  'response_too_large','invalid_json','invalid_shape','guard_rejected')
        if (operation in operations and failure in failures
                and set(gitdata_diagnostic) <= {'operation','failure','http_status'}):
            diagnostic={'operation':operation,'failure':failure}
            status=gitdata_diagnostic.get('http_status')
            if type(status) is int and 100<=status<=599:diagnostic['http_status']=status
            out['gitdata_api_diagnostic']=diagnostic
    history_check=result.get('history_check')
    if (out.get('error')=='entry_foreign_get_change' and out.get('stage')=='trusted_states'
            and history_check in HISTORY_CHECKS):
        out['history_check']=history_check
        history_interval=result.get('history_interval')
        if history_interval in HISTORY_INTERVALS:out['history_interval']=history_interval
    category=result.get('cas_error_category')
    cas_categories=('missing_object','shallow_history','authentication_or_permission','network','size_or_remote_rejection','ref_changed','resource_limit','other')
    if out['status']=='recurring_failed' and category in cas_categories:
        out['cas_error_category']=category
    diagnostic=result.get('cas_diagnostic')
    if out['status']=='recurring_failed' and isinstance(diagnostic,dict) and diagnostic.get('stage') in ('merge_base','push_start','push_exit','push_timeout','push_exception'):
        safe={'stage':diagnostic['stage']}
        code=diagnostic.get('exit_code')
        if type(code) is int and -255<=code<=255:safe['exit_code']=code
        kind=diagnostic.get('exception')
        if kind in ('timeout','spawn_error','other'):safe['exception']=kind
        for key in ('stdout_category','stderr_category'):
            value=diagnostic.get(key)
            if isinstance(value,str) and value in (*cas_categories,'none'):safe[key]=value
        for key in ('stdout_words','stderr_words'):
            value=diagnostic.get(key)
            if type(value) is list and len(value)<=len(CAS_GIT_WORDS) and all(isinstance(x,str) and x in CAS_GIT_WORDS for x in value) and value==sorted(set(value)):
                safe[key]=value
        out['cas_diagnostic']=safe
    metrics=result.get('cas_candidate_metrics')
    if out['status']=='recurring_failed' and type(metrics) is dict and set(metrics)=={'changed_paths','changed_bytes','transaction_bytes'}:
        if (type(metrics['changed_paths']) is int and 0<=metrics['changed_paths']<=1000000
                and all(type(metrics[k]) is int and 0<=metrics[k]<=64*1024*1024 for k in ('changed_bytes','transaction_bytes'))):
            out['cas_candidate_metrics']=dict(metrics)
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
        if phase in ('not_started', 'list', 'detail', 'single_detail'):
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
