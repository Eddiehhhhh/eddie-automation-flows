import ast
from copy import deepcopy
import hashlib
from pathlib import Path
import re
import types
import unittest


def inventory_function():
    path = Path(__file__).resolve().parents[1] / 'scripts/get_canonical_writer.py'
    tree = ast.parse(path.read_text())
    node = next(x for x in tree.body if isinstance(x, ast.FunctionDef) and x.name == 'inventory')
    allowlist = next(ast.literal_eval(x.value.args[0]) for x in tree.body
        if isinstance(x, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'SCOPED_DUPLICATE_IDS' for t in x.targets))
    case_duplicates = next(ast.literal_eval(x.value) for x in tree.body
        if isinstance(x, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'SCOPED_CASE_DUPLICATES' for t in x.targets))
    def fail(code):
        raise ValueError(code)
    def parse(data):
        match = re.search(rb'note_id: ([0-9a-fA-F]+)', data)
        return ({'note_id': match.group(1).decode()} if match else {}), None
    namespace = {
        'legacy': types.SimpleNamespace(path_ok=lambda value: value,
            owned=types.SimpleNamespace(MAX_NOTE=1024)),
        'legacy_document': parse,
        'inventory_identity': lambda value: value,
        'api_identity': lambda value: True,
        'owner_budget': lambda values: len(values) <= 5000,
        'is_markdown': lambda value: value.endswith('.md'),
        'SCOPED_DUPLICATE_IDS': allowlist,
        'SCOPED_CASE_DUPLICATES': case_duplicates,
        'hashlib': hashlib,
        'fail': fail,
    }
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), 'exec'), namespace)
    return namespace['inventory']


def scope_validator():
    path = Path(__file__).resolve().parents[1] / 'scripts/get_recurring_runtime.py'
    tree = ast.parse(path.read_text())
    node = next(x for x in tree.body if isinstance(x, ast.FunctionDef) and x.name == 'validate_single_note_scope')
    def fail(code):
        raise ValueError(code)
    def fail_scope(check):
        fail('runtime_scope')
    def identity(value):
        if not isinstance(value, str) or re.fullmatch(r'[0-9]{1,64}', value) is None:
            raise ValueError('canonical_identity')
    namespace = {'fail': fail, 'fail_scope': fail_scope,
                 'engine': types.SimpleNamespace(n=types.SimpleNamespace(identity=identity))}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), 'exec'), namespace)
    return namespace['validate_single_note_scope']


def initial_registry_function(inventory):
    path = Path(__file__).resolve().parents[1] / 'scripts/get_canonical_writer.py'
    tree = ast.parse(path.read_text())
    node = next(x for x in tree.body if isinstance(x, ast.FunctionDef) and x.name == 'initial_registry')
    namespace = {'legacy': types.SimpleNamespace(validate_state=lambda state, sha, evidence: state),
                 'inventory': inventory, 'deepcopy': deepcopy, 'CONTRACT': 'test', 'fail': ValueError}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), 'exec'), namespace)
    return namespace['initial_registry']


def validate_registry_function(inventory):
    path = Path(__file__).resolve().parents[1] / 'scripts/get_canonical_writer.py'
    tree = ast.parse(path.read_text())
    node = next(x for x in tree.body if isinstance(x, ast.FunctionDef) and x.name == 'validate_registry')
    ident = '1924184859089342232'
    def fail(code):
        raise ValueError(code)
    def fake_initial(evidence, legacy_state, legacy_sha, current, *, only_id=None, known_owners=None):
        return {'contract': 'test', 'local_proposal_only': True, 'apply_ready': False,
                'capture_anchor': evidence.anchor, 'legacy_state_sha256': legacy_sha,
                'reserved': {}, 'newById': {}, 'conflictsById': {}, 'scan_cursor': None}
    def fake_document(blob):
        match = re.search(rb'note_id: ([0-9]+)', blob)
        return ({'note_id': match.group(1).decode()} if match else {}), None
    namespace = {
        'legacy': types.SimpleNamespace(sha=lambda value: 'state-sha' if value == b'state' else hashlib.sha256(value).hexdigest(),
            encoded=lambda value: b'state', checked=lambda value, width: value,
            document=None),
        'raw': types.SimpleNamespace(_raw_valid=lambda value: True, SOURCE_TEXT_FIELDS=set()),
        'identity': lambda value: value, 'path_ok': lambda value: value,
        'inventory': lambda current, **kwargs: {ident: None},
        'initial_registry': fake_initial, 'legacy_document': fake_document,
        'validate_extensions': lambda *args, **kwargs: None,
        'owner_budget': lambda value: True, 'deepcopy': deepcopy,
        'CONTRACT': 'test', 'MAX_STATE': 100000, 'fail': fail,
    }
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), 'exec'), namespace)
    return namespace['validate_registry'], ident


class ScopedDuplicateInventory(unittest.TestCase):
    def setUp(self):
        self.inventory = inventory_function()
        self.validate_scope = scope_validator()
        self.ident = '1924184859089342232'
        self.owner = '01 语音记录/数字游民旅居社区产品方向与增长策略讨论会-' + self.ident + '.md'
        self.shadow = self.owner[:-len('-' + self.ident + '.md')] + '.md'
        self.data = ('---\nnote_id: ' + self.ident + '\n---\ntext\n').encode()
        self.case_ident = 'f5536f23631233577ac2a37f15fa0e09'
        self.case_owner = '03 纯文本记录/Untitled.md'
        self.case_shadow = '03 纯文本记录/untitled.md'
        self.case_data = ('---\nnote_id: ' + self.case_ident + '\n---\ntext\n').encode()
        blob = hashlib.sha1(b'blob ' + str(len(self.case_data)).encode() + b'\0' + self.case_data).hexdigest()
        self.inventory.__globals__['SCOPED_CASE_DUPLICATES'][self.case_ident] = (
            self.case_owner, self.case_shadow, blob, hashlib.sha256(self.case_data).hexdigest())

    def test_full_inventory_still_rejects_duplicate_identity(self):
        with self.assertRaisesRegex(ValueError, 'canonical_duplicate_identity'):
            self.inventory({self.owner: self.data, self.shadow: self.data})

    def test_single_note_scope_blocks_unrelated_duplicate_owner(self):
        found = self.inventory({self.owner: self.data, self.shadow: self.data},
            only_id='99999', known_owners={self.ident: {'filePath': self.owner}})
        self.assertEqual(found, {self.ident: None})

    def test_single_note_scope_does_not_infer_unrelated_duplicate_owner(self):
        files = {self.owner: self.data, self.shadow: self.data}
        self.assertEqual(self.inventory(files, only_id='99999', allow_infer=True), {self.ident: None})

    def test_target_duplicate_or_unregistered_owner_fails(self):
        files = {self.owner: self.data, self.shadow: self.data}
        with self.assertRaisesRegex(ValueError, 'canonical_duplicate_identity'):
            self.inventory(files, only_id=self.ident, known_owners={self.ident: {'filePath': self.owner}})
        self.assertEqual(self.inventory(files, only_id='99999'), {self.ident: None})

    def test_unrelated_content_conflict_is_ambiguous_only_in_single_note_scope(self):
        other = '54321'
        first = '03 纯文本记录/原稿-'+other+'.md'
        second = '03 纯文本记录/另一稿-'+other+'.md'
        files = {first: b'---\nnote_id: '+other.encode()+b'\n---\nA',
                 second: b'---\nnote_id: '+other.encode()+b'\n---\nB'}
        with self.assertRaisesRegex(ValueError, 'canonical_duplicate_identity'):
            self.inventory(files)
        self.assertEqual(self.inventory(files, only_id='99999'), {other: None})
        with self.assertRaisesRegex(ValueError, 'canonical_duplicate_identity'):
            self.inventory(files, only_id=other)
        make_registry = initial_registry_function(self.inventory)
        legacy_state = {'conflictsById': {}, 'legacyById': {}, 'pending_ids': []}
        evidence = types.SimpleNamespace(anchor={'commit': 'anchor'})
        state = make_registry(evidence, legacy_state, 'sha', files, only_id='99999')
        self.assertEqual(state['reserved'], {other: 'blocked'})

    def test_registered_owner_in_unrelated_conflict_must_stay_byte_exact(self):
        validate, ident = validate_registry_function(self.inventory)
        owner = '01 语音记录/原稿-'+ident+'.md'
        shadow = '01 语音记录/原稿.md'
        owner_data = ('---\nnote_id: '+ident+'\n---\nowner').encode()
        other_data = ('---\nnote_id: '+ident+'\n---\nother').encode()
        digest=hashlib.sha256(owner_data).hexdigest()
        row = {'filePath': owner, 'mirror_sha256': digest, 'baseline_blob': 'b'*40,
               'source_version': 'v', 'summary_sha256': 'c'*64, 'source_sha256': 'd'*64,
               'unknown_sha256': 'e'*64, 'text_fields': [], 'created_at': 'v',
               'preview_anchor': None, 'source_metadata': {}}
        state = {'contract': 'test', 'local_proposal_only': True, 'apply_ready': False,
                 'capture_anchor': {'commit': 'anchor'}, 'legacy_state_sha256': 'f'*64,
                 'newById': {ident: row}, 'reserved': {}, 'conflictsById': {}, 'scan_cursor': None}
        evidence = types.SimpleNamespace(anchor={'commit': 'anchor'}, census={'eligible': {}})
        legacy_state = {'legacyById': {}, 'conflictsById': {}, 'pending_ids': []}
        self.assertEqual(validate(state, 'state-sha', evidence, legacy_state, 'f'*64,
                                  {owner: owner_data, shadow: other_data}, only_id='99999'), state)
        for bad_path, bad_data in ((owner+'-changed', owner_data),
                                   (owner, owner_data.replace(b'owner', b'changed')),
                                   (owner, owner_data.replace(ident.encode(), b'12345'))):
            with self.subTest(path=bad_path, data_changed=bad_data != owner_data), self.assertRaisesRegex(ValueError, 'canonical_duplicate_path'):
                bad_row = {**row, 'filePath': bad_path}
                validate({**state, 'newById': {ident: bad_row}}, 'state-sha', evidence,
                         legacy_state, 'f'*64, {owner: bad_data, shadow: other_data}, only_id='99999')

    def test_unknown_or_extra_duplicate_paths_remain_ambiguous_in_single_scope(self):
        data = b'---\nnote_id: 54321\n---\ntext\n'
        first={self.owner: self.data, self.shadow: self.data, '01 语音记录/额外.md': self.data}
        with self.assertRaisesRegex(ValueError, 'canonical_duplicate_identity'):
            self.inventory(first)
        self.assertEqual(self.inventory(first, only_id='99999'), {self.ident: None})
        second={'01 Notes/two-54321.md': data, '01 Notes/two.md': data}
        with self.assertRaisesRegex(ValueError, 'canonical_duplicate_identity'):
            self.inventory(second)
        self.assertEqual(self.inventory(second, only_id='99999'), {'54321': None})

    def test_case_only_duplicate_is_allowed_only_for_single_note_and_exact_bytes(self):
        files = {self.case_owner: self.case_data, self.case_shadow: self.case_data}
        with self.assertRaisesRegex(ValueError, 'canonical_duplicate_identity'):
            self.inventory(files)
        self.assertEqual(self.inventory(files, only_id='99999'), {self.case_ident: self.case_owner})
        with self.assertRaisesRegex(ValueError, 'canonical_duplicate_identity'):
            self.inventory(files, only_id=self.case_ident)
        self.assertEqual(self.inventory({self.case_owner: self.case_data, self.case_shadow: self.case_data + b'changed'}, only_id='99999'), {self.case_ident: None})
        self.assertEqual(self.inventory({**files, '03 纯文本记录/third.md': self.case_data}, only_id='99999'), {self.case_ident: None})

    def test_case_only_duplicate_paths_and_hashes_stay_pinned(self):
        tree = ast.parse((Path(__file__).resolve().parents[1] / 'scripts/get_canonical_writer.py').read_text())
        pinned = next(ast.literal_eval(x.value) for x in tree.body
            if isinstance(x, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'SCOPED_CASE_DUPLICATES' for t in x.targets))
        self.assertEqual(pinned, {self.case_ident: (self.case_owner, self.case_shadow,
            '7b147f377c1137c45612e2ed9f828d3150aba1ce',
            '07e67e4d9a54928d56cfc75e9a9de2b00fc2e70a0f8e1127e9574533745583c4')})

    def test_single_note_scope_is_manual_apply_only_and_id_is_validated_early(self):
        self.validate_scope('12345', 'manual', True)
        for ident, trigger, apply in (('12345', 'schedule', True), ('12345', 'manual', False),
                                      ('not-an-id', 'manual', True)):
            with self.assertRaises(ValueError):
                self.validate_scope(ident, trigger, apply)


if __name__ == '__main__':
    unittest.main()
