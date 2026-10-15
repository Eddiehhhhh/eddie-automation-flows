#!/usr/bin/env python3
"""Explicit temporary two-pass mirror migration. Never writes a remote repo."""
import argparse
import json
from pathlib import Path
import tempfile
import time

from get_mirror_incremental_replay import replay,read_mirrors,validate_manifest
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

COUNTS=('enumerated','cached_skipped','fresh_details','adopted_after_detail','new_or_changed','written_or_changed',
        'pages','api_response_bytes','downloaded_assets','downloaded_bytes','asset_transfer_bytes')
FLAGS=('data_bytes_paths_mtimes_unchanged','second_data_unchanged','state_lastSyncedAt_changed',
       'source_content_complete_claimed','renderer_exercised')
ERRORS=('incomplete_list','invalid_list_identity_version','duplicate_mirror_identity','empty_mirror',
 'detail_identity','source_changed_during_pass','source_changed_between_passes','second_pass_semantic_state_changed',
 'data_not_stable','old_unselected_data_modified','unexpected_data_mutation','unchanged_mirror_modified','invalid_manifest','manifest_path_escape',
 'duplicate_manifest_path','manifest_missing_file','manifest_identity_conflict','manifest_version_conflict',
 'conflicting_identity_fields','malformed_frontmatter','unsupported_frontmatter_scalar','duplicate_frontmatter_field',
 'transcript_pending','unsupported_note_type','destination_collision','invalid_attachment','path_escape','readback',
 'note_limit','detail_limit','invalid_identity','invalid_detail','list_required','symlink')

def safe_report(value):
 value=value if isinstance(value,dict) else {}
 result={'mode':'mirror_incremental_replay','status':'failed','source_content_complete_claimed':False}

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
 def valid(c):
  required=('enumerated','cached_skipped','fresh_details','adopted_after_detail','new_or_changed','written_or_changed')
  return all(k in c for k in required) and c['enumerated']>0 and c['cached_skipped']+c['fresh_details']==c['enumerated'] and c['adopted_after_detail']+c['new_or_changed']==c['fresh_details'] and c['written_or_changed']==c['new_or_changed']
 if value.get('status')=='mirror_replay_ok' and 'error' not in result and valid(first) and valid(second) and first['enumerated']==second['enumerated'] and second['fresh_details']==0 and result.get('second_data_unchanged') is True:
  result['status']='mirror_replay_ok'
 return result

def main():
 p=argparse.ArgumentParser();p.add_argument('--publish-report',type=Path);args=p.parse_args()
 if args.publish_report:
  try:result=json.loads(args.publish_report.read_text())
  except (OSError,ValueError):result={}
  print(json.dumps(safe_report(result)));return 0
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
   with pack.deadline(110*60):result=replay(root,enumerate_source,detail,writer_factory=factory,progress=progress)
   stats[-1]['api_response_bytes']=source.budget.used
   for i,which in enumerate(('first','second')):
    result[which].update(stats[i]);store,transfer=stores[i]
    result[which].update(downloaded_assets=len(store.memo),downloaded_bytes=store.bytes,asset_transfer_bytes=transfer.used)
   result['status']='mirror_replay_ok'
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
 return 0 if public['status']=='mirror_replay_ok' else 1

if __name__=='__main__':raise SystemExit(main())
