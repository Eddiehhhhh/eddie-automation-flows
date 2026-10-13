import hashlib
import json
import sys
import tempfile
from pathlib import Path
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import cloud_weekly_bundle as bundle
import iteration_closeout as closeout
import build_observatory_frontend as frontend


class CloudWeeklyBoundaryTests(unittest.TestCase):
    def test_weekly_bundle_has_only_approved_sixteen_paths(self):
        paths = bundle.allowed_paths("2026-W41", "2026-10-14")
        self.assertEqual(len(paths), 16)
        self.assertFalse(any(path.startswith("Wiki/") for path in paths))
        self.assertFalse(any("01a12eb2" in path for path in paths))

    def test_source_count_never_auto_promotes_wiki(self):
        with mock.patch.object(closeout, "upsert_managed_period_block") as upsert:
            result = closeout.promote_selected_themes("weekly", {"label": "2026-W41"},
                [object()], {"get": [object()], "notion": [object()]}, ["Observatory/weekly/2026/2026-W41/启发洞察.md"])
        self.assertEqual(result, [])
        upsert.assert_not_called()

    def test_frontend_links_are_portable_and_keep_all_history(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            obs = root / "Observatory"
            target = root / "Raw" / "05 Chat" / "记事.md"
            target.parent.mkdir(parents=True)
            target.write_text("test")
            with mock.patch.object(frontend, "ROOT", root), mock.patch.object(frontend, "OBS_ROOT", obs):
                self.assertEqual(frontend.path_to_uri(target), "../Raw/05%20Chat/%E8%AE%B0%E4%BA%8B.md")

    def evidence_fixture(self, root, period="2026-W42", pending=False):
        path = root / "Raw/03 Get/evidence.md"
        path.parent.mkdir(parents=True)
        path.write_text("private source", encoding="utf-8")
        relative = path.relative_to(root).as_posix()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        pages = {"period": period, "tabs": {"个人状态": "#待确认" if pending else "#个人状态"},
                 "evidence": {"source_snapshot_sha": "a" * 40, "synthesis_status": "complete",
                              "synthesis_reference": "private report for " + period,
                              "semantic_review_run_id": "dot-run-42", "registered_sources_examined": ["Raw/03 Get/"],
                              "read_receipts": [{"path": relative, "sha256": digest}], "coverage_gaps": []},
                 "wiki_candidates": [], "skill_candidates": [], "confirmed_facts": []}
        discovery = {"summary": {"registered_existing": ["Raw/03 Get/"]}}
        audit = {"candidates": [{"path": relative}]}
        return pages, discovery, audit

    def test_next_week_complete_evidence_has_no_w41_inherited_gap(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            pages, discovery, audit = self.evidence_fixture(root)
            result = bundle.validate_evidence(root, "a" * 40, pages, discovery, audit)
            self.assertEqual(result["status"], "complete")
            self.assertEqual(result["gaps"], [])
            self.assertNotIn("W41", json.dumps(result))
            self.assertNotIn("55", json.dumps(result))
            self.assertNotIn("53", json.dumps(result))
            receipt = bundle.render_receipt("2026-W42", "2026-10-21", "key", "a" * 40, 1,
                                            ["Observatory/weekly/2026/2026-W42/系统状态.md"], result)
            self.assertIn("2026-W42", receipt)
            self.assertNotIn("W41", receipt)
            self.assertNotIn("55", receipt)
            self.assertNotIn("53", receipt)

    def test_w41_pending_fact_must_remain_candidate_not_wiki_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            pages, discovery, audit = self.evidence_fixture(root, period="2026-W41", pending=True)
            pages["evidence"]["synthesis_status"] = "partial"
            pages["evidence"]["semantic_review_run_id"] = ""
            pages["evidence"]["coverage_gaps"] = ["historical_ledger_unavailable"]
            with self.assertRaisesRegex(ValueError, "untracked confirmation"):
                bundle.validate_evidence(root, "a" * 40, pages, discovery, audit)
            pages["wiki_candidates"] = [{"kind": "personal_memory", "status": "pending_confirmation",
                                          "summary": "role remains undecided", "source_refs": ["Raw/03 Get/evidence.md"]}]
            result = bundle.validate_evidence(root, "a" * 40, pages, discovery, audit)
            self.assertEqual(result["status"], "partial")
            self.assertEqual(result["gaps"], ["historical_ledger_unavailable"])

    def test_missing_coverage_or_inference_cannot_claim_complete(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            pages, discovery, audit = self.evidence_fixture(root)
            pages["evidence"]["read_receipts"] = []
            with self.assertRaisesRegex(ValueError, "Complete synthesis"):
                bundle.validate_evidence(root, "a" * 40, pages, discovery, audit)
            pages["evidence"]["synthesis_status"] = "partial"
            pages["evidence"]["coverage_gaps"] = []
            with self.assertRaisesRegex(ValueError, "requires explicit gaps"):
                bundle.validate_evidence(root, "a" * 40, pages, discovery, audit)

    def test_weekly_streak_preserves_daily_and_other_weeks(self):
        system4 = {"daily": {"ready": True, "statuses": {"2026-10-11": "success"}},
                   "weekly": {"start": "2026-W40", "through": "2026-W41", "required": 2,
                              "max_streak": 0, "weeks": {"2026-W40": {"status": "success"},
                                                         "2026-W41": {"status": "partial", "missing": ["historical_ledger"]}}}}
        complete = {"status": "complete", "gaps": []}
        first = bundle.update_system4(system4, "2026-W42", complete)
        self.assertEqual(first["weekly"]["weeks"]["2026-W41"]["missing"], ["historical_ledger"])
        self.assertEqual(first["weekly"]["weeks"]["2026-W42"]["status"], "partial")
        self.assertEqual(first["weekly"]["current_streak"], 0)
        acceptance = {k: k + "-proof" for k in ("remote_commit", "remote_checked_at", "mobile_result_url", "mobile_checked_at", "natural_run_id")}
        second = bundle.update_system4(first, "2026-W42", complete, acceptance)
        self.assertEqual(second["weekly"]["weeks"]["2026-W42"]["status"], "success")
        self.assertEqual(second["weekly"]["current_streak"], 1)
        third = bundle.update_system4(second, "2026-W43", complete, acceptance)
        self.assertEqual(third["weekly"]["current_streak"], 2)
        self.assertTrue(third["ready_for_4_0"])
        self.assertEqual(third["daily"], {"ready": True, "statuses": {"2026-10-11": "success"}})
        self.assertEqual(third["weekly"]["weeks"]["2026-W41"]["status"], "partial")

    def test_duplicate_run_key_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "already published"):
            bundle.ensure_new_run_key({"run_key": "same"}, "same")

    def test_postpublication_acceptance_only_updates_four_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            period, run_date, source = "2026-W42", "2026-10-21", "a" * 40
            run_key = hashlib.sha256(f"weekly:{period}:{source}:cloud".encode()).hexdigest()
            closeout = {"run_key": run_key, "source_completeness": "complete", "source_gaps": []}
            latest = {"run_key": run_key}
            system4 = {"daily": {"ready": True}, "weekly": {"start": period, "through": period,
                "required": 1, "max_streak": 0, "weeks": {period: {"status": "partial"}}}}
            for name, value in (("Raw/00 Meta/iteration-weekly-closeout-latest.json", closeout),
                                ("Raw/00 Meta/iteration-weekly-latest.json", latest),
                                ("Raw/00 Meta/system4-acceptance.json", system4)):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(value), encoding="utf-8")
            receipt = root / f"Raw/05 Chat/{run_date}-艾迪宇宙每周迭代-{period}.md"
            receipt.parent.mkdir(parents=True, exist_ok=True)
            receipt.write_text("# Initial receipt\n", encoding="utf-8")
            acceptance = {"remote_commit": "b" * 40, "remote_checked_at": "2026-10-21T09:10:00+08:00",
                "mobile_result_url": "https://example.invalid/result", "mobile_checked_at": "2026-10-21T09:11:00+08:00",
                "natural_run_id": "scheduled-run-42", "source_sha": source, "run_key": run_key}
            expected = {"Raw/00 Meta/iteration-weekly-closeout-latest.json",
                        "Raw/00 Meta/iteration-weekly-latest.json", "Raw/00 Meta/system4-acceptance.json",
                        f"Raw/05 Chat/{run_date}-艾迪宇宙每周迭代-{period}.md"}
            with mock.patch.object(bundle, "git", side_effect=lambda _, *args: "b" * 40 if args[0] == "rev-parse" else ""), \
                 mock.patch.object(bundle, "changed_paths", return_value=expected):
                result = bundle.record_acceptance(root, source, {"period": period}, run_date, acceptance)
            self.assertEqual(result["weekly_status"], "success")
            self.assertEqual(result["accepted_paths"], 4)
            self.assertIn("scheduled-run-42", receipt.read_text())
            self.assertEqual(json.loads((root / "Raw/00 Meta/system4-acceptance.json").read_text())["weekly"]["current_streak"], 1)


if __name__ == "__main__":
    unittest.main()
