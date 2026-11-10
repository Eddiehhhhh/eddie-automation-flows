import hashlib
from pathlib import Path
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import get_canonical_assets as a

class CanonicalAssets(unittest.TestCase):
 def test_suffix_preserves_formal_path_original_and_hash(self):
  raw=b'original source body\n';binary=b'image';path='_assets/images/'+hashlib.sha256(binary).hexdigest()+'.png'
  result=a.append('99 录音卡笔记/old.md',raw,{path:binary})
  self.assertTrue(result.startswith(raw));self.assertEqual(a.references({'99 录音卡笔记/old.md':result}),{path})
  a.verify(path,binary)
 def test_forged_hash_escape_and_double_append_rejected(self):
  with self.assertRaises(ValueError):a.verify('_assets/images/'+'0'*64+'.png',b'x')
  with self.assertRaises(ValueError):a.references({'old.md':(a.MARKER+'\n[x](../../secret)').encode()})
  with self.assertRaises(ValueError):a.append('old.md',a.MARKER.encode(),{})

 def test_source_wrapper_only_downloads_disposable_assets(self):
  import tempfile
  from pathlib import Path
  with tempfile.TemporaryDirectory() as d:
   class Source:
    def enumerate(self):return ['unchanged-protocol']
   source=a.SourceWithAssets(Source(),d,fetch=lambda url,max_bytes:(b'image','image/png'))
   note={'content':'![x](https://example.com/a.png)'}
   self.assertEqual(source.enumerate(),['unchanged-protocol'])
   files=source.attachment_files(note,'03 纯文本记录/old.md')
   self.assertEqual(len(files),1)
   for path,data in files.items():a.verify(path,data)
   self.assertEqual(note,{'content':'![x](https://example.com/a.png)'})

 def test_download_failure_does_not_modify_source(self):
  import tempfile
  from source_sync_assets import AssetError
  note={'content':'![x](https://example.com/a.png)'}
  def failure(url,max_bytes):raise AssetError('network failed')
  with tempfile.TemporaryDirectory() as d:
   source=a.SourceWithAssets(object(),d,fetch=failure)
   with self.assertRaises(AssetError):source.attachment_files(note,'03 纯文本记录/old.md')
   self.assertEqual(note,{'content':'![x](https://example.com/a.png)'})
 def test_failed_and_held_downloads_never_advance_candidate(self):
  from copy import deepcopy
  from unittest.mock import Mock
  import tempfile
  registry={'newById':{'1':{'filePath':'03 纯文本记录/a.md','mirror_sha256':'old','baseline_blob':'old'}}}
  changes={'03 纯文本记录/a.md':b'original'}
  before=deepcopy(registry)
  with tempfile.TemporaryDirectory() as d:
   source=a.SourceWithAssets(object(),d)
   for status,kind in [('failed','image'),('held','file'),('failed','audio'),('held','video'),('not-downloaded','file')]:
    source.store=Mock();source.store.materialize.return_value=[{'status':status,'kind':kind}]
    with self.assertRaisesRegex(ValueError,'asset_materialization_required'):
     a.enrich(registry,changes,{'1':{'content':'text'}},source.attachment_files)
    self.assertEqual(registry,before);self.assertEqual(changes,{'03 纯文本记录/a.md':b'original'})
 def test_media_pending_is_explicit_refreshable_and_does_not_copy_signed_url(self):
  import json,tempfile
  from unittest.mock import Mock
  note={'id':'1','content':'original text','updated_at':'2026-10-13T00:00:00Z',
        'attachments':[{'type':'audio','url':'https://example.com/recording.mp3?Expires=1&Signature=fixture'}]}
  with tempfile.TemporaryDirectory() as d:
   fetch=Mock(side_effect=AssertionError('media is pending, not downloaded'))
   source=a.SourceWithAssets(object(),d,fetch=fetch,download_media=False)
   files=source.attachment_files(note,'99 录音卡笔记/a.md');self.assertEqual(len(files),1)
   path,data=next(iter(files.items()));a.verify(path,data)
   record=json.loads(data);self.assertEqual(record['source_note_id'],'1')
   self.assertEqual(record['items'][0]['status'],'pending_download')
   self.assertEqual(record['resolve'],'refresh_source_note_before_download')
   self.assertNotIn(b'Signature',data);self.assertNotIn(b'https://',data)
   body=a.append('99 录音卡笔记/a.md',b'original text',files)
   self.assertTrue(body.startswith(b'original text'));self.assertIn('待补全'.encode(),body)
   self.assertEqual(a.references({'99 录音卡笔记/a.md':body}),{path})
   self.assertEqual(source.pending_media,{'1':1});fetch.assert_not_called()
 def test_media_without_stable_source_id_is_held(self):
  import tempfile
  with tempfile.TemporaryDirectory() as d:
   source=a.SourceWithAssets(object(),d)
   with self.assertRaisesRegex(ValueError,'asset_materialization_required'):
    source.attachment_files({'attachments':[{'type':'audio','url':'https://example.com/a.mp3'}]},'a.md')
 def test_update_rebuilds_suffix_and_baseline_from_new_source_body(self):
  binary=b'new image';asset='_assets/images/'+hashlib.sha256(binary).hexdigest()+'.png'
  registry={'newById':{'1':{'filePath':'03 纯文本记录/a.md','mirror_sha256':'old','baseline_blob':'old'}}}
  outputs={'03 纯文本记录/a.md':b'new source version'}
  updated,files=a.enrich(registry,outputs,{'1':{}},lambda note,path:{asset:binary})
  raw=files['03 纯文本记录/a.md']
  self.assertEqual(raw.count(a.MARKER.encode()),1);self.assertTrue(raw.startswith(b'new source version'))
  self.assertEqual(updated['newById']['1']['mirror_sha256'],hashlib.sha256(raw).hexdigest())
  self.assertEqual(registry['newById']['1']['mirror_sha256'],'old')

 def test_audio_only_url_is_pending_or_downloaded_without_leaking_signed_url(self):
  import json,tempfile
  from source_sync_assets import AssetError
  note={'id':'1','updated_at':'1','audio':{'play_url':'https://example.com/a.m4a?Signature=secret'}}
  with tempfile.TemporaryDirectory() as d:
   def failed(url,max_bytes):raise AssetError('expired')
   source=a.SourceWithAssets(object(),d,fetch=failed)
   files=source.attachment_files(note,'a.md');record=json.loads(next(iter(files.values())))
   self.assertEqual(record['items'][0]['source_field'],'audio.play_url')
   self.assertNotIn(b'Signature',next(iter(files.values())))
   source=a.SourceWithAssets(object(),d,fetch=lambda url,max_bytes:(b'media','audio/mp4'))
   files=source.attachment_files(note,'a.md')
   self.assertIn(b'media',files.values());self.assertTrue(any(p.endswith('.media.json') for p in files));self.assertEqual(source.pending_media,{})
 def test_unchanged_pending_retries_rebuild_suffix_and_rotate_bounded_selection(self):
  import json
  blob=b'pending';pending='_assets/files/'+hashlib.sha256(blob).hexdigest()+'.pending.json'
  old=a.append('a.md',b'original source', {pending:blob})
  registry={'scan_cursor':None,'newById':{'1':{'filePath':'a.md','source_version':{'type':'integer','value':1},'mirror_sha256':hashlib.sha256(old).hexdigest(),'baseline_blob':'unused'}}}
  self.assertEqual(a.retry_ids(registry,{'a.md':old}),['1'])
  binary=b'audio';path='_assets/files/'+hashlib.sha256(binary).hexdigest()+'.bin'
  result,outputs=a.enrich(registry,{}, {'1':{'updated_at':1}},lambda note,path_: {path:binary},current={'a.md':old},retry=['1'])
  self.assertEqual(outputs['a.md'],a.append('a.md',b'original source',{path:binary}))
  self.assertEqual(a.retry_ids(result,outputs),[])
  with self.assertRaisesRegex(ValueError,'baseline_changed'):
   a.enrich(registry,{}, {'1':{'updated_at':1}},lambda note,path_: {path:binary},current={'a.md':old+b'edit'},retry=['1'])
 def test_hls_remains_explicit_pending_without_fetch(self):
  import tempfile
  from unittest.mock import Mock
  with tempfile.TemporaryDirectory() as d:
   fetch=Mock(side_effect=AssertionError('HLS is unsupported'))
   source=a.SourceWithAssets(object(),d,fetch=fetch)
   files=source.attachment_files({'id':'1','updated_at':'1','attachments':[{'type':'video','url':'https://example.com/a.m3u8'}]},'a.md')
   self.assertTrue(next(iter(files)).endswith('.pending.json'));fetch.assert_not_called()

 def test_flat_audio_url_and_per_run_media_attempt_budget(self):
  import json,tempfile
  from unittest.mock import Mock
  with tempfile.TemporaryDirectory() as d:
   fetch=Mock(return_value=(b'media','audio/mpeg'))
   source=a.SourceWithAssets(object(),d,fetch=fetch)
   for ident in ('1','2'):
    files=source.attachment_files({'id':ident,'updated_at':1,'audio_url':'https://example.com/'+ident+'.mp3'},ident+'.md')
    self.assertTrue(any(p.endswith('.bin') for p in files))
   files=source.attachment_files({'id':'3','updated_at':1,'audio_url':'https://example.com/3.mp3'},'3.md')
   record=json.loads(next(iter(files.values())))
   self.assertEqual(record['items'][0]['source_field'],'audio_url');self.assertEqual(fetch.call_count,2)
 def test_oversized_or_wrong_mime_media_stays_pending(self):
  import json,tempfile
  with tempfile.TemporaryDirectory() as d:
   for payload,mime in [(b'01234567890','audio/mpeg'),(b'html','text/html')]:
    source=a.SourceWithAssets(object(),d,fetch=lambda url,max_bytes:(payload,mime));source.store.max_file=10
    files=source.attachment_files({'id':'1','updated_at':1,'audio_url':'https://example.com/a.mp3'},'a.md')
    self.assertTrue(next(iter(files)).endswith('.pending.json'))

 def test_media_http_opt_in_keeps_existing_public_endpoint_and_size_checks(self):
  import io
  from urllib.parse import urlsplit
  from unittest.mock import patch
  import source_sync_assets as transport
  class Response(io.BytesIO):
   status=200
   def getheader(self,name):return {'Content-Type':'audio/mpeg','Content-Length':'5'}.get(name)
  with patch.object(transport,'public_endpoint',return_value=(urlsplit('https://example.com/a.mp3'),'8.8.8.8')),patch.object(transport,'PinnedHTTPSConnection') as conn:
   conn.return_value.getresponse.return_value=Response(b'media')
   self.assertEqual(transport.fetch_once('https://example.com/a.mp3',allow_media=True),(b'media','audio/mpeg'))
   headers=conn.return_value.request.call_args.kwargs['headers']
   self.assertNotIn('Authorization',headers);self.assertNotIn('Cookie',headers)
   conn.return_value.getresponse.return_value=Response(b'media')
   with self.assertRaises(transport.AssetError):transport.fetch_once('https://example.com/a.mp3')

 def test_media_transport_exceptions_become_pending_and_default_fetch_retries_are_bounded(self):
  import http.client,json,tempfile
  from unittest.mock import Mock,patch
  import source_sync_assets as transport
  for error in (TimeoutError(),OSError(),http.client.IncompleteRead(b'partial')):
   with tempfile.TemporaryDirectory() as d:
    fetch=Mock(side_effect=error);source=a.SourceWithAssets(object(),d,fetch=fetch)
    files=source.attachment_files({'id':'1','updated_at':1,'audio_url':'https://example.com/a.mp3'},'a.md')
    self.assertTrue(next(iter(files)).endswith('.pending.json'));self.assertEqual(fetch.call_count,1)
   with tempfile.TemporaryDirectory() as d,patch.object(transport,'fetch_once',side_effect=error) as lower,patch.object(transport.time,'sleep'):
    source=a.SourceWithAssets(object(),d)
    files=source.attachment_files({'id':'1','updated_at':1,'audio_url':'https://example.com/a.mp3'},'a.md')
    self.assertTrue(next(iter(files)).endswith('.pending.json'));self.assertEqual(lower.call_count,3)
 def test_media_cursor_does_not_starve_behind_continuous_new_selections(self):
  data=b'pending';path='_assets/files/'+hashlib.sha256(data).hexdigest()+'.pending.json'
  current={str(i)+'.md':a.append(str(i)+'.md',b'body',{path:data}) for i in range(1,7)}
  registry={'newById':{str(i):{'filePath':str(i)+'.md'} for i in range(1,7)},'scan_cursor':'1000'}
  seen=[]
  for new_cursor in ('2000','3000','4000'):
   registry['scan_cursor']=new_cursor
   retry=a.retry_ids(registry,current);seen.extend(retry);registry['media_retry_cursor']=retry[-1]
  self.assertEqual(seen,['1','2','3','4','5','6'])

 def test_rotating_debt_gets_media_budget_before_other_changed_notes(self):
  pending=b'pending';ref='_assets/files/'+hashlib.sha256(pending).hexdigest()+'.pending.json'
  old=a.append('2.md',b'original',{ref:pending})
  registry={'newById':{'1':{'filePath':'1.md'},'2':{'filePath':'2.md','mirror_sha256':hashlib.sha256(old).hexdigest(),'source_version':{'type':'integer','value':1}}}}
  binary=b'media';asset='_assets/files/'+hashlib.sha256(binary).hexdigest()+'.bin';calls=[]
  def loader(note,path):calls.append(note['id']);return {asset:binary}
  a.enrich(registry,{'1.md':b'changed'},{'1':{'id':'1'},'2':{'id':'2','updated_at':1}},loader,current={'2.md':old},retry=['2'])
  self.assertEqual(calls,['2','1'])

 def test_historical_audio_without_block_is_selected_and_original_bytes_preserved(self):
  raw=b'---\nnote_type: "recorder_audio"\naudio_url: ""\n---\noriginal transcript\n'
  registry={'newById':{'1':{'filePath':'a.md','mirror_sha256':hashlib.sha256(raw).hexdigest(),'source_version':{'type':'integer','value':1}}}}
  self.assertEqual(a.retry_ids(registry,{'a.md':raw}),['1'])
  blob=b'audio';path='_assets/files/'+hashlib.sha256(blob).hexdigest()+'.bin'
  updated,files=a.enrich(registry,{}, {'1':{'updated_at':1}},lambda note,p:{path:blob},current={'a.md':raw},retry=['1'])
  self.assertTrue(files['a.md'].startswith(raw))
  with self.assertRaisesRegex(ValueError,'source_version_changed'):
   a.enrich(registry,{}, {'1':{'updated_at':2}},lambda note,p:{path:blob},current={'a.md':raw},retry=['1'])
 def test_historical_completed_block_not_reselected_and_missing_url_explicit(self):
  import tempfile,json
  raw=b'---\nnote_type: "recorder_audio"\naudio_url: ""\n---\ntranscript'
  with tempfile.TemporaryDirectory() as d:
   source=a.SourceWithAssets(object(),d,fetch=lambda url,max_bytes:(b'audio','audio/mpeg'))
   done=source.attachment_files({'id':'1','note_type':'recorder_audio','updated_at':1,'audio_url':'https://example.com/a.mp3'},'a.md')
   body=a.append('a.md',raw,done)
   registry={'newById':{'1':{'filePath':'a.md'}}}
   self.assertEqual(a.retry_ids(registry,{'a.md':body}),[])
   missing=source.attachment_files({'id':'1','note_type':'recorder_audio','updated_at':1},'a.md')
   record=json.loads(next(iter(missing.values())))
   self.assertEqual(record['items'][0]['reason'],'source_media_url_unavailable')

 def test_partial_media_resume_reuses_verified_bytes_and_completes_later_items(self):
  import tempfile,json
  from unittest.mock import Mock
  note={'id':'1','updated_at':1,'attachments':[{'type':'audio','url':'https://example.com/'+str(i)+'.mp3?Signature=fresh'} for i in range(3)]}
  with tempfile.TemporaryDirectory() as d:
   first=a.SourceWithAssets(object(),d+'/one',fetch=lambda url,max_bytes:(url.split('?')[0].encode(),'audio/mpeg'))
   files=first.attachment_files(note,'a.md');body=a.append('a.md',b'original',files)
   self.assertTrue(any(p.endswith('.pending.json') for p in files))
   registry={'newById':{'1':{'filePath':'a.md','mirror_sha256':hashlib.sha256(body).hexdigest(),'source_version':{'type':'integer','value':1}}}}
   fetch=Mock(side_effect=lambda url,max_bytes:(url.split('?')[0].encode(),'audio/mpeg'))
   second=a.SourceWithAssets(object(),d+'/two',fetch=fetch)
   updated,outputs=a.enrich(registry,{}, {'1':note},second.attachment_files,current={'a.md':body},retry=['1'],read_asset=files.__getitem__)
   self.assertEqual(fetch.call_count,1);self.assertIn('/2.mp3',fetch.call_args.args[0])
   self.assertFalse(any(p.endswith('.pending.json') for p in outputs));self.assertEqual(a.retry_ids(updated,outputs),[])
   receipts=[json.loads(v) for p,v in outputs.items() if p.endswith('.media.json')]
   self.assertEqual(receipts[0]['status'],'complete');self.assertEqual(len(receipts[0]['items']),3)
   self.assertNotIn(b'Signature',json.dumps(receipts).encode())
   changed=a.SourceWithAssets(object(),d+'/three',fetch=fetch)
   changed.restore_media({**note,'updated_at':2},'a.md',body,files.__getitem__)
   self.assertEqual(changed.store.memo,{})

 def test_historical_existing_asset_block_keeps_all_previously_saved_bytes(self):
  raw=b'---\nnote_type: "recorder_audio"\naudio_url: ""\n---\ntranscript'
  image=b'old image';oldpath='_assets/images/'+hashlib.sha256(image).hexdigest()+'.png'
  body=a.append('a.md',raw,{oldpath:image})
  registry={'newById':{'1':{'filePath':'a.md','mirror_sha256':hashlib.sha256(body).hexdigest(),'source_version':{'type':'integer','value':1}}}}
  self.assertEqual(a.retry_ids(registry,{'a.md':body}),['1'])
  media=b'audio';newpath='_assets/files/'+hashlib.sha256(media).hexdigest()+'.bin'
  updated,outputs=a.enrich(registry,{}, {'1':{'updated_at':1}},lambda note,path:{newpath:media},current={'a.md':body},retry=['1'],read_asset={oldpath:image}.__getitem__)
  self.assertEqual(a.references({'a.md':outputs['a.md']}),{oldpath,newpath})
  self.assertEqual(outputs[oldpath],image);self.assertTrue(outputs['a.md'].startswith(raw))
