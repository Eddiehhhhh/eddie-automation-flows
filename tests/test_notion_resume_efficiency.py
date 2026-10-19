"""Synthetic native PNG pagination and per-invocation admission measurements."""
from contextlib import contextmanager
from copy import deepcopy
import unittest
from unittest.mock import patch

import get_notion_create_batches as batches
import get_notion_executor as e
import get_notion_live_transport as live
import get_notion_uploaded_png as image
import test_notion_executor as notes
import test_png_create_batches as png_batches


class Getter:
    def __init__(self, data):
        self.data = data
        self.downloads = 0
        self.response_bytes = 0
        self.elapsed = 0

    def __call__(self, url):
        # All URLs and bytes are synthetic; no network is used.
        assert self.downloads < 12 and self.elapsed < 120
        assert self.response_bytes + len(self.data) <= 8 * 1024 * 1024
        self.downloads += 1
        self.response_bytes += len(self.data)
        return self.data


class Tests(unittest.TestCase):
    @contextmanager
    def fixture(self):
        case = png_batches.Tests()
        fixture = case.fixture()
        original = fixture.outer.compare_and_set
        fixture.outer.max_commits = 400
        fixture.outer.max_batch_bytes = 192 * 1024 * 1024
        fixture.outer.commits_attempted = 0
        fixture.outer.new_blob_bytes = 0
        def save(expected, value):
            size = len(e.encoded(value))
            assert size <= e.MAX_STATE
            assert fixture.outer.commits_attempted + 1 <= fixture.outer.max_commits
            assert fixture.outer.new_blob_bytes + size <= fixture.outer.max_batch_bytes
            fixture.outer.commits_attempted += 1
            fixture.outer.new_blob_bytes += size
            return original(expected, value)
        fixture.outer.compare_and_set = save
        base = type(fixture.transport)
        class RotatingTransport(base):
            def __init__(self):
                super().__init__(fixture.api, fixture.g.images[notes.N])
                self.serial = 0
                self.clock = lambda: 0
                self.deadline = 1200
            def block(self, row, page):
                result = super().block(row, page)
                if row['type'] == 'image':
                    self.serial += 1
                    result['image']['file']['url'] = (
                        'https://prod-files-secure.s3.us-west-2.amazonaws.com/'
                        'synthetic-rotation-' + str(self.serial) + '.png')
                return result
            def __call__(self, method, path, body=None):
                assert self.attempts < 2000 and self.clock() < self.deadline
                self.attempts += 1
                result = super().__call__(method, path, body)
                self.bytes += len(e.encoded(result))
                assert self.bytes <= live.ro.MAX_HTTP_BYTES
                return result
        fixture.Transport = RotatingTransport
        try:
            yield fixture
        finally:
            case.doCleanups()

    def fresh_invocation(self, fixture):
        fixture.transport = fixture.Transport()
        fixture.getter = Getter(fixture.g.images[notes.N])
        fixture.adapter = image.SnapshotAdapter(fixture.resolver, fixture.outer.read,
            request=fixture.transport, asset_get=fixture.getter)
        fixture.outer.commits_attempted = 0
        fixture.outer.new_blob_bytes = 0

    def finish(self, fixture):
        metrics = []
        for _ in range(4):
            self.fresh_invocation(fixture)
            result = fixture.run_it(apply=True)
            metrics.append((fixture.transport.attempts, fixture.getter.downloads,
                            fixture.outer.commits_attempted))
            self.assertLessEqual(fixture.getter.downloads, 12)
            if notes.N in fixture.outer.value['engine_state']['receipts']:
                return result, metrics
            self.assertEqual(result['execution']['reason'], 'batch_boundary_yield', result)
        self.fail('Synthetic three-batch page did not finish')

    def test_rotating_png_three_batches_finish_in_two_invocations(self):
        with self.fixture() as fixture:
            result, metrics = self.finish(fixture)
            # Frozen old path is measured separately with this same synthetic fixture.
            # No cache or skipped verification: each native binding downloads
            # all three distinct returned URLs in every required fresh read.
            self.assertEqual(metrics, [(48, 9, 9), (39, 9, 6)])
            self.assertEqual(result['native_notes_read'], 0)
            self.assertEqual(result['execution']['created'], 1, result)
            self.assertEqual([c for c in fixture.api.calls if c[0] != 'read'],
                             [('create',), ('append',), ('append',)])
            state = fixture.outer.value
            self.assertIsNone(state['engine_state']['journal'])
            self.assertEqual(len(state['engine_state']['receipts'][notes.N]['managed_blocks']), 268)
            self.assertIn(notes.N, state['png_assets']['attachments'])
            self.assertNotIn(notes.N, state['pending_by_id'])
            self.fresh_invocation(fixture)
            repeat = fixture.run_it(apply=True)
            self.assertEqual(repeat['external_write_attempts'], 0, repeat)
            # The two untouched deferred records still need their own schema
            # and identity reads; the completed PNG note is not read or written.
            self.assertEqual(repeat['selected'], 2)
            self.assertEqual((fixture.transport.attempts, fixture.getter.downloads), (4, 0))
            self.assertEqual(len(fixture.api.calls), 3)
            self.fresh_invocation(fixture)
            settled_repeat = fixture.run_it(apply=True)
            self.assertEqual(settled_repeat['external_write_attempts'], 0)
            self.assertEqual((fixture.transport.attempts, fixture.getter.downloads), (0, 0))

    def final_boundary(self, fixture):
        original = batches.resources
        def hold(api, store, value, row, index, **kwargs):
            return False if kwargs.get('completion') else original(api, store, value, row, index)
        with patch.object(batches, 'resources', side_effect=hold):
            for _ in range(2):
                self.fresh_invocation(fixture)
                result = fixture.run_it(apply=True)
        operation = fixture.outer.value['engine_state']['journal']['operation']
        self.assertEqual((operation['batch_index'], operation['phase']), (2, 'verified'), result)
        return deepcopy(fixture.outer.value)

    def test_completion_only_uses_one_read_three_downloads_and_two_saves(self):
        with self.fixture() as fixture:
            before = self.final_boundary(fixture)
            self.fresh_invocation(fixture)
            fixture.getter.downloads = 9
            result = fixture.run_it(apply=True)
            self.assertEqual(result['execution']['created'], 1, result)
            self.assertEqual(result['native_notes_read'], 0)
            self.assertEqual((fixture.transport.attempts, fixture.getter.downloads,
                              fixture.outer.commits_attempted), (13, 12, 2))
            self.assertEqual(len(fixture.api.calls), 3)
            self.assertNotIn(notes.N, before['engine_state']['receipts'])

    def test_completion_admission_retains_all_hard_budget_boundaries(self):
        with self.fixture() as fixture:
            self.final_boundary(fixture)
            self.fresh_invocation(fixture)
            state, _ = fixture.store.read()
            row = state['journal']['plan']['records'][0]
            api = live.API(fixture.transport, notes.DS, [notes.N],
                data_source_pin=live.native.sha(notes.DS), database_pin=fixture.policy['database_pin'],
                snapshot_adapter=fixture.adapter, uploaded_pngs=fixture.resolver.handles())
            size = batches.checkpoint_size(state, row, fixture.store)
            reads = batches.read_requests(row['rendered']) + 4
            response_reserve = len(e.encoded(row['rendered'])) + batches.node_count(row['rendered']) * 1024 + 512 * 1024
            duration = batches.SAFETY_SECONDS + 2 * batches.CHECKPOINT_SECONDS + .4 * reads
            def admitted():
                return batches.resources(api, fixture.store, state, row, 2, completion=True)
            cases = [
                (fixture.getter, 'downloads', 9, 10),
                (fixture.getter, 'response_bytes', 8*1024*1024 - 3*len(fixture.getter.data), 8*1024*1024 - 3*len(fixture.getter.data) + 1),
                (fixture.getter, 'elapsed', 74, 75),
                (fixture.transport, 'attempts', 2000 - reads, 2001 - reads),
                (fixture.transport, 'bytes', live.ro.MAX_HTTP_BYTES - response_reserve, live.ro.MAX_HTTP_BYTES - response_reserve + 1),
                (fixture.transport, 'deadline', duration + 1, duration),
                (fixture.outer, 'commits_attempted', 398, 399),
                (fixture.outer, 'new_blob_bytes', 192*1024*1024 - 2*size, 192*1024*1024 - 2*size + 1),
            ]
            for obj, name, permitted, denied in cases:
                with self.subTest(resource=name):
                    old = getattr(obj, name)
                    setattr(obj, name, permitted); self.assertTrue(admitted())
                    setattr(obj, name, denied); self.assertFalse(admitted())
                    setattr(obj, name, old)
            with self.assertRaisesRegex(e.Blocked, 'state_invalid'):
                batches.resources(api, fixture.store, state, row, 1, completion=True)

    def test_manual_change_at_final_boundary_is_detected_before_ownership(self):
        for kind in ('edit', 'delete', 'insert', 'replace', 'reorder', 'property', 'image_bytes'):
            with self.subTest(kind=kind), self.fixture() as fixture:
                self.final_boundary(fixture)
                page = fixture.api.pages[notes.N]
                if kind == 'edit': page['recursive_blocks'][0]['payload']['rich_text'] = [e.p.text_run('Synthetic manual edit')]
                elif kind == 'delete': page['recursive_blocks'].pop()
                elif kind == 'insert':
                    extra, _ = fixture.api.rows([notes.paragraph('Synthetic manual insertion')], page['page_id'])
                    page['recursive_blocks'] += extra
                elif kind == 'replace': page['recursive_blocks'][0]['id'] = fixture.api.new_id()
                elif kind == 'reorder': page['recursive_blocks'][0], page['recursive_blocks'][1] = page['recursive_blocks'][1], page['recursive_blocks'][0]
                elif kind == 'property': page['properties_raw']['Manual'] = {'type': 'number', 'number': 3}
                self.fresh_invocation(fixture)
                if kind == 'image_bytes': fixture.getter.data = b'synthetic-invalid-png'
                changed = deepcopy(page)
                result = fixture.run_it(apply=True)
                self.assertEqual(result['execution']['status'], 'blocked', result)
                self.assertEqual(page, changed)
                self.assertNotIn(notes.N, fixture.outer.value['engine_state']['receipts'])
                self.assertNotIn(notes.N, fixture.outer.value['png_assets']['attachments'])
                self.assertIn(notes.N, fixture.outer.value['pending_by_id'])
                self.assertEqual(len(fixture.api.calls), 3)


    def test_rotating_png_crashes_before_and_after_every_checkpoint_hold_or_resume(self):
        # A three-batch page has 15 saves across however many invocations it
        # needs. Crash both sides of every save, including receipt and clear.
        for point in range(1, 16):
            for persisted in (False, True):
                with self.subTest(point=point, persisted=persisted), self.fixture() as fixture:
                    original = fixture.outer.compare_and_set
                    number = [0]
                    def crash(expected, value):
                        number[0] += 1
                        if number[0] == point and not persisted:
                            raise RuntimeError('Synthetic checkpoint interruption')
                        result = original(expected, value)
                        if number[0] == point:
                            raise RuntimeError('Synthetic checkpoint interruption')
                        return result
                    fixture.outer.compare_and_set = crash
                    for _ in range(3):
                        self.fresh_invocation(fixture)
                        result = fixture.run_it(apply=True)
                        if number[0] >= point:
                            break
                    self.assertEqual(number[0], point)
                    fixture.outer.compare_and_set = original
                    for _ in range(3):
                        self.fresh_invocation(fixture)
                        result = fixture.run_it(apply=True)
                        if result.get('execution', {}).get('reason') != 'batch_boundary_yield':
                            break
                    state = fixture.outer.value['engine_state']
                    writes = fixture.api.calls
                    self.assertLessEqual(writes.count(('create',)), 1)
                    self.assertLessEqual(writes.count(('append',)), 2)
                    self.assertFalse(any(c[0] == 'archive' for c in writes))
                    if notes.N in state['receipts']:
                        self.assertEqual(len(state['receipts'][notes.N]['managed_blocks']), 268)
                        self.assertIn(notes.N, fixture.outer.value['png_assets']['attachments'])
                        self.assertNotIn(notes.N, fixture.outer.value['pending_by_id'])
                        self.assertIsNone(state['journal'])
                    else:
                        self.assertEqual(result['execution']['status'], 'blocked', result)
                        self.assertIn(result['execution']['reason'],
                                      ('uncertain_create', 'uncertain_append', 'transport_read_failed'))
                        self.assertEqual(state['journal']['operation']['phase'], 'in_flight')
                        self.assertIn(notes.N, fixture.outer.value['pending_by_id'])
                        self.assertNotIn(notes.N, fixture.outer.value['png_assets']['attachments'])
                        before = deepcopy(fixture.outer.value)
                        count = len(writes)
                        self.fresh_invocation(fixture)
                        fixture.run_it(apply=True)
                        self.assertEqual(len(writes), count)
                        self.assertEqual(fixture.outer.value, before)

    def test_saved_plan_source_writer_and_duplicate_guards_still_run(self):
        for guard in ('source', 'writer', 'duplicate'):
            with self.subTest(guard=guard), self.fixture() as fixture:
                before = self.final_boundary(fixture)
                self.fresh_invocation(fixture)
                if guard == 'source':
                    with patch.object(fixture.pub, 'check', side_effect=e.Blocked('source_changed')):
                        result = fixture.run_it(apply=True)
                    self.assertEqual(result['stage'], 'source', result)
                    self.assertEqual(fixture.transport.attempts, 0)
                elif guard == 'writer':
                    fixture.writers['fresh_readback_verified'] = False
                    result = fixture.run_it(apply=True)
                    self.assertEqual(result['stage'], 'writers', result)
                    self.assertEqual(fixture.transport.attempts, 0)
                else:
                    with patch.object(live.API, 'matches', return_value=['a'*32, 'b'*32]):
                        result = fixture.run_it(apply=True)
                    self.assertEqual(result['execution']['status'], 'blocked', result)
                self.assertEqual(fixture.outer.value, before)
                self.assertEqual(len(fixture.api.calls), 3)


if __name__ == '__main__':
    unittest.main()
