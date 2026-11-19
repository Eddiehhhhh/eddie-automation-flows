import base64
import hashlib
import json
import unittest
from urllib.parse import parse_qs, unquote, urlsplit

from core import Hold, Store, snapshot
from reviewed_history import SCHEMA, candidates, check_candidate, clock_parent, plan_at_head, should_resume_migration, verify_clock
from test_core import note
from notion_native import read_blocks, sections
from notion_sink import utf16_chunks
from vault_sink import markdown


def clock_env():
    return {'GITHUB_EVENT_NAME': 'workflow_dispatch', 'GITHUB_REF': 'refs/heads/main',
            'GITHUB_ACTOR': 'github-actions[bot]', 'GITHUB_TRIGGERING_ACTOR': 'github-actions[bot]',
            'GITHUB_RUN_ATTEMPT': '1', 'GET_HOURLY_DISPATCH_KEY': 'clock-123',
            'GITHUB_REPOSITORY': 'owner/runtime'}


def fileset(path, data):
    return {'type': 'file', 'path': path, 'encoding': 'base64', 'content': base64.b64encode(data).decode(),
            'size': len(data), 'sha': hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()}


class Fixture:
    cfg = {'private_repo': 'owner/private', 'namespace': 'test'}
    plan_path = 'get-direct/test/reviewed-backfill.json'
    head = 'a' * 40

    def __init__(self, snaps):
        self.snaps = {s['id']: s for s in snaps}
        self.legacy = {}
        self.exists = set()
        self.reads = []
        self.private = True
        self.rate_limit = {'resources': {'core': {'remaining': 2277}}}
        self.plan = {'schema': SCHEMA, 'namespace': 'test', 'excluded_source_ids': [], 'reviewed': []}
        for snap in snaps:
            sid, version = snap['id'], snap['version']
            path = 'Raw/03 Get/' + sid + '.md'
            self.legacy[path] = fileset(path, b'---\n---\n' + snap['note']['content'].encode())
            self.plan['reviewed'].append({'source_id': sid, 'source_version': version,
                                          'path': path, 'local_blob': self.legacy[path]['sha'],
                                          'reviewed_revision': hashlib.sha256((sid+':'+version).encode()).hexdigest()})

    def request(self, method, path):
        assert method == 'GET'
        self.reads.append(path)
        if path == '/rate_limit':
            return self.rate_limit
        if path == '/repos/owner/private':
            return {'private': self.private, 'default_branch': 'main'}
        if path.endswith('/git/ref/heads/main'):
            return {'object': {'sha': self.head}}
        if '/contents/' in path:
            target = unquote(urlsplit(path).path.split('/contents/', 1)[1])
            assert parse_qs(urlsplit(path).query) == {'ref': [self.head]}
            if target == self.plan_path:
                return fileset(target, json.dumps(self.plan).encode()) if self.plan else None
            return self.legacy.get(target)
        if '/actions/runs/' in path:
            return {'id': 123, 'path': '.github/workflows/get-cloud-clock.yml', 'head_branch': 'main',
                    'event': 'workflow_dispatch', 'status': 'completed', 'repository': {'full_name': 'owner/runtime'},
                    'head_repository': {'full_name': 'owner/runtime', 'fork': False}}
        sid = parse_qs(urlsplit(path).query)['id'][0]
        snap = self.snaps.get(sid)
        return {'success': True, 'data': {'note': snap['note']}} if snap else None

    def pages(self, sid):
        return [object()] if sid in self.exists else []


class ReviewedHistoryTests(unittest.TestCase):
    def setUp(self):
        snaps = [snapshot(dict(note(str(i)), note_type='plain_text')) for i in range(1, 11)]
        self.api = Fixture(snaps)
        self.store = Store(':memory:')
        self.addCleanup(self.store.db.close)
        self.store.accept(snaps)

    def test_explicit_migration_resumes_only_after_progress_and_no_pending_work(self):
        report = {'phase': 'history', 'pending': 0, 'notion': {'verified': 1},
                  'reviewed_history': {'remaining_approved': 9}}
        self.assertTrue(should_resume_migration(report))
        for changed in (
            {'phase': 'sync'},
            {'pending': 1},
            {'reviewed_history': {'remaining_approved': 0}},
            {'reviewed_history': {'remaining_approved': 9, 'deferred': 'rest_budget'}},
            {'notion': {'verified': 0}},
        ):
            self.assertFalse(should_resume_migration({**report, **changed}))
        self.assertTrue(should_resume_migration({**report, 'notion': {'verified': 0},
            'reviewed_history': {'remaining_approved': 9, 'newly_suspended': {'source_changed': 1}}}))
        self.assertTrue(should_resume_migration({'phase': 'sync', 'pending': 0,
                                                 'resume_history_from_clock': True}))
        self.assertFalse(should_resume_migration({'phase': 'sync', 'pending': 1,
                                                  'resume_history_from_clock': True}))

    def test_flomo_tag_stays_excluded_even_if_listed_in_reviewed_plan(self):
        raw = dict(note('30'), note_type='plain_text', tags=[{'name': 'Flomo'}])
        fixture = Fixture([snapshot(raw)])
        blob, plan = plan_at_head(fixture, fixture.cfg)
        args = (plan['reviewed'][0], blob, fixture.cfg, fixture, fixture, fixture)
        self.assertEqual(check_candidate(*args, allow_media=True), (None, 'flomo_excluded'))

    def test_exact_media_is_only_admitted_by_explicit_history(self):
        raw = dict(note('31'), note_type='audio', audio={'play_url': 'https://cdn.test/audio.ogg'})
        snap = snapshot(raw)
        fixture = Fixture([snap])
        blob, plan = plan_at_head(fixture, fixture.cfg)
        args = (plan['reviewed'][0], blob, fixture.cfg, fixture, fixture, fixture)
        self.assertEqual(check_candidate(*args), (None, 'source_changed'))
        self.assertEqual(check_candidate(*args, allow_media=True), (snap, None))
        fixture.exists.add(snap['id'])
        self.assertEqual(check_candidate(*args, allow_media=True), (None, 'notion_exists'))

    def test_local_summary_keeps_the_exact_original_and_immutable_get_fields(self):
        transcript = 'Transcript words. ' * 10
        raw = dict(note('32'), note_type='recorder_audio', content='Different upstream summary',
                   audio={'original': transcript, 'play_url': 'https://cdn.test/recording.ogg'})
        snap = snapshot(raw)
        fixture = Fixture([snap])
        row = fixture.plan['reviewed'][0]
        expected = '#### Earlier summary\n\nLocal material'
        fixture.legacy[row['path']] = fileset(row['path'],
            ('---\n---\n' + expected + '\n\n## 录音原文\n\n' + transcript).encode())
        row['local_blob'] = fixture.legacy[row['path']]['sha']
        row['preserve_local_summary'] = True
        blob, plan = plan_at_head(fixture, fixture.cfg)
        result, reason = check_candidate(plan['reviewed'][0], blob, fixture.cfg,
                                         fixture, fixture, fixture, allow_media=True)
        self.assertIsNone(reason)
        self.assertEqual(result['note'], raw)
        self.assertEqual(result['version'], snap['version'])
        self.assertEqual(result['legacy_summary'], expected)
        self.assertIn(expected.encode(), markdown(result, 'test', 'Raw/03 Get'))
        summary = read_blocks(sections(result, utf16_chunks)[0][3])
        self.assertEqual(summary[0][0], 'heading_4')
        self.assertIn('Earlier summary', repr(summary))
        self.assertNotIn('Different upstream summary', repr(summary))

    def select(self, *, available=lambda: True, paused=(), plan=None):
        report = {}
        active = plan or plan_at_head(self.api, self.api.cfg)
        selected = list(candidates(active, self.api.cfg, self.store, self.api, self.api, self.api,
                                   paused, report, available))
        return [row['id'] for row in selected], report

    def test_hourly_identity_and_optional_plan(self):
        env = clock_env()
        self.assertEqual(clock_parent(env), '123')
        self.assertTrue(verify_clock(self.api, env['GITHUB_REPOSITORY'], '123'))
        self.assertFalse(verify_clock(self.api, env['GITHUB_REPOSITORY'], '456'))
        self.assertIsNone(clock_parent(dict(env, GITHUB_ACTOR='person')))
        self.assertIsNone(clock_parent(dict(env, GITHUB_RUN_ATTEMPT='2')))
        self.api.plan = None
        self.assertIsNone(plan_at_head(self.api, self.api.cfg))
        self.api.private = False
        with self.assertRaisesRegex(Hold, 'private_main_required'):
            plan_at_head(self.api, self.api.cfg)

    def test_isolates_pause_legacy_exclusion_changed_source_and_existing_page(self):
        self.api.plan['excluded_source_ids'] = ['2']
        self.api.snaps['3'] = snapshot(dict(note('3', text='modified'), note_type='plain_text'))
        self.api.exists.add('4')
        ids, result = self.select(paused=['1'])
        self.assertEqual(ids, ['5'])
        self.assertEqual(result['checked'], 5)
        self.assertEqual(result['newly_suspended'], {'paused': 2, 'source_changed': 1, 'notion_exists': 1})
        ids, result = self.select()
        self.assertEqual(ids, ['5', '6', '7', '8', '9'])
        self.assertEqual(result['suspended'], 4)

    def test_replaced_plan_and_exhausted_budget_do_not_admit_rows(self):
        plan = plan_at_head(self.api, self.api.cfg)
        self.api.plan['excluded_source_ids'].append('2')
        self.assertEqual(self.select(plan=plan)[0], [])
        self.assertEqual(self.select(available=lambda: False)[0], [])
        self.assertIsNone(self.store.get('reviewed_hourly_history'))

    def test_missing_or_low_rest_budget_only_defers_history(self):
        for shape in ({}, {'resources': {'core': {'remaining': 1499}}}):
            self.api.rate_limit = shape
            selected, result = self.select()
            self.assertEqual(selected, [])
            self.assertEqual((result['checked'], result['deferred']), (0, 'rest_budget'))
            self.assertEqual(self.store.pending_count(), 0)
        self.api.rate_limit = {'resources': {'core': {'remaining': 2277}}}
        report = {}
        iterator = candidates(plan_at_head(self.api, self.api.cfg), self.api.cfg,
                              self.store, self.api, self.api, self.api, [], report, lambda: True)
        self.assertEqual(next(iterator)['id'], '1')
        self.api.rate_limit['resources']['core']['remaining'] = 1499
        self.assertEqual(list(iterator), [])
        self.assertEqual((report['checked'], report['deferred']), (1, 'rest_budget'))

    def test_one_unfinished_write_stops_later_admission_in_same_tick(self):
        report = {}
        iterator = candidates(plan_at_head(self.api, self.api.cfg), self.api.cfg,
                              self.store, self.api, self.api, self.api, [], report, lambda: True)
        first = next(iterator)
        self.store.enqueue([first])
        self.assertEqual(list(iterator), [])
        self.assertEqual(report['checked'], 1)
        self.assertEqual(self.store.pending_count(), 1)
