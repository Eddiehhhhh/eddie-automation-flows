import contextlib
import copy
import email.message
import io
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import urllib.request
import urllib.response

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import source_sync_sample as sample
import source_sync_assets as assets
import sync_getnote_to_raw as sync
import verify_shadow_repeat as shadow


def pair():
    return {'notes': [
        {'kind': 'ordinary', 'note_id': '1001', 'note_type': 'plain_text'},
        {'kind': 'recording', 'note_id': '2002', 'note_type': 'recorder_audio'}]}


def manifest():
    notes = [dict(n, path=('03 纯文本记录/old ordinary.md' if n['kind'] == 'ordinary'
                          else '99 录音卡笔记/old recording.md')) for n in pair()['notes']]
    return {'source_system': 'getnote', 'count': 2, 'notes': notes}


def legacy(note):
    return ('---\nsource_system: getnote\nnote_id: "' + note['note_id'] + '"\n'
            'note_type: ' + note['note_type'] + '\n---\nPRIVATE LEGACY BODY\n').encode()


def detail(note):
    result = dict(note, title='renamed title', content='PRIVATE BODY',
                  created_at='2020-01-01T00:00:00Z', updated_at='2020-01-01T00:00:00Z')
    result.pop('kind', None)
    if result['note_type'] == 'recorder_audio':
        result['audio'] = {'original': 'BEGIN' + 'x' * 44764 + 'END',
                           'play_url': 'https://example.invalid/private.m4a?sig=PRIVATE'}
        result['attachments'] = [{'type': 'audio', 'url': result['audio']['play_url']}]
    return result


class FixedSampleTests(unittest.TestCase):
    def test_pair_ids_are_strings_types_are_exact_and_duplicates_fail(self):
        self.assertEqual(len(sample.validate_pair(pair())), 2)
        bad_pairs = []
        for value in (1001, 1001.0, True, '', '1e3', '１２３', 'a/b'):
            bad = pair(); bad['notes'][0]['note_id'] = value; bad_pairs.append(bad)
        for kind in ('text', 'audio', 'other'):
            bad = pair(); bad['notes'][0]['note_type'] = kind; bad_pairs.append(bad)
        bad = pair(); bad['notes'][1]['note_id'] = '1001'; bad_pairs.append(bad)
        bad = pair(); bad['notes'].pop(); bad_pairs.append(bad)
        for bad in bad_pairs:
            with self.assertRaises(sample.FixedSampleError):
                sample.validate_pair(bad)

    def test_authoritative_string_note_id_tolerates_numeric_cache_alias_only(self):
        expected = pair()['notes'][0]
        value = detail(expected)
        value['id'] = 9999  # Synthetic rounded numeric alias, not authoritative.
        sample.validate_detail(value, expected)
        for changes in ({'note_id': 1001}, {'note_id': '9999'}, {'id': '9999'},
                        {'note_type': 'text'}, {'content': None}):
            with self.assertRaises(sample.FixedSampleError):
                sample.validate_detail(dict(value, **changes), expected)

    def test_manifest_is_bounded_deterministic_and_rejects_unsafe_paths(self):
        data = manifest()
        extra = dict(data['notes'][0], note_id='3003', path='03 纯文本记录/z.md')
        voice_folder = dict(data['notes'][1], note_id='4004', path='01 语音记录/a.md')
        data['notes'] = [voice_folder, extra] + list(reversed(data['notes'])); data['count'] = 4
        self.assertEqual([n['note_id'] for n in sample.select_manifest_pair(data)], ['1001', '2002'])
        for path in ('../escape.md', '/absolute.md', 'folder/../escape.md', 'folder\\escape.md',
                     'folder//empty.md', 'folder/a\n.md', 'folder/not-json.json'):
            bad = manifest(); bad['notes'][0]['path'] = path
            with self.assertRaises(sample.FixedSampleError):
                sample.select_manifest_pair(bad)
        for change in ({'count': 3}, {'notes': []}, {'source_system': 'other'}):
            with self.assertRaises(sample.FixedSampleError):
                sample.select_manifest_pair(dict(manifest(), **change))
        too_many = manifest(); too_many['notes'] *= 2501; too_many['count'] = len(too_many['notes'])
        with self.assertRaises(sample.FixedSampleError):
            sample.select_manifest_pair(too_many)
        duplicate = manifest(); duplicate['notes'].append(duplicate['notes'][0]); duplicate['count'] = 3
        with self.assertRaises(sample.FixedSampleError):
            sample.select_manifest_pair(duplicate)

    def test_private_seed_reads_only_five_fixed_host_resources_at_pinned_commit(self):
        source = manifest(); commit = 'a' * 40
        responses = [json.dumps({'private': True}).encode(),
                     json.dumps({'object': {'sha': commit, 'type': 'commit'}}).encode(),
                     json.dumps(source).encode(), *[legacy(n) for n in source['notes']]]
        with tempfile.TemporaryDirectory() as tmp, \
             patch.dict(os.environ, {'PRIVATE_VAULT_TOKEN': 'SYNTHETIC_SECRET'}), \
             patch.object(sample, 'github_read', side_effect=responses) as read:
            root = Path(tmp) / 'vault'; root.mkdir()
            private_pair = Path(tmp) / 'pair.json'
            seeded = sample.prepare_private_sample(root, private_pair)
            self.assertEqual(len(seeded), 2)
            self.assertEqual(sample.load_pair(private_pair), pair()['notes'])
            self.assertTrue(all(p.stat().st_mode & 0o777 == 0o600 for p in [*seeded, private_pair]))
            self.assertEqual(read.call_count, 5)
            self.assertEqual(read.call_args_list[0].args[0], '')
            self.assertEqual(read.call_args_list[1].args[0], '/git/ref/heads/main')
            self.assertTrue(all(c.args[0].endswith('?ref=' + commit) for c in read.call_args_list[2:]))
            self.assertTrue(all(c.kwargs['raw'] for c in read.call_args_list[2:]))

    def test_private_seed_rejects_public_repo_or_mismatched_frontmatter(self):
        for responses in ([b'{"private":false}'],
                          [b'{"private":true}', json.dumps({'object': {'sha': 'a'*40, 'type': 'commit'}}).encode(),
                           json.dumps(manifest()).encode(), legacy(pair()['notes'][1]),
                           legacy(pair()['notes'][1])]):
            with tempfile.TemporaryDirectory() as tmp, \
                 patch.dict(os.environ, {'PRIVATE_VAULT_TOKEN': 'SECRET'}), \
                 patch.object(sample, 'github_read', side_effect=responses):
                with self.assertRaises(sample.FixedSampleError):
                    sample.prepare_private_sample(Path(tmp), Path(tmp)/'pair.json')

    def test_github_reader_caps_bytes_and_does_not_follow_credential_redirects(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'x' * 33
        opener = MagicMock(); opener.open.return_value = response
        with patch.object(sample.urllib.request, 'build_opener', return_value=opener) as build:
            with self.assertRaises(sample.FixedSampleError):
                sample.github_read('/git/ref/heads/main', 'SECRET', 32)
            request = opener.open.call_args.args[0]
            self.assertTrue(request.full_url.startswith(sample.API_ROOT + '/'))
            self.assertTrue(any(isinstance(h, urllib.request.ProxyHandler) and h.proxies == {}
                                for h in build.call_args.args))
            self.assertTrue(any(isinstance(h, sample.NoRedirect) for h in build.call_args.args))
        class HTTPS(urllib.request.HTTPSHandler):
            def __init__(self): super().__init__(); self.calls = []
            def https_open(self, request):
                self.calls.append(request.full_url)
                headers = email.message.Message(); headers['Location'] = 'https://other.invalid/SECRET'
                result = urllib.response.addinfourl(io.BytesIO(b'PRIVATE'), headers, request.full_url, 302)
                result.msg = 'redirect'; return result
        handler = HTTPS()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), sample.NoRedirect(), handler)
        with patch.object(sample.urllib.request, 'build_opener', return_value=opener):
            with self.assertRaises(Exception):
                sample.github_read('', 'SECRET', 1024)
        self.assertEqual(handler.calls, [sample.API_ROOT])

    def test_fixed_pair_zero_lists_two_details_old_paths_original_and_repeat(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)/'vault'; raw = root/'Raw'/'03 Get'
            seed = []
            for note in manifest()['notes']:
                path = raw/note['path']; path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(legacy(note)); seed.append(path)
            private_pair = Path(tmp)/'pair.json'; private_pair.write_text(json.dumps(pair()))
            details = {n['note_id']: detail(n) for n in pair()['notes']}
            with patch.multiple(sync, ROOT=root, RAW_DIR=raw, NOTES_DIR=raw/'01 Notes', STATE_DIR=raw/'03 State'), \
                 patch.dict(os.environ, {'VAULT_ROOT': str(root)}), \
                 patch.object(sys, 'argv', ['sync', '--sample', '--sample-pair', str(private_pair)]), \
                 patch.object(sync, 'load_credentials', return_value=('key', 'client')), \
                 patch.object(sync, 'fetch_bounded_sample', side_effect=AssertionError('list discovery forbidden')) as listing, \
                 patch.object(sync, 'fetch_note_list', side_effect=AssertionError('full list forbidden')), \
                 patch.object(sync, 'fetch_note_detail', side_effect=lambda ident, *_: details[ident]) as fetch, \
                 patch.object(assets, 'fetch_asset', side_effect=AssertionError('audio download forbidden')) as download:
                output = io.StringIO()
                with contextlib.redirect_stdout(output): self.assertEqual(sync.main(), 0)
                first = json.loads(output.getvalue())
                self.assertEqual((first['sample_pages'], first['fetched']), (0, 2))
                self.assertEqual(fetch.call_count, 2); listing.assert_not_called(); download.assert_not_called()
                self.assertEqual(set(raw.rglob('*.md')), set(seed))
                recording = seed[1].read_text()
                self.assertIn(details['2002']['audio']['original'], recording)
                self.assertEqual(len(details['2002']['audio']['original']), 44772)
                self.assertIn(details['2002']['audio']['play_url'], recording)
                before = shadow.snapshot(root)
                with contextlib.redirect_stdout(io.StringIO()): self.assertEqual(sync.main(), 0)
                self.assertEqual(fetch.call_count, 4)
                self.assertEqual(before, shadow.snapshot(root))

    def test_missing_pair_and_wrong_detail_never_write(self):
        for change in ({'note_id': '9999'}, {'note_type': 'audio'}, {'note_id': 1001}):
            with tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp)/'vault'; raw=root/'Raw'/'03 Get'
                config=Path(tmp)/'pair.json'; config.write_text(json.dumps(pair()))
                details={n['note_id']:detail(n) for n in pair()['notes']}
                details['1001'].update(change)
                with patch.multiple(sync, ROOT=root, RAW_DIR=raw, NOTES_DIR=raw/'01 Notes', STATE_DIR=raw/'03 State'), \
                     patch.dict(os.environ, {'VAULT_ROOT':str(root)}), \
                     patch.object(sys,'argv',['sync','--sample','--sample-pair',str(config)]), \
                     patch.object(sync,'load_credentials',return_value=('key','client')), \
                     patch.object(sync,'fetch_note_detail',side_effect=lambda ident,*_:details[ident]):
                    out=io.StringIO()
                    with contextlib.redirect_stdout(out): self.assertEqual(sync.run_cli(),1)
                    self.assertEqual(json.loads(out.getvalue())['error_code'],'fixed_sample_invalid')
                    self.assertFalse(raw.exists())
        with self.assertRaises(sample.FixedSampleError): sample.load_pair('/nonexistent-synthetic-pair.json')

    def test_shadow_private_failure_is_redacted_and_never_launches_get(self):
        out=io.StringIO()
        with patch.object(sys,'argv',['shadow','--sample-existing']), \
             patch.object(shadow,'prepare_private_sample',side_effect=RuntimeError('SECRET private title ID URL')), \
             patch.object(shadow.subprocess,'run') as launch, contextlib.redirect_stdout(out):
            self.assertEqual(shadow.main(),1)
        launch.assert_not_called()
        report=json.loads(out.getvalue())
        self.assertEqual(report['error_code'],'private_sample_failed')
        self.assertNotIn('SECRET',out.getvalue())

    def test_shadow_strips_private_token_and_keeps_pair_fixed_with_safe_report(self):
        commands=[]
        def seed(root, config):
            paths=[]
            for note in manifest()['notes']:
                p=root/'Raw'/'03 Get'/note['path'];p.parent.mkdir(parents=True,exist_ok=True)
                p.write_bytes(legacy(note));paths.append(p)
            config.write_text(json.dumps(pair()));return paths
        def run(command, env, capture_output, text):
            self.assertNotIn('PRIVATE_VAULT_TOKEN',env)
            self.assertNotIn('1001',' '.join(command));commands.append(command[:])
            cache=Path(env['VAULT_ROOT'])/'Raw'/'03 Get'/'03 State'/'raw-cache'
            cache.mkdir(parents=True,exist_ok=True)
            for note in pair()['notes']:
                sync.write_if_changed(cache/(hashlib.sha256(note['note_id'].encode()).hexdigest()+'.json'),
                                      json.dumps(detail(note)))
            return type('Result',(),{'returncode':0,'stdout':json.dumps({'scope':'sample',
                'source_complete':False,'sample_pages':0,'fetched':2,'written_or_changed':0}),
                'stderr':'PRIVATE CONTENT'})()
        with patch.object(sys,'argv',['shadow','--sample-existing']), \
             patch.dict(os.environ,{'PRIVATE_VAULT_TOKEN':'SECRET'}), \
             patch.object(shadow,'prepare_private_sample',side_effect=seed) as prepare, \
             patch.object(shadow.subprocess,'run',side_effect=run), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(shadow.main(),0)
        prepare.assert_called_once();self.assertEqual(commands[0],commands[1])
        report=json.loads(out.getvalue())
        self.assertEqual((report['sample_pages_limit'],report['seeded_existing_notes']), (0,2))
        self.assertTrue(report['legacy_paths_preserved'])
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'result.json';p.write_text(json.dumps(dict(report, private='SECRET')))
            safe=io.StringIO();shadow.publish_safe_report(p,safe)
            self.assertNotIn('SECRET',safe.getvalue());self.assertNotIn('old ordinary',safe.getvalue())
            self.assertTrue(json.loads(safe.getvalue())['legacy_paths_preserved'])

    def test_repeat_diagnostics_distinguish_query_refresh_without_exposing_private_data(self):
        before={n['note_id']:detail(n) for n in pair()['notes']}
        after=copy.deepcopy(before)
        after['2002']['audio']['play_url']='https://example.invalid/private.m4a?sig=NEW_SECRET'
        after['2002']['fetched_at']='PRIVATE TIME'
        report=shadow.private_detail_changes(before,after)
        self.assertEqual(report['audio_query_only_refreshes'],1)
        self.assertTrue(report['content_and_original_equal'])
        self.assertEqual(report['detail_change_counts']['audio_play_url'],1)
        self.assertEqual(report['detail_change_counts']['audit_fields'],1)
        after['2002']['audio']['original']+='PRIVATE EDIT'
        after['1001']['content']='PRIVATE CONTENT EDIT'
        after['2002']['audio']['play_url']='https://other.invalid/different?sig=SECRET'
        report=shadow.private_detail_changes(before,after)
        self.assertFalse(report['content_and_original_equal'])
        self.assertEqual(report['audio_query_only_refreshes'],0)
        self.assertEqual(report['detail_change_counts']['content'],1)
        self.assertEqual(report['detail_change_counts']['audio_original'],1)
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'report.json'
            path.write_text(json.dumps(dict(report,scope='sample',status='changed-between-runs',
                                           private='SECRET')))
            out=io.StringIO();shadow.publish_safe_report(path,out)
            self.assertNotIn('PRIVATE',out.getvalue());self.assertNotIn('SECRET',out.getvalue())
            self.assertNotIn('2002',out.getvalue());self.assertNotIn('https',out.getvalue())
            self.assertEqual(json.loads(out.getvalue())['detail_change_counts'],report['detail_change_counts'])
            report['detail_change_counts']['PRIVATE INJECTED KEY']=1
            path.write_text(json.dumps(dict(report,scope='sample',status='failed')))
            out=io.StringIO();shadow.publish_safe_report(path,out)
            self.assertNotIn('detail_change_counts',json.loads(out.getvalue()))

    def test_private_snapshot_requires_two_bounded_json_objects(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);cache=root/'Raw'/'03 Get'/'03 State'/'raw-cache'
            cache.mkdir(parents=True)
            with self.assertRaises(ValueError):shadow.private_detail_snapshot(root)
            (cache/'one.json').write_text('{}');(cache/'two.json').write_text('{}')
            self.assertEqual(len(shadow.private_detail_snapshot(root)),2)
            (cache/'one.json').write_bytes(b' '*(2*1024*1024+1))
            with self.assertRaises(ValueError):shadow.private_detail_snapshot(root)

    def test_attachment_change_classes_reject_real_edits_and_redact_query_values(self):
        before={'attachments':[{'type':'audio','id':'PRIVATE','url':'https://example.invalid/a.m4a?s=SECRET'}]}
        after=copy.deepcopy(before)
        after['attachments'][0]['url']='https://example.invalid/a.m4a?s=NEW_SECRET'
        changes=shadow.attachment_changes(before,after)
        self.assertEqual(changes['url_query_only'],1);self.assertEqual(sum(changes.values()),1)
        after['attachments'][0]['url']='https://example.invalid/b.m4a?s=SECRET'
        after['attachments'][0]['id']='OTHER_PRIVATE'
        changes=shadow.attachment_changes(before,after)
        self.assertEqual(changes['url_location'],1);self.assertEqual(changes['stable_id'],1)
        after['attachments'].append({})
        self.assertEqual(shadow.attachment_changes(before,after)['added'],1)
        self.assertEqual(shadow.attachment_changes(after,before)['removed'],1)
        before={'attachments':[{'id':'ONE','url':'https://example.invalid/one'},
                               {'id':'TWO','url':'https://example.invalid/two'}]}
        changes=shadow.attachment_changes(before,{'attachments':list(reversed(before['attachments']))})
        self.assertEqual(changes['reordered'],1);self.assertEqual(sum(changes.values()),1)
        after=copy.deepcopy(before)
        after['attachments'][0].update(path='PRIVATE PATH',size=123,mime='PRIVATE MIME',
                                      name='PRIVATE NAME',updated_at='PRIVATE TIME',secret_key='PRIVATE')
        changes=shadow.attachment_changes(before,after)
        for key in ('stable_path','size','mime','name','updated_timestamp','unknown_changed_keys'):
            self.assertEqual(changes[key],1)
        self.assertNotIn('PRIVATE',json.dumps(changes));self.assertNotIn('secret_key',json.dumps(changes))

    def test_query_diagnostics_allowlist_names_only_and_classify_expiry(self):
        before = {'attachments': [{'original_url':
            'https://example.invalid/private?OSSAccessKeyId=PRIVATE&Expires=2000000000&Signature=SECRET&PRIVATE_KEY=SECRET'}]}
        after = copy.deepcopy(before)
        after['attachments'][0]['original_url'] = before['attachments'][0]['original_url'].replace('SECRET', 'NEW_SECRET')
        result = shadow.attachment_query_diagnostics(before, after, now=1900000000)
        self.assertEqual(result['query_keys_changed'], {'Signature': 1})
        self.assertEqual(result['query_refresh_counts']['unknown_keys'], 1)
        self.assertEqual(result['query_refresh_counts']['original_url'], 1)
        self.assertEqual(result['query_refresh_counts']['expiry_at_least_day'], 1)
        self.assertNotIn('PRIVATE', json.dumps(result)); self.assertNotIn('SECRET', json.dumps(result))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'report.json'
            path.write_text(json.dumps(dict(result, scope='sample', status='changed-between-runs')))
            out = io.StringIO(); shadow.publish_safe_report(path, out)
            self.assertEqual(json.loads(out.getvalue())['query_keys_changed'], {'Signature': 1})
            result['query_keys_changed']['PRIVATE_KEY'] = 1
            path.write_text(json.dumps(dict(result, scope='sample', status='changed-between-runs')))
            out = io.StringIO(); shadow.publish_safe_report(path, out)
            self.assertNotIn('query_keys_changed', json.loads(out.getvalue()))
        for expiry, expected in ((1899999999, 'expiry_expired'), (1900000030, 'expiry_under_hour'),
                                 (1900007200, 'expiry_under_day')):
            old = {'attachments': [{'url': f'https://example.invalid/a?q-sign-time=1;{expiry}&q-signature=SECRET'}]}
            new = {'attachments': [{'url': f'https://example.invalid/a?q-sign-time=1;{expiry}&q-signature=NEW'}]}
            self.assertEqual(shadow.attachment_query_diagnostics(old, new, now=1900000000)['query_refresh_counts'][expected], 1)


if __name__ == '__main__':
    unittest.main()
