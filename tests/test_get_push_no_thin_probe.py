import contextlib
import io
import unittest
from unittest.mock import patch
import get_push_modified_probe as modified
import get_push_no_thin_probe as probe
import test_get_push_modified_probe as fixture


class NoThinProbeTest(unittest.TestCase):
    def test_only_update_adds_flag_and_disables_hydration(self):
        calls = []
        def original(local, stage, *args):
            calls.append((stage, args)); return True
        def fake_main(hydrate):
            self.assertFalse(hydrate)
            modified.run_git(None, 'seed_branch', 'push', 'seed')
            modified.run_git(None, 'update_branch', 'push', 'update')
        with patch.object(modified, 'run_git', original), patch.object(modified, 'main', fake_main):
            probe.main()
            self.assertIs(modified.run_git, original)
        self.assertEqual(calls, [('seed_branch', ('push', 'seed')), ('update_branch', ('push', '--no-thin', 'update'))])

    def test_local_shallow_fixture_missing_old_blobs(self):
        original = modified.run_git
        def run(local, stage, *args):
            if stage == 'update_branch': args = (args[0], '--no-thin', *args[1:])
            return original(local, stage, *args)
        with patch.object(modified, 'run_git', run), contextlib.redirect_stdout(io.StringIO()):
            fixture.ModifiedBlobProbeTest('test_actual_shallow_modified_blob_push_only_changes_probe_branch').test_actual_shallow_modified_blob_push_only_changes_probe_branch()

if __name__ == '__main__':
    unittest.main()
