import hashlib
import sys
import tempfile
from datetime import date
from pathlib import Path
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import iteration_source_discovery as discovery
import iteration_incremental_audit as audit
import cloud_weekly_bundle as weekly


class ArchiveOnlySourceTests(unittest.TestCase):
    def test_retired_source_is_visible_but_not_an_active_candidate_or_gap(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry = root / "Schema/来源目录注册.md"
            registry.parent.mkdir()
            registry.write_text(
                "- `Raw/03 Get/` — active\n"
                "- `Raw/15 微信读书/` — source_status=archive-only（source_retired）\n",
                encoding="utf-8",
            )
            active = root / "Raw/03 Get/2026-10-11-active.md"
            archived = root / "Raw/15 微信读书/2026-10-11-history.md"
            for item in (active, archived):
                item.parent.mkdir(parents=True)
                item.write_text("---\ndate: 2026-10-11\n---\nsource text\n", encoding="utf-8")
            with mock.patch.object(discovery, "ROOT", root), mock.patch.object(discovery, "RAW_DIR", root / "Raw"), \
                 mock.patch.object(discovery, "REGISTRY", registry), \
                 mock.patch.object(discovery, "DEFAULT_OUTPUT", root / "missing-snapshot.json"), \
                 mock.patch.object(audit, "ROOT", root):
                found = discovery.discover()
                self.assertEqual(found["summary"]["registered_existing"], ["Raw/03 Get/"])
                self.assertEqual(found["summary"]["archive_only_existing"], ["Raw/15 微信读书/"])
                self.assertEqual(found["summary"]["registered_missing"], [])
                self.assertEqual(found["warnings"], [])
                self.assertTrue(archived.is_file())
                reviewed = audit.audit(date(2026, 10, 11), 1, 100)
                self.assertEqual([item["path"] for item in reviewed["candidates"]],
                                 ["Raw/03 Get/2026-10-11-active.md"])
                self.assertNotIn("Raw/15 微信读书/", reviewed["counts"]["per_source"])
                pages = {"tabs": {"个人状态": "#个人状态"}, "wiki_candidates": [],
                         "skill_candidates": [], "confirmed_facts": [], "evidence": {
                             "source_snapshot_sha": "a" * 40, "synthesis_status": "complete",
                             "synthesis_reference": "fixture semantic report",
                             "semantic_review_run_id": "fixture-run",
                             "registered_sources_examined": ["Raw/03 Get/"],
                             "read_receipts": [{"path": active.relative_to(root).as_posix(),
                                                "sha256": hashlib.sha256(active.read_bytes()).hexdigest()}],
                             "coverage_gaps": []}}
                completeness = weekly.validate_evidence(root, "a" * 40, pages, found, reviewed)
                self.assertEqual(completeness["registered_total"], 1)
                self.assertEqual(completeness["status"], "complete")
                archived.unlink()
                archived.parent.rmdir()
                missing_archive = discovery.discover()
                self.assertEqual(missing_archive["summary"]["archive_only_missing"], ["Raw/15 微信读书/"])
                self.assertEqual(missing_archive["summary"]["registered_missing"], [])
                active.unlink()
                active.parent.rmdir()
                missing_active = discovery.discover()
                self.assertEqual(missing_active["summary"]["registered_missing"], ["Raw/03 Get/"])
                self.assertIn("registered_sources_missing_on_disk", missing_active["warnings"])


if __name__ == "__main__":
    unittest.main()
