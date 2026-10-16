#!/usr/bin/env python3
"""Explicit temporary two-pass mirror migration. Never writes a remote repo."""
import argparse
import json
from pathlib import Path
import tempfile
import time

from get_mirror_incremental_replay import replay,read_mirrors,validate_manifest,diagnose_pending,DIFF_KEYS
from get_mirror_source import MirrorSource
from get_mirror_render import CompatibleWriter,FaithfulWriter

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

COUNTS=DIFF_KEYS+('legacy_conflicts','source_updated','source_update_conflicts','source_missing','pending_signing_refreshed','diagnostic_probed','diagnostic_rechecks','pending','list_time_epoch','list_time_aware','list_time_naive','list_time_missing','list_time_type','list_time_invalid','enumerated','cached_skipped','fresh_details','adopted_after_detail','new_or_changed','written_or_changed',
        'pages','api_response_bytes','downloaded_assets','downloaded_bytes','asset_transfer_bytes')
FLAGS=('diagnostic_truncated','diagnostic_only','pending_set_stable','data_bytes_paths_mtimes_unchanged','second_data_unchanged','state_lastSyncedAt_changed',
       'source_content_complete_claimed','renderer_exercised')
ERRORS=('pending_changed_between_passes','invalid_detail_time','list_version_missing','list_version_type','list_version_timezone_missing','list_version_invalid','incomplete_list','invalid_list_identity_version','duplicate_mirror_identity','empty_mirror',
 'detail_identity','source_changed_during_pass','source_changed_between_passes','second_pass_semantic_state_changed',
 'data_not_stable','old_unselected_data_modified','unexpected_data_mutation','unchanged_mirror_modified','invalid_manifest','manifest_path_escape',
 'duplicate_manifest_path','manifest_missing_file','manifest_identity_conflict','manifest_version_conflict',
 'conflicting_identity_fields','malformed_frontmatter','unsupported_frontmatter_scalar','duplicate_frontmatter_field',
 'transcript_pending','unsupported_note_type','destination_collision','invalid_attachment','path_escape','readback',
 'invalid_source_text','invalid_raw_version','untrusted_legacy_state','legacy_reconciliation_required','note_limit','detail_limit','invalid_identity','invalid_detail','list_required','symlink')

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
 if value.get('local_proposal_only') is True:
  clean['status']='failed'
  result['local_proposal_only']=True
  conflicts=value.get('conflicts')
  if type(conflicts) is int and 0<=conflicts<=2500:result['conflicts']=conflicts
  def valid_fidelity(counts):
   required=('enumerated','cached_skipped','fresh_details','legacy_conflicts','source_updated','source_update_conflicts','written_or_changed','pending','source_missing')
   return all(type(counts.get(k)) is int and 0<=counts[k]<=2500 for k in required) and counts['enumerated']>0 and counts['cached_skipped']+counts['fresh_details']+counts['legacy_conflicts']==counts['enumerated'] and counts['source_update_conflicts']+counts['written_or_changed']+counts['pending']==counts['fresh_details'] and counts['source_update_conflicts']<=counts['source_updated']
  a=clean.get('first',{});b=clean.get('second',{})
  if valid_fidelity(a) and valid_fidelity(b) and a['enumerated']==b['enumerated'] and b['written_or_changed']==0 and value.get('second_data_unchanged') is True and value.get('pending_set_stable') is True and 'error' not in clean and 'conflicts' in result:
   partial=bool(conflicts or a['pending'] or a['source_missing'])
   expected='private_preview_partial' if partial else 'private_preview_ready_for_review'
   if value.get('status')==expected:clean['status']='mirror_replay_partial' if partial else 'mirror_replay_ok'
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
   return FaithfulWriter(root,store)
  with pack.deadline(110*60):
   result,counts=prepare_private_preview(args.private_preview_vault,args.private_preview_output,args.expected_source_commit,source.enumerate,source.detail,factory,progress,faithful=True)
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

# This read-only probe deliberately does not persist source bodies or adopt state.
# Commit pins identify previously retained private evidence, never public note IDs.
LEGACY_SOURCE = '4fbc69f2d41bc3c7da81eae8f0c91584f0923190'
LEGACY_OLD_PREVIEW = 'dab0abf680075e3f073b225872be010c3ae99561'
LEGACY_NEW_PREVIEW = '013328cc962d9921befb3e25710ba699dd7e46a9'
LEGACY_COUNTS = ('selected','fresh_details','original_nonempty','original_missing_or_empty',
 'legacy_transcript_nonempty','original_equals_legacy_transcript','legacy_transcript_differs',
 'whole_body_equals_original','whole_body_equals_detail_text_render','render_unavailable',
 'source_version_same_as_retained','source_version_changed','attachments_present')

def legacy_cohort(old,new):
 import re
 from get_mirror_incremental_replay import _raw_valid
 if not isinstance(old,dict) or old.get('contract')!='get-v7-cloud-mirror-1' or not isinstance(old.get('pendingById'),dict):raise ValueError('legacy_probe_input')
 if not isinstance(new,dict) or new.get('contract')!='get-source-raw-local-2' or new.get('local_proposal_only') is not True or not isinstance(new.get('conflictsById'),dict) or not isinstance(new.get('pendingById'),dict):raise ValueError('legacy_probe_input')
 prior=set(old['pendingById']);conflicts=new['conflictsById'];selected=sorted(prior & set(conflicts))
 if len(prior)!=9 or len(selected)!=8 or prior-set(selected)!=set(new['pendingById']):raise ValueError('legacy_probe_cohort')
 paths=set()
 for ident in selected:
  item=conflicts[ident]
  if not re.fullmatch(r'[0-9]{1,64}',ident) or not isinstance(item,dict) or item.get('reason')!='legacy_baseline_missing' or item.get('sourceContentHash') is not None or not _raw_valid(item.get('sourceVersion')):raise ValueError('legacy_probe_cohort')
  path=item.get('filePath')
  if not isinstance(path,str) or not path.endswith('.md') or path in paths or path.startswith('/') or '..' in Path(path).parts or '\\' in path:raise ValueError('legacy_probe_cohort')
  paths.add(path)
 return selected

def legacy_document(data):
 # Parse only simple existing scalar fields. Preserve body bytes exactly; no
 # strip, newline conversion, YAML timestamp coercion or substring coverage.
 text=data.decode('utf-8')
 if not text.startswith('---\n') or '\n---\n' not in text:raise ValueError('legacy_probe_document')
 header,body=text[4:].split('\n---\n',1);fields={}
 for line in header.split('\n'):
  if not line or line[0].isspace() or line.startswith('#'):raise ValueError('legacy_probe_document')
  key,sep,value=line.partition(':')
  if not sep or key in fields:raise ValueError('legacy_probe_document')
  value=value.strip()
  if key not in ('note_id','note_type','audio_transcript'):continue
  if value.startswith('"'):
   try:value=json.loads(value)
   except ValueError:raise ValueError('legacy_probe_document')
  elif value.startswith("'"):
   if not value.endswith("'"):raise ValueError('legacy_probe_document')
   value=value[1:-1].replace("''", "'")
  elif value in ('|','>') or value.startswith(('&','*','!','[','{')):raise ValueError('legacy_probe_document')
  if not isinstance(value,str):raise ValueError('legacy_probe_document')
  fields[key]=value
 return fields,body

def diagnose_legacy_eight(old,new,read_mirror,fetch_detail):
 from get_mirror_incremental_replay import raw_source_version,source_text_fields
 from get_v7_compat import note_identity
 from get_mirror_render import validate_note_shape,render
 selected=legacy_cohort(old,new);counts=dict.fromkeys(LEGACY_COUNTS,0);counts['selected']=len(selected)
 for ident in selected:
  entry=new['conflictsById'][ident];data=read_mirror(entry['filePath']);fields,body=legacy_document(data)
  if fields.get('note_id')!=ident:raise ValueError('legacy_probe_identity')
  detail=fetch_detail(ident);counts['fresh_details']+=1
  if not isinstance(detail,dict) or note_identity(detail)!=ident:raise ValueError('legacy_probe_identity')
  validate_note_shape(detail);source_text_fields(detail)
  token=raw_source_version(detail.get('updated_at'))
  counts['source_version_same_as_retained' if token==entry['sourceVersion'] else 'source_version_changed']+=1
  original=(detail.get('audio') or {}).get('original')
  if original is None:original=''
  if not isinstance(original,str):raise ValueError('legacy_probe_detail')
  has_original=bool(original.strip());counts['original_nonempty' if has_original else 'original_missing_or_empty']+=1
  prior=fields.get('audio_transcript','')
  if prior:
   counts['legacy_transcript_nonempty']+=1
   counts['original_equals_legacy_transcript' if has_original and original==prior else 'legacy_transcript_differs']+=1
  if has_original and body==original:counts['whole_body_equals_original']+=1
  counts['attachments_present']+=bool(detail.get('attachments'))
  try:
   rendered,_=render(detail,[],Path(entry['filePath']),faithful=True)
  except ValueError as error:
   if str(error)!='transcript_pending':raise
   counts['render_unavailable']+=1
  else:
   _,rendered_body=legacy_document(rendered.encode())
   if body==rendered_body:counts['whole_body_equals_detail_text_render']+=1
 return {'mode':'legacy_eight_readonly','status':'legacy_eight_diagnostic_complete','counts':counts,
  'read_only':True,'raw_payload_retained':False,'baseline_adopted':0,'source_content_complete_claimed':False}

def safe_legacy_eight_report(value):
 result={'mode':'legacy_eight_readonly','status':'failed','read_only':True,
  'raw_payload_retained':False,'baseline_adopted':0,'source_content_complete_claimed':False}
 if not isinstance(value,dict):return result
 c=value.get('counts')
 valid=isinstance(c,dict) and set(c)==set(LEGACY_COUNTS) and all(type(c[k]) is int and 0<=c[k]<=8 for k in LEGACY_COUNTS)
 if valid and c['selected']==c['fresh_details']==8 and c['original_nonempty']+c['original_missing_or_empty']==8 and c['source_version_same_as_retained']+c['source_version_changed']==8 and c['original_equals_legacy_transcript']+c['legacy_transcript_differs']==c['legacy_transcript_nonempty'] and c['whole_body_equals_original']<=c['original_nonempty'] and c['whole_body_equals_detail_text_render']+c['render_unavailable']<=8:
  if value.get('status')=='legacy_eight_diagnostic_complete' and value.get('read_only') is True and value.get('raw_payload_retained') is False and type(value.get('baseline_adopted')) is int and value.get('baseline_adopted')==0 and value.get('source_content_complete_claimed') is False and value.get('refs_unchanged') is True:
   result.update(status='legacy_eight_diagnostic_complete',counts=dict(c),refs_unchanged=True)
 if result['status']=='failed':result['error']='legacy_probe_failed'
 return result

def legacy_eight_main():
 import os
 from get_private_preview_retention import (GitHubRemote,review_source,GET,BACKUP_REF,PREVIEW_REF,FIDELITY_PREVIEW_REF,MAIN_REF,strict_json)
 from source_sync_inventory import ResponseBudget
 from source_sync_rate import RequestPolicy
 import sync_getnote_to_raw as sync
 try:
  remote=GitHubRemote(None,os.environ.get('PRIVATE_VAULT_TOKEN'))
  if remote.metadata().get('private') is not True:raise ValueError('legacy_probe_target')
  def refs():return {ref:remote.ref(ref) for ref in (MAIN_REF,BACKUP_REF,PREVIEW_REF,FIDELITY_PREVIEW_REF)}
  before=refs()
  if before[BACKUP_REF]!=LEGACY_SOURCE or before[PREVIEW_REF]!=LEGACY_OLD_PREVIEW or before[FIDELITY_PREVIEW_REF]!=LEGACY_NEW_PREVIEW:raise ValueError('legacy_probe_refs')
  review_source(remote,LEGACY_SOURCE)
  trees={commit:remote.check_source_budget(commit) for commit in (LEGACY_SOURCE,LEGACY_OLD_PREVIEW,LEGACY_NEW_PREVIEW)}
  def read(commit,path):
   mode,kind,sha,size=trees[commit][GET+'/'+path]
   if mode!='100644' or kind!='blob' or size>2*1024*1024:raise ValueError('legacy_probe_budget')
   return remote.blob(sha,size)
  old=strict_json(read(LEGACY_OLD_PREVIEW,'03 State/cloud-sync-state.json'));new=strict_json(read(LEGACY_NEW_PREVIEW,'03 State/cloud-sync-state.json'))
  selected=legacy_cohort(old,new)
  for ident in selected:
   path=GET+'/'+new['conflictsById'][ident]['filePath']
   if trees[LEGACY_SOURCE].get(path)!=trees[LEGACY_NEW_PREVIEW].get(path):raise ValueError('legacy_probe_mirror_changed')
  key,client=sync.load_credentials();policy=RequestPolicy(seconds=300);budget=ResponseBudget(16*1024*1024)
  def detail(ident):
   value=sync.api_get(sync.DETAIL_PATH,{'id':ident,'image_quality':'original'},key,client,
    max_response_bytes=2*1024*1024,response_budget=budget,request_policy=policy)
   if not isinstance(value,dict):raise ValueError('legacy_probe_detail')
   value=value.get('data',value)
   if not isinstance(value,dict):raise ValueError('legacy_probe_detail')
   return value.get('note',value)
  result=diagnose_legacy_eight(old,new,lambda path:read(LEGACY_SOURCE,path),detail)
  review_source(remote,LEGACY_SOURCE)
  if refs()!=before:raise ValueError('legacy_probe_refs')
  result['refs_unchanged']=True
 except (Exception,SystemExit):result={}
 public=safe_legacy_eight_report(result);print(json.dumps(public));return 0 if public['status']=='legacy_eight_diagnostic_complete' else 1


def main():
 p=argparse.ArgumentParser();p.add_argument('--publish-report',type=Path);p.add_argument('--pending-diagnostic',action='store_true');p.add_argument('--private-preview-vault',type=Path);p.add_argument('--private-preview-output',type=Path);p.add_argument('--expected-source-commit');args=p.parse_args()
 if args.private_preview_vault or args.private_preview_output or args.expected_source_commit:
  if args.publish_report or args.pending_diagnostic:
   print(json.dumps({'mode':'private_preview','status':'failed','error':'conflicting_modes','published':False,'preview_only':True}));return 1
  return private_preview_main(args)
 if args.publish_report:
  try:result=json.loads(args.publish_report.read_text())
  except (OSError,ValueError):result={}
  print(json.dumps(safe_legacy_eight_report(result) if isinstance(result,dict) and result.get('mode')=='legacy_eight_readonly' else (safe_preview_report(result) if isinstance(result,dict) and result.get('mode')=='private_preview' else safe_report(result))));return 0
 if args.pending_diagnostic:return legacy_eight_main()
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
