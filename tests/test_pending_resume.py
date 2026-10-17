"""Offline persisted-pending regression; no source API or external Git."""
import json
from copy import deepcopy
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from get_mirror_incremental_replay import prepare_private_preview, ParityFailure, snapshot
from get_mirror_render import CompatibleWriter
from source_sync_assets import AssetStore


class PendingResumeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name); self.vault = self.work / 'vault'
        self.root = self.vault / 'Raw/03 Get'; (self.root / '03 State').mkdir(parents=True)
        (self.root / '03 State/manifest.json').write_text('{"notes":[]}')
        self.git('init', '-q'); self.git('config', 'user.name', 'Offline')
        self.git('config', 'user.email', 'offline@example.invalid')
        self.git('add', '.'); self.git('commit', '-qm', 'seed')
        self.head = self.git('rev-parse', 'HEAD').strip()
        self.note = {'id': '123', 'title': 'Recorder fixture', 'note_type': 'recorder_audio',
                     'created_at': '2026-10-13 08:00:00', 'updated_at': '2026-10-13 08:00:00',
                     'content': 'AI summary', 'audio': {'original': ''}}
        self.calls = []

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.vault), *args], text=True, stderr=subprocess.DEVNULL)

    def factory(self, root):
        def forbidden(*args, **kwargs): raise AssertionError('no network')
        return CompatibleWriter(root, AssetStore(root, fetch=forbidden))

    def preview(self, name, detail=None):
        def fetch(ident): self.calls.append(ident); return deepcopy(self.note)
        return prepare_private_preview(self.vault, self.work / name, self.head,
            lambda: ([deepcopy(self.note)], True), detail or fetch, self.factory)

    def adopt_offline(self, name):
        shutil.copytree(self.work / name / 'candidate', self.root, dirs_exist_ok=True)
        self.git('add', '.'); self.git('commit', '-qm', 'synthetic published state')
        self.head = self.git('rev-parse', 'HEAD').strip(); self.calls.clear()

    def state(self, name):
        return json.loads((self.work / name / 'candidate/03 State/cloud-sync-state.json').read_text())

    def test_next_independent_run_completes_pending_even_same_source_version(self):
        _, counts = self.preview('pending'); self.assertEqual(counts['pending'], 1)
        self.adopt_offline('pending'); before = snapshot(self.root)
        self.note['audio']['original'] = 'The full original transcript is now available.'
        result, counts = self.preview('ready')
        self.assertEqual(counts['pending'], 0); self.assertEqual(result['first']['written_or_changed'], 1)
        self.assertEqual(result['second']['fresh_details'], 0); self.assertEqual(self.calls, ['123'])
        self.assertEqual(self.state('ready')['pendingById'], {})
        self.assertIn('api:123', self.state('ready')['notesByCanonicalId'])
        self.assertEqual(snapshot(self.root), before)
        self.adopt_offline('ready'); _, counts = self.preview('stable')
        self.assertEqual(self.calls, []); self.assertEqual(counts['changed_paths'], 0)

    def test_old_success_and_body_remain_until_new_pending_version_completes(self):
        self.note['audio']['original'] = 'Previously verified original.'
        self.preview('old'); self.adopt_offline('old')
        old_state = json.loads((self.root / '03 State/cloud-sync-state.json').read_text())
        path = old_state['notesByCanonicalId']['api:123']['filePath']; old_bytes = (self.root / path).read_bytes()
        self.note.update(updated_at='2026-10-14 08:00:00'); self.note['audio']['original'] = ''
        self.preview('pending'); pending_state = self.state('pending')
        self.assertEqual(pending_state['notesByCanonicalId'], old_state['notesByCanonicalId'])
        self.assertEqual((self.work / 'pending/candidate' / path).read_bytes(), old_bytes)
        self.adopt_offline('pending'); self.note['audio']['original'] = 'New verified original.'
        _, counts = self.preview('ready'); self.assertEqual(counts['pending'], 0)
        entry = self.state('ready')['notesByCanonicalId']['api:123']
        self.assertEqual(entry['filePath'], path); self.assertNotEqual(entry['updatedAt'], old_state['notesByCanonicalId']['api:123']['updatedAt'])
        self.assertEqual((self.root / path).read_bytes(), old_bytes)
        self.assertIn(b'New verified original.', (self.work / 'ready/candidate' / path).read_bytes())

    def test_pending_to_complete_inside_same_two_pass_run_still_hard_fails(self):
        calls = []
        def detail(ident):
            calls.append(ident); value = deepcopy(self.note)
            if len(calls) > 1: value['audio']['original'] = 'Changed during this run.'
            return value
        before = snapshot(self.root)
        with self.assertRaisesRegex(ParityFailure, 'pending_changed_between_passes'):
            self.preview('unstable', detail)
        self.assertEqual(snapshot(self.root), before); self.assertFalse((self.work / 'unstable').exists())

    def test_failed_ready_detail_keeps_persisted_pending_and_old_tree(self):
        self.preview('pending'); self.adopt_offline('pending'); before = snapshot(self.root)
        def failure(_): raise RuntimeError('offline synthetic failure')
        with self.assertRaises(RuntimeError): self.preview('failed', failure)
        self.assertEqual(snapshot(self.root), before); self.assertFalse((self.work / 'failed').exists())

    def test_persisted_pending_with_still_missing_original_remains_partial(self):
        self.preview('pending'); self.adopt_offline('pending')
        _, counts = self.preview('still_pending')
        self.assertEqual(counts['pending'], 1); self.assertEqual(self.calls, ['123', '123'])
        self.assertEqual(self.state('still_pending')['notesByCanonicalId'], {})


if __name__ == '__main__': unittest.main()
