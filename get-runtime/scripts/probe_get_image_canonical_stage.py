"""Privately stage one authenticated Get image candidate without publishing main."""
import argparse
import base64
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import quote

import get_private_runtime_bindings as bindings
import get_gitdata_staging as staging
import get_legacy_manual as manual
import get_private_preview_retention as retention
import get_publication_provenance as provenance
import get_recurring_runtime as runtime
import sync_getnote_to_raw as sync
from source_sync_rate import RequestPolicy

PROOF_CONTRACT = 'getnote-private-content-image-probe-1'
PROOF_COMMIT = '328fe058d8ad3190eeb3cce69a66ff49009c19fd'
PROOF_ROOT = '.sync/get-notion-asset-probes'
STAGES = {'proof', 'bindings', 'policy', 'source', 'canonical_prepare', 'private_stage', 'readback'}


class ProbeFailure(Exception):
    def __init__(self, stage, category='verification_failed'):
        self.stage = stage if stage in STAGES else 'readback'
        self.category = category if re.fullmatch(r'[a-z_]{1,64}', category or '') else 'verification_failed'
        super().__init__(self.category)


def fail(stage, category='verification_failed'):
    raise ProbeFailure(stage, category) from None


def fetch_proof(api, proof_ref):
    if (not isinstance(proof_ref, str) or not re.fullmatch(r'codex/get-asset-proof-[1-9][0-9]{0,19}-[1-9][0-9]{0,5}', proof_ref)):
        fail('proof')
    ref = api.call('GET', '/git/ref/heads/' + proof_ref, missing=True)
    if not isinstance(ref, dict) or ref.get('ref') != 'refs/heads/' + proof_ref:
        fail('proof')
    commit = ref.get('object', {}).get('sha')
    if commit != PROOF_COMMIT:
        fail('proof')
    rows = api.call('GET', '/contents/' + PROOF_ROOT + '?ref=' + commit, list_result=True)
    if not isinstance(rows, list) or len(rows) != 1:
        fail('proof')
    row = rows[0]
    path = row.get('path') if isinstance(row, dict) else None
    if (not isinstance(path, str) or not path.startswith(PROOF_ROOT + '/')
            or not path.endswith('.json') or row.get('type') != 'file'):
        fail('proof')
    value = api.call('GET', '/contents/' + quote(path, safe='/') + '?ref=' + commit)
    if value.get('path') != path or value.get('encoding') != 'base64' or not isinstance(value.get('content'), str):
        fail('proof')
    try:
        raw = base64.b64decode(value['content'].replace('\n', ''), validate=True)
        proof = json.loads(raw)
    except (ValueError, UnicodeError, TypeError):
        fail('proof')
    ident = proof.get('source_id') if isinstance(proof, dict) else None
    if (proof.get('contract') != PROOF_CONTRACT or proof.get('ref') != proof_ref
            or type(ident) is not str or re.fullmatch(r'[0-9]{19}', ident) is None
            or path != PROOF_ROOT + '/' + ident + '.json'
            or proof.get('source_field') != 'content_embed'
            or re.fullmatch(r'[0-9a-f]{64}', proof.get('source_content_sha256', '')) is None
            or not re.fullmatch(r'[0-9a-f]{64}', proof.get('source_json_sha256', ''))
            or not isinstance(proof.get('assets'), list) or len(proof['assets']) != 1
            or not isinstance(proof.get('parent'), str) or re.fullmatch(r'[0-9a-f]{40}', proof['parent']) is None):
        fail('proof')
    asset = proof['assets'][0]
    if (not isinstance(asset, dict)
            or not isinstance(asset.get('path'), str) or not asset['path'].startswith('Raw/03 Get/')
            or not re.fullmatch(r'[0-9a-f]{64}', asset.get('sha256', ''))
            or not re.fullmatch(r'[0-9a-f]{64}', asset.get('source_url_sha256', ''))
            or type(asset.get('bytes')) is not int or not 1 <= asset['bytes'] <= 16 * 1024 * 1024
            or asset.get('mime') not in {'image/png', 'image/jpeg', 'image/webp', 'image/gif'}):
        fail('proof')
    return proof


def load_policy():
    workflow = Path(__file__).resolve().parents[2] / '.github/workflows/get-canonical.yml'
    raw = workflow.read_text(encoding='utf-8')
    encoded = re.search(r"POLICY_B64: '([^']+)'", raw)
    digest = re.search(r"POLICY_SHA256: '([0-9a-f]{64})'", raw)
    if encoded is None or digest is None:
        fail('policy')
    try:
        value = json.loads(base64.b64decode(encoded.group(1), validate=True))
    except (ValueError, UnicodeError, TypeError):
        fail('policy')
    return runtime.validate_policy(value, digest.group(1), runtime.entry.BOOTSTRAP), digest.group(1)


def extract_candidate(plan, files, source_id, proof, source_json_sha256, source_content_sha256):
    get_root = runtime.r.GET + '/'
    state_path = get_root + runtime.engine.n.STATE
    receipt_path = runtime.entry.FULL_RECEIPT
    if (plan.get('source_scope') != 'single_note' or plan.get('single_source_id') != source_id
            or plan.get('proposed_content_ids') != [source_id]
            or state_path not in files or receipt_path not in files):
        fail('canonical_prepare')
    registry = json.loads(files[state_path])
    receipt = json.loads(files[receipt_path])
    row = registry.get('newById', {}).get(source_id)
    images = (((row or {}).get('source_metadata') or {}).get('inline_images') or {}).get('images')
    if (not isinstance(row, dict) or not isinstance(images, list) or len(images) != 1
            or receipt.get('contract') != runtime.entry.RECEIPT_CONTRACT
            or receipt.get('source_scope') != 'single_note'
            or receipt.get('changed_content_ids') != [source_id]
            or type(receipt.get('sequence')) is not int or receipt['sequence'] < 1):
        fail('canonical_prepare')
    markdown = get_root + row['filePath']
    image = images[0]
    proof_asset = proof['assets'][0]
    if (proof.get('source_field') != 'content_embed'
            or source_content_sha256 != proof['source_content_sha256']
            or source_json_sha256 != proof['source_json_sha256']
            or image.get('asset_sha256') != proof_asset['sha256']
            or image.get('asset_bytes') != proof_asset['bytes']
            or image.get('asset_mime') != proof_asset['mime']):
        fail('canonical_prepare', 'proof_binding_mismatch')
    asset_path = image.get('asset_path')
    if (markdown not in files or asset_path not in files
            or not asset_path.startswith(get_root + '_assets/images/')):
        fail('canonical_prepare')
    asset = files[asset_path]
    if (hashlib.sha256(asset).hexdigest() != image.get('asset_sha256')
            or len(asset) != image.get('asset_bytes')
            or manual.r.oid(asset) != image.get('asset_blob')):
        fail('canonical_prepare')
    # The Markdown uses a relative reference; never log or persist the source URL.
    if Path(asset_path).name.encode() not in files[markdown]:
        fail('canonical_prepare')
    return image


def verify_proof_parent(api, proof, parent):
    if not isinstance(proof, dict) or not re.fullmatch(r'[0-9a-f]{40}', parent or ''):
        fail('proof', 'proof_parent_not_ancestor')
    value = api.call('GET', '/compare/' + proof['parent'] + '...' + parent + '?per_page=1&page=1')
    if (value.get('status') not in {'ahead', 'identical'}
            or value.get('base_commit', {}).get('sha') != proof['parent']
            or value.get('merge_base_commit', {}).get('sha') != proof['parent']
            or value.get('head_commit', {}).get('sha') != parent):
        fail('proof', 'proof_parent_not_ancestor')


def publish_private_stage(api, token, candidate, proof):
    plan = candidate['plan'];files = candidate['files']
    parent = plan['parent_commit'];commit = plan['candidate_commit'];tree = plan['candidate_tree']
    # The proof was captured from an earlier main snapshot. Accept only a
    # forward descendant, never a fork or unrelated replacement history.
    verify_proof_parent(api, proof, parent)
    run_id = os.environ.get('GITHUB_RUN_ID', '');attempt = os.environ.get('GITHUB_RUN_ATTEMPT', '')
    if (not re.fullmatch(r'[1-9][0-9]{0,19}', run_id)
            or not re.fullmatch(r'[1-9][0-9]{0,5}', attempt)):
        fail('private_stage')
    ref = 'refs/heads/codex/get-image-proof-' + run_id + '-' + attempt
    ref_path = '/git/ref/' + ref.removeprefix('refs/')
    if api.call('GET', ref_path, missing=True) is not None:
        fail('private_stage', 'stage_already_exists')
    remote = manual.Remote(None, token)
    if manual.guard(remote) != parent:
        fail('private_stage', 'main_moved')
    parent_tree = candidate['parent_tree'];timestamp = candidate['parent_timestamp']
    if not timestamp.isdigit() or not re.fullmatch(r'[0-9a-f]{40}', parent_tree):
        fail('private_stage')
    actor_line = (staging.NAME + ' <' + staging.EMAIL + '> ' + timestamp + ' +0000').encode()
    raw_commit = (b'tree ' + tree.encode() + b'\nparent ' + parent.encode()
                  + b'\nauthor ' + actor_line + b'\ncommitter ' + actor_line
                  + b'\n\n' + staging.MESSAGE.encode())
    if hashlib.sha1(b'commit ' + str(len(raw_commit)).encode() + b'\0' + raw_commit).hexdigest() != commit:
        fail('private_stage')
    entries = []
    for path, data in sorted(files.items()):
        oid = manual.r.oid(data)
        value = api.call('POST', '/git/blobs', {'content': base64.b64encode(data).decode(), 'encoding': 'base64'})
        if value.get('sha') != oid:
            fail('private_stage')
        entries.append({'path': path, 'mode': '100644', 'type': 'blob', 'sha': oid})
    value = api.call('POST', '/git/trees', {'base_tree': parent_tree, 'tree': entries})
    if value.get('sha') != tree:
        fail('private_stage')
    date = datetime.fromtimestamp(int(timestamp), timezone.utc).isoformat().replace('+00:00', 'Z')
    actor = {'name': staging.NAME, 'email': staging.EMAIL, 'date': date}
    value = api.call('POST', '/git/commits', {'message': staging.MESSAGE, 'tree': tree,
        'parents': [parent], 'author': actor, 'committer': actor})
    if value.get('sha') != commit:
        fail('private_stage')
    if manual.guard(remote) != parent:
        fail('private_stage', 'main_moved')
    try:
        api.call('POST', '/git/refs', {'ref': ref, 'sha': commit})
    except Exception:
        observed = api.call('GET', ref_path, missing=True)
    else:
        observed = api.call('GET', ref_path)
    if (not isinstance(observed, dict) or observed.get('ref') != ref
            or observed.get('object', {}).get('type') != 'commit'
            or observed.get('object', {}).get('sha') != commit):
        fail('private_stage')
    if remote.commit(commit) != {'tree': tree, 'parents': [parent]}:
        fail('readback')
    metadata = remote.check_source_budget(commit)
    readback = {}
    for path, data in files.items():
        row = metadata.get(path)
        if row != ('100644', 'blob', manual.r.oid(data), len(data)):
            fail('readback')
        readback[path] = remote.blob(row[2], row[3])
        if readback[path] != data:
            fail('readback')
    if manual.guard(remote) != parent:
        fail('readback', 'main_moved')
    return ref, commit, readback


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--proof-ref', required=True)
    args = parser.parse_args(argv)
    token = os.environ.get('PRIVATE_VAULT_TOKEN')
    if not token:
        fail('bindings', 'credential_missing')
    api = staging.API(token)
    proof = fetch_proof(api, args.proof_ref)
    source_id = proof['source_id']
    # Mask before the identity is passed to any source/runtime call. The value
    # is never written to an Actions input, output, file, or summary.
    print('::add-mask::' + source_id, flush=True)
    try:
        bindings_path = os.environ.get(bindings.ENV_FILE)
        if not bindings_path:
            fail('bindings', 'binding_missing')
        bindings.value('get_canonical_writer', 'PREVIEW')
        policy, policy_sha = load_policy()
        source_key, client_id = sync.load_credentials()
        source = manual.c.Source(source_key, client_id, RequestPolicy(seconds=35 * 60))
        public = provenance.PublicGithub(os.environ.get('GITHUB_TOKEN'))
        captured = {}
        original_prepare = runtime.entry.prepare
        original_publish = runtime.m.publish
        def capture_prepare(reader, evidence, parent, source, **kwargs):
            plan, files = original_prepare(reader, evidence, parent, source, **kwargs)
            preflight_sha = os.environ.get('GET_PREFLIGHT_SOURCE_SHA', '')
            if not re.fullmatch(r'[0-9a-f]{40}', preflight_sha) or parent != preflight_sha:
                fail('canonical_prepare', 'preflight_parent_mismatch')
            snapshot = source.snapshot
            if snapshot is None or snapshot[0] != source_id or source_id not in source.details:
                fail('canonical_prepare', 'source_replay_missing')
            summaries, terminal = snapshot[1]
            items = runtime.entry.engine.a.source_snapshot(summaries, terminal, only_id=source_id)
            detail = manual.detail_note(source.details[source_id])
            runtime.entry.engine.n.source_note(items.get(source_id), detail)
            source_json_sha256 = hashlib.sha256(sync.json_text(detail).encode('utf-8')).hexdigest()
            source_content = detail.get('content') if isinstance(detail, dict) else None
            if not isinstance(source_content, str):
                fail('canonical_prepare', 'source_content_missing')
            source_content_sha256 = hashlib.sha256(source_content.encode('utf-8')).hexdigest()
            candidate = {'plan': plan, 'files': files,
                'parent_tree': reader.local.run('rev-parse', parent + '^{tree}').decode().strip(),
                'parent_timestamp': reader.local.run('show', '-s', '--format=%ct', parent).decode().strip(),
                'source_json_sha256': source_json_sha256,
                'source_content_sha256': source_content_sha256}
            captured.update(candidate)
            return plan, files
        def forbid_main_publish(*args, **kwargs):
            fail('private_stage', 'main_publish_forbidden')
        original_scope = runtime.validate_single_note_scope
        runtime.entry.prepare = capture_prepare
        runtime.m.publish = forbid_main_publish
        runtime.validate_single_note_scope = lambda ident, trigger, apply: runtime.engine.n.identity(ident) if ident is not None and trigger == 'manual' and not apply else original_scope(ident, trigger, apply)
        try:
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                result = runtime.run(token, source, policy=policy, approved_policy_sha256=policy_sha,
                    apply=False, trigger='manual', public_client=public, source_id=source_id)
        finally:
            runtime.entry.prepare = original_prepare
            runtime.m.publish = original_publish
            runtime.validate_single_note_scope = original_scope
        if result.get('status') != 'recurring_candidate_verified' or not captured:
            fail('canonical_prepare')
        image = extract_candidate(captured['plan'], captured['files'], source_id, proof,
                                  captured['source_json_sha256'], captured['source_content_sha256'])
        ref, commit, readback = publish_private_stage(api, token, captured, proof)
        if (set(readback) != set(captured['files'])
                or readback[image['asset_path']] != captured['files'][image['asset_path']]
                or readback[runtime.entry.FULL_RECEIPT] != captured['files'][runtime.entry.FULL_RECEIPT]):
            fail('readback')
        print(json.dumps({'status':'verified','private_ref':ref,'commit':commit,
            'main_updated':False,'changed_files':len(readback),'image_assets':1,
            'registry_receipt_readback':'verified','source_id_emitted':False},sort_keys=True))
        return 0
    except ProbeFailure:
        raise
    except Exception as exc:
        stage = getattr(exc, 'runtime_stage', 'canonical_prepare')
        category = str(exc) if re.fullmatch(r'[a-z_]{1,64}', str(exc) or '') else 'verification_failed'
        fail(stage, category)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except ProbeFailure as exc:
        print(f'Get image private candidate stopped at stage={exc.stage} category={exc.category}', file=sys.stderr)
        raise SystemExit(1)
