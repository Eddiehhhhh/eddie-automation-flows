"""Synthetic signatures/bytes only. No upstream requests or actual source IDs."""
import base64
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import quote
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import flomo_original_source as s
import sync_notion_flomo_to_raw as b
import flomo_cloud_current as c
from notion_source_budget import Budget
from test_flomo_original_source import memo

PNG=b'\x89PNG\r\nsynthetic-old-image'
NEW=PNG.replace(b'old',b'new')
HOST=next(iter(s.AUTH_CDN_HOSTS))


def url(name='a',rotation=1,extra=''):
    signature=quote(base64.b64encode(bytes([rotation])*20).decode(),safe='')
    return 'https://'+HOST+'/synthetic-fixture/'+name+'.png?OSSAccessKeyId=synthetic-provider-id&Expires='+str(2000000000+rotation)+'&Signature='+signature+extra


def image(mid='a',rotation=1):
    value=memo(mid,content='<p>Note '+mid+'</p>',files=[{'id':'synthetic-'+mid,'path':'synthetic-fixture/'+mid+'.png',
        'name':mid+'.png','type':'image','size':len(PNG),'url':url(mid,rotation),'thumbnail_url':url(mid,rotation)}])
    return value


class StableInput(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        path=self.root/b.STATE;path.parent.mkdir(parents=True);path.write_bytes(b.encoded({'version':1,'pages':{}}))
    def run_source(self,values,max_pages=2,downloader=lambda u:PNG,budget=None):
        return s.enrich(self.root,b,token='synthetic-invalid-access',signing_suffix='synthetic',max_pages=max_pages,
            reader=lambda *args:deepcopy(values),downloader=downloader,budget=budget)
    def state(self):return json.loads((self.root/b.STAGE/s.STATE).read_bytes())
    def record(self,mid):return self.state()['pages'][mid]['source_pages']['upstream-only']
    def raw(self,record):return (self.root/b.STAGE/next(p for p in record['files'] if p.endswith('/original.json'))).read_bytes()
    def seed(self,values,legacy=False):
        self.run_source(values,max_pages=0)
        if legacy:
            state=self.state()
            for group in state['pages'].values():
                for record in group['source_pages'].values():
                    record.pop('input_fingerprint');record.pop('input_fingerprint_version')
            (self.root/b.STAGE/s.STATE).write_bytes(b.encoded(state))
        return deepcopy(self.state())
    def small_budget(self,requests):return Budget({'max_pages':50,'max_seconds':300,'max_requests':requests})

    def test_original_job_reserve_skips_current_without_discarding_stage_result(self):
        from contextlib import nullcontext
        with patch.object(b.private_checkout,'verify_private_checkout'), \
             patch.object(b,'writer_lock',return_value=nullcontext()), \
             patch.object(b,'stage',return_value={'complete':True,'written':2}), \
             patch.object(s,'enrich',return_value={'complete':False,'soft_exit':'job_budget_reserved_for_persistence'}), \
             patch.object(c,'export') as current:
            result=b.run(self.root,'synthetic-db','invalid-fixture',apply=True,
                original_source_enabled=True,current_export_enabled=True)
        current.assert_not_called()
        self.assertEqual(result['written'],2)
        self.assertEqual(result['followup_pending'],{'formal_current':True})
        self.assertFalse(result['complete'])

    def test_only_proved_auth_fields_are_ignored_and_unknown_query_bytes_remain(self):
        first=url(extra='&x-oss-process=image%2Fresize%2Cw_100&unknown=%20&unknown=two#fragment')
        second=url(rotation=2,extra='&x-oss-process=image%2Fresize%2Cw_100&unknown=%20&unknown=two#fragment')
        self.assertEqual(s.stable_asset_url(first),s.stable_asset_url(second))
        self.assertIn('unknown=%20&unknown=two#fragment',s.stable_asset_url(first))
        self.assertIn('OSSAccessKeyId=synthetic-provider-id',s.stable_asset_url(first))
        for changed in [second.replace('w_100','w_200'),second.replace('unknown=%20','unknown=+'),second.replace('provider-id','other-id'),second.replace('/a.png','/other.png'),second.replace('#fragment','#other')]:
            self.assertNotEqual(s.stable_asset_url(first),s.stable_asset_url(changed))

    def test_unknown_host_missing_duplicate_or_invalid_signature_shape_not_normalized(self):
        valid=url()
        for value in [valid.replace(HOST,'files.invalid'),valid+'&Signature=duplicate',valid.replace('Expires=2000000001','Expires=bad'),valid.split('&Signature=')[0],valid.replace('Signature=','Signature=invalid-'),valid.replace('https://','http://'),valid.replace(HOST,'user:pass@'+HOST)]:
            self.assertEqual(s.stable_asset_url(value),value)

    def test_same_two_rotations_do_not_consume_capture_slots_and_later_real_change_progresses(self):
        baseline=[image('a'),image('b'),memo('c')];self.seed(baseline,legacy=True);c.export(self.root)
        old_a=deepcopy(self.record('a'));old_b=deepcopy(self.record('b'))
        values=[image('a',2),image('b',2),memo('c',content='<p>Actual new body</p>')]
        result=self.run_source(values,max_pages=2)
        self.assertEqual((result['written'],result['capture_attempted'],result['pending']),(1,1,0))
        self.assertEqual(result['stable_input_unchanged'],2)
        self.assertEqual(self.record('a'),old_a);self.assertEqual(self.record('b'),old_b)
        self.assertEqual(json.loads(self.raw(self.record('c')))['content'],'<p>Actual new body</p>')
        self.assertEqual(c.export(self.root)['written'],1)
        second=self.run_source([image('a',3),image('b',3),values[2]],max_pages=2)
        self.assertEqual((second['written'],second['capture_attempted'],second['pending']),(0,0,0))
        self.assertEqual(c.export(self.root)['written'],0)

    def test_pure_rotation_preserves_legacy_state_raw_sha_versions_and_current_bytes(self):
        self.seed([image('a'),image('b')],legacy=True);c.export(self.root)
        state_path=self.root/b.STAGE/s.STATE;before=state_path.read_bytes()
        current_path=self.root/c.STATE;old_current=current_path.read_bytes();old_raw={mid:self.raw(self.record(mid)) for mid in ('a','b')}
        result=self.run_source([image('a',2),image('b',2)])
        self.assertEqual((result['written'],result['capture_attempted']),(0,0));self.assertEqual(result['asset_probe_verified'],2)
        self.assertEqual(state_path.read_bytes(),before)
        self.assertTrue(all(self.raw(self.record(mid))==old_raw[mid] for mid in old_raw))
        self.assertEqual(c.export(self.root)['written'],0);self.assertEqual(current_path.read_bytes(),old_current)

    def test_same_path_asset_bytes_changed_is_captured_and_fresh_bytes_not_downloaded_twice(self):
        self.seed([image('a')]);old=self.record('a');calls=[]
        def download(value):calls.append(value);return NEW
        result=self.run_source([image('a',2)],downloader=download)
        self.assertEqual(result['written'],1);self.assertEqual(result['asset_probe_changed'],1);self.assertEqual(len(calls),1)
        self.assertNotEqual(old['current_version'],self.record('a')['current_version'])
        self.assertTrue(set(old['versions'])<set(self.record('a')['versions']))
        self.assertTrue(any((self.root/b.STAGE/path).read_bytes()==NEW for path in self.record('a')['files'] if '/assets/' in path))

    def test_exact_raw_url_but_asset_bytes_changed_is_not_noop(self):
        value=image('a');self.seed([value]);result=self.run_source([value],downloader=lambda u:NEW)
        self.assertEqual(result['asset_probe_changed'],1);self.assertEqual(result['written'],1)

    def test_meaningful_path_identity_text_time_transform_or_unknown_query_change_is_capture(self):
        baseline=image('a')
        for mutate in [lambda x:x['files'][0].update(path='changed'),lambda x:x['files'][0].update(url=url(name='other')),lambda x:x['files'][0].update(id='changed'),lambda x:x.update(content='<p>Changed</p>'),lambda x:x.update(updated_at='2026-01-02 12:34:56'),lambda x:x['files'][0].update(url=url(extra='&x-oss-process=resize')),lambda x:x['files'][0].update(url=url(extra='&unknown=changed'))]:
            self.seed([baseline]);changed=deepcopy(baseline);mutate(changed)
            result=self.run_source([changed]);self.assertEqual(result['written'],1)
            self.assertEqual(json.loads(self.raw(self.record('a'))),changed)

    def test_bad_cache_held_not_silently_used_for_input_equality(self):
        self.seed([image('a')]);record=self.record('a');asset=next(p for p in record['files'] if '/assets/' in p)
        (self.root/b.STAGE/asset).write_bytes(b'human edit');before=(self.root/b.STAGE/s.STATE).read_bytes()
        result=self.run_source([image('a',2)]);self.assertEqual(result['written'],0);self.assertIn('stage_edited',result['held'].values())
        self.assertEqual((self.root/b.STAGE/s.STATE).read_bytes(),before)

    def test_credential_in_auth_parameter_is_held_before_ignored_input_hash(self):
        self.seed([image('a')]);value=image('a',2);value['files'][0]['url']+='&Signature=ghp_'+'Z'*36
        result=self.run_source([value]);self.assertIn('potential_credential_in_source',result['held'].values());self.assertEqual(result['written'],0)

    def test_state_race_during_healthy_noop_probe_is_held(self):
        self.seed([image('a')]);path=self.root/b.STAGE/s.STATE
        def race(value):path.write_bytes(b'{"user":"edit"}');return PNG
        result=self.run_source([image('a',2)],downloader=race)
        self.assertIn('state_changed',result['held'].values());self.assertEqual(path.read_bytes(),b'{"user":"edit"}')

    def test_asset_cache_race_during_probe_is_held(self):
        self.seed([image('a')]);asset=next(p for p in self.record('a')['files'] if '/assets/' in p)
        def race(value):(self.root/b.STAGE/asset).write_bytes(b'human asset edit');return PNG
        result=self.run_source([image('a',2)],downloader=race)
        self.assertIn('stage_edited',result['held'].values());self.assertEqual(result['written'],0)

    def test_probe_budget_cursor_resumes_later_unit_without_capturing_auth_noise(self):
        self.seed([image('a'),image('b')]);first=[];second=[]
        self.run_source([image('a',2),image('b',2)],budget=self.small_budget(2),downloader=lambda value:first.append(value) or PNG)
        state=self.state();self.assertEqual(state['asset_probe_cursor'],['b','upstream-only'])
        result=self.run_source([image('a',3),image('b',3)],budget=self.small_budget(2),downloader=lambda value:second.append(value) or (NEW if '/b.png?' in value else PNG))
        self.assertIn('/b.png?',second[0]);self.assertEqual(result['written'],1);self.assertEqual(result['asset_probe_changed'],1)

    def test_changed_asset_over_capture_quota_is_durable_and_resumed_as_real_work(self):
        self.seed([image('a'),memo('c'),memo('d')])
        values=[image('a',2),memo('c',content='<p>C changed</p>'),memo('d',content='<p>D changed</p>')]
        first=self.run_source(values,max_pages=2,downloader=lambda u:NEW)
        self.assertEqual(first['written'],2);self.assertEqual(self.state()['asset_change_pending'],[['a','upstream-only']])
        values[0]=image('a',3);second=self.run_source(values,max_pages=2,downloader=lambda u:NEW)
        self.assertEqual(second['written'],1);self.assertNotIn('asset_change_pending',self.state())
        self.assertTrue(any((self.root/b.STAGE/path).read_bytes()==NEW for path in self.record('a')['files'] if '/assets/' in path))

    def test_failed_fresh_probe_keeps_old_healthy_bytes_and_specific_http_reason(self):
        import urllib.error
        self.seed([image('a')]);before=(self.root/b.STAGE/s.STATE).read_bytes()
        def deny(url):raise urllib.error.HTTPError(url,403,'synthetic',{},None)
        result=self.run_source([image('a',2)],downloader=deny)
        self.assertEqual(result['written'],0);self.assertIn('asset_probe_http_403',result['held'].values())
        self.assertEqual((self.root/b.STAGE/s.STATE).read_bytes(),before)

    def test_probe_asset_file_budget_is_specific_not_silent_exclusion(self):
        self.seed([image('a')])
        budget=Budget({'max_pages':50,'max_seconds':300,'max_requests':10},max_asset_bytes=1)
        result=self.run_source([image('a',2)],budget=budget)
        self.assertEqual(result['written'],0);self.assertIn('asset_file_budget',result['held'].values())
        self.assertFalse(result['complete'])

    def test_degraded_cache_is_retried_not_claimed_healthy(self):
        import urllib.error
        def deny(url):raise urllib.error.HTTPError(url,403,'synthetic',{},None)
        first=self.run_source([image('a')],downloader=deny);self.assertFalse(first['media_complete'])
        second=self.run_source([image('a',2)]);self.assertEqual(second['written'],1);self.assertFalse(self.record('a')['degraded'])

if __name__=='__main__':unittest.main()
