import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import get_legacy_capture as capture
import get_legacy_adapter as adapter


class Policy:
    def before_request(self):
        pass

    def rate_limited(self, *args, **kwargs):
        pass


class Transport:
    LIST_PATH = '/list'
    DETAIL_PATH = '/detail'

    def __init__(self, body):
        self.body = body
        self.calls = []

    def api_get(self, path, params, *args, **kwargs):
        self.calls.append((path, params))
        if path == self.LIST_PATH:
            raise AssertionError('single-note mode must not enumerate the catalog')
        return self.body


class SingleSourceScope(unittest.TestCase):
    def test_snapshot_requires_an_explicit_partial_scope(self):
        note = {'note_id': '123', 'updated_at': '2026-11-05T00:00:00Z'}
        marker = {'contract': 'get-single-note-source-scope-1',
                  'note_id': '123', 'complete_catalog': False}
        self.assertEqual(adapter.source_snapshot([note], True), {'123': note})
        self.assertEqual(adapter.source_snapshot([note], marker, only_id='123'), {'123': note})
        for terminal, only_id, rows in (
            (marker, None, [note]),
            (True, '123', [note]),
            ({**marker, 'complete_catalog': True}, '123', [note]),
            ({**marker, 'note_id': '456'}, '123', [note]),
            (marker, '123', [note, note]),
            (marker, '123', [{'note_id': '456', 'updated_at': note['updated_at']}]),
        ):
            with self.subTest(terminal=terminal, only_id=only_id, count=len(rows)), self.assertRaises(adapter.ParityFailure):
                adapter.source_snapshot(rows, terminal, only_id=only_id)

    def test_exact_note_fetch_uses_detail_and_labels_catalog_incomplete(self):
        note = {'note_id': '123', 'updated_at': '2026-11-05T00:00:00Z', 'content': 'body'}
        transport = Transport(json.dumps({'success': True, 'data': {'note': note}}).encode())
        source = capture.Source('key', 'client', Policy(), transport=transport)

        summaries, scope = source.enumerate('123')

        self.assertEqual(summaries, [note])
        self.assertEqual(scope, {'contract': 'get-single-note-source-scope-1',
                                 'note_id': '123', 'complete_catalog': False})
        self.assertEqual(transport.calls, [(Transport.DETAIL_PATH,
                                            {'id': '123', 'image_quality': 'original'})])
        self.assertEqual(source.details, 1)

    def test_exact_note_rejects_wrong_identity_and_malformed_scope(self):
        wrong = {'note_id': '456', 'updated_at': '2026-11-05T00:00:00Z', 'content': 'body'}
        transport = Transport(json.dumps({'success': True, 'data': {'note': wrong}}).encode())
        source = capture.Source('key', 'client', Policy(), transport=transport)
        with self.assertRaises(capture.ParityFailure):
            source.enumerate('123')

    def test_exact_note_rejects_invalid_timestamp(self):
        note = {'note_id': '123', 'updated_at': 'not-a-time', 'content': 'body'}
        transport = Transport(json.dumps({'success': True, 'data': {'note': note}}).encode())
        source = capture.Source('key', 'client', Policy(), transport=transport)
        with self.assertRaises(capture.ParityFailure):
            source.enumerate('123')


if __name__ == '__main__':
    unittest.main()
