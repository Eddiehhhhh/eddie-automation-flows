import tempfile,unittest
from pathlib import Path
from source_sync_assets import AssetStore
from get_canonical_assets import verify

class OggMedia(unittest.TestCase):
 def test_only_get_audio_with_ogg_header_is_preserved(self):
  page=bytearray(b'OggS\x00'+bytes(21)+b'\x01\x07payload');crc=0
  for byte in page:
   crc^=byte<<24
   for _ in range(8):crc=((crc<<1)^(0x04c11db7 if crc&0x80000000 else 0))&0xffffffff
  page[22:26]=crc.to_bytes(4,'little');data=bytes(page)
  with tempfile.TemporaryDirectory() as d:
   store=AssetStore(d,allow_media=True,fetch=lambda url,max_bytes:(data,'application/octet-stream'))
   row=store.materialize({'attachments':[{'type':'audio','url':'https://example.test/a'}]})[0]
   self.assertEqual(row['mime'],'audio/ogg');self.assertTrue(row['path'].endswith('.ogg'))
   output=(Path(d)/row['path']).read_bytes();self.assertEqual(output,data);verify(row['path'],output)
 def test_bad_header_and_other_sources_are_not_accepted_as_audio(self):
  for data,allow,kind in [(b'OggS\x01'+bytes(30),True,'audio'),(b'not-ogg'+bytes(30),True,'audio'),(b'OggS\x00'+bytes(30),True,'audio'),(b'OggS\x00'+bytes(30),False,'audio'),(b'OggS\x00'+bytes(30),True,'video')]:
   with tempfile.TemporaryDirectory() as d:
    store=AssetStore(d,allow_media=allow,fetch=lambda url,max_bytes:(data,'application/octet-stream'))
    row=store.materialize({'attachments':[{'type':kind,'url':'https://example.test/a'}]})[0]
    self.assertEqual(row['status'],'not-downloaded')
