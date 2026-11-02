import sys,tempfile,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from source_sync_assets import AssetStore,AssetError
from get_canonical_assets import SourceWithAssets

class MediaBudget(unittest.TestCase):
 def test_media_and_normal_limits_and_pending(self):
  calls=[]
  def fetch(url,max_bytes):
   calls.append(max_bytes)
   if 'large' in url: raise AssetError('asset exceeds byte limit')
   return b'x','audio/mpeg' if 'audio' in url else 'image/png'
  with tempfile.TemporaryDirectory() as d:
   src=SourceWithAssets(object(),d,fetch=fetch)
   src.attachment_files({'id':'1','updated_at':'2026-01-01T00:00:00Z','attachments':[{'type':'audio','url':'https://example.test/audio'},{'type':'audio','url':'https://example.test/large'}]},'x.md')
   self.assertEqual(calls,[48*1024*1024,48*1024*1024-1])
   store=AssetStore(d,fetch=fetch,allow_media=True)
   store.materialize({'attachments':[{'type':'image','url':'https://example.test/image'}]})
   self.assertEqual(calls[-1],25*1024*1024)

 def test_real_44_mib_media_and_oversize_pending(self):
  from get_canonical_assets import verify
  payload=b'a'*(46384699)
  def fetch(url,max_bytes):
   if len(payload)>max_bytes: raise AssetError('asset exceeds byte limit')
   return payload,'audio/mpeg'
  with tempfile.TemporaryDirectory() as d:
   source=SourceWithAssets(object(),d,fetch=fetch)
   files=source.attachment_files({'id':'1','updated_at':'2026-01-01T00:00:00Z','attachments':[{'type':'audio','url':'https://example.test/audio'}]},'x.md')
   binary=next((p,v) for p,v in files.items() if p.endswith('.bin'))
   verify(*binary);self.assertEqual(len(binary[1]),46384699)
   source=SourceWithAssets(object(),d,fetch=fetch);source.store.max_run=46384698
   files=source.attachment_files({'id':'2','updated_at':'2026-01-01T00:00:00Z','attachments':[{'type':'audio','url':'https://example.test/audio'}]},'x.md')
   self.assertTrue(any(p.endswith('.pending.json') for p in files))
   self.assertFalse(any(p.endswith('.media.json') for p in files))
 def test_legacy_image_receipt_reused_and_not_reclassified_as_media(self):
  import hashlib,json
  from get_canonical_assets import append
  image=b'image';path='_assets/images/'+hashlib.sha256(image).hexdigest()+'.png'
  note={'id':'1','updated_at':'2026-01-01T00:00:00Z','content':'![x](https://example.test/a.png)','attachments':[{'type':'audio','url':'https://example.test/a.mp3'}]}
  receipt={'contract':'get-media-complete-1','status':'partial','source_note_id':'1','source_updated_at':note['updated_at'],'items':[{'source_field':'content.inline_images','source_index':0,'path':path,'sha256':hashlib.sha256(image).hexdigest(),'bytes':len(image),'mime':'image/png'}]}
  data=(json.dumps(receipt,ensure_ascii=False,sort_keys=True,separators=(',',':'))+'\n').encode();rp='_assets/files/'+hashlib.sha256(data).hexdigest()+'.media.json';old=append('x.md',b'original',{path:image,rp:data});objects={path:image,rp:data};calls=[]
  def fetch(url,max_bytes):
   calls.append(url)
   if url.endswith('.png'):raise AssertionError('existing image must be reused')
   return b'audio','audio/mpeg'
  with tempfile.TemporaryDirectory() as d:
   src=SourceWithAssets(object(),d,fetch=fetch);src.restore_media(note,'x.md',old,objects.__getitem__);out=src.attachment_files(note,'x.md')
   self.assertEqual(out[path],image);self.assertEqual(len(calls),1)
   newreceipt=json.loads(next(v for p,v in out.items() if p.endswith('.media.json')))
   self.assertEqual(sorted(i['mime'] for i in newreceipt['items']),['audio/mpeg','image/png'])
 def test_large_audio_does_not_consume_image_budget(self):
  audio=b'a'*46384699;image=b'i'*(8*1024*1024)
  def fetch(url,max_bytes):
   data,mime=(audio,'audio/mpeg') if url.endswith('.mp3') else (image,'image/png')
   if len(data)>max_bytes:raise AssetError('asset exceeds byte limit')
   return data,mime
  with tempfile.TemporaryDirectory() as d:
   src=SourceWithAssets(object(),d,fetch=fetch)
   files=src.attachment_files({'id':'1','updated_at':'2026-01-01T00:00:00Z','attachments':[{'type':'audio','url':'https://example.test/a.mp3'},{'type':'image','url':'https://example.test/a.png'}]},'x.md')
   self.assertTrue(any(len(v)==len(audio) for v in files.values()))
   self.assertTrue(any(len(v)==len(image) for v in files.values()))
   self.assertFalse(any(p.endswith('.pending.json') for p in files))
 def test_second_large_audio_pending_image_still_downloaded(self):
  audio1=b'a'*30391327;audio2=b'b'*46384699;image=b'i'*2048
  def fetch(url,max_bytes):
   data,mime=(audio1,'audio/mpeg') if url.endswith('one') else (audio2,'audio/mpeg') if url.endswith('two') else (image,'image/png')
   if len(data)>max_bytes:raise AssetError('asset exceeds byte limit')
   return data,mime
  with tempfile.TemporaryDirectory() as d:
   src=SourceWithAssets(object(),d,fetch=fetch)
   files=src.attachment_files({'id':'1','updated_at':'2026-01-01T00:00:00Z','attachments':[{'type':'audio','url':'https://example.test/one'},{'type':'audio','url':'https://example.test/two'},{'type':'image','url':'https://example.test/image'}]},'x.md')
   self.assertTrue(any(len(v)==len(audio1) for v in files.values()))
   self.assertTrue(any(len(v)==len(image) for v in files.values()))
   self.assertTrue(any(p.endswith('.pending.json') for p in files))
