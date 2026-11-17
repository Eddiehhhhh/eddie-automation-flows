"""Offline runner assembly tests; no secrets, HTTP, or hosted writes."""
import contextlib
import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from assets import asset_references
from core import CheckpointFailure, Hold, snapshot
from remote_state import unpack_checkpoint
from test_core import note
from test_remote_state import GitStateFake
import runner


def config():
    return {'private_repo': 'owner/private', 'data_branch': 'proof',
            'state_branch': 'codex/get-state-test', 'namespace': 'test',
            'raw_prefix': 'Raw/03 Get/isolated', 'notion_database_id': 'a' * 32,
            'notion_properties': {}, 'allowed_asset_hosts': ['cdn.test'],
            'scope': 'isolated-validation'}


class Source:
    def __init__(self):
        self.notes = [snapshot(note())]
        self.calls = 0
        self.error = None
        self.holds = []

    def scan(self, *, on_note_hold=None):
        self.calls += 1
        if self.error:
            raise self.error
        for value in self.holds:
            on_note_hold(copy.deepcopy(value))
        return copy.deepcopy(self.notes)


class Text:
    def __init__(self):
        self.calls = []
        self.error = None

    def sync(self, s, receipt=None):
        self.calls.append((copy.deepcopy(s), copy.deepcopy(receipt)))
        if self.error:
            raise self.error
        return {'version': s['version'], 'title': 'synthetic', 'content_scope': 'text_and_metadata'}


class Media:
    def restore(self, asset, root):
        return True

    def __init__(self):
        self.calls = []
        self.error = None

    def sync_manifest(self, s, manifest, root):
        self.calls.append(copy.deepcopy(s))
        if self.error:
            raise self.error
        return {'version': s['version'], 'binary_assets_verified': True, 'assets': []}

    def sync(self, s, text, manifest, root):
        return self.sync_manifest(s, manifest, root)


class Archive:
    calls = []

    def __init__(self, root, transport):
        self.root = root
        self.closed = False

    def archive_snapshot(self, s):
        self.calls.append(copy.deepcopy(s))
        assets = [{'sha256': 'b' * 64, 'bytes': 1, 'local_name': 'b' * 64 + '.bin',
                   'source_field': ref['field'],
                   'source_url_sha256': hashlib.sha256(ref['url'].encode()).hexdigest()}
                  for ref in asset_references(s['note'])]
        return {'source_id': s['id'], 'source_version': s['version'], 'assets': assets,
                'coverage': 'declared_attachments_and_audio_only', 'destination_delivery': 'not_verified'}

    def close(self):
        self.closed = True


class Tests(unittest.TestCase):
    def setUp(self):
        self.cfg = config()
        self.source = Source()
        self.github = GitStateFake()
        self.ghtext, self.notext, self.ghmedia, self.nomedia = Text(), Text(), Media(), Media()
        Archive.calls = []
        for name, value in [('GetSource', self.source), ('VaultGitHubSink', self.ghtext),
                            ('NotionSink', self.notext), ('GitHubMedia', self.ghmedia),
                            ('NotionMedia', self.nomedia)]:
            p = patch('runner.' + name, return_value=value)
            p.start()
            self.addCleanup(p.stop)
        p = patch('runner.AssetArchive', Archive)
        p.start()
        self.addCleanup(p.stop)

    def run_mode(self, mode, batch=10):
        return runner.execute(self.cfg, mode, batch, object(), self.github, object())

    def baseline(self):
        result, status = self.run_mode('baseline')
        self.assertEqual(status, 0)
        self.assertEqual(result['pending'], 0)

    def test_baseline_never_delivers_and_repeat_never_rescans(self):
        self.baseline()
        self.assertEqual(self.ghtext.calls, [])
        self.assertEqual(self.source.calls, 1)
        with self.assertRaisesRegex(Hold, 'already_initialized'):
            self.run_mode('baseline')
        self.assertEqual(self.source.calls, 1)
        self.assertIsNone(self.github.document()['lease'])

    def test_sync_needs_baseline_and_releases_lease(self):
        with self.assertRaisesRegex(Hold, 'baseline_required'):
            self.run_mode('sync')
        self.assertIsNone(self.github.document()['lease'])
        self.assertEqual(self.source.calls, 0)

    def test_sync_delivers_new_and_modified_notes(self):
        self.baseline()
        self.source.notes = [snapshot(note(text='modified')), snapshot(note('2'))]
        result, status = self.run_mode('sync')
        self.assertEqual(status, 0)
        self.assertEqual(result['observed'], 2)
        self.assertEqual(result['github']['verified'], 2)
        self.assertEqual(result['notion']['verified'], 2)
        self.assertEqual(len(self.ghtext.calls), 2)
        self.assertIsNone(self.github.document()['lease'])

    def test_backfill_only_selects_bounded_unverified_current_batch(self):
        self.source.notes = [snapshot(note(str(i))) for i in range(1, 26)]
        self.baseline()
        first, status = self.run_mode('backfill', 7)
        self.assertEqual(status, 0)
        self.assertEqual((first['observed'], first['selected']), (25, 7))
        self.assertEqual(len(self.ghtext.calls), 7)
        self.run_mode('backfill', 7)
        self.assertEqual(len(self.ghtext.calls), 14)
        self.assertEqual(len({s['id'] for s, _ in self.ghtext.calls}), 14)
        with self.assertRaisesRegex(Hold, 'arguments_invalid'):
            self.run_mode('backfill', 11)

    def test_backfill_pending_conflicts_do_not_starve_later_notes(self):
        self.source.notes = [snapshot(note(str(i))) for i in range(1, 5)]
        self.baseline()
        self.notext.error = Hold('duplicate_source_pages')
        first, status = self.run_mode('backfill', 2)
        self.assertEqual(first['pending'], 2)
        second, status = self.run_mode('backfill', 2)
        self.assertEqual(second['pending'], 4)
        self.assertEqual({s['id'] for s, _ in self.ghtext.calls}, {'1','2','3','4'})

    def test_held_returns_two_then_drain_reuses_persisted_text_receipt(self):
        self.baseline()
        n = note(text='asset')
        n['attachments'] = [{'url': 'https://cdn.test/synthetic'}]
        self.source.notes = [snapshot(n)]
        self.ghmedia.error = Hold('binary_assets_not_verified')
        result, status = self.run_mode('sync')
        self.assertEqual(status, 2)
        self.assertEqual(result['pending'], 1)
        self.assertEqual(result['github']['held'], 1)
        self.assertEqual(result['notion']['verified'], 1)
        self.assertEqual(len(Archive.calls), 1)
        self.ghmedia.error = None
        scans = self.source.calls
        result, status = self.run_mode('drain')
        self.assertEqual(status, 0)
        self.assertEqual(self.source.calls, scans)
        self.assertIsNotNone(self.ghtext.calls[-1][1])
        self.assertEqual(self.ghtext.calls[-1][1]['version'], self.source.notes[0]['version'])
        self.assertIsNone(self.github.document()['lease'])

    def test_held_old_media_does_not_allow_later_version_to_overtake(self):
        self.baseline()
        n = note(text='old')
        n['attachments'] = [{'url': 'https://cdn.test/synthetic'}]
        self.source.notes = [snapshot(n)]
        self.ghmedia.error = Hold('binary_assets_not_verified')
        self.run_mode('sync')
        old_version = self.source.notes[0]['version']
        n['content'] = 'new'
        n['updated_at'] = '2026-11-18'
        self.source.notes = [snapshot(n)]
        result, status = self.run_mode('sync')
        self.assertEqual(status, 2)
        self.assertEqual(result['pending'], 2)
        self.assertTrue(all(s['version'] == old_version for s, _ in self.ghtext.calls))

    def test_unknown_embedded_media_remains_held_without_text_or_media_write(self):
        self.baseline()
        self.source.notes = [snapshot(note(text='![synthetic](https://cdn.test/unknown)'))]
        result, status = self.run_mode('sync')
        self.assertEqual(status, 2)
        self.assertEqual(result['pending'], 1)
        self.assertEqual(self.ghtext.calls, [])
        self.assertEqual(self.notext.calls, [])
        self.assertEqual(self.ghmedia.calls, [])

    def test_partial_source_hold_preserves_history_without_blocking_valid_ids(self):
        self.baseline()
        raw = note('9', text='synthetic incomplete recording')
        raw['note_type'] = 'recorder_audio'
        held = {'id': '9', 'version': 'c' * 64, 'reason': 'recording_transcript_unavailable', 'note': raw}
        self.source.holds = [held]
        self.source.notes = [snapshot(note('2', text='new valid'))]
        result, status = self.run_mode('sync')
        self.assertEqual(status, 2)
        self.assertEqual((result['source_holds'], result['source_holds_active']), (1, 1))
        self.assertEqual(result['github']['verified'], 1)
        self.assertEqual(result['pending'], 0)
        self.source.holds = []
        recovered = note('9', text='now valid')
        self.source.notes.append(snapshot(recovered))
        result, status = self.run_mode('sync')
        self.assertEqual(status, 0)
        self.assertEqual(result['source_holds_active'], 0)
        raw_db = unpack_checkpoint(self.github.document()['checkpoint'])
        with tempfile.TemporaryDirectory() as directory:
            import sqlite3
            path = Path(directory) / 'readback.sqlite'
            path.write_bytes(raw_db)
            db = sqlite3.connect(path)
            current = json.loads(db.execute("SELECT v FROM journal WHERE k='source_hold:9'").fetchone()[0])
            history = json.loads(db.execute("SELECT v FROM journal WHERE k=?", ('source_hold_history:9:' + held['version'],)).fetchone()[0])
            db.close()
        self.assertEqual(current['status'], 'resolved')
        self.assertEqual(history['note'], held['note'])

    def test_source_failure_releases_own_lease(self):
        self.baseline()
        self.source.error = RuntimeError('synthetic private payload')
        with self.assertRaises(RuntimeError):
            self.run_mode('sync')
        self.assertIsNone(self.github.document()['lease'])

    def test_checkpoint_failure_releases_own_lease_without_sink_write(self):
        p = patch('runner.Store', side_effect=CheckpointFailure('state_checkpoint_failed'))
        with p, self.assertRaises(CheckpointFailure):
            self.run_mode('baseline')
        self.assertIsNone(self.github.document()['lease'])
        self.assertEqual(self.ghtext.calls, [])

    def test_config_change_is_bound_and_held(self):
        self.baseline()
        self.cfg['raw_prefix'] = 'Raw/03 Get/other'
        with self.assertRaisesRegex(Hold, 'state_target_changed'):
            self.run_mode('drain')
        self.assertIsNone(self.github.document()['lease'])

    def test_main_never_outputs_config_payload_or_exception(self):
        self.baseline()
        self.source.notes = [snapshot(note(text='PRIVATE_SNAPSHOT_PAYLOAD'))]
        self.ghtext.error = RuntimeError('PRIVATE_ERROR_VALUE')
        env = {k: 'synthetic-private-value' for k in runner.ENV_NAMES}
        env['GET_SYNC_CONFIG_JSON'] = json.dumps(self.cfg)
        output = io.StringIO()
        with patch('runner.transports', return_value=(object(), self.github, object())), contextlib.redirect_stdout(output):
            code = runner.main(['--mode', 'sync'], env)
        self.assertEqual(code, 2)
        report = json.loads(output.getvalue())
        self.assertEqual(report['pending'], 1)
        for private in ('PRIVATE_SNAPSHOT_PAYLOAD', 'PRIVATE_ERROR_VALUE', 'synthetic-private-value', self.cfg['notion_database_id'], self.cfg['private_repo']):
            self.assertNotIn(private, output.getvalue())

    def test_verified_run_owner_is_traceable_without_credentials(self):
        env = {k: 'synthetic-private-token' for k in runner.ENV_NAMES}
        env['GET_SYNC_CONFIG_JSON'] = json.dumps(self.cfg)
        env['CODEX_THREAD_ID'] = 'get-direct-12345-2'
        env['GITHUB_RUN_ID'], env['GITHUB_RUN_ATTEMPT'] = '12345', '2'
        self.assertEqual(runner.run_owner(env), 'get-direct-12345-2')
        output = io.StringIO()
        with patch('runner.transports', return_value=(object(), object(), object())), \
             patch('runner.execute', return_value=({'phase': 'inspected'}, 0)) as execute, \
             contextlib.redirect_stdout(output):
            self.assertEqual(runner.main(['--mode', 'inspect'], env), 0)
        self.assertEqual(execute.call_args.kwargs['owner'], 'get-direct-12345-2')
        self.assertNotIn('synthetic-private-token', str(execute.call_args.kwargs))
        env['CODEX_THREAD_ID'] = '../invalid-thread'
        self.assertEqual(runner.run_owner(env), 'github-12345-2')
        env['GITHUB_RUN_ID'] = 'invalid/12345'
        self.assertRegex(runner.run_owner(env), '^get-run-[0-9a-f]{24}$')

    def test_invalid_config_returns_safe_error(self):
        env = {k: 'synthetic' for k in runner.ENV_NAMES}
        env['GET_SYNC_CONFIG_JSON'] = '{bad-json-private'
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = runner.main(['--mode', 'inspect'], env)
        self.assertEqual(code, 1)
        self.assertNotIn('bad-json-private', output.getvalue())

    def test_configuration_rejects_shared_branches_and_unbounded_hosts(self):
        env = {k: 'synthetic' for k in runner.ENV_NAMES}
        for key, value in [('state_branch', 'proof'), ('data_branch', 'main'), ('allowed_asset_hosts', ['*.example.com']),
                           ('raw_prefix', '../private'), ('scope', 'unbound')]:
            cfg = config()
            cfg[key] = value
            env['GET_SYNC_CONFIG_JSON'] = json.dumps(cfg)
            with self.assertRaises(Hold):
                runner.configuration(env)

    def test_inspect_only_reads_source_and_schema_without_state_lease(self):
        class SourceHTTP:
            def request(self, method, path, body=None):
                assert method == 'GET'
                if '/detail?' in path:
                    return {'success': True, 'data': {'note': note()}}
                return {'success': True, 'data': {'notes': [note()], 'has_more': False}}
        class Schema:
            def __init__(self):
                self.calls = 0
            def preflight(self):
                self.calls += 1
        schema = Schema()
        with patch('runner.NotionSink', return_value=schema):
            result, status = runner.execute(self.cfg, 'inspect', 10, SourceHTTP(), self.github, object())
        self.assertEqual(status, 0)
        self.assertEqual(result['phase'], 'inspected')
        self.assertEqual(schema.calls, 1)
        self.assertTrue(all(m == 'GET' for m, _, _ in self.github.calls))
        self.assertEqual(self.github.blobs, {})
        self.assertEqual(self.source.calls, 0)


class ManifestRecoveryTests(unittest.TestCase):
    def recovery_case(self, *, changed_bytes=False, source_id='1', same_url=False):
        from assets import AssetArchive
        from media_delivery import GitHubMedia
        from retry import HTTPFailure
        from test_assets import Transport, Response
        from test_core import GitFake
        from test_notion_sink import Journal
        n = note(text='synthetic recoverable source')
        n['attachments'] = [{'url': 'https://cdn.test/expired', 'type': 'file'}]
        s = snapshot(n)
        journal = Journal()
        with tempfile.TemporaryDirectory() as directory:
            archive = AssetArchive(Path(directory).resolve(), Transport())
            try:
                original = copy.deepcopy(runner.ManifestCache(archive, journal).get(s))
                size = original['assets'][0]['bytes']
            finally:
                archive.close()
        refreshed = copy.deepcopy(n)
        refreshed['note_id'] = source_id
        if not same_url:
            refreshed['attachments'][0]['url'] = 'https://cdn.test/current'
        class SourceHTTP:
            calls = 0
            def request(self, method, path, body=None):
                self.calls += 1
                self.asserted = (method == 'GET' and path.startswith('/open/api/v1/resource/note/detail?')
                                 and 'id=1' in path)
                return {'success': True, 'data': {'note': refreshed}}
        class Expiring(Transport):
            def __init__(self):
                super().__init__()
                self.urls = []
            def open(self, url):
                self.urls.append(url)
                if url.endswith('/expired'):
                    raise HTTPFailure(403)
                if changed_bytes:
                    self.response = Response(data=b'Z' * size)
                return super().open(url)
        return s, journal, original, GitHubMedia(GitFake(), 'owner/private', 'proof', 'test'), SourceHTTP(), Expiring()

    def test_expired_unpublished_source_refresh_requires_exact_old_bytes(self):
        from assets import AssetArchive
        from media_delivery import read_asset_bytes
        s, journal, original, github, source, transport = self.recovery_case()
        before = copy.deepcopy(s)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            archive = AssetArchive(root, transport)
            try:
                cache = runner.ManifestCache(archive, journal, github, source)
                self.assertEqual(cache.get(s), original)
                self.assertEqual(len(read_asset_bytes(original['assets'][0], root)), original['assets'][0]['bytes'])
                self.assertEqual(source.calls, 1)
                self.assertTrue(source.asserted)
                self.assertEqual(transport.urls, ['https://cdn.test/expired', 'https://cdn.test/current'])
                self.assertEqual(s, before)
                self.assertEqual(journal.get('asset_manifest:' + s['id'] + ':' + s['version']), original)
            finally:
                archive.close()

    def test_refreshed_source_cannot_replace_old_revision_with_new_bytes(self):
        from assets import AssetArchive
        s, journal, original, github, source, transport = self.recovery_case(changed_bytes=True)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            archive = AssetArchive(root, transport)
            try:
                with self.assertRaisesRegex(Hold, 'asset_hash_mismatch'):
                    runner.ManifestCache(archive, journal, github, source).get(s)
                self.assertFalse((root / original['assets'][0]['local_name']).exists())
                self.assertEqual(journal.get('asset_manifest:' + s['id'] + ':' + s['version']), original)
            finally:
                archive.close()

    def test_refresh_wrong_source_id_or_unchanged_expired_url_is_held(self):
        from assets import AssetArchive
        for options, reason in [({'source_id': '2'}, 'asset_refresh_source_invalid'),
                                ({'same_url': True}, 'asset_refresh_reference_unavailable')]:
            s, journal, original, github, source, transport = self.recovery_case(**options)
            with tempfile.TemporaryDirectory() as directory:
                archive = AssetArchive(Path(directory).resolve(), transport)
                try:
                    with self.assertRaisesRegex(Hold, reason):
                        runner.ManifestCache(archive, journal, github, source).get(s)
                    self.assertEqual(transport.urls, ['https://cdn.test/expired'])
                    self.assertEqual(journal.get('asset_manifest:' + s['id'] + ':' + s['version']), original)
                finally:
                    archive.close()

    def test_new_runner_uses_delivered_github_bytes_after_source_url_expires(self):
        from assets import AssetArchive
        from media_delivery import GitHubMedia, read_asset_bytes
        from test_assets import Transport
        from test_core import GitFake
        from test_notion_sink import Journal
        n = note(text='synthetic media recovery')
        n['attachments'] = [{'url': 'https://cdn.test/expiring', 'type': 'file'}]
        s = snapshot(n)
        journal, gh = Journal(), GitFake()
        media = GitHubMedia(gh, 'owner/private', 'proof', 'test')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            archive = AssetArchive(root, Transport())
            try:
                cache = runner.ManifestCache(archive, journal)
                github = runner.CompleteSink('github', Text(), media, journal, cache, root)
                receipt = github.sync(s)
                self.assertTrue(receipt['binary_assets_verified'])
                notion_media = Media()
                notion_media.error = Hold('synthetic_notion_failure')
                notion = runner.CompleteSink('notion', Text(), notion_media, journal, cache, root)
                with self.assertRaisesRegex(Hold, 'synthetic_notion_failure'):
                    notion.sync(s)
                saved = copy.deepcopy(journal.get('asset_manifest:' + s['id'] + ':' + s['version']))
            finally:
                archive.close()
        class Expired:
            def open(self, url):
                raise AssertionError('expired source URL must not be requested after GitHub delivery')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            archive = AssetArchive(root, Expired())
            try:
                cache = runner.ManifestCache(archive, journal, github_media=media)
                notion = runner.CompleteSink('notion', Text(), Media(), journal, cache, root)
                self.assertTrue(notion.sync(s)['binary_assets_verified'])
                self.assertEqual(cache.get(s), saved)
                self.assertEqual(journal.get('asset_manifest:' + s['id'] + ':' + s['version']), saved)
                self.assertEqual(len(read_asset_bytes(saved['assets'][0], root)), saved['assets'][0]['bytes'])
                self.assertEqual(gh.writes, 1)
            finally:
                archive.close()


if __name__ == '__main__':
    unittest.main()
