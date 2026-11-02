"""One resumable page per run; isolated Get proof branch only."""
import hashlib
import json
import os
from pathlib import Path
from source_sync_assets import AssetStore
from sync_getnote_to_raw import (api_get, LIST_PATH, load_credentials,
    fetch_note_detail, note_id_from, render_note, write_if_changed)


def sync_page(root, listing, detail, store=None):
    folder = root / 'Raw/03 Get/01 Notes/Cloud Proof'
    state_path = folder / 'state.json'
    state = json.loads(state_path.read_text()) if state_path.exists() else {'cursor': None, 'notes': {}}
    payload = listing(state['cursor'])
    batch = payload.get('notes', payload.get('list', payload.get('records', payload.get('items'))))
    if not isinstance(batch, list) or not isinstance(payload.get('has_more'), bool):
        raise RuntimeError('Invalid page contract')
    ids = [note_id_from(item) for item in batch]
    if len(set(ids)) != len(ids) or any(not item.isdigit() for item in ids):
        raise RuntimeError('Invalid or repeated note IDs')
    cursor = payload.get('cursor', payload.get('next_cursor')) if payload['has_more'] else None
    if payload['has_more'] and (not batch or cursor is None or str(cursor) == str(state['cursor'])):
        raise RuntimeError('Invalid next cursor')
    rendered = {}
    assets = {}
    store = store or AssetStore(root / "Raw/03 Get")
    for identity in ids:
        note = detail(identity)
        if note_id_from(note) != identity or not isinstance(note.get('content'), str):
            raise RuntimeError('Invalid note detail')
        assets[identity] = store.materialize(note)
        rendered[identity] = render_note(note, assets[identity], folder / (identity + ".md"))
    # Advance only after every detail in the page is successfully rendered.
    for identity, body in rendered.items():
        write_if_changed(folder / (identity + '.md'), body)
        state['notes'][identity] = hashlib.sha256(body.encode()).hexdigest()
    state.setdefault('assets', {}).update(assets)
    state['cursor'] = cursor
    write_if_changed(state_path, json.dumps(state, sort_keys=True, ensure_ascii=False) + '\n')
    return len(rendered)


def main():
    key, client = load_credentials()
    def listing(cursor):
        response = api_get(LIST_PATH, {'cursor': str(cursor)} if cursor is not None else {}, key, client)
        return response.get('data', response)
    count = sync_page(Path(os.environ['VAULT_ROOT']), listing,
                      lambda identity: fetch_note_detail(identity, key, client))
    print('Get page synchronized; note count:', count)


if __name__ == '__main__':
    main()
