import ast
from pathlib import Path
import re
import types
import unittest


def selected_function():
    path = Path(__file__).resolve().parents[1] / 'scripts/get_canonical_manual.py'
    tree = ast.parse(path.read_text())
    node = next(x for x in tree.body if isinstance(x, ast.FunctionDef) and x.name == 'select_new')
    def identity(value):
        if not isinstance(value, str) or re.fullmatch(r'[0-9]{1,64}', value) is None:
            raise ValueError('canonical_identity')
    def fail(code):
        raise ValueError(code)
    namespace = {'n': types.SimpleNamespace(identity=identity),
                 'm': types.SimpleNamespace(CANONICAL_COUNT=22, fail=fail), 'MAX_NEW': 50}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), 'exec'), namespace)
    return namespace['select_new']


def scope_checker():
    path = Path(__file__).resolve().parents[1] / 'scripts/get_canonical_manual.py'
    tree = ast.parse(path.read_text())
    node = next(x for x in tree.body if isinstance(x, ast.FunctionDef) and x.name == 'verify_single_scope')
    def identity(value):
        if not isinstance(value, str) or re.fullmatch(r'[0-9]{1,64}', value) is None:
            raise ValueError('canonical_identity')
    def fail(code):
        raise ValueError(code)
    namespace = {'n': types.SimpleNamespace(identity=identity, STATE='03 State/canonical-registry.json'),
                 'a': types.SimpleNamespace(STATE='03 State/legacy-reconciliation-state.json'),
                 'r': types.SimpleNamespace(GET='Raw/03 Get', safe_path=lambda value: value),
                 'm': types.SimpleNamespace(fail=fail)}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), 'exec'), namespace)
    return namespace['verify_single_scope']


def notion_scope_selector():
    path = Path(__file__).resolve().parents[1] / 'scripts/get_notion_outbox.py'
    tree = ast.parse(path.read_text())
    nodes = [x for x in tree.body if isinstance(x, ast.FunctionDef) and x.name in ('single_note_selection', 'single_note_delivery', 'single_note_records_allowed')]
    def identity(value):
        if not isinstance(value, str) or re.fullmatch(r'[0-9]{1,64}', value) is None:
            raise ValueError('canonical_identity')
    def fail(code):
        raise ValueError(code)
    namespace = {'p': types.SimpleNamespace(note_id=identity), 'fail': fail}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), 'exec'), namespace)
    return namespace['single_note_selection'], namespace['single_note_delivery'], namespace['single_note_records_allowed']


class SingleNoteSelection(unittest.TestCase):
    def setUp(self):
        self.select_new = selected_function()
        self.verify_scope = scope_checker()
        self.select_notion, self.notion_delivery, self.single_records = notion_scope_selector()
        self.registry = {'reserved': {'111': 'legacy'}, 'newById': {'222': {}},
                         'conflictsById': {}, 'scan_cursor': None}

    def test_new_id_selects_only_that_id(self):
        selected, remaining = self.select_new(
            {'111': {}, '222': {}, '333': {}, '444': {}}, self.registry, None,
            source_id='333', legacy_ids={'111'})
        self.assertEqual(selected, ['333'])
        self.assertEqual(remaining, 1)

    def test_existing_id_is_not_recreated(self):
        selected, _ = self.select_new(
            {'111': {}, '222': {}, '333': {}}, self.registry, None,
            source_id='222', legacy_ids={'111'})
        self.assertEqual(selected, [])

    def test_legacy_id_is_not_recreated(self):
        selected, _ = self.select_new(
            {'111': {}, '222': {}, '333': {}}, self.registry, None,
            source_id='111', legacy_ids={'111'})
        self.assertEqual(selected, [])

    def test_missing_conflicted_and_invalid_ids_fail_closed(self):
        with self.assertRaises(ValueError):
            self.select_new({}, self.registry, None, source_id='333')
        conflicted = {**self.registry, 'conflictsById': {'333': 'detail_required'}}
        with self.assertRaises(ValueError):
            self.select_new({'333': {}}, conflicted, None, source_id='333')
        with self.assertRaises(ValueError):
            self.select_new({'not-an-id': {}}, self.registry, None, source_id='not-an-id')

    def test_status_only_change_is_valid(self):
        self.verify_scope({'single_source_id': '333', 'single_source_path': 'Raw/03 Get/01 Notes/333.md',
                           'source_scope': 'single_note', 'catalog_complete': False,
                           'changes': [{'path': 'Raw/03 Get/03 State/canonical-registry.json'}]})

    def test_only_the_selected_note_path_is_valid(self):
        path = 'Raw/03 Get/01 Notes/333.md'
        self.verify_scope({'single_source_id': '333', 'single_source_path': path,
                           'source_scope': 'single_note', 'catalog_complete': False,
                           'changes': [{'path': path}, {'path': 'Raw/03 Get/03 State/canonical-registry.json'}]})

    def test_unrelated_note_and_missing_path_fail_closed(self):
        selected = 'Raw/03 Get/01 Notes/333.md'
        unrelated = 'Raw/03 Get/01 Notes/444.md'
        with self.assertRaises(ValueError):
            self.verify_scope({'single_source_id': '333', 'single_source_path': selected,
                               'source_scope': 'single_note', 'catalog_complete': False,
                               'changes': [{'path': unrelated}]})
        with self.assertRaises(ValueError):
            self.verify_scope({'single_source_id': '333', 'source_scope': 'single_note',
                               'catalog_complete': False, 'changes': []})

    def test_notion_selects_only_target_from_current_publication(self):
        state = {'engine_state': {'journal': None},
                 'pending_by_id': {'222': {'sequence': 8, 'commit': 'current', 'receipt_sha256': 'proof'},
                                   '333': {'sequence': 7, 'commit': 'older', 'receipt_sha256': 'old-proof'}},
                 'observed_publication_tip': {'sequence': 8, 'commit': 'current', 'receipt_sha256': 'proof'}}
        self.assertEqual(self.select_notion(state, '222'), ['222'])
        self.assertEqual(self.select_notion(state, '444'), [])
        already_done = {**state, 'pending_by_id': {}, 'rotation_order': [],
                        'engine_state': {'journal': None, 'receipts': {'222': {}}}}
        self.assertEqual(self.notion_delivery(already_done, '222'), [])
        with self.assertRaises(ValueError):
            self.notion_delivery(state, '444')
        with self.assertRaises(ValueError):
            self.select_notion(state, '333')

    def test_notion_resumes_only_exact_single_note_journal(self):
        state = {'engine_state': {'journal': {'plan': {'publication': {'records': {'222': {}},
                    'commit': 'current', 'outbox_receipt_sha256': 'proof'}}}},
                 'pending_by_id': {'222': {'sequence': 8, 'commit': 'current', 'receipt_sha256': 'proof'}},
                 'observed_publication_tip': {'sequence': 8, 'commit': 'current', 'receipt_sha256': 'proof'}}
        self.assertEqual(self.select_notion(state, '222', resume=True), ['222'])
        stale = {**state, 'pending_by_id': {'222': {'sequence': 7, 'commit': 'current', 'receipt_sha256': 'proof'}}}
        with self.assertRaises(ValueError):
            self.select_notion(stale, '222', resume=True)
        state['engine_state']['journal']['plan']['publication']['records']['333'] = {}
        with self.assertRaises(ValueError):
            self.select_notion(state, '222', resume=True)
        with self.assertRaises(ValueError):
            self.select_notion(state, '222')

    def test_single_note_outbox_rejects_sibling_ids_before_state_write(self):
        self.single_records({'222': {}}, '222')
        self.single_records({}, '222')
        with self.assertRaises(ValueError):
            self.single_records({'222': {}, '333': {}}, '222')


if __name__ == '__main__':
    unittest.main()
