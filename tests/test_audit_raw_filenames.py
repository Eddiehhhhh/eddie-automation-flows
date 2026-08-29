import importlib.util
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "audit_raw_filenames.py"
SPEC = importlib.util.spec_from_file_location("audit_raw_filenames", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)


class AuditRawFilenamesTests(unittest.TestCase):
    def test_space_separated_timestamp_falls_back_to_source_id(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "2026-04-25-18-55-53.md"
            path.write_text('---\ntitle: "2026-04-25 18:55:53"\nmemo_id: MjMzNjAzNjE3\n---\n', encoding="utf-8")
            self.assertEqual(MODULE.expected_clean_name(path), "untitled-MjMzNjAzNjE3.md")

    def test_source_labels_are_unique(self):
        labels = [label for _, label in MODULE.SOURCE_DIRS]
        self.assertEqual(len(labels), len(set(labels)))


if __name__ == "__main__":
    unittest.main()
