"""Queue admission and one-attempt behavior for the cloud Notion controller."""
import importlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
worker = types.ModuleType('get_notion_incremental_worker')
worker.ATTEMPTS = '.sync/get-notion-incremental/attempts'
worker.OWNED = '.sync/get-notion-incremental/owned'
worker.run = lambda *args, **kwargs: None
native = types.ModuleType('get_notion_native')
native.DATA_SOURCE_PIN = '0' * 64
native.uuid = lambda value: value.replace('-', '')
native.sha = lambda value: '0' * 64
with patch.dict(sys.modules, {'get_notion_incremental_worker': worker,
                              'get_notion_native': native}):
    controller = importlib.import_module('get_notion_incremental_controller')


class QueueAdmission(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.git('init', '-q')
        self.git('config', 'user.name', 'Test')
        self.git('config', 'user.email', 'test@example.invalid')

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.root), *args],
                                       stderr=subprocess.DEVNULL).decode().strip()

    def commit_receipt(self, sequence, prior, ids):
        path = self.root / controller.RECEIPT
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'contract': 'get-canonical-sync-receipt-1',
            'sequence': sequence, 'previous_receipt_commit': prior,
            'changed_content_ids': ids}))
        self.git('add', '--', controller.RECEIPT)
        self.git('commit', '-qm', 'receipt')
        return self.git('rev-parse', 'HEAD')

    def test_seed_and_successor_enqueue_both_without_historical_scan(self):
        first = '1926106578851738352'
        second = '1926106578851738353'
        seed = self.commit_receipt(4, 'a' * 40, [first])
        with patch.object(controller, 'SEED', seed):
            before = controller.admission(self.root, None)
            self.assertEqual((before['sequence'], before['pending']), (4, [first]))
            self.commit_receipt(5, seed, [second])
            after = controller.admission(self.root, before)
        self.assertEqual((after['sequence'], after['pending']), (5, [first, second]))

    def test_gap_holds_instead_of_dropping_ids(self):
        seed = self.commit_receipt(4, 'a' * 40, ['1926106578851738352'])
        with patch.object(controller, 'SEED', seed):
            old = controller.admission(self.root, None)
            self.commit_receipt(6, seed, ['1926106578851738353'])
            with self.assertRaisesRegex(ValueError, 'receipt_chain_invalid'):
                controller.admission(self.root, old)


class AttemptSafety(unittest.TestCase):
    def plan(self):
        return {'contract': controller.CONTRACT, 'receipt_commit': 'a' * 40,
                'sequence': 4, 'pending': ['1926106578851738352']}

    def test_existing_marker_never_dispatches_second_create(self):
        ident = '1926106578851738352'
        planned = self.plan()
        with (patch.object(controller, 'head', return_value='b' * 40),
              patch.object(controller, 'git', return_value='b' * 40 + '\trefs/heads/main'),
              patch.object(controller, 'config', return_value='c' * 32),
              patch.object(controller, 'queue', return_value=None),
              patch.object(controller, 'admission', return_value=planned),
              patch.object(controller, 'save', return_value='b' * 40),
              patch.object(controller, 'committed_json', return_value={'status': 'attempt_reserved'}),
              patch.object(controller.worker, 'run', return_value={'status': 'ready',
                  'source_blob': 'd' * 64}) as write):
            result = controller.run(Path('/unused'), 'token', 1, True)
        self.assertEqual(result['held'], 1)
        write.assert_called_once()

    def test_unknown_write_keeps_queue_and_reserved_marker(self):
        planned = self.plan()
        commits = []
        def save(_root, changes, _message):
            commits.append(changes)
            return 'b' * 40
        with (patch.object(controller, 'head', return_value='b' * 40),
              patch.object(controller, 'git', return_value='b' * 40 + '\trefs/heads/main'),
              patch.object(controller, 'config', return_value='c' * 32),
              patch.object(controller, 'queue', return_value=None),
              patch.object(controller, 'admission', return_value=planned),
              patch.object(controller, 'save', side_effect=save),
              patch.object(controller, 'committed_json', return_value=None),
              patch.dict(controller.os.environ, {'GITHUB_RUN_ID': '42', 'GITHUB_RUN_ATTEMPT': '1'}),
              patch.object(controller.worker, 'run', side_effect=[
                  {'status': 'ready', 'source_blob': 'd' * 64},
                  ValueError('write_uncertain')]) as write):
            with self.assertRaisesRegex(ValueError, 'write_uncertain'):
                controller.run(Path('/unused'), 'token', 1, True)
        self.assertEqual(write.call_count, 2)
        self.assertEqual(len(commits), 2)
        self.assertEqual(commits[0][controller.QUEUE]['pending'], planned['pending'])
        self.assertIn(controller.worker.ATTEMPTS + '/' + planned['pending'][0] + '.json', commits[1])

    def test_held_prefix_rotates_and_later_id_gets_next_turn(self):
        ids = ['1926106578851738352', '1926106578851738353', '1926106578851738354']
        planned = {**self.plan(), 'pending': ids.copy()}
        saves = []
        def save(_root, changes, _message):
            saves.append(json.loads(json.dumps(changes)))
            return 'b' * 40
        with (patch.object(controller, 'head', return_value='b' * 40),
              patch.object(controller, 'git', return_value='b' * 40 + '\trefs/heads/main'),
              patch.object(controller, 'config', return_value='c' * 32),
              patch.object(controller, 'queue', return_value=None),
              patch.object(controller, 'admission', return_value=planned),
              patch.object(controller, 'save', side_effect=save),
              patch.object(controller.worker, 'run', side_effect=ValueError('recording_metadata_held')) as write):
            result = controller.run(Path('/unused'), 'token', 2, True)
        self.assertEqual((result['status'], result['held']), ('partial', 2))
        self.assertEqual(write.call_count, 2)
        self.assertEqual(saves[-1][controller.QUEUE]['pending'], [ids[2], ids[0], ids[1]])

    def test_public_error_output_never_contains_arbitrary_private_text(self):
        self.assertEqual(controller.public_reason(ValueError('private note title')),
                         'controller_failed')
        self.assertEqual(controller.public_reason(ValueError('permission_denied')),
                         'permission_denied')

    def test_dry_run_checks_notion_and_holds_only_note_scoped_errors(self):
        ids = ['1926106578851738352', '1926106578851738353']
        planned = {**self.plan(), 'pending': ids}
        with (patch.object(controller, 'head', return_value='b' * 40),
              patch.object(controller, 'git', return_value='b' * 40 + '\trefs/heads/main'),
              patch.object(controller, 'config', return_value='c' * 32),
              patch.object(controller, 'queue', return_value=None),
              patch.object(controller, 'admission', return_value=planned),
              patch.object(controller, 'save') as save,
              patch.object(controller.worker, 'run', side_effect=[
                  {'status': 'ready'}, ValueError('recording_layout_held')]) as read):
            result = controller.run(Path('/unused'), 'token', 2, False)
        self.assertEqual(result, {'status': 'dry_run_partial', 'ready': 1,
                                  'verified': 0, 'held': 1, 'pending': 2})
        self.assertEqual(read.call_count, 2)
        save.assert_not_called()

    def test_dry_run_does_not_hide_schema_or_auth_failure(self):
        with (patch.object(controller, 'head', return_value='b' * 40),
              patch.object(controller, 'git', return_value='b' * 40 + '\trefs/heads/main'),
              patch.object(controller, 'config', return_value='c' * 32),
              patch.object(controller, 'queue', return_value=None),
              patch.object(controller, 'admission', return_value=self.plan()),
              patch.object(controller.worker, 'run', side_effect=ValueError('permission_denied'))):
            with self.assertRaisesRegex(ValueError, 'permission_denied'):
                controller.run(Path('/unused'), 'token', 1, False)


if __name__ == '__main__':
    unittest.main()
