from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import get_notion_exact_media_projection as media


class ExactMediaProjection(unittest.TestCase):
    def setUp(self):
        self.nid = '12345'
        self.url = 'https://audio.invalid/a?Signature=synthetic'
        self.line = '![](https://image.invalid/p?Signature=synthetic)'
        self.body = 'start\n'+self.line+'\nend\n'
        self.source = ('---\nnote_id: "12345"\n---\n'+self.body).encode()
        self.meta = {'attachments':[{'url':self.url,'type':'audio','title':'fixture','duration':321}]}
        self.fields = {'audio_url':self.url,'attachments':[self.url],'audio_transcript':'transcript'}
        for name, raw in (('ID_SHA',self.nid.encode()),('SOURCE_SHA',self.source),
                          ('IMAGE_LINE_SHA',self.line.encode()),('AUDIO_URL_SHA',self.url.encode()),
                          ('METADATA_SHA', json.dumps(self.meta['attachments'],ensure_ascii=False,
                                                      sort_keys=True,separators=(',',':')).encode())):
            p=patch.object(media,name,hashlib.sha256(raw).hexdigest())
            p.start(); self.addCleanup(p.stop)

    def test_original_unchanged_and_direct_audio_is_not_the_note_link(self):
        original=deepcopy((self.fields,self.meta))
        body = media.body_view(self.source,self.nid,self.body)
        fields, metadata=media.field_view(self.source,self.nid,self.fields,self.meta)
        self.assertTrue(body.startswith('start\n') and body.endswith('\nend\n'))
        self.assertIn('https://www.biji.com/note/12345',body)
        self.assertEqual(fields['audio_url'],'')
        self.assertNotIn('Signature=',str((fields,metadata,body)))
        self.assertEqual(fields['audio_transcript'],'transcript')
        self.assertEqual(metadata['attachments'][0],{'type':'audio','title':'fixture',
            'duration':321,'source_note_url':'https://www.biji.com/note/12345'})
        self.assertEqual((self.fields,self.meta),original)

    def test_unreviewed_version_other_id_and_unexpected_sensitive_fields(self):
        for bad in (self.source+b'changed',b'no source'):
            with self.assertRaisesRegex(ValueError,'content_review_required'):
                media.body_view(bad,self.nid,self.body)
        self.assertEqual(media.body_view(self.source,'777',self.body),self.body)
        self.assertEqual(media.field_view(self.source,'777',self.fields,self.meta),(self.fields,self.meta))
        for bad in (None, {'attachments':[]},{'attachments':[{**self.meta['attachments'][0],'duration':322}]}):
            with self.assertRaisesRegex(ValueError,'content_review_required'):
                media.field_view(self.source,self.nid,self.fields,bad)
        with self.assertRaisesRegex(ValueError,'content_review_required'):
            media.field_view(self.source,self.nid,{**self.fields,'audio_transcript':self.url},self.meta)
        with self.assertRaisesRegex(ValueError,'content_review_required'):
            media.body_view(self.source,self.nid,self.body+'changed')
