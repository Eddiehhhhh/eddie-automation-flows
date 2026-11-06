"""The one-page origin probe is read-only and rejects ambiguous ownership."""
from pathlib import Path
import hashlib
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import get_notion_origin_probe as probe


PAGE = '1' * 32
NOTE_ID = '1234567890123456789'


class API:
    def __init__(self):
        self.page = {'object': 'page', 'id': PAGE, 'created_by': {'object': 'user', 'id': 'bot'},
                     'parent': {'type': 'data_source_id', 'data_source_id': '2' * 32},
                     'created_time': '2026-11-06T18:04:07Z',
                     'last_edited_time': '2026-11-06T18:04:07Z',
                     'archived': False, 'in_trash': False}
        self.calls = []

    def schema(self): return {}
    def matches(self, ident):
        self.calls.append('matches')
        return [PAGE]
    def request(self, method, path, body):
        self.calls.append((method, path))
        return self.page


class OriginProbe(unittest.TestCase):
    def setUp(self):
        self.api = API()
        source_sha = hashlib.sha256(b'body').hexdigest()
        self.attempt = {'note_id': NOTE_ID, 'source_sha256': source_sha,
                        'run_id': str(probe.RUN_ID), 'run_attempt': '1',
                        'status': 'attempt_reserved'}
        self.owned = {'note_id': NOTE_ID, 'page_id': PAGE,
                      'source_sha256': source_sha, 'target_sha256': 'b' * 64}
        self.bot = {'object': 'user', 'type': 'bot', 'id': 'bot'}
        self.run = {'id': probe.RUN_ID, 'run_attempt': 1,
                    'workflow_id': probe.WORKFLOW_ID, 'event': 'workflow_dispatch',
                    'status': 'completed', 'conclusion': 'failure',
                    'head_sha': probe.RUN_HEAD_SHA,
                    'created_at': '2026-11-06T18:03:23Z',
                    'updated_at': '2026-11-06T18:04:24Z',
                    'repository': {'full_name': probe.REPOSITORY, 'private': False},
                    'head_repository': {'full_name': probe.REPOSITORY, 'private': False}}
        def git(_root, *args, **kwargs):
            if args[0] == 'diff-tree':
                return (probe.worker.ATTEMPTS + '/' + NOTE_ID + '.json\n').encode()
            if args[:2] == ('show', '-s'):
                return b'1793988242' if args[-1] == probe.ATTEMPT_COMMIT else b'1793988251'
            return b''
        self.patches = [
            patch.object(probe, 'introduced_unchanged'),
            patch.object(probe.controller, 'head', return_value='c' * 40),
            patch.object(probe.controller, 'git', return_value='c' * 40 + '\trefs/heads/main'),
            patch.object(probe.controller, 'committed_json', side_effect=lambda root, path: self.attempt if '/attempts/' in path else self.owned),
            patch.object(probe.worker, 'git', side_effect=git),
            patch.object(probe.worker, 'source', return_value=('Raw/03 Get/note.md', b'body', {})),
            patch.object(probe.controller, 'config', return_value='2' * 32),
            patch.object(probe.transport, 'Transport', return_value=object()),
            patch.object(probe.transport, 'API', return_value=self.api),
            patch.object(probe.worker, 'desired', return_value=({}, [])),
            patch.object(probe.worker, 'verify_note', return_value='b' * 64),
            patch.object(probe, 'fixed_read', side_effect=lambda url, *_args, **_kw: self.run if 'github.com' in url else self.bot),
            patch.object(probe, 'time_of', side_effect=lambda value: {
                '2026-11-06T18:03:23Z': 1793988203,
                '2026-11-06T18:04:24Z': 1793988264,
                '2026-11-06T18:04:07Z': 1793988247}[value]),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def test_matching_creator_time_unique_id_and_hash_only_returns_safe_status(self):
        self.assertEqual(probe.assess(Path('/unused'), 'notion-token'),
                         {'status': 'origin_proof_ready'})
        self.assertTrue(all(call == 'matches' or call[0] == 'GET' for call in self.api.calls))

    def test_different_creator_cannot_promote(self):
        self.api.page['created_by']['id'] = 'someone-else'
        with self.assertRaisesRegex(ValueError, 'creator_unverified'):
            probe.assess(Path('/unused'), 'notion-token')

    def test_edited_after_owned_commit_cannot_promote(self):
        self.api.page['last_edited_time'] = '2026-11-06T18:04:12Z'
        def times(value):
            return {'2026-11-06T18:03:23Z': 1793988203,
                    '2026-11-06T18:04:24Z': 1793988264,
                    '2026-11-06T18:04:07Z': 1793988247,
                    '2026-11-06T18:04:12Z': 1793988252}[value]
        with patch.object(probe, 'time_of', side_effect=times):
            with self.assertRaisesRegex(ValueError, 'page_edited_after_receipt'):
                probe.assess(Path('/unused'), 'notion-token')

    def test_created_before_attempt_cannot_promote(self):
        self.api.page['created_time'] = '2026-11-06T18:04:01Z'
        def times(value):
            return {'2026-11-06T18:03:23Z': 1793988203,
                    '2026-11-06T18:04:24Z': 1793988264,
                    '2026-11-06T18:04:01Z': 1793988241,
                    '2026-11-06T18:04:07Z': 1793988247}[value]
        with patch.object(probe, 'time_of', side_effect=times):
            with self.assertRaisesRegex(ValueError, 'page_preexisted_attempt'):
                probe.assess(Path('/unused'), 'notion-token')

    def test_source_hash_mismatch_stops_before_notion_read(self):
        self.owned['source_sha256'] = 'd' * 64
        with self.assertRaisesRegex(ValueError, 'owned_unverified'):
            probe.assess(Path('/unused'), 'notion-token')
        self.assertEqual(self.api.calls, [])
