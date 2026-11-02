import unittest
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import sync_tencent_docs_to_raw as sync
from sync_tencent_docs_to_raw import order_sheet_content, SyncFailure

class SheetOrderTests(unittest.TestCase):
    def test_order_is_source_order_and_keeps_contents(self):
        self.assertEqual(order_sheet_content('|B\nb\n\n|A\na', [{'sheet_name': 'A'}, {'sheet_name': 'B'}]), '|A\na\n\n|B\nb')

    def test_different_source_order_is_reflected(self):
        self.assertEqual(order_sheet_content('|A\na\n\n|B\nb', [{'sheet_name': 'B'}, {'sheet_name': 'A'}]), '|B\nb\n\n|A\na')

    def test_blank_lines_in_table_body_are_preserved(self):
        text = "|B\nrow b1\n\nrow b2\n\n|A\nrow a1\n\nrow a2"
        expected = "|A\nrow a1\n\nrow a2\n\n|B\nrow b1\n\nrow b2"
        self.assertEqual(order_sheet_content(text, [{"sheet_name": "A"}, {"sheet_name": "B"}]), expected)

    def test_regex_metacharacters_in_names(self):
        text = "|B(1)\nx\n\n|A+\ny"
        self.assertEqual(order_sheet_content(text, [{"sheet_name": "A+"}, {"sheet_name": "B(1)"}]), "|A+\ny\n\n|B(1)\nx")

    def test_ambiguous_heading_in_body_fails_closed(self):
        text = "|A\nx\n\n|B\ny\n\n|B\nz"
        with self.assertRaises(SyncFailure):
            order_sheet_content(text, [{"sheet_name": "A"}, {"sheet_name": "B"}])

    def test_incomplete_or_ambiguous_fails_closed(self):
        cases = [('|A\na', [{'sheet_name': 'A'}, {'sheet_name': 'B'}]), ('|A\na\n\n|A\na', [{'sheet_name': 'A'}, {'sheet_name': 'B'}]), ('|A\na\n\n|B\nb', [{'sheet_name': 'A'}, {'sheet_name': 'A'}]), ('', [])]
        for text, sheets in cases:
            with self.subTest(text=text), self.assertRaises(SyncFailure):
                order_sheet_content(text, sheets)

class SyncTargetRegressionTests(unittest.TestCase):
    def test_empty_sheet_uses_legacy_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = {"file_id": "id", "title": "name", "url": "https://docs.qq.com/sheet/id"}
            with patch.object(sync, "RAW_ROOT", root), patch.object(sync, "mcp_call", return_value={"content": ""}) as call:
                sync.sync_target(target, {}, "2026-11-03", "unused")
                self.assertEqual(call.call_count, 1)
                self.assertEqual(call.call_args.args[0], "get_content")

    def test_non_sheet_does_not_read_sheet_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = {"file_id": "id", "title": "name", "url": "https://docs.qq.com/doc/id"}
            with patch.object(sync, "RAW_ROOT", root), patch.object(sync, "mcp_call", return_value={"content": "body"}) as call:
                sync.sync_target(target, {}, "2026-11-03", "unused")
                self.assertEqual(call.call_count, 1)
                self.assertEqual(call.call_args.args[0], "get_content")

    def test_empty_sheet_does_not_overwrite_existing_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            previous = root / "prior.md"
            previous.write_text("---\nsource: tencent-docs\n---\n\nold body\n")
            target = {"file_id": "id", "title": "name", "url": "https://docs.qq.com/sheet/id"}
            with patch.object(sync, "RAW_ROOT", root), patch.object(sync, "mcp_call", return_value={"content": ""}):
                with self.assertRaises(SyncFailure):
                    sync.sync_target(target, {"id": previous}, "2026-11-03", "unused")
            self.assertIn("old body", previous.read_text())

if __name__ == '__main__':
    unittest.main()
