import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import sync_getnote_to_raw as getnote


class WorkflowTestNoteLookupTests(unittest.TestCase):
    def test_full_lookup_finds_note_after_first_page(self):
        calls = []

        def page(path, params, api_key, client_id, **options):
            calls.append(params)
            if len(calls) == 1:
                return {'data': {'notes': [{'note_id': '100', 'title': 'other'}],
                                  'has_more': True, 'cursor': 'next-page'}}
            return {'data': {'notes': [{'note_id': '200', 'title': 'test'}],
                             'has_more': False, 'cursor': 'done'}}

        with patch.object(getnote, 'api_get', side_effect=page):
            notes = getnote.fetch_note_list('key', 'client', max_pages=None)

        self.assertEqual([getnote.note_id_from(note) for note in notes], ['100', '200'])
        self.assertEqual(calls, [{}, {'cursor': 'next-page'}])


if __name__ == '__main__':
    unittest.main()
