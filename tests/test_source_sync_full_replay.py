import contextlib
import copy
import hashlib
import io
import http.client
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import source_sync_full_replay as replay
import source_sync_inventory as inventory
import source_sync_assets as assets
import sync_getnote_to_raw as sync
from source_sync_store import write_if_changed
from verify_shadow_repeat import snapshot


def note(ident='1001', **kwargs):
    return dict(id=ident, title='PRIVATE TITLE', content='PRIVATE BODY', created_at='2020-01-01T00:00:00Z',
                updated_at='2020-01-01T00:00:00Z', **kwargs)


class FullReplayTests(unittest.TestCase):
    def seed(self, work):
        root = work/'vault'/'Raw'/'03 Get'
        root.mkdir(parents=True)
        for ident, path in [('1001','old-title.md'),('1999','not-enumerated.md'),('legacy-private','opaque.md')]:
            write_if_changed(root/path, f'---\nsource_system: getnote\nnote_id: "{ident}"\n---\nPRIVATE OLD BODY\n')
        write_if_changed(root/'missing-id.md', 'PRIVATE OLD MANUAL BODY\n')
        write_if_changed(root/'02 Index'/'manual.md', 'PRIVATE OLD MANUAL INDEX\n')
        write_if_changed(root/'_assets'/'old.bin', 'PRIVATE OLD BINARY')
        return root

    def execute(self, root, work, changed=False, pending=False, fail_assets=False):
        passes = [0]
        def api(path, params, key, client, max_response_bytes=None, response_budget=None):
            if path == sync.LIST_PATH:
                passes[0] += 1
                return {'data': {'notes': [
                    {'id':'1001', 'updated_at':'2020-01-01T00:00:00Z'},
                    {'id':'1002', 'updated_at':'2020-01-01T00:00:00Z'}], 'has_more':False}}
            value = note(params['id'])
            if params['id'] == '1001':
                value['attachments'] = [{'type':'image','url':'https://example.com/PRIVATE.png'}]
                if changed and passes[0] == 2: value['content'] += ' EDIT'
            if pending and params['id'] == '1002':
                value.update(note_type='recorder_audio', audio={})
            response_budget.consume(len(json.dumps(value).encode()))
            return {'data': {'note':value}}
        def fetch(url, **kwargs):
            if fail_assets: raise assets.AssetError('PRIVATE')
            return b'\x89PNG\r\n\x1a\nPRIVATE IMAGE', 'image/png'
        report = {'passes':[], 'real_seed_verified':True}
        with patch.object(sync, 'api_get', side_effect=api):
            replay.replay(root, work/'checkpoints', 'KEY', 'CLIENT', report, fetch)
        return report

    def test_real_baseline_two_passes_keep_all_old_paths_and_unselected_bytes(self):
        with tempfile.TemporaryDirectory() as temp:
            work=Path(temp); root=self.seed(work); before=snapshot(root)
            report=self.execute(root, work)
            self.assertEqual(report['status'],'full_replay_ok')
            self.assertEqual([p['written_or_changed'] for p in report['passes']],[2,0])
            self.assertEqual([p['downloaded_assets'] for p in report['passes']],[1,1])
            self.assertTrue(report['hashes_and_mtimes_equal'])
            self.assertEqual(report['unselected_old_markdown'],4)
            current=snapshot(root)
            for path, value in before.items():
                if path != 'old-title.md': self.assertEqual(current[path],value)
            self.assertEqual(len(list(root.rglob('*.md'))),6)
            self.assertIn('PRIVATE BODY',(root/'old-title.md').read_text())
            safe=replay.safe_report(report)
            self.assertEqual(safe['status'],'full_replay_ok'); self.assertFalse(safe['published'])
            self.assertNotIn('PRIVATE',json.dumps(safe)); self.assertNotIn('1001',json.dumps(safe))

    def test_actual_source_change_never_relaxes_exact_repeat(self):
        with tempfile.TemporaryDirectory() as temp:
            work=Path(temp); root=self.seed(work)
            with self.assertRaisesRegex(replay.ReplayFailure,'repeat_changed'):
                self.execute(root, work, changed=True)

    def test_list_detail_version_mismatch_stops_before_any_note_write(self):
        with tempfile.TemporaryDirectory() as temp:
            work=Path(temp); root=self.seed(work); before=snapshot(root)
            def api(path, params, key, client, max_response_bytes=None, response_budget=None):
                if path == sync.LIST_PATH:
                    return {'data': {'notes': [
                        {'id':'1001', 'updated_at':'2020-01-02T00:00:00Z'}],
                        'has_more':False}}
                return {'data': {'note':note('1001')}}
            report={'segments_completed':0}
            with patch.object(sync,'api_get',side_effect=api), \
                    self.assertRaisesRegex(replay.ReplayFailure,'list_detail_mismatch'):
                replay.one_pass(root, work/'checkpoint', 'KEY', 'CLIENT', report, lambda stage: None)
            self.assertEqual(snapshot(root), before)

    def test_missing_list_update_version_fails_closed(self):
        with patch.object(sync,'fetch_note_pages',return_value=[(1,[{'id':'1001'}],False)]), \
                self.assertRaisesRegex(replay.ReplayFailure,'list_detail_mismatch'):
            replay.enumerate_ids('KEY','CLIENT',inventory.ResponseBudget(),{}, {})
        safe=replay.safe_report({'error_code':'list_detail_mismatch','PRIVATE':'SECRET'})
        self.assertEqual(safe['error_code'],'list_detail_mismatch')
        self.assertNotIn('PRIVATE',json.dumps(safe))

    def test_asset_failure_keeps_old_notes_and_receipts_untouched(self):
        with tempfile.TemporaryDirectory() as temp:
            work=Path(temp); root=self.seed(work); before=snapshot(root)
            with self.assertRaises(assets.AssetError): self.execute(root,work,fail_assets=True)
            self.assertEqual(snapshot(root),before)
            self.assertFalse((root/'03 State'/'receipt.json').exists())

    def test_pending_transcript_does_not_claim_complete(self):
        with tempfile.TemporaryDirectory() as temp:
            work=Path(temp); root=self.seed(work)
            with self.assertRaisesRegex(replay.ReplayFailure,'transcript_pending'):
                self.execute(root,work,pending=True)
            self.assertFalse(json.loads((root/'03 State'/'receipt.json').read_text())['complete'])

    def test_concurrent_response_reservations_and_retried_partial_bytes_are_bounded(self):
        pool=replay.BytePool(10)
        with pool.request() as first:
            self.assertEqual(pool.reserved,10)
            with self.assertRaises(replay.ReplayFailure):
                with pool.request(): pass
            first.consume(4)
        self.assertEqual((pool.used,pool.reserved),(4,0))
        with pool.request() as second:
            self.assertEqual(second.maximum,5)
            second.consume(5)
        self.assertEqual(pool.used,9)
        with self.assertRaises(replay.ReplayFailure):
            with pool.request(): pass
        class Partial(io.BytesIO):
            def __init__(self): super().__init__(b'{')
            def read1(self, amount):
                value=super().read1(amount)
                if not value: raise ConnectionError('PRIVATE')
                return value
        opener=Mock(); opener.open.side_effect=[Partial(), io.BytesIO(b'{}')]
        budget=inventory.ResponseBudget(20)
        with patch.object(sync.urllib.request,'build_opener',return_value=opener), patch.object(sync.time,'sleep'):
            self.assertEqual(sync.api_get(sync.DETAIL_PATH,{},'KEY','CLIENT',10,budget),{})
        self.assertEqual(budget.used,3)

    def test_truncated_json_response_and_conflicting_string_alias_fail_closed(self):
        class Socket:
            def makefile(self, *args, **kwargs):
                return io.BytesIO(b'HTTP/1.1 200 OK\r\nContent-Length: 156\r\nConnection: close\r\n\r\n'
                                  b'{"data":{"notes":[],"has_more":false}}')
        response=http.client.HTTPResponse(Socket()); response.begin()
        opener=Mock(); opener.open.return_value=response
        with patch.object(sync.urllib.request,'build_opener',return_value=opener), \
                self.assertRaisesRegex(RuntimeError,'incomplete Get response'):
            sync.api_get(sync.LIST_PATH,{},'KEY','CLIENT',1024,inventory.ResponseBudget(2048))
        bad=note('1002',note_id='1001')
        with tempfile.TemporaryDirectory() as temp, patch.object(sync,'api_get',return_value={'data':bad}), \
                self.assertRaises(ValueError):
            replay.acquire_details(['1001'],'KEY','CLIENT',replay.BytePool(10*replay.MIB),Path(temp)/'cp',{'segments_completed':0})
        with patch.object(sync,'fetch_note_pages',return_value=[(1,[bad],False)]), self.assertRaises(ValueError):
            replay.enumerate_ids('KEY','CLIENT',inventory.ResponseBudget(),{})

    def test_binary_failed_attempt_consumes_same_transfer_budget(self):
        budget=replay.TransferBudget(5)
        def attempt(url, max_bytes, transfer_budget):
            transfer_budget.consume(3)
            raise assets.RetryableAssetError('PRIVATE')
        with patch.object(assets,'fetch_once',side_effect=attempt), patch.object(assets.time,'sleep'), \
                self.assertRaisesRegex(replay.ReplayFailure,'limit'):
            assets.fetch_asset('https://example.com/PRIVATE',transfer_budget=budget)
        self.assertEqual(budget.used,6)  # Mock deliberately violates read_limit; real reads never do.
        with self.assertRaises(replay.ReplayFailure): budget.read_limit(1)

    def test_limits_unsupported_collections_and_public_report_fail_closed(self):
        with patch.object(sync,'fetch_note_pages',return_value=[(1,[{'id':'1001'},{'id':'1002'}],False)]), \
                patch.object(replay,'MAX_NOTES',1), self.assertRaisesRegex(replay.ReplayFailure,'limit'):
            replay.enumerate_ids('KEY','CLIENT',inventory.ResponseBudget(),{})
        with tempfile.TemporaryDirectory() as temp, patch.object(sync,'api_get',return_value={'data':note(images=[{'url':'PRIVATE'}])}), \
                self.assertRaisesRegex(replay.ReplayFailure,'unsupported_assets'):
            replay.acquire_details(['1001'],'KEY','CLIENT',replay.BytePool(10*replay.MIB),Path(temp)/'cp',{'segments_completed':0})
        value={'status':'full_replay_ok','PRIVATE':'SECRET','published':True,'source_complete':True,'passes':[]}
        safe=replay.safe_report(value)
        self.assertEqual(safe['status'],'failed');self.assertFalse(safe['source_complete']);self.assertFalse(safe['published'])
        self.assertNotIn('PRIVATE',json.dumps(safe))


if __name__ == '__main__': unittest.main()
