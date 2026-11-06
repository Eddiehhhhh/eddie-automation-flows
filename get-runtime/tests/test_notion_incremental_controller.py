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

    def test_exact_adoption_dry_run_does_not_write(self):
        ident = self.plan()['pending'][0]
        old = {'note_id': ident, 'page_id': '1' * 32,
               'source_sha256': 'a' * 64, 'target_sha256': 'b' * 64}
        candidate = {**old, 'origin': 'adopted', 'adoption': {'contract': 'test'}}
        with (patch.object(controller, 'head', return_value='b' * 40),
              patch.object(controller, 'git', return_value='b' * 40 + '\trefs/heads/main'),
              patch.object(controller, 'config', return_value='c' * 32),
              patch.object(controller, 'committed_json', side_effect=[old, None]),
              patch.object(controller.worker, 'adopt', return_value=candidate, create=True),
              patch.object(controller, 'save') as save):
            result = controller.adopt_one(Path('/unused'), 'token', ident, False)
        self.assertEqual(result['status'], 'adoption_ready')
        save.assert_not_called()

    def test_adoption_with_uncertain_update_journal_stops(self):
        ident = self.plan()['pending'][0]
        with (patch.object(controller, 'head', return_value='b' * 40),
              patch.object(controller, 'git', return_value='b' * 40 + '\trefs/heads/main'),
              patch.object(controller, 'config', return_value='c' * 32),
              patch.object(controller, 'committed_json', side_effect=[{}, {'phase': 'append_in_flight'}]),
              patch.object(controller.worker, 'adopt', create=True) as adopt,
              patch.object(controller, 'save') as save):
            with self.assertRaisesRegex(ValueError, 'unowned_target'):
                controller.adopt_one(Path('/unused'), 'token', ident, True)
        adopt.assert_not_called()
        save.assert_not_called()

    def test_exact_adoption_writes_only_one_owned_receipt_and_reads_back(self):
        ident = self.plan()['pending'][0]
        old = {'note_id': ident, 'page_id': '1' * 32,
               'source_sha256': 'a' * 64, 'target_sha256': 'b' * 64}
        candidate = {**old, 'origin': 'adopted', 'adoption': {'contract': 'test'}}
        writes = []
        with (patch.object(controller, 'head', return_value='b' * 40),
              patch.object(controller, 'git', return_value='b' * 40 + '\trefs/heads/main'),
              patch.object(controller, 'config', return_value='c' * 32),
              patch.object(controller, 'committed_json', side_effect=[old, None]),
              patch.object(controller.worker, 'adopt', return_value=candidate, create=True),
              patch.object(controller.worker, 'run', return_value={'status': 'already_present', 'owned': candidate}),
              patch.object(controller, 'save', side_effect=lambda _root, changes, _message: writes.append(changes))):
            result = controller.adopt_one(Path('/unused'), 'token', ident, True)
        self.assertEqual(result['status'], 'adopted')
        self.assertEqual(writes, [{controller.OWNED + '/' + ident + '.json': candidate}])

    def test_private_attempt_commit_selects_one_id_without_public_input(self):
        ident = self.plan()['pending'][0]
        path = controller.worker.ATTEMPTS + '/' + ident + '.json'
        def git(_root, *args):
            if args[0] == 'diff-tree':
                return path
            if args[0] == 'cat-file':
                raise subprocess.CalledProcessError(1, args)
            return ''
        with patch.object(controller, 'git', side_effect=git), \
                patch.object(controller, 'committed_json', return_value={'note_id': ident}):
            self.assertEqual(controller.note_from_attempt_commit(Path('/unused'), 'a' * 40), ident)
        with patch.object(controller, 'git', side_effect=lambda _root, *args:
                          path + '\n' + path if args[0] == 'diff-tree' else ''):
            with self.assertRaisesRegex(ValueError, 'unowned_target'):
                controller.note_from_attempt_commit(Path('/unused'), 'a' * 40)

    def test_existing_page_receipt_uses_controller_owned_path(self):
        planned = self.plan()
        owned = {'note_id': planned['pending'][0], 'page_id': '1' * 32,
                 'source_sha256': 'a' * 64, 'target_sha256': 'b' * 64}
        saves = []
        with (patch.object(controller, 'head', return_value='b' * 40),
              patch.object(controller, 'git', return_value='b' * 40 + '\trefs/heads/main'),
              patch.object(controller, 'config', return_value='c' * 32),
              patch.object(controller, 'queue', return_value=None),
              patch.object(controller, 'admission', return_value=planned),
              patch.object(controller, 'save', side_effect=lambda _root, changes, _message: saves.append(changes) or 'b' * 40),
              patch.object(controller.worker, 'run', return_value={'status': 'already_present',
                  'page_id': '1' * 32, 'owned': owned})):
            result = controller.run(Path('/unused'), 'token', 1, True)
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(saves[-1][controller.OWNED + '/' + owned['note_id'] + '.json'], owned)
        self.assertEqual(saves[-1][controller.QUEUE]['pending'], [])

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
                                  'verified': 0, 'held': 1, 'pending': 2,
                                  'held_reasons': {'recording_layout_held': 1}})
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
