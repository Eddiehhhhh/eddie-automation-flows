import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('get_push_diagnostic',
    Path(__file__).resolve().parents[1] / 'scripts/get_push_diagnostic.py')
diagnostic = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diagnostic)


class ErrorCategories(unittest.TestCase):
    def test_only_fixed_categories_are_returned(self):
        for raw, expected in ((b'fatal: missing blob private-content', 'missing_object'),
                              (b'error: shallow update not allowed', 'shallow_history'),
                              (b'403 private-content', 'authentication_or_permission'),
                              (b'could not resolve host private-content', 'network'),
                              (b'stale info private-content', 'ref_changed'),
                              (b'private-content', 'other')):
            self.assertEqual(diagnostic.category(raw), expected)


if __name__ == '__main__':
    unittest.main()
