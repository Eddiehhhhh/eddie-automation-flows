import copy
import io
import json
import sys
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from iteration_public_state import public_state
import iteration_closeout
import run_iteration_pipeline


def fixture():
    secret = "PRIVATE_SENTINEL_NOT_FOR_STATE"
    return {
        "generated_at": "2026-06-16T02:35:42.603225+00:00",
        "mode": "weekly",
        "period": {"label": "2026-W24", "target_date": "2026-06-14",
                   "window_start": "2026-06-08", "window_end": "2026-06-14", "extra": secret},
        "discovery": {"summary": {"source_count": 15, "tree_path_count": 887,
                      "registered_existing": [secret] * 14, "registered_missing": []},
                      "warnings": [secret]},
        "audit": {"counts": {"total_candidates": 356, "high_priority_candidates": 28,
                  "per_source": {secret: 9}}, "high_priority": [secret]},
        "closeout": {"written_tabs": [secret] * 3, "actual_wiki_updates": [secret],
                     "candidate_wiki_updates": [secret], "candidate_skill_promotions": [secret],
                     "follow_ups": [secret] * 2, "known_context": [secret]},
        "path": secret, "source": secret, "future_private_field": {secret: secret},
    }


class PublicIterationStateTests(unittest.TestCase):
    def test_projection_preserves_dates_counts_without_content_or_mutation(self):
        summary = fixture()
        original = copy.deepcopy(summary)
        projected = public_state(summary)
        self.assertEqual(summary, original)
        self.assertNotIn("PRIVATE_SENTINEL", json.dumps(projected))
        self.assertEqual(projected["generated_at"], original["generated_at"])
        self.assertEqual(projected["period"]["label"], "2026-W24")
        self.assertEqual(projected["discovery_counts"]["registered_existing"], 14)
        self.assertEqual(projected["audit_counts"], {"total_candidates": 356, "high_priority_candidates": 28})
        self.assertEqual(projected["closeout_counts"]["actual_wiki_updates"], 1)
        self.assertNotIn("known_context", projected["closeout_counts"])

    def test_direct_closeout_shape_is_supported(self):
        summary = fixture()
        summary.update(summary.pop("closeout"))
        summary["discovery"] = summary["discovery"]["summary"]
        summary["audit"] = summary["audit"]["counts"]
        projected = public_state(summary)
        self.assertEqual(projected["audit_counts"]["total_candidates"], 356)
        self.assertEqual(projected["closeout_counts"]["written_tabs"], 3)
        self.assertNotIn("PRIVATE_SENTINEL", json.dumps(projected))

    def test_untrusted_types_and_strings_are_not_copied(self):
        for key in ("generated_at", "mode", "period", "discovery", "audit", "closeout"):
            summary = fixture()
            summary[key] = "PRIVATE_SENTINEL"
            self.assertNotIn("PRIVATE_SENTINEL", json.dumps(public_state(summary)))
        summary = fixture()
        summary["period"] = {"label": "PRIVATE_SENTINEL", "target_date": "2026-02-31"}
        summary["audit"]["counts"] = {"total_candidates": True, "high_priority_candidates": -1}
        summary["generated_at"] = "2026-06-16T02:35:42"
        projected = public_state(summary)
        self.assertNotIn("period", projected)
        self.assertNotIn("audit_counts", projected)
        self.assertNotIn("generated_at", projected)

    def test_malformed_mode_does_not_break_projection(self):
        for value in ([], {}, True, None):
            self.assertNotIn("mode", public_state({"mode": value}))

    def test_committed_snapshots_are_metadata_only(self):
        root = Path(__file__).resolve().parents[1]
        for mode in ("weekly", "daily"):
            data = json.loads((root / ".github" / "state" / f"iteration-{mode}-last-run.json").read_text())
            self.assertEqual(set(data), {"schema_version", "content_scope", "generated_at", "mode",
                                         "period", "discovery_counts", "audit_counts", "closeout_counts"})
            self.assertEqual(data["mode"], mode)
            self.assertEqual(data["content_scope"], "aggregate_metadata_only")
            self.assertEqual(public_state(data)["generated_at"], data["generated_at"])
            for section in ("discovery_counts", "audit_counts", "closeout_counts"):
                self.assertTrue(all(type(value) is int and value >= 0 for value in data[section].values()))

    def test_closeout_state_writer_projects_but_private_latest_preserves_summary(self):
        summary = fixture()
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.json"
            args = Namespace(mode="weekly", target_date=None, window_days=None,
                             write_latest=True, state_output=str(state), format="json")
            with mock.patch.object(iteration_closeout, "parse_args", return_value=args), \
                 mock.patch.object(iteration_closeout, "closeout", return_value=summary), \
                 mock.patch.object(iteration_closeout, "stable_write_json") as writer, \
                 redirect_stdout(io.StringIO()):
                self.assertEqual(iteration_closeout.main(), 0)
            self.assertEqual(writer.call_args_list[0].args[1], summary)
            self.assertEqual(writer.call_args_list[1].args, (state, public_state(summary)))

    def test_pipeline_state_writer_projects_but_private_latest_preserves_summary(self):
        summary = fixture()
        closeout = {"frontend": {}}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state.json"
            args = Namespace(mode="weekly", target_date=None, window_days=None, limit_per_source=1,
                             finalize=True, dry_run=False, write_latest=True,
                             state_output=str(state), format="json")
            writes = []
            def capture(path, data):
                writes.append((path, copy.deepcopy(data)))
                return True
            with mock.patch.object(run_iteration_pipeline, "parse_args", return_value=args), \
                 mock.patch.object(run_iteration_pipeline, "normalize_target", return_value=(None, 7, {})), \
                 mock.patch.object(run_iteration_pipeline, "load_json", return_value={}), \
                 mock.patch.object(run_iteration_pipeline.iteration_incremental_audit, "audit", return_value={}), \
                 mock.patch.object(run_iteration_pipeline.iteration_source_discovery, "discover", return_value={}), \
                 mock.patch.object(run_iteration_pipeline.iteration_closeout, "closeout", return_value=closeout), \
                 mock.patch.object(run_iteration_pipeline, "build_summary", return_value=summary), \
                 mock.patch.object(run_iteration_pipeline.iteration_source_discovery, "rel", return_value="PRIVATE_SENTINEL"), \
                 mock.patch.object(run_iteration_pipeline, "stable_write_json", side_effect=capture), \
                 redirect_stdout(io.StringIO()):
                self.assertEqual(run_iteration_pipeline.main(), 0)
            private_summary = writes[-2][1]
            public_summary = writes[-1][1]
            self.assertIn("PRIVATE_SENTINEL", json.dumps(private_summary))
            self.assertEqual(writes[-1][0], state)
            self.assertEqual(public_summary, public_state(summary))
            self.assertNotIn("PRIVATE_SENTINEL", json.dumps(public_summary))


if __name__ == "__main__":
    unittest.main()
