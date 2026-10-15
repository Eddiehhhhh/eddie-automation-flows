#!/usr/bin/env python3
"""Explicit temporary two-pass mirror migration. Never writes a remote repo."""
import argparse
import json
from pathlib import Path
import tempfile
import time

from get_mirror_incremental_replay import replay,read_mirrors,validate_manifest,diagnose_pending,DIFF_KEYS
from get_mirror_source import MirrorSource
from get_mirror_render import CompatibleWriter

ASSET_ERROR_CODES = {
    'asset exceeds byte limit': 'byte_limit',
    'asset byte limit violated': 'byte_limit',
    'run byte limit reached': 'run_byte_limit',
    'asset count limit reached': 'count_limit',
    'asset transfer failed after bounded retries': 'transport_failed',
    'asset HTTP request rejected': 'http_rejected',
    'temporary asset HTTP failure': 'http_temporary',
    'incomplete asset body': 'body_incomplete',
    'asset URL must be credential-free HTTPS on port 443': 'destination_rejected',
    'asset destination must resolve only to public addresses': 'destination_rejected',
    'invalid attachment list': 'descriptor_invalid',
    'invalid attachment descriptor': 'descriptor_invalid',
    'attachment URL missing': 'descriptor_invalid',
    'invalid asset length': 'length_invalid',
    'audio/video body download is disabled': 'mime_rejected',
    'image MIME mismatch': 'mime_rejected',
    'redirect missing destination': 'redirect_invalid',
    'asset redirect limit exceeded': 'redirect_invalid',
    'asset path escapes root': 'path_invalid',
    'symlink in asset path': 'path_invalid',
    'symlink asset': 'path_invalid',
    'empty asset body': 'empty_body',
}

COUNTS=DIFF_KEYS+('pending_signing_refreshed','diagnostic_probed','diagnostic_rechecks','pending','list_time_epoch','list_time_aware','list_time_naive','list_time_missing','list_time_type','list_time_invalid','enumerated','cached_skipped','fresh_details','adopted_after_detail','new_or_changed','written_or_changed',
        'pages','api_response_bytes','downloaded_assets','downloaded_bytes','asset_transfer_bytes')
FLAGS=('diagnostic_truncated','diagnostic_only','pending_set_stable','data_bytes_paths_mtimes_unchanged','second_data_unchanged','state_lastSyncedAt_changed',
       'source_content_complete_claimed','renderer_exercised')
ERRORS=('pending_changed_between_passes','invalid_detail_time','list_version_missing','list_version_type','list_version_timezone_missing','list_version_invalid','incomplete_list','invalid_list_identity_version','duplicate_mirror_identity','empty_mirror',
 'detail_identity','source_changed_during_pass','source_changed_between_passes','second_pass_semantic_state_changed',
 'data_not_stable','old_unselected_data_modified','unexpected_data_mutation','unchanged_mirror_modified','invalid_manifest','manifest_path_escape',
 'duplicate_manifest_path','manifest_missing_file','manifest_identity_conflict','manifest_version_conflict',
 'conflicting_identity_fields','malformed_frontmatter','unsupported_frontmatter_scalar','duplicate_frontmatter_field',
 'transcript_pending','unsupported_note_type','destination_collision','invalid_attachment','path_escape','readback',
 'note_limit','detail_limit','invalid_identity','invalid_detail','list_required','symlink')

def safe_report(value):
 value=value if isinstance(value,dict) else {}
 result={'mode':'mirror_incremental_replay','status':'failed','source_content_complete_claimed':False,'pending_binary_verified':False}

 for key in FLAGS:
  if key!='source_content_complete_claimed' and type(value.get(key)) is bool:result[key]=value[key]
 for which in ('first','second'):
  item=value.get(which)
  if isinstance(item,dict):result[which]={k:item[k] for k in COUNTS if type(item.get(k)) is int and 0<=item[k]<=1024**4}
 error=value.get('error')
 if error in ERRORS+('api_rate_limited','asset_failed','source_failed','unexpected','limit','api_auth','api_transport','api_rejected','response_limit','response_invalid','api_redirect','identity'):result['error']=error
 if value.get('rate_limit_reason') in ('qps_global','qps_bucket','quota_day','quota_month','not_member','unknown'):result['rate_limit_reason']=value['rate_limit_reason']
 if type(value.get('http_status')) is int and 400<=value['http_status']<=599:result['http_status']=value['http_status']
 if type(value.get('budget')) is bool:result['budget']=value['budget']
 if value.get('stage') in ('seed','list','detail','write','compare'):result['stage']=value['stage']
 if value.get('asset_error_code') in tuple(sorted(set(ASSET_ERROR_CODES.values())))+('other',):result['asset_error_code']=value['asset_error_code']
 if value.get('failure_kind') in ('value_error','type_error','attribute_error','key_error','os_error','system_exit','other'):result['failure_kind']=value['failure_kind']
 first=result.get('first',{});second=result.get('second',{})
 if value.get('status') in ('pending_diagnostic_changed','pending_diagnostic_no_change','pending_diagnostic_no_candidates') and value.get('diagnostic_only') is True and 'error' not in result:
  if first.get('enumerated',0)>0 and first.get('diagnostic_probed',-1)<=first['enumerated'] and first.get('pending',-1)<=first.get('diagnostic_probed',-1) and all(first.get(k,0)==0 for k in ('written_or_changed','downloaded_assets','downloaded_bytes','asset_transfer_bytes')) and 0<=first.get('diagnostic_rechecks',-1)<=9 and 0<=first.get('diagnostic_probed',-1)<=64 and first.get('diagnostic_rechecks')==first.get('pending') and first.get('fresh_details')==first.get('diagnostic_probed') and all(k in first for k in DIFF_KEYS):
   changed=any(first[k] for k in DIFF_KEYS)
   result['status']='pending_diagnostic_changed' if changed else ('pending_diagnostic_no_change' if first['pending'] else 'pending_diagnostic_no_candidates')
   result['diagnostic_truncated']=first['diagnostic_probed']<first['enumerated']
 def valid(c):
  required=('enumerated','cached_skipped','fresh_details','adopted_after_detail','new_or_changed','written_or_changed')
  return all(k in c for k in required) and c['enumerated']>0 and c['cached_skipped']+c['fresh_details']==c['enumerated'] and c['adopted_after_detail']+c['new_or_changed']+c.get('pending',0)==c['fresh_details'] and c['written_or_changed']==c['new_or_changed']
 if value.get('status')=='mirror_replay_ok' and 'error' not in result and valid(first) and valid(second) and first['enumerated']==second['enumerated'] and second['fresh_details']==0 and result.get('second_data_unchanged') is True:
  if first.get('pending',0)==second.get('pending',0)==second.get('pending_signing_refreshed',0)==0:result['status']='mirror_replay_ok'
 if value.get('status')=='mirror_replay_partial' and 'error' not in result and valid(first) and valid(second) and first['enumerated']==second['enumerated'] and first.get('pending',0)>0 and first.get('pending')==second.get('pending') and second.get('pending_signing_refreshed',0)<=second['pending'] and second['fresh_details']==second['pending'] and second['new_or_changed']==0 and second['written_or_changed']==0 and second['adopted_after_detail']==0 and result.get('second_data_unchanged') is True and result.get('pending_set_stable') is True and all(second.get(k,0)==0 for k in ('downloaded_assets','downloaded_bytes','asset_transfer_bytes')):
  result['status']='mirror_replay_partial'
 return result

def safe_preview_report(value):
 result={'mode':'private_preview','status':'failed','published':False,'preview_only':True,'apply_ready':False,'source_content_complete_claimed':False,'pending_binary_verified':False}
 if not isinstance(value,dict):return result
 mapped=dict(value);mapped['status']={'private_preview_partial':'mirror_replay_partial','private_preview_ready_for_review':'mirror_replay_ok'}.get(value.get('status'),'failed')
 clean=safe_report(mapped)
 for key in ('first','second','error','rate_limit_reason','http_status','asset_error_code',*FLAGS):
  if key in clean:result[key]=clean[key]
 if value.get('error') in ('invalid_preview_source','invalid_preview_output','preview_head_changed','dirty_preview_source','untracked_preview_source','preview_deleted_path','preview_ignored_path','preview_source_changed','invalid_cloud_state','cloud_mirror_changed','conflicting_modes'):result['error']=value['error']
 changed=value.get('changed_paths')
 if type(changed) is int and 0<=changed<=25000:result['changed_paths']=changed
 if 'changed_paths' in result and value.get('published') is False and value.get('preview_only') is True:
  if clean['status']=='mirror_replay_partial':result['status']='private_preview_partial'
  elif clean['status']=='mirror_replay_ok':result['status']='private_preview_ready_for_review'
 return result

def private_preview_main(args):
 # An explicit CLI, no scheduler/publisher. Caller supplies a pinned private
 # checkout; the output directory must remain private and is not an artifact.
 from get_mirror_incremental_replay import prepare_private_preview
 import source_sync_assets as assets
 import source_sync_pack_seed as pack
 import sync_getnote_to_raw as sync
 from source_sync_rate import RequestPolicy,RateLimitFailure
 from source_sync_full_replay import TransferBudget,failure_details
 progress=[];stores=[];source=None
 try:
  if not (args.private_preview_vault and args.private_preview_output and args.expected_source_commit):raise ValueError('invalid_preview_source')
  key,client=sync.load_credentials();policy=RequestPolicy();source=MirrorSource(key,client,policy)
  def factory(root):
   transfer=TransferBudget(200*1024*1024)
   def fetch(url,max_bytes):
    if time.monotonic()>=policy.deadline:raise RateLimitFailure(budget=True)
    return assets.fetch_asset(url,max_bytes=max_bytes,transfer_budget=transfer)
   store=assets.AssetStore(root,fetch=fetch,max_run=200*1024*1024,max_assets=256);stores.append(store)
   return CompatibleWriter(root,store)
  with pack.deadline(110*60):
   result,counts=prepare_private_preview(args.private_preview_vault,args.private_preview_output,args.expected_source_commit,source.enumerate,source.detail,factory,progress)
  public={**result,'mode':'private_preview','status':'private_preview_partial' if counts['pending'] else 'private_preview_ready_for_review',**counts,'source_content_complete_claimed':False,'pending_binary_verified':False}
  public['first']={k:result['first'][k] for k in COUNTS if k in result['first']}
  public['second']={k:result['second'][k] for k in COUNTS if k in result['second']}
  public=safe_preview_report(public);print(json.dumps(public))
  return 0 if public['status']=='private_preview_ready_for_review' else (2 if public['status']=='private_preview_partial' else 1)
 except (Exception,SystemExit) as exc:
  errors=('invalid_preview_source','invalid_preview_output','preview_head_changed','dirty_preview_source','untracked_preview_source','preview_deleted_path','preview_ignored_path','preview_source_changed','invalid_cloud_state','cloud_mirror_changed')
  classified=failure_details('compare',exc)
  code=str(exc);public={'mode':'private_preview','status':'failed','published':False,'preview_only':True,'source_content_complete_claimed':False,'error':code if code in ERRORS+errors else classified.get('error_code','source_failed')}
  for key in ('rate_limit_reason','http_status'):
   if key in classified:public[key]=classified[key]
  if isinstance(exc,assets.AssetError):public['asset_error_code']=ASSET_ERROR_CODES.get(code,'other')
  for i,which in enumerate(('first','second')):
   if i<len(progress):public[which]={k:progress[i][k] for k in COUNTS if k in progress[i]}
  print(json.dumps(public));return 1

def main():
 p=argparse.ArgumentParser();p.add_argument('--publish-report',type=Path);p.add_argument('--pending-diagnostic',action='store_true');p.add_argument('--private-preview-vault',type=Path);p.add_argument('--private-preview-output',type=Path);p.add_argument('--expected-source-commit');args=p.parse_args()
 if args.private_preview_vault or args.private_preview_output or args.expected_source_commit:
  if args.publish_report or args.pending_diagnostic:
   print(json.dumps({'mode':'private_preview','status':'failed','error':'conflicting_modes','published':False,'preview_only':True}));return 1
  return private_preview_main(args)
 if args.publish_report:
  try:result=json.loads(args.publish_report.read_text())
  except (OSError,ValueError):result={}
  print(json.dumps(safe_preview_report(result) if isinstance(result,dict) and result.get('mode')=='private_preview' else safe_report(result)));return 0
 result={};progress=[];stats=[];stores=[];stage='seed';source=None
 try:
  import source_sync_assets as assets
  import source_sync_migration_check as migration
  import source_sync_pack_seed as pack
  import sync_getnote_to_raw as sync
  from source_sync_rate import RequestPolicy,RateLimitFailure
  from source_sync_full_replay import TransferBudget
  token,commit,blobs,manifest,mapping,meta=migration.private_snapshot()
  with tempfile.TemporaryDirectory(prefix='private-get-mirror-') as folder:
   work=Path(folder);root=work/'vault'/'Raw'/'03 Get';root.mkdir(parents=True,mode=0o700)
   seed_report={};pack.seed(root,work,token,blobs,seed_report);pack.audit_actual(root,mapping,seed_report)
   if not validate_manifest(root,read_mirrors(root)):raise ValueError('invalid_manifest')
   key,client=sync.load_credentials();policy=RequestPolicy()
   source=MirrorSource(key,client,policy)
   def enumerate_source():
    nonlocal stage
    stage='list'
    if stats:stats[-1]['api_response_bytes']=source.budget.used
    stats.append({})
    try:return source.enumerate()
    finally:stats[-1].update(pages=source.pages,api_response_bytes=source.budget.used if source.budget else 0)
   class ObservedWriter(CompatibleWriter):
    def write(self,*args):
     nonlocal stage
     stage='write';return super().write(*args)
   def factory(stage_root):
    transfer=TransferBudget(200*1024*1024)
    def fetch(url,max_bytes):
     if time.monotonic()>=policy.deadline:raise RateLimitFailure(budget=True)
     return assets.fetch_asset(url,max_bytes=max_bytes,transfer_budget=transfer)
    store=assets.AssetStore(stage_root,fetch=fetch,max_run=200*1024*1024,max_assets=256)
    stores.append((store,transfer))
    return ObservedWriter(stage_root,store)
   def detail(ident):
    nonlocal stage
    stage='detail'
    value=source.detail(ident);stats[-1]['api_response_bytes']=source.budget.used;return value
   with pack.deadline(110*60):
    result=diagnose_pending(root,enumerate_source,detail,progress=progress) if args.pending_diagnostic else replay(root,enumerate_source,detail,writer_factory=factory,progress=progress)
   stats[-1]['api_response_bytes']=source.budget.used
   for i,which in enumerate(('first',) if args.pending_diagnostic else ('first','second')):
    result[which].update(stats[i])
    if args.pending_diagnostic:continue
    store,transfer=stores[i]
    result[which].update(downloaded_assets=len(store.memo),downloaded_bytes=store.bytes,asset_transfer_bytes=transfer.used)
   if not args.pending_diagnostic:result['status']='mirror_replay_partial' if result['first'].get('pending',0) else 'mirror_replay_ok'
 except (Exception,SystemExit) as exc:
  # Preserve only already validated public progress and fixed classifications.
  from source_sync_full_replay import failure_details
  classified=failure_details(stage,exc)
  code=str(exc)
  if isinstance(exc,assets.AssetError):classified['asset_error_code']=ASSET_ERROR_CODES.get(code,'other')
  result={'status':'failed','stage':stage,'error':code if code in ERRORS else classified.get('error_code','source_failed')}
  for key in ('rate_limit_reason','http_status','asset_error_code','failure_kind'):
   if key in classified:result[key]=classified[key]
  if type(exc).__name__=='RateLimitFailure':result['budget']=bool(getattr(exc,'budget',False))
  for i,which in enumerate(('first','second')):
   if i<len(progress):result[which]=dict(progress[i])
   if i<len(stats):result.setdefault(which,{}).update(stats[i])
   if i<len(stores):
    store,transfer=stores[i]
    result.setdefault(which,{}).update(downloaded_assets=len(store.memo),downloaded_bytes=store.bytes,asset_transfer_bytes=transfer.used)
  if source is not None and stats and source.budget is not None:
   result.setdefault('first' if len(stats)==1 else 'second',{})['api_response_bytes']=source.budget.used
 public=safe_report(result);print(json.dumps(public))
 return 0 if public['status']=='mirror_replay_ok' else (2 if public['status'] in ('mirror_replay_partial','pending_diagnostic_no_change','pending_diagnostic_no_candidates') else 1)

if __name__=='__main__':raise SystemExit(main())
