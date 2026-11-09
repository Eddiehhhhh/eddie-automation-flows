"""Audio attachments supply a URL without mixing their duration units."""
import importlib
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
bindings = types.ModuleType('get_private_runtime_bindings')
bindings.value = lambda *args: '0' * 64
with patch.dict(sys.modules, {'get_private_runtime_bindings': bindings}):
    renderer = importlib.import_module('get_mirror_render')
    canonical = importlib.import_module('get_canonical_writer')


class GetAudioAttachmentRender(unittest.TestCase):
    def setUp(self):
        self.attachment = {
            'type': 'audio', 'url': 'https://example.invalid/audio.mp3',
            'title': 'sample audio', 'duration': 8151120,
        }
        self.note = {
            'note_id': '1234567890', 'note_type': 'audio',
            'created_at': 1760000000000, 'updated_at': 1760000000000,
            'audio': {'original': 'transcript'},
            'attachments': [self.attachment],
        }

    def render_fields(self, note=None):
        _, fields = renderer.render(note or self.note, [], Path('audio.md'))
        return fields

    def test_audio_attachment_url_fills_missing_audio_url_only(self):
        fields = self.render_fields()
        self.assertEqual(fields['audio_url'], self.attachment['url'])
        self.assertEqual(fields['audio_duration'], 0)
        self.assertEqual(fields['audio_transcript'], 'transcript')
        self.assertEqual(fields['attachments'], [self.attachment['url']])

    def test_explicit_audio_url_keeps_precedence(self):
        note = {**self.note, 'audio': {'original': 'transcript',
                                      'play_url': 'https://example.invalid/official.mp3'}}
        self.assertEqual(self.render_fields(note)['audio_url'],
                         'https://example.invalid/official.mp3')

    def test_registry_keeps_attachment_duration_as_source_value(self):
        metadata = canonical.source_metadata(self.note)
        self.assertEqual(metadata['attachments'], [self.attachment])
        self.assertEqual(metadata['attachments'][0]['duration'], 8151120)


if __name__ == '__main__':
    unittest.main()
