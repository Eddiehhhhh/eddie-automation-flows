"""Tencent source failures must not erase the private mirror."""
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import sync_tencent_docs_to_raw as sync


class TencentIntegrityTests(unittest.TestCase):
    def test_transport_error_shapes_fail_without_exposing_details(self):
        for response in [
            {"error": {"message": "PRIVATE_SENTINEL"}},
            {"result": {"isError": True, "content": [{"text": "PRIVATE_SENTINEL"}]}},
            {"result": {"structuredContent": {"error": "PRIVATE_SENTINEL"}}},
            {"result": {"content": [{"text": json.dumps({"error": "PRIVATE_SENTINEL"})}]}},
        ]:
            with self.subTest(response=response), mock.patch.object(
                sync.request, "urlopen", return_value=io.BytesIO(json.dumps(response).encode())
            ), self.assertRaises(RuntimeError) as error:
                sync.mcp_call("get_content", {"file_id": "synthetic"}, "synthetic")
            self.assertNotIn("PRIVATE_SENTINEL", str(error.exception))

    def test_source_failure_and_empty_content_preserve_existing_bytes(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(sync, "RAW_ROOT", Path(tmp)):
            path = Path(tmp) / "其他" / "synthetic.md"
            path.parent.mkdir()
            original = sync.render_markdown("https://example.com", "synthetic", "2000-01-01", "doc", "retained body")
            path.write_text(original)
            for payload in [{"error": "failure", "content": "wrong"}, {}, {"content": None}, {"content": 42}, {"content": ""}, {"content": "synthetic\n\n"}]:
                with self.subTest(payload=payload), mock.patch.object(sync, "mcp_call", return_value=payload), self.assertRaises(RuntimeError):
                    sync.sync_target({"file_id": "synthetic", "title": "synthetic"}, {"synthetic": path}, "2000-01-02", "synthetic")
                self.assertEqual(path.read_text(), original)

    def test_fetch_date_only_is_noop_and_real_body_change_is_written(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(sync, "RAW_ROOT", Path(tmp)):
            target = {"file_id": "synthetic", "title": "synthetic"}
            mapping = {}
            with mock.patch.object(sync, "mcp_call", return_value={"content": "body\nfetched_at: body text"}):
                self.assertTrue(sync.sync_target(target, mapping, "2000-01-01", "synthetic")[1])
                path = mapping["synthetic"]
                before, stamp = path.read_bytes(), path.stat().st_mtime_ns
                self.assertFalse(sync.sync_target(target, mapping, "2000-01-02", "synthetic")[1])
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(path.stat().st_mtime_ns, stamp)
            with mock.patch.object(sync, "mcp_call", return_value={"content": "changed body"}):
                self.assertTrue(sync.sync_target(target, mapping, "2000-01-02", "synthetic")[1])
                self.assertIn("changed body", path.read_text())

    def test_partial_batch_failure_does_not_emit_success_payload(self):
        import contextlib
        from types import SimpleNamespace
        output = io.StringIO()
        targets = {"a": {"file_id": "a"}, "b": {"file_id": "b"}}
        with mock.patch.object(sync, "parse_args", return_value=SimpleNamespace(config="synthetic")), mock.patch.object(sync, "load_config", return_value={}), mock.patch.object(sync, "existing_file_map", return_value={}), mock.patch.object(sync, "collect_targets", return_value=targets), mock.patch.object(sync, "load_token", return_value="synthetic"), mock.patch.object(sync, "sync_target", side_effect=[("其他/a.md", True, "其他"), RuntimeError("source unavailable")]), contextlib.redirect_stdout(output), self.assertRaises(RuntimeError):
            sync.main()
        self.assertEqual(output.getvalue(), "")

    def test_valid_new_empty_source_is_allowed_and_remains_noop(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(sync, "RAW_ROOT", Path(tmp)), mock.patch.object(sync, "mcp_call", return_value={"content": "", "error": None}):
            mapping = {}
            target = {"file_id": "empty", "title": "empty"}
            self.assertTrue(sync.sync_target(target, mapping, "2000-01-01", "synthetic")[1])
            self.assertFalse(sync.sync_target(target, mapping, "2000-01-02", "synthetic")[1])


if __name__ == "__main__":
    unittest.main()
