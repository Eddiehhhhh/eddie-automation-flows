"""Synthetic regression checks for public snapshots and private result routing."""
import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import sync_tencent_docs_to_raw as tencent


class PublicStateBoundaryTests(unittest.TestCase):
    def test_public_snapshots_have_allowlisted_aggregate_schema(self):
        state = ROOT / ".github" / "state"
        value = json.loads((state / "tencent-docs-sync-last-run.json").read_text())
        self.assertEqual(set(value), {"schema_version", "content_scope", "synced_at",
                                    "fetched_at", "target_count", "written_or_changed"})
        self.assertEqual(value["schema_version"], 1)
        self.assertEqual(value["content_scope"], "aggregate_metadata_only")
        self.assertRegex(value["synced_at"], r"^[0-9T:.+Z-]+$")
        self.assertIsNotNone(datetime.fromisoformat(value["synced_at"].replace("Z", "+00:00")).tzinfo)
        date.fromisoformat(value["fetched_at"])
        for key in ("target_count", "written_or_changed"):
            self.assertIs(type(value[key]), int)
            self.assertGreaterEqual(value[key], 0)
        self.assertLessEqual(value["written_or_changed"], value["target_count"])
        for mode in ("daily", "weekly"):
            value = json.loads((state / f"iteration-{mode}-last-run.json").read_text())
            self.assertEqual(set(value), {"schema_version", "content_scope", "generated_at",
                                        "mode", "period", "discovery_counts", "audit_counts", "closeout_counts"})
            self.assertEqual(value["schema_version"], 1)
            self.assertEqual(value["content_scope"], "aggregate_metadata_only")
            self.assertEqual(value["mode"], mode)
            self.assertEqual(set(value["period"]), {"target_date", "window_start", "window_end", "label"})
            for key in ("target_date", "window_start", "window_end"):
                date.fromisoformat(value["period"][key])
            self.assertRegex(value["period"]["label"], r"^\d{4}-(?:W\d{2}|\d{2}-\d{2})$")
            for key in ("discovery_counts", "audit_counts", "closeout_counts"):
                self.assertTrue(all(type(v) is int and v >= 0 for v in value[key].values()))

    def test_tencent_workflow_preserves_private_payload_and_public_counts_only(self):
        workflow = (ROOT / ".github" / "workflows" / "sync-tencent-docs.yml").read_text()
        self.assertIn("VAULT_ROOT: ${{ github.workspace }}/vault", workflow)
        self.assertIn("repository: Eddiehhhhh/eddie-llm-wiki", workflow)
        self.assertIn("--stdout-file /tmp/tencent-docs-sync-result.json -- python scripts/sync_tencent_docs_to_raw.py", workflow)
        self.assertIn('python scripts/commit_private_vault.py --message "Sync Tencent Docs to Raw"', workflow)
        self.assertIn('--path "Raw/06 TencentDocs" --path ".github/state/tencent-docs-sync-last-run.json"', workflow)
        step = workflow.split("      - name: Summarize sync result\n", 1)[1].split("      - name:", 1)[0]
        script = textwrap.dedent(step.split("python - <<'PY'\n", 1)[1].rsplit("          PY", 1)[0])
        self.assertEqual(script.count("/tmp/tencent-docs-sync-result.json"), 1)
        sentinel = "SYNTHETIC_PRIVATE_SENTINEL"
        payload = {"synced_at": "2000-01-01T00:00:00Z", "fetched_at": "2000-01-01",
                   "target_count": 1, "written_or_changed": 1,
                   "folders": {sentinel: 1}, "files": [{"file_id": sentinel,
                   "relative_path": sentinel + ".md", "folder": sentinel, "changed": True}]}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "synthetic-result.json"
            source.write_text(json.dumps(payload))
            public = root / ".github" / "state" / "tencent-docs-sync-last-run.json"
            public.parent.mkdir(parents=True)
            public.write_text('{"synthetic_public_snapshot":true}\n')
            before = public.read_bytes()
            summary = root / "summary.md"
            result = subprocess.run([sys.executable, "-c", script.replace("/tmp/tencent-docs-sync-result.json", str(source))],
                                    cwd=root, env={**os.environ, "GITHUB_STEP_SUMMARY": str(summary)},
                                    capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertEqual(public.read_bytes(), before)
            private = root / "vault" / ".github" / "state" / "tencent-docs-sync-last-run.json"
            self.assertEqual(json.loads(private.read_text()), payload)
            self.assertEqual(summary.read_text(), "## Tencent Docs sync result\n- target_count: 1\n- written_or_changed: 1\n")
            self.assertNotIn(sentinel, summary.read_text() + result.stdout + result.stderr)

    def test_tencent_identity_map_uses_private_documents_not_public_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = root / "vault" / "Raw" / "06 TencentDocs"
            raw.mkdir(parents=True)
            source = raw / "synthetic.md"
            source.write_text('---\nfile_id: "synthetic-document"\n---\nSYNTHETIC BODY\n')
            public = root / ".github" / "state" / "tencent-docs-sync-last-run.json"
            public.parent.mkdir(parents=True)
            public.write_text("not valid JSON: this public snapshot is not an input")
            with mock.patch.object(tencent, "RAW_ROOT", raw):
                self.assertEqual(tencent.existing_file_map(), {"synthetic-document": source})


if __name__ == "__main__":
    unittest.main()
