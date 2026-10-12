import contextlib
import io
import json
import os
from pathlib import Path
import sys
import unittest
import urllib.error
import urllib.parse
from unittest.mock import Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import probe_getnote_details as d

SECRET='SYNTHETIC-SECRET-NEVER-LOG'
BODY='SYNTHETIC-PRIVATE-BODY-NEVER-LOG'
NOTE_ID='9223372036854775807'
STAMP='2026-10-12T12:34:56Z'


class Response(io.BytesIO):
    status=200


def envelope(data):
    return Response(json.dumps({'success':True,'data':data}).encode())


def note(nid=NOTE_ID,kind='plain_text',original=''):
    return {'note_id':nid,'note_type':kind,'updated_at':STAMP,'title':SECRET,
            'content':BODY,'audio':{'original':original},
            'attachments':[{'url':'https://example.invalid/?signature='+SECRET}]}


class DetailProbeTests(unittest.TestCase):
    def run_probe(self,responses):
        opener=Mock();opener.open.side_effect=responses
        stdout,stderr=io.StringIO(),io.StringIO()
        with patch.dict(os.environ,{'GETNOTE_API_KEY':SECRET,'GETNOTE_CLIENT_ID':SECRET}),patch.object(d.urllib.request,'build_opener',return_value=opener),contextlib.redirect_stdout(stdout),contextlib.redirect_stderr(stderr):
            code=d.main()
        for private in [SECRET,BODY,NOTE_ID,STAMP,'signature=']:
            self.assertNotIn(private,stdout.getvalue()+stderr.getvalue())
        self.assertEqual(stderr.getvalue(),'')
        self.assertLessEqual(opener.open.call_count,3)
        for call in opener.open.call_args_list:
            request=call.args[0]
            self.assertEqual(urllib.parse.urlsplit(request.full_url).netloc,'openapi.biji.com')
            self.assertEqual(request.get_method(),'GET')
        return code,json.loads(stdout.getvalue()),opener

    def test_one_ordinary_one_recording_full_strings_no_private_output(self):
        ordinary=note();recording=note('9223372036854775808','recorder_audio',BODY*3000)
        code,result,opener=self.run_probe([envelope({'notes':[ordinary,recording,ordinary],'has_more':True,'cursor':SECRET}),envelope({'note':ordinary}),envelope({'note':recording})])
        self.assertEqual(code,0);self.assertEqual(opener.open.call_count,3)
        self.assertEqual(result['samples_available'],2)
        self.assertEqual(result['samples'][0]['content_chars'],len(BODY))
        self.assertEqual(result['samples'][1]['transcript_chars'],len(BODY)*3000)
        self.assertTrue(result['samples'][1]['attachment_urls_present'])
        self.assertTrue(result['samples'][1]['stable_id_matches'])
        self.assertTrue(result['samples'][1]['updated_at_matches_list'])
        self.assertIsInstance(result['samples'][1]['elapsed_ms'],int)
        self.assertFalse(result['source_completeness_claimed'])
        self.assertFalse(result['cross_destination_comparison'])
        query=urllib.parse.parse_qs(urllib.parse.urlsplit(opener.open.call_args_list[2].args[0].full_url).query)
        self.assertEqual(query['id'],['9223372036854775808'])
        self.assertEqual(query['image_quality'],['original'])

    def test_missing_recording_does_not_paginate_or_fetch_substitute(self):
        ordinary=note()
        code,result,opener=self.run_probe([envelope({'notes':[ordinary]*20,'has_more':True,'cursor':SECRET}),envelope({'note':ordinary})])
        self.assertEqual(code,0);self.assertEqual(opener.open.call_count,2)
        self.assertEqual(result['samples'][1]['category'],'not_in_first_page')
        self.assertEqual(result['samples_available'],1)

    def test_no_matching_sample_is_not_successful_detail_acceptance(self):
        code,result,opener=self.run_probe([envelope({'notes':[note(kind='unsupported')],'has_more':True})])
        self.assertEqual(code,1);self.assertEqual(opener.open.call_count,1)
        self.assertEqual(result['category'],'no_samples_selected')

    def test_pending_transcript_wrong_id_changed_timestamp_and_truncation_fail(self):
        summary=note(kind='recorder_audio')
        for detail in [note(kind='recorder_audio'),dict(note(kind='recorder_audio',original=BODY),note_id='other'),dict(note(kind='recorder_audio',original=BODY),updated_at='changed'),dict(note(kind='recorder_audio',original=BODY),truncated=True)]:
            code,result,_=self.run_probe([envelope({'notes':[summary]}),envelope({'note':detail})])
            self.assertEqual(code,1)
            self.assertEqual(result['samples_available'],0)

    def test_failure_never_retries_or_echoes_exceptions(self):
        ordinary=note()
        for error in [urllib.error.HTTPError(d.DETAIL_URL+'?'+SECRET,403,BODY,{},io.BytesIO(BODY.encode())),d.auth.RedirectBlocked(),TimeoutError(SECRET)]:
            code,result,opener=self.run_probe([envelope({'notes':[ordinary]}),error])
            self.assertEqual(code,1);self.assertEqual(opener.open.call_count,2)
            self.assertNotEqual(result['samples'][0]['category'],'fields_available')

    def test_list_failure_stops_detail_requests(self):
        code,result,opener=self.run_probe([RuntimeError(BODY)])
        self.assertEqual(code,1);self.assertEqual(opener.open.call_count,1)
        self.assertEqual(result['category'],'list_failed')

    def test_lossy_id_and_detail_limit_fail_closed(self):
        invalid=note(nid=9.223372036854776e18)
        code,result,opener=self.run_probe([envelope({'notes':[invalid]})])
        self.assertEqual(code,1);self.assertEqual(opener.open.call_count,1)
        with patch.object(d,'DETAIL_MAX_BYTES',8):
            code,result,_=self.run_probe([envelope({'notes':[note()]}),Response(b'x'*9)])
        self.assertEqual(code,1)
        self.assertEqual(result['samples'][0]['category'],'response_too_large')


if __name__=='__main__':unittest.main()
