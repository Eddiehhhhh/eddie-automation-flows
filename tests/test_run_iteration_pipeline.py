import sys
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import run_iteration_pipeline  # noqa: E402


class RunIterationPipelineTests(unittest.TestCase):
    def test_dry_run_does_not_write_observatory_outputs(self):
        discovery = {
            "summary": {"registered_missing": []},
            "delta": {},
            "warnings": [],
        }
        audit = {
            "counts": {"total_candidates": 0, "high_priority_candidates": 0},
            "source_delta": {},
            "candidates": [],
        }
        snapshot = {
            "views": {
                "daily": {"current_period": None},
                "weekly": {"current_period": None},
                "monthly": {"current_period": None},
            }
        }

        argv = ["run_iteration_pipeline.py", "--mode", "weekly", "--dry-run"]
        with (
            mock.patch.object(sys, "argv", argv),
            mock.patch.object(run_iteration_pipeline, "load_json", return_value=None),
            mock.patch.object(run_iteration_pipeline.iteration_incremental_audit, "audit", return_value=audit),
            mock.patch.object(run_iteration_pipeline.iteration_source_discovery, "discover", return_value=discovery),
            mock.patch.object(run_iteration_pipeline.build_observatory_frontend, "build_snapshot", return_value=snapshot),
            mock.patch.object(run_iteration_pipeline.build_observatory_frontend, "write_outputs") as write_outputs,
            mock.patch.object(run_iteration_pipeline, "stable_write_json") as stable_write_json,
        ):
            self.assertEqual(run_iteration_pipeline.main(), 0)

        write_outputs.assert_not_called()
        stable_write_json.assert_not_called()


if __name__ == "__main__":
    unittest.main()
