import hashlib
import json
import re
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
import iteration_source_discovery as discovery_module
import iteration_incremental_audit as audit_module


class ObservatoryEmbeddedJsonTests(unittest.TestCase):
    def embedded_json(self, snapshot):
        rendered = frontend.render_html(snapshot)
        match = re.search(r'<script id="observatory-data" type="application/json">(.*?)</script>',
                          rendered, re.DOTALL)
        self.assertIsNotNone(match)
        # Script text is raw text in HTML; entities must not need unescaping.
        return rendered, match.group(1), json.loads(match.group(1))

    def test_script_json_round_trips_quotes_unicode_and_ampersands(self):
        snapshot = {"title": 'A "quoted" title & 中文', "empty": {}, "values": [True, None, 3]}
        _, _, parsed = self.embedded_json(snapshot)
        self.assertEqual(parsed, snapshot)

    def test_template_brace_normalization_does_not_change_payload(self):
        snapshot = {"nested": {"child": {"value": "{{literal}} and }}"}}, "empty": {}}
        rendered, _, parsed = self.embedded_json(snapshot)
        self.assertEqual(parsed, snapshot)
        self.assertIn("function update(nextState) {", rendered)
        self.assertNotIn("function update(nextState) {{", rendered)

    def test_script_terminators_are_json_escaped_without_losing_data(self):
        snapshot = {"text": '</script><script>alert("example")</script><!-- & > < 中文'}
        rendered, payload, parsed = self.embedded_json(snapshot)
        self.assertEqual(parsed, snapshot)
        self.assertNotIn("<", payload)
        self.assertNotIn(">", payload)
        self.assertNotIn("&", payload)
        self.assertEqual(rendered.count("</script>"), 2)


class CloudWeeklyBoundaryTests(unittest.TestCase):
    def test_candidate_identity_ignores_source_snapshot_and_never_writes_wiki(self):
        item = {"kind": "wiki", "target_path": "Wiki/05 Themes/example.md",
                "proposed_text": "\n## Point\n", "source_refs": ["Raw/03 Get/source.md"]}
        first = bundle.knowledge_candidate(item, "a" * 40)
        second = bundle.knowledge_candidate(item, "b" * 40)
        self.assertEqual(first["candidate_id"], second["candidate_id"])
        self.assertNotEqual(first["source_sha"], second["source_sha"])
        self.assertFalse(hasattr(bundle, "execute_approved_knowledge"))
        self.assertFalse(hasattr(bundle, "prepare_knowledge_manifest"))
        with self.assertRaisesRegex(ValueError, "Invalid knowledge target"):
            bundle.knowledge_candidate({**item, "kind": "skill", "target_path": "Schema/Skills/example.md"}, "a")

    def test_same_day_source_change_and_empty_input_preserve_receipt(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = "Raw/05 Chat/2026-10-14-艾迪宇宙每周迭代-2026-W41.md"
            item = bundle.knowledge_candidate({"kind": "question", "target_path": "Wiki/07 Questions/待澄清.md",
                "proposed_text": "\n## Question\n", "source_refs": ["Raw/03 Get/source.md"]}, "a" * 40)
            bundle.append_weekly_receipt(root, path, "- run_key: `one`\n", [item], "2026-W41", "one")
            receipt = root / path
            original = receipt.read_bytes() + b"\n```weekly-knowledge-result-v1\n{\"status\":\"completed\"}\n```\n"
            receipt.write_bytes(original)
            bundle.append_weekly_receipt(root, path, "- run_key: `two`\n", [
                bundle.knowledge_candidate({key: item[key] for key in
                    ("kind", "target_path", "proposed_text", "source_refs")}, "b" * 40)], "2026-W41", "two")
            self.assertTrue(receipt.read_bytes().startswith(original))
            self.assertEqual(len(bundle.read_knowledge_candidates(root, "2026-W41")), 1)
            after = receipt.read_bytes()
            bundle.append_weekly_receipt(root, path, "- run_key: `two`\n", [], "2026-W41", "two")
            self.assertEqual(receipt.read_bytes(), after)

    def test_cross_day_and_cross_week_pending_candidates_remain_readable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            item = bundle.knowledge_candidate({"kind": "wiki", "target_path": "Wiki/topic.md",
                "proposed_text": "\nPoint\n", "source_refs": ["Raw/source.md"]}, "a")
            old = "Raw/05 Chat/2026-10-14-艾迪宇宙每周迭代-2026-W41.md"
            bundle.append_weekly_receipt(root, old, "- run_key: `old`\n", [item], "2026-W41", "old")
            self.assertEqual(bundle.weekly_receipt_path(root, "2026-W41", "2026-10-15"), old)
            new = bundle.weekly_receipt_path(root, "2026-W42", "2026-10-21")
            bundle.append_weekly_receipt(root, new, "- run_key: `new`\n", [], "2026-W42", "new")
            self.assertEqual(len(bundle.read_knowledge_candidates(root)), 1)
            self.assertEqual(bundle.read_knowledge_candidates(root)[0]["receipt_path"], old)

    def test_jsonl_preserves_prefix_and_appends_only_five_fields(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            index = root / "Raw/05 Chat/_index.jsonl"
            index.parent.mkdir(parents=True)
            old = b'{"ts":"old","agent":"x","action":"chat_saved","target":"other","summary":"old","run_key":"legacy"}\n'
            index.write_bytes(old)
            row = {"ts": "new", "agent": "Codex", "action": "chat_saved", "target": "new", "summary": "new"}
            bundle.add_jsonl(root, "Raw/05 Chat/_index.jsonl", row, "ignored")
            self.assertTrue(index.read_bytes().startswith(old))
            self.assertEqual(set(json.loads(index.read_bytes().splitlines()[-1])), bundle.INDEX_FIELDS)
            bundle.add_jsonl(root, "Raw/05 Chat/_index.jsonl", row, "different")
            self.assertEqual(len(index.read_bytes().splitlines()), 2)
            with self.assertRaisesRegex(ValueError, "exactly five"):
                bundle.add_jsonl(root, "Raw/05 Chat/_index.jsonl", {**row, "run_key": "bad"}, "bad")

    def test_produce_rerun_preserves_actual_results_and_old_receipt(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            period = "2026-W41"
            receipt_path = f"Raw/05 Chat/2026-10-13-艾迪宇宙每周迭代-{period}.md"
            item = bundle.knowledge_candidate({"kind": "wiki", "target_path": "Wiki/topic.md",
                "proposed_text": "\nPoint\n", "source_refs": ["Raw/source.md"]}, "a" * 40)
            bundle.append_weekly_receipt(root, receipt_path, "- run_key: `old`\n", [item], period, "old")
            receipt = root / receipt_path
            original = receipt.read_bytes() + b"\n## Confirmed knowledge result\ncompleted\n"
            receipt.write_bytes(original)
            for relative in ("Observatory/索引.md", "Observatory/weekly/索引.md", "Observatory/weekly/2026/索引.md"):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("# Index\n")
            for relative, value in (("Raw/00 Meta/iteration-weekly-latest.json", {"period": {"label": period}}),
                                    ("Raw/00 Meta/iteration-weekly-closeout-latest.json",
                                     {"period": period, "actual_wiki_updates": ["Wiki/topic.md"],
                                      "knowledge_results": [{"status": "completed"}]}),
                                    ("Raw/00 Meta/system4-acceptance.json",
                                     {"daily": {"ready": False}, "weekly": {"start": period, "through": period,
                                      "required": 1, "max_streak": 0, "weeks": {}}})):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(value))
            pages = {"period": period, "window_end": "2026-10-11", "tabs": {
                name: f"date: {period}\n## 标签\n## 引用文章\n" for name in bundle.TABS},
                "wiki_candidates": [], "skill_candidates": [], "confirmed_facts": [], "knowledge_candidates": []}
            snapshot = {"views": {name: {"current_period": period if name == "weekly" else None, "entries": []}
                                  for name in ("daily", "weekly", "monthly")}}
            discovery = {"summary": {"registered_missing": [], "unregistered_existing": []}}
            audit = {"counts": {"total_candidates": 0}, "candidates": []}
            completeness = {"status": "complete", "gaps": [], "read_count": 0,
                            "registered_examined": 0, "registered_total": 0,
                            "synthesis_reference": "review", "semantic_review_run_id": "run", "selected_count": 0}
            for source in ("b" * 40, "c" * 40):
                with mock.patch.object(bundle, "git", side_effect=lambda _, *args: source if args[0] == "rev-parse" else ""), \
                     mock.patch.object(discovery_module, "DEFAULT_OUTPUT", root / "missing.json"), \
                     mock.patch.object(discovery_module, "discover", return_value=discovery), \
                     mock.patch.object(audit_module, "audit", return_value=audit), \
                     mock.patch.object(bundle, "validate_evidence", return_value=completeness), \
                     mock.patch.object(frontend, "build_snapshot", return_value=snapshot), \
                     mock.patch.object(frontend, "write_outputs"), \
                     mock.patch.object(bundle, "verify_existing", return_value={"changed_count": 0}):
                    bundle.produce(root, source, pages, "2026-10-14")
            self.assertTrue(receipt.read_bytes().startswith(original))
            self.assertEqual(len(bundle.read_knowledge_candidates(root, period)), 1)
            closeout = json.loads((root / "Raw/00 Meta/iteration-weekly-closeout-latest.json").read_text())
            self.assertEqual(closeout["actual_wiki_updates"], ["Wiki/topic.md"])
            self.assertEqual(closeout["knowledge_results"], [{"status": "completed"}])
            self.assertEqual(closeout["chat_record"], receipt_path)
            latest_path = root / "Raw/00 Meta/iteration-weekly-latest.json"
            later = json.loads(latest_path.read_text())
            later["period"]["label"] = "2026-W42"
            latest_path.write_text(json.dumps(later))
            with mock.patch.object(bundle, "git", side_effect=lambda _, *args: "d" * 40 if args[0] == "rev-parse" else ""):
                with self.assertRaisesRegex(ValueError, "Older week"):
                    bundle.produce(root, "d" * 40, pages, "2026-10-14")
            later["period"]["label"] = period
            latest_path.write_text(json.dumps(later))
            closeout["period"] = "2026-W40"
            (root / "Raw/00 Meta/iteration-weekly-closeout-latest.json").write_text(json.dumps(closeout))
            with mock.patch.object(bundle, "git", side_effect=lambda _, *args: "d" * 40 if args[0] == "rev-parse" else ""):
                with self.assertRaisesRegex(ValueError, "period mismatch"):
                    bundle.produce(root, "d" * 40, pages, "2026-10-14")

    def test_same_run_key_cannot_reset_published_acceptance(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            period, source = "2026-W41", "a" * 40
            run_key = hashlib.sha256(f"weekly:{period}:{source}:cloud".encode()).hexdigest()
            latest = root / "Raw/00 Meta/iteration-weekly-latest.json"
            latest.parent.mkdir(parents=True)
            latest.write_text(json.dumps({"period": {"label": period}, "run_key": run_key,
                                          "production_publish": "remote_and_mobile_verified"}))
            closeout = root / "Raw/00 Meta/iteration-weekly-closeout-latest.json"
            closeout.write_text(json.dumps({"period": period, "run_key": run_key,
                                            "production_acceptance": {"natural_run_id": "real-run"}}))
            receipt = root / f"Raw/05 Chat/2026-10-14-艾迪宇宙每周迭代-{period}.md"
            receipt.parent.mkdir(parents=True)
            receipt.write_text("# Original\n## Verified\n")
            before = {path: path.read_bytes() for path in (latest, closeout, receipt)}
            pages = {"period": period, "window_end": "2026-10-11", "tabs": {
                name: f"date: {period}\n## 标签\n## 引用文章\n" for name in bundle.TABS}}
            with mock.patch.object(bundle, "git", side_effect=lambda _, *args: source if args[0] == "rev-parse" else ""):
                with self.assertRaisesRegex(ValueError, "already published"):
                    bundle.produce(root, source, pages, "2026-10-14")
            self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_untrusted_receipt_paths_fail_before_verify_or_acceptance_writes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            period, run_date, source = "2026-W41", "2026-10-14", "a" * 40
            run_key = hashlib.sha256(f"weekly:{period}:{source}:cloud".encode()).hexdigest()
            latest_path = root / "Raw/00 Meta/iteration-weekly-latest.json"
            closeout_path = root / "Raw/00 Meta/iteration-weekly-closeout-latest.json"
            system_path = root / "Raw/00 Meta/system4-acceptance.json"
            latest_path.parent.mkdir(parents=True)
            system_path.write_text("{}")
            acceptance = {"remote_commit": "b" * 40, "remote_checked_at": "time",
                "mobile_result_url": "https://example.invalid", "mobile_checked_at": "time",
                "natural_run_id": "natural", "source_sha": source, "run_key": run_key}
            paths = ("/tmp/other.md", "Raw/05 Chat/../other.md",
                     "Raw/05 Chat/2026-10-14-艾迪宇宙每周迭代-2026-W40.md",
                     "Raw/05 Chat/not-a-date-艾迪宇宙每周迭代-2026-W41.md")
            for malicious in paths:
                with self.subTest(malicious=malicious):
                    latest_path.write_text(json.dumps({"period": {"label": period}, "run_key": run_key,
                                                       "chat_record": malicious}))
                    closeout_path.write_text(json.dumps({"period": period, "run_key": run_key,
                                                         "chat_record": malicious,
                                                         "source_completeness": "complete", "source_gaps": []}))
                    before = {path: path.read_bytes() for path in (latest_path, closeout_path, system_path)}
                    with mock.patch.object(bundle, "changed_paths", return_value=set()):
                        with self.assertRaisesRegex(ValueError, "weekly receipt"):
                            bundle.verify_existing(root, period, run_date, run_key)
                    with mock.patch.object(bundle, "git", side_effect=lambda _, *args: "b" * 40 if args[0] == "rev-parse" else ""):
                        with self.assertRaisesRegex(ValueError, "weekly receipt"):
                            bundle.record_acceptance(root, source, {"period": period}, run_date, acceptance)
                    self.assertEqual({path: path.read_bytes() for path in before}, before)
            external = root / "elsewhere.md"
            external.write_text("untouched")
            canonical = root / f"Raw/05 Chat/{run_date}-艾迪宇宙每周迭代-{period}.md"
            canonical.parent.mkdir(parents=True, exist_ok=True)
            canonical.symlink_to(external)
            with self.assertRaisesRegex(ValueError, "escapes"):
                bundle.validate_weekly_receipt_path(root, canonical.relative_to(root).as_posix(), period)
            latest_path.write_text(json.dumps({"period": {"label": period}, "run_key": run_key,
                                               "chat_record": canonical.relative_to(root).as_posix()}))
            closeout_path.write_text(json.dumps({"period": period, "run_key": run_key,
                                                 "chat_record": canonical.relative_to(root).as_posix(),
                                                 "source_completeness": "complete", "source_gaps": []}))
            with mock.patch.object(bundle, "changed_paths", return_value=set()):
                with self.assertRaisesRegex(ValueError, "escapes"):
                    bundle.verify_existing(root, period, run_date, run_key)
            with mock.patch.object(bundle, "git", side_effect=lambda _, *args: "b" * 40 if args[0] == "rev-parse" else ""):
                with self.assertRaisesRegex(ValueError, "escapes"):
                    bundle.record_acceptance(root, source, {"period": period}, run_date, acceptance)
            self.assertEqual(external.read_text(), "untouched")
            linked_root = root / "linked-checkout"
            linked_root.mkdir()
            (linked_root / "Raw").symlink_to(root / "Raw", target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "escapes"):
                bundle.validate_weekly_receipt_path(linked_root,
                    f"Raw/05 Chat/{run_date}-艾迪宇宙每周迭代-{period}.md", period)

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
