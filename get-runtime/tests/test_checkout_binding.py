"""The reviewed runtime must execute inside its clean, pinned Git checkout."""
import ast
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import get_private_preview_retention as retention


def fail(reason):
    raise ValueError(reason)


def selected_function(script):
    tree = ast.parse((SCRIPTS / 'get_recurring_runtime.py').read_text())
    node = next(x for x in tree.body if isinstance(x, ast.FunctionDef) and x.name == 'executed_code_sha')
    adapter = types.SimpleNamespace(OfflineGit=lambda path: types.SimpleNamespace(local=retention.LocalGit(path)),
                                    checked=lambda value, length: value if len(value) == length else fail('sha'))
    ns = {'Path': Path, '__file__': str(script), 'a': adapter, 'r': retention, 'fail': fail}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(script), 'exec'), ns)
    return ns['executed_code_sha']


class CheckoutBinding(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.script = self.root / 'get-runtime/scripts/get_recurring_runtime.py'
        self.script.parent.mkdir(parents=True); self.script.write_text('reviewed\n')
        self.git('init', '-q'); self.git('add', 'get-runtime/scripts/get_recurring_runtime.py')
        self.git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'fixture')
        self.head = self.git('rev-parse', 'HEAD').strip()

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.root), *args], text=True)

    def test_real_nested_checkout_and_dirty_rejection(self):
        self.assertEqual(selected_function(self.script)(), self.head)
        self.script.write_text('changed\n')
        with self.assertRaises(ValueError): selected_function(self.script)()

    def test_untracked_and_wrong_runtime_are_rejected(self):
        (self.root / 'extra').write_text('x')
        with self.assertRaises(ValueError): selected_function(self.script)()
        (self.root / 'extra').unlink()
        with self.assertRaises(Exception): selected_function(self.root / 'other/scripts/get_recurring_runtime.py')()

    def test_git_pointer_is_rejected(self):
        other = self.root / 'other'; other.mkdir()
        (other / '.git').write_text('gitdir: ../.git\n')
        runtime = other / 'get-runtime/scripts'; runtime.mkdir(parents=True)
        script = runtime / 'get_recurring_runtime.py'; script.write_text('x')
        with self.assertRaises(Exception): selected_function(script)()

if __name__ == '__main__': unittest.main()
