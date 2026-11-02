"""Verify one privately selected attachment sample without advancing pagination."""
import json
import os
import re
import urllib.parse
from pathlib import Path
from source_sync_assets import AssetStore
from sync_getnote_to_raw import load_credentials, fetch_note_detail, note_id_from, render_note, write_if_changed

root = Path(os.environ['VAULT_ROOT'])
folder = root / 'Raw/03 Get/01 Notes/Cloud Proof'
identity = json.loads((folder / 'asset-probe.json').read_text())['note_id']
if not isinstance(identity, str) or not identity.isdigit():
    raise RuntimeError('Invalid private sample ID')
key, client = load_credentials()
note = fetch_note_detail(identity, key, client)
if note_id_from(note) != identity or not isinstance(note.get('content'), str):
    raise RuntimeError('Sample detail mismatch')
# Get may embed images in Markdown rather than expose attachment descriptors.
inline = re.findall(r'!\[[^\]]*\]\((https://[^\s)]+)\)', note['content'])
note['attachments'] = list(note.get('attachments') or []) + [
    {'type': 'image', 'url': url} for url in dict.fromkeys(inline)]
assets = AssetStore(root / 'Raw/03 Get').materialize(note)
for asset in assets:
    if asset['status'] == 'downloaded':
        relative = os.path.relpath(root / 'Raw/03 Get' / asset['path'], folder)
        note['content'] = note['content'].replace(asset['url'], urllib.parse.quote(relative))
if not any(a['status'] == 'downloaded' for a in assets):
    raise RuntimeError('Sample contains no downloadable image/file descriptors')
path = folder / (identity + '.md')
write_if_changed(path, render_note(note, assets, path))
write_if_changed(folder / 'asset-result.json', json.dumps({'note_id': identity, 'assets': assets}, ensure_ascii=False) + '\n')
print('Private sample attachment files and relative links prepared')
