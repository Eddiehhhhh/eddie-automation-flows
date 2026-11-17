"""Indexes describe verified receipts, never the baseline inventory."""
import base64
import json
import unittest
from unittest.mock import patch

import core

from core import Hold, Store, canonical, snapshot
from source_index import publish
from test_core import note
from test_vault_sink import Git
from vault_sink import VaultGitHubSink


class Tests(unittest.TestCase):
    def setUp(self):
        self.store, self.http = Store(':memory:'), Git()
        self.cfg = {'private_repo': 'owner/private', 'data_branch': 'proof', 'namespace': 'test',
                    'raw_prefix': 'Raw/Get/current'}
        self.sink = VaultGitHubSink(self.http, 'owner/private', 'proof', 'test', self.store,
                                   raw_prefix=self.cfg['raw_prefix'])

    def save(self, sid='1', title='Synthetic source'):
        n = note(sid)
        n['title'] = title
        s = snapshot(n)
        receipt = self.sink.sync(s)
        with self.store.db:
            self.store.db.execute('INSERT OR REPLACE INTO receipts VALUES(?,?,?)',
                                  ('github', sid, canonical(receipt)))
        return s, receipt

    def body(self, relative):
        from urllib.parse import quote
        path = '/repos/owner/private/contents/' + quote(relative, safe='/')
        return base64.b64decode(self.http.files[path]['content'])

    def test_baseline_is_not_saved_notes_and_status_stays_partial(self):
        self.store.accept([snapshot(note('1')), snapshot(note('2'))])
        result = publish(self.store, self.http, self.cfg)
        current = json.loads(self.body('Raw/Get/current/current.json'))
        self.assertEqual(current['notes'], {})
        self.assertEqual(current['summary']['inventory_observed'], 2)
        self.assertEqual(current['summary']['github_saved'], 0)
        self.assertEqual(current['status'], 'partial')
        self.assertEqual(result['status'], 'partial')

    def test_only_verified_receipts_make_readable_links_and_current_versions(self):
        self.store.accept([snapshot(note('1')), snapshot(note('2'))])
        s, receipt = self.save(title='可读标题 🧪')
        publish(self.store, self.http, self.cfg)
        current = json.loads(self.body('Raw/Get/current/current.json'))
        self.assertEqual(set(current['notes']), {'1'})
        row = current['notes']['1']
        self.assertEqual(row['version'], s['version'])
        self.assertEqual(row['markdown']['sha'], receipt['markdown']['sha'])
        self.assertEqual(row['source_json']['sha'], receipt['source_json']['sha'])
        self.assertEqual(row['notion_verified_version'], None)
        self.assertEqual(current['status'], 'partial')
        index = self.body('Raw/Get/current/index.md').decode()
        self.assertIn('可读标题 🧪', index)
        self.assertIn('1/', index)
        self.assertNotIn('2/', index)

    def test_unknown_existing_index_is_not_overwritten(self):
        from urllib.parse import quote
        relative = 'Raw/Get/current/index.md'
        path = '/repos/owner/private/contents/' + quote(relative, safe='/')
        self.http.files[path] = {'sha': 'human', 'content': base64.b64encode(b'human index').decode()}
        with self.assertRaisesRegex(Hold, 'unmanaged'):
            publish(self.store, self.http, self.cfg)
        self.assertEqual(self.body(relative), b'human index')

    def test_repeat_is_read_only_and_human_change_is_held(self):
        self.store.accept([])
        self.save()
        publish(self.store, self.http, self.cfg)
        writes = self.http.count
        publish(self.store, self.http, self.cfg)
        self.assertEqual(self.http.count, writes)
        from urllib.parse import quote
        path = '/repos/owner/private/contents/' + quote('Raw/Get/current/current.json', safe='/')
        self.http.files[path]['content'] = base64.b64encode(b'human modified current').decode()
        self.http.files[path]['sha'] = 'human'
        with self.assertRaisesRegex(Hold, 'unmanaged|human_edit'):
            publish(self.store, self.http, self.cfg)
        self.assertEqual(self.body('Raw/Get/current/current.json'), b'human modified current')

    def test_notion_media_pending_and_source_holds_are_not_reported_verified(self):
        self.store.accept([])
        s,receipt=self.save()
        with self.store.db:
            self.store.db.execute('INSERT OR REPLACE INTO receipts VALUES(?,?,?)',
                                  ('notion','1',canonical({'version':s['version']})))
        self.assertEqual(publish(self.store,self.http,self.cfg)['status'],'partial')
        # Explicit backfill/enqueue does not manufacture an observed baseline.
        with self.store.db:self.store.db.execute('INSERT OR REPLACE INTO inventory VALUES(?,?)',('1',s['version']))
        self.assertEqual(publish(self.store,self.http,self.cfg)['status'],'verified')
        self.store.set('source_hold:2',{'status':'held'})
        self.store.enqueue([snapshot(note('3'))])
        result=publish(self.store,self.http,self.cfg)
        self.assertEqual(result['status'],'partial')
        self.assertEqual(result['summary']['source_holds'],1)
        self.assertEqual(result['summary']['pending'],1)

    def test_binary_completion_comes_from_destination_receipts_not_urls(self):
        n=note();n['attachments']=[{'url':'https://cdn.test/synthetic','type':'image'}]
        s=snapshot(n);self.store.accept([s])
        receipt=self.sink.sync(s)
        with self.store.db:
            self.store.db.execute('INSERT OR REPLACE INTO receipts VALUES(?,?,?)',('github','1',canonical(receipt)))
            self.store.db.execute('INSERT OR REPLACE INTO receipts VALUES(?,?,?)',
                                  ('notion','1',canonical({'version':s['version'],'binary_assets_verified':True})))
        result=publish(self.store,self.http,self.cfg)
        self.assertEqual(result['status'],'partial')
        current=json.loads(self.body('Raw/Get/current/current.json'))
        self.assertTrue(current['notes']['1']['binary_assets_required'])
        self.assertFalse(current['notes']['1']['github_binary_assets_verified'])

    def test_index_write_intent_exists_before_target_write_and_response_loss_recovers(self):
        self.store.accept([])
        original=self.http.request
        writes=[]
        def request(method,path,body=None):
            if method=='PUT' and path.split('?')[0].endswith('/index.md'):
                state=self.store.get('source_index:owner/private:proof:test:index.md')
                self.assertEqual(state['phase'],'write_intent')
                writes.append(path)
                result=original(method,path,body)
                if len(writes)==1:raise TimeoutError('synthetic index response loss')
                return result
            return original(method,path,body)
        self.http.request=request
        publish(self.store,self.http,self.cfg)
        publish(self.store,self.http,self.cfg)
        self.assertEqual(len(writes),1)

    def test_unchanged_receipts_reuse_checks_and_rotate_bounded_audit(self):
        self.store.accept([])
        receipts=[]
        for number in range(1,13):receipts.append(self.save(str(number))[1])
        calls=[];original=self.http.request
        def request(method,path,body=None):
            if method=='GET' and '/contents/' in path:calls.append(path.split('?')[0])
            return original(method,path,body)
        self.http.request=request
        publish(self.store,self.http,self.cfg)
        markdown_paths={row['markdown']['path'] for row in receipts}
        self.assertEqual(len(markdown_paths.intersection(calls)),12)
        rounds=[]
        for _ in range(3):
            calls.clear();publish(self.store,self.http,self.cfg)
            checked=markdown_paths.intersection(calls)
            self.assertEqual(len(checked),5)
            rounds.append(checked)
        self.assertEqual(rounds[0].intersection(rounds[1]),set())
        self.assertEqual(set.union(*rounds),markdown_paths)

    def test_changed_receipt_is_checked_immediately_outside_audit_window(self):
        self.store.accept([])
        previous={}
        for number in range(1,13):previous[str(number)]=self.save(str(number))[1]
        publish(self.store,self.http,self.cfg)
        n=note('9',text='changed verified source')
        receipt=self.sink.sync(snapshot(n),previous['9'])
        with self.store.db:self.store.db.execute('INSERT OR REPLACE INTO receipts VALUES(?,?,?)',('github','9',canonical(receipt)))
        self.http.files[receipt['markdown']['path']]['content']=base64.b64encode(b'changed after verification').decode()
        self.http.files[receipt['markdown']['path']]['sha']='changed'
        with self.assertRaisesRegex(Hold,'receipt_target_changed'):
            publish(self.store,self.http,self.cfg)

    def test_cached_target_change_is_detected_by_rotating_audit_without_index_write(self):
        self.store.accept([])
        receipts={str(number):self.save(str(number))[1] for number in range(1,13)}
        publish(self.store,self.http,self.cfg)
        publish(self.store,self.http,self.cfg)
        # First audit checks lexical IDs 1,10,11,12,2; ID3 is in the next round.
        path=receipts['3']['markdown']['path']
        self.http.files[path]['content']=base64.b64encode(b'human changed cached target').decode()
        self.http.files[path]['sha']='human'
        before=self.body('Raw/Get/current/current.json')
        writes=self.http.count
        with self.assertRaisesRegex(Hold,'receipt_target_changed'):
            publish(self.store,self.http,self.cfg)
        self.assertEqual(self.http.count,writes)
        self.assertEqual(self.body('Raw/Get/current/current.json'),before)

    def test_index_checks_use_shared_source_version_not_raw_json_digest(self):
        n=note('1')
        n['attachments']=[{'type':'link','url':'https://example.test/source?signature=synthetic'}]
        source={'id':'1','version':'f'*64,'note':n}
        receipt=self.sink.sync(source)
        with self.store.db:self.store.db.execute('INSERT OR REPLACE INTO receipts VALUES(?,?,?)',('github','1',canonical(receipt)))
        with patch.object(core,'source_version',return_value=source['version'],create=True) as version:
            publish(self.store,self.http,self.cfg)
        version.assert_called_once_with(n)

    def test_normalized_signed_source_version_validates_full_raw_source_json(self):
        n=note('1')
        url='https://mediacdn.umiwi.com/synthetic.ogg?Expires=100&OSSAccessKeyId=synthetic&Signature=synthetic'
        n['attachments']=[{'type':'audio','url':url}]
        source=snapshot(n)
        self.assertNotEqual(source['version'],core.digest(n))
        receipt=self.sink.sync(source)
        with self.store.db:self.store.db.execute('INSERT OR REPLACE INTO receipts VALUES(?,?,?)',('github','1',canonical(receipt)))
        publish(self.store,self.http,self.cfg)
        current=json.loads(self.body('Raw/Get/current/current.json'))
        self.assertEqual(current['notes']['1']['version'],core.source_version(n))
        self.assertIn(url,self.body(receipt['source_json']['relative_path']).decode())

    def test_index_files_are_read_each_publish_even_when_note_checks_are_cached(self):
        self.store.accept([])
        for number in range(1,13):self.save(str(number))
        publish(self.store,self.http,self.cfg)
        calls=[];original=self.http.request
        def request(method,path,body=None):
            if method=='GET':calls.append(path.split('?')[0])
            return original(method,path,body)
        self.http.request=request
        publish(self.store,self.http,self.cfg)
        self.assertTrue(any(path.endswith('/index.md') for path in calls))
        self.assertTrue(any(path.endswith('/current.json') for path in calls))

    def test_notion_receipt_changes_are_not_hidden_by_github_receipt_cache(self):
        s,receipt=self.save()
        self.store.accept([s])
        publish(self.store,self.http,self.cfg)
        with self.store.db:self.store.db.execute('INSERT OR REPLACE INTO receipts VALUES(?,?,?)',
                                                ('notion','1',canonical({'version':s['version']})))
        result=publish(self.store,self.http,self.cfg)
        current=json.loads(self.body('Raw/Get/current/current.json'))
        self.assertEqual(current['notes']['1']['notion_verified_version'],s['version'])
        self.assertEqual(result['summary']['both_destinations_verified'],1)

    def test_remote_markdown_changed_after_receipt_is_not_claimed_current(self):
        self.store.accept([])
        s, receipt = self.save()
        self.http.files[receipt['markdown']['path']]['content'] = base64.b64encode(b'new unverified revision').decode()
        self.http.files[receipt['markdown']['path']]['sha'] = 'new'
        with self.assertRaisesRegex(Hold, 'receipt_target_changed'):
            publish(self.store, self.http, self.cfg)


if __name__ == '__main__':
    unittest.main()
