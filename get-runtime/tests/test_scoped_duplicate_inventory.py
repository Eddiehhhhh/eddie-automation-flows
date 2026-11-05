import ast
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
    def fail(code):
        raise ValueError(code)
    def parse(data):
        match = re.search(rb'note_id: ([0-9]+)', data)
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
    def identity(value):
        if not isinstance(value, str) or re.fullmatch(r'[0-9]{1,64}', value) is None:
            raise ValueError('canonical_identity')
    namespace = {'fail': fail, 'engine': types.SimpleNamespace(n=types.SimpleNamespace(identity=identity))}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), 'exec'), namespace)
    return namespace['validate_single_note_scope']


class ScopedDuplicateInventory(unittest.TestCase):
    def setUp(self):
        self.inventory = inventory_function()
        self.validate_scope = scope_validator()
        self.ident = '1924184859089342232'
        self.owner = '01 语音记录/数字游民旅居社区产品方向与增长策略讨论会-' + self.ident + '.md'
        self.shadow = self.owner[:-len('-' + self.ident + '.md')] + '.md'
        self.data = ('---\nnote_id: ' + self.ident + '\n---\ntext\n').encode()

    def test_full_inventory_still_rejects_duplicate_identity(self):
        with self.assertRaisesRegex(ValueError, 'canonical_duplicate_identity'):
            self.inventory({self.owner: self.data, self.shadow: self.data})

    def test_single_note_scope_tolerates_only_registered_shadow_pair(self):
        found = self.inventory({self.owner: self.data, self.shadow: self.data},
            only_id='99999', known_owners={self.ident: {'filePath': self.owner}})
        self.assertEqual(found, {self.ident: self.owner})

    def test_pinned_tree_inventory_infers_then_registry_must_confirm_owner(self):
        files = {self.owner: self.data, self.shadow: self.data}
        self.assertEqual(self.inventory(files, only_id='99999', allow_infer=True), {self.ident: self.owner})
        with self.assertRaisesRegex(ValueError, 'canonical_duplicate_identity'):
            self.inventory(files, only_id='99999')

    def test_target_duplicate_or_unregistered_owner_fails(self):
        files = {self.owner: self.data, self.shadow: self.data}
        with self.assertRaisesRegex(ValueError, 'canonical_duplicate_identity'):
            self.inventory(files, only_id=self.ident, known_owners={self.ident: {'filePath': self.owner}})
        with self.assertRaisesRegex(ValueError, 'canonical_duplicate_identity'):
            self.inventory(files, only_id='99999')

    def test_unknown_or_extra_duplicate_path_fails(self):
        data = b'---\nnote_id: 54321\n---\ntext\n'
        with self.assertRaisesRegex(ValueError, 'canonical_duplicate_identity'):
            self.inventory({self.owner: self.data, self.shadow: self.data,
                            '01 语音记录/额外.md': self.data}, only_id='99999',
                known_owners={self.ident: {'filePath': self.owner}})
        with self.assertRaisesRegex(ValueError, 'canonical_duplicate_identity'):
            self.inventory({self.owner: self.data, self.shadow: self.data,
                            '01 Notes/two-54321.md': data, '01 Notes/two.md': data},
                only_id='99999', known_owners={self.ident: {'filePath': self.owner}})

    def test_single_note_scope_is_manual_apply_only_and_id_is_validated_early(self):
        self.validate_scope('12345', 'manual', True)
        for ident, trigger, apply in (('12345', 'schedule', True), ('12345', 'manual', False),
                                      ('not-an-id', 'manual', True)):
            with self.assertRaises(ValueError):
                self.validate_scope(ident, trigger, apply)


if __name__ == '__main__':
    unittest.main()
