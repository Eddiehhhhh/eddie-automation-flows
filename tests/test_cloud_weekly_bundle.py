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


if __name__ == "__main__":
    unittest.main()
