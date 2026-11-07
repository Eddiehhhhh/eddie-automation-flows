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
import tempfile
from urllib.parse import quote

# The probe and the runtime under review intentionally live in separate
# checkouts. Import every production module from the exact pinned runtime,
# never from this probe branch.
FORMAL_RUNTIME_SHA = 'f6dcd238afd3599f37d8b2a6420cb6cc9b47f143'
FORMAL_RUNTIME_ROOT = Path(os.environ.get(
    'GET_FORMAL_RUNTIME_ROOT', str(Path(__file__).resolve().parents[1])))
FORMAL_SCRIPTS = FORMAL_RUNTIME_ROOT / 'scripts'
import subprocess
if not FORMAL_SCRIPTS.is_dir():
    raise SystemExit('formal_runtime_checkout_invalid')
sys.path.insert(0, str(FORMAL_SCRIPTS))


def verify_formal_runtime_checkout():
    if not re.fullmatch(r'[0-9a-f]{40}', FORMAL_RUNTIME_SHA):
        fail('bindings', 'formal_runtime_sha_mismatch')
    try:
        formal_head = subprocess.check_output(
            ['git', '-C', str(FORMAL_RUNTIME_ROOT.parent), 'rev-parse', 'HEAD'],
            text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        fail('bindings', 'formal_runtime_checkout_invalid')
    if formal_head != FORMAL_RUNTIME_SHA:
        fail('bindings', 'formal_runtime_sha_mismatch')

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
STAGES = {'proof', 'bindings', 'policy', 'source', 'canonical_prepare', 'private_proof', 'readback'}


class ProbeFailure(Exception):
    def __init__(self, stage, category='verification_failed'):
        self.stage = stage if stage in STAGES else 'readback'
        self.category = category if re.fullmatch(r'[a-z_]{1,64}', category or '') else 'verification_failed'
        super().__init__(self.category)


def fail(stage, category='verification_failed'):
    raise ProbeFailure(stage, category) from None


def verify_proof_tree_scope(api, parent_tree, candidate_tree, proof_path, proof_oid, proof_size):
    """Prove one leaf changed by comparing complete non-recursive tree levels."""
    if (not re.fullmatch(r'[0-9a-f]{40}', parent_tree or '')
            or not re.fullmatch(r'[0-9a-f]{40}', candidate_tree or '')
            or not isinstance(proof_path, str)):
        fail('private_proof', 'proof_tree_scope_mismatch')
    parts = proof_path.split('/')
    if (not parts or any(not part or part in {'.', '..'} for part in parts)
            or type(proof_size) is not int or proof_size < 0
            or re.fullmatch(r'[0-9a-f]{40}', proof_oid or '') is None):
        fail('private_proof', 'proof_tree_scope_mismatch')

    def entries(tree_oid):
        value = api.call('GET', '/git/trees/' + tree_oid)
        rows = value.get('tree') if isinstance(value, dict) else None
        if (not isinstance(value, dict) or value.get('sha') != tree_oid
                or value.get('truncated') is not False or not isinstance(rows, list)):
            fail('private_proof', 'proof_tree_incomplete')
        found = {}
        for row in rows:
            if not isinstance(row, dict):
                fail('private_proof', 'proof_tree_invalid')
            name, mode, kind, oid = row.get('path'), row.get('mode'), row.get('type'), row.get('sha')
            if (not isinstance(name, str) or not name or '/' in name or name in {'.', '..'}
                    or name in found or not isinstance(mode, str)
                    or kind not in {'blob', 'tree', 'commit'}
                    or not isinstance(oid, str) or re.fullmatch(r'[0-9a-f]{40}', oid) is None
                    or (kind == 'tree' and mode != '040000')
                    or (kind == 'commit' and mode != '160000')
                    or (kind == 'blob' and mode not in {'100644', '100755', '120000'})):
                fail('private_proof', 'proof_tree_invalid')
            size = row.get('size')
            if kind == 'blob':
                if type(size) is not int or size < 0:
                    fail('private_proof', 'proof_tree_invalid')
            elif size is not None:
                fail('private_proof', 'proof_tree_invalid')
            found[name] = (mode, kind, oid, size)
        return found

    before_oid, after_oid = parent_tree, candidate_tree
    for index, name in enumerate(parts):
        before = {} if before_oid is None else entries(before_oid)
        after = entries(after_oid)
        before_target = before.pop(name, None)
        after_target = after.pop(name, None)
        if before != after:
            fail('private_proof', 'proof_tree_sibling_mismatch')
        is_leaf = index == len(parts) - 1
        if is_leaf:
            if before_target is not None:
                fail('private_proof', 'proof_path_exists')
            if after_target != ('100644', 'blob', proof_oid, proof_size):
                fail('private_proof', 'proof_tree_scope_mismatch')
            return
        if (after_target is None or after_target[0:2] != ('040000', 'tree')
                or (before_target is not None and before_target[0:2] != ('040000', 'tree'))):
            fail('private_proof', 'proof_tree_scope_mismatch')
        before_oid = None if before_target is None else before_target[2]
        after_oid = after_target[2]
    fail('private_proof', 'proof_tree_scope_mismatch')


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
    # This is an isolated candidate policy, not a production policy update.
    # Keep repository/workflow/job identity fields from the reviewed policy,
    # and bind the exact runtime and template that this probe imports.
    value['code_sha'] = FORMAL_RUNTIME_SHA
    value['rules_sha256'] = runtime.entry.rules_sha256()
    value['get_policy_sha256'] = runtime.entry.policy_sha256(
        value['rules_sha256'], runtime.entry.BOOTSTRAP)
    value['controller_template_sha256'] = runtime.controller.template_sha256()
    candidate_sha = runtime.entry.a.sha(runtime.entry.a.encoded(value))
    return runtime.validate_policy(value, candidate_sha, runtime.entry.BOOTSTRAP), candidate_sha


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
    matches = {
        'source_json_match': source_json_sha256 == proof['source_json_sha256'],
        'source_content_match': source_content_sha256 == proof['source_content_sha256'],
        'image_sha256_match': image.get('asset_sha256') == proof_asset['sha256'],
        'image_bytes_match': image.get('asset_bytes') == proof_asset['bytes'],
        'image_mime_match': image.get('asset_mime') == proof_asset['mime'],
    }
    return image, matches, markdown


def build_current_proof(*, proof_ref, source_id, old_proof_ref, parent, detail,
                        summary, image, markdown_path, files, source_json_sha256,
                        source_content_sha256, detail_received_at_utc,
                        created_at_utc):
    if (not isinstance(proof_ref, str)
            or not re.fullmatch(r'codex/get-asset-proof-[1-9][0-9]{0,19}-[1-9][0-9]{0,5}', proof_ref)
            or not isinstance(old_proof_ref, str)
            or not re.fullmatch(r'codex/get-asset-proof-[1-9][0-9]{0,19}-[1-9][0-9]{0,5}', old_proof_ref)
            or proof_ref == old_proof_ref
            or not re.fullmatch(r'[0-9]{19}', source_id)
            or not re.fullmatch(r'[0-9a-f]{40}', parent or '')
            or not isinstance(detail, dict) or not isinstance(summary, dict)
            or not isinstance(image, dict) or not isinstance(files, dict)):
        fail('private_proof', 'proof_record_invalid')
    try:
        detail_version = runtime.entry.engine.raw_source_version(detail.get('updated_at'))
        summary_version = runtime.entry.engine.raw_source_version(summary.get('updated_at'))
    except Exception:
        fail('canonical_prepare', 'source_version_invalid')
    if detail_version != summary_version:
        fail('canonical_prepare', 'source_version_race')
    if (hashlib.sha256(sync.json_text(detail).encode('utf-8')).hexdigest() != source_json_sha256
            or not isinstance(detail.get('content'), str)
            or hashlib.sha256(detail['content'].encode('utf-8')).hexdigest() != source_content_sha256):
        fail('canonical_prepare', 'captured_detail_changed')
    timestamp_pattern = r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ'
    if (not re.fullmatch(timestamp_pattern, detail_received_at_utc or '')
            or not re.fullmatch(timestamp_pattern, created_at_utc or '')):
        fail('private_proof', 'proof_record_invalid')
    try:
        if (runtime.entry.engine.n.note_identity(detail) != source_id
                or runtime.entry.engine.n.note_identity(summary) != source_id):
            fail('canonical_prepare', 'proof_source_identity_mismatch')
        references = runtime.entry.engine.inline_images.extract(detail['content'])
    except ProbeFailure:
        raise
    except Exception:
        fail('canonical_prepare', 'proof_source_binding_invalid')
    image_path = image.get('asset_path')
    if (not isinstance(image_path, str) or image_path not in files
            or not isinstance(markdown_path, str)
            or not markdown_path.startswith(runtime.r.GET + '/')
            or markdown_path not in files
            or len(references) != 1
            or references[0].get('reference_sha256') != image.get('reference_sha256')
            or references[0].get('source_sha256') != image.get('source_sha256')
            or not isinstance(image.get('reference_sha256'), str)
            or not re.fullmatch(r'[0-9a-f]{64}', image['reference_sha256'])
            or not isinstance(image.get('source_sha256'), str)
            or not re.fullmatch(r'[0-9a-f]{64}', image['source_sha256'])
            or not isinstance(image.get('asset_sha256'), str)
            or not re.fullmatch(r'[0-9a-f]{64}', image['asset_sha256'])
            or image.get('asset_bytes') != len(files[image_path])
            or image.get('asset_sha256') != hashlib.sha256(files[image_path]).hexdigest()
            or not isinstance(image.get('asset_mime'), str)
            or image.get('asset_mime') not in {'image/png', 'image/jpeg', 'image/webp', 'image/gif'}):
        fail('canonical_prepare', 'proof_asset_binding_invalid')
    try:
        runtime.r.safe_path(markdown_path)
        runtime.r.safe_path(image_path)
    except Exception:
        fail('canonical_prepare', 'proof_asset_path_invalid')
    proof_path = PROOF_ROOT + '/' + source_id + '.json'
    return {
        'contract': PROOF_CONTRACT,
        'proof_version': 2,
        'ref': proof_ref,
        'supersedes_ref': old_proof_ref,
        'source_id': source_id,
        'source_field': 'content_embed',
        'source_json_sha256': source_json_sha256,
        'source_content_sha256': source_content_sha256,
        'source_detail_updated_at': detail_version,
        'source_summary_updated_at': summary_version,
        'source_summary_sha256': hashlib.sha256(sync.json_text(summary).encode('utf-8')).hexdigest(),
        'source_detail_received_at_utc': detail_received_at_utc,
        'proof_created_at_utc': created_at_utc,
        'parent': parent,
        'note_path': markdown_path,
        'note_sha256': hashlib.sha256(files[markdown_path]).hexdigest(),
        'assets': [{
            'path': image_path,
            'sha256': image['asset_sha256'],
            'source_url_sha256': image['reference_sha256'],
            'source_embed_sha256': image['source_sha256'],
            'bytes': image['asset_bytes'],
            'mime': image['asset_mime'],
        }],
    }


def publish_current_proof(api, token, parent, parent_tree, proof):
    proof_ref = proof['ref']
    proof_path = PROOF_ROOT + '/' + proof['source_id'] + '.json'
    ref_path = '/git/ref/heads/' + proof_ref
    remote = manual.Remote(None, token)
    try:
        if manual.guard(remote) != parent:
            fail('private_proof', 'main_moved')
        if api.call('GET', ref_path, missing=True) is not None:
            fail('private_proof', 'proof_ref_exists')
        parent_proofs = api.call('GET', '/contents/' + PROOF_ROOT + '?ref=' + parent,
            missing=True, list_result=True)
        if parent_proofs not in (None, []):
            fail('private_proof', 'proof_inventory_not_empty')
        if api.call('GET', '/contents/' + quote(proof_path, safe='/') + '?ref=' + parent, missing=True) is not None:
            fail('private_proof', 'proof_path_exists')
        proof_bytes = (json.dumps(proof, sort_keys=True, ensure_ascii=False,
            separators=(',', ':')) + '\n').encode('utf-8')
        proof_oid = manual.r.oid(proof_bytes)
        blob = api.call('POST', '/git/blobs', {
            'content': base64.b64encode(proof_bytes).decode('ascii'), 'encoding': 'base64'})
        if blob.get('sha') != proof_oid:
            fail('private_proof', 'proof_blob_mismatch')
        tree = api.call('POST', '/git/trees', {
            'base_tree': parent_tree,
            'tree': [{'path': proof_path, 'mode': '100644', 'type': 'blob', 'sha': proof_oid}],
        }).get('sha')
        if not isinstance(tree, str) or not re.fullmatch(r'[0-9a-f]{40}', tree):
            fail('private_proof', 'proof_tree_invalid')
        timestamp = datetime.strptime(proof['proof_created_at_utc'], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
        epoch = str(int(timestamp.timestamp()))
        actor_line = ('Get source proof <get-source-proof@example.invalid> '
                      + epoch + ' +0000').encode('ascii')
        message = b'Capture verified Get image source proof\n'
        raw_commit = (b'tree ' + tree.encode('ascii') + b'\nparent ' + parent.encode('ascii')
            + b'\nauthor ' + actor_line + b'\ncommitter ' + actor_line + b'\n\n' + message)
        commit_sha = hashlib.sha1(b'commit ' + str(len(raw_commit)).encode('ascii')
            + b'\0' + raw_commit).hexdigest()
        date = timestamp.isoformat().replace('+00:00', 'Z')
        actor = {'name': 'Get source proof', 'email': 'get-source-proof@example.invalid', 'date': date}
        commit = api.call('POST', '/git/commits', {'message': message.decode('utf-8'),
            'tree': tree, 'parents': [parent], 'author': actor, 'committer': actor})
        if commit.get('sha') != commit_sha:
            fail('private_proof', 'proof_commit_mismatch')
        verify_proof_tree_scope(api, parent_tree, tree, proof_path, proof_oid, len(proof_bytes))
        if manual.guard(remote) != parent:
            fail('private_proof', 'main_moved')
        try:
            api.call('POST', '/git/refs', {'ref': 'refs/heads/' + proof_ref, 'sha': commit_sha})
        except Exception:
            # One read only after an ambiguous create. Never try the create again.
            observed = api.call('GET', ref_path, missing=True)
        else:
            observed = api.call('GET', ref_path)
        if (not isinstance(observed, dict) or observed.get('ref') != 'refs/heads/' + proof_ref
                or observed.get('object', {}).get('type') != 'commit'
                or observed.get('object', {}).get('sha') != commit_sha):
            fail('private_proof', 'proof_ref_unverified')
        commit_readback = remote.commit(commit_sha)
        if commit_readback != {'tree': tree, 'parents': [parent]}:
            fail('private_proof', 'proof_commit_readback_mismatch')
        rows = api.call('GET', '/contents/' + PROOF_ROOT + '?ref=' + commit_sha, list_result=True)
        if (not isinstance(rows, list) or len(rows) != 1
                or rows[0].get('path') != proof_path or rows[0].get('type') != 'file'):
            fail('private_proof', 'proof_inventory_mismatch')
        value = api.call('GET', '/contents/' + quote(proof_path, safe='/') + '?ref=' + commit_sha)
        if value.get('path') != proof_path or value.get('encoding') != 'base64':
            fail('private_proof', 'proof_readback_mismatch')
        readback = base64.b64decode(value.get('content', '').replace('\n', ''), validate=True)
        if readback != proof_bytes or json.loads(readback) != proof:
            fail('private_proof', 'proof_readback_mismatch')
        if manual.guard(remote) != parent:
            fail('private_proof', 'main_moved')
        return proof_ref, commit_sha
    except ProbeFailure:
        raise
    except Exception:
        fail('private_proof', 'proof_publication_failed')


class TimedReplaySource(runtime.ReplaySource):
    """Record when the one canonical detail response was received."""
    def __init__(self, source):
        super().__init__(source)
        self.detail_received_at_utc = {}

    def detail_raw(self, ident):
        was_replaying = self.replaying
        value = super().detail_raw(ident)
        if not was_replaying:
            self.detail_received_at_utc[ident] = datetime.now(timezone.utc).replace(
                microsecond=0).isoformat().replace('+00:00', 'Z')
        return value


def verify_proof_parent(api, proof, parent):
    if not isinstance(proof, dict) or not re.fullmatch(r'[0-9a-f]{40}', parent or ''):
        fail('proof', 'proof_parent_not_ancestor')
    value = api.call('GET', '/compare/' + proof['parent'] + '...' + parent + '?per_page=1&page=1')
    status = value.get('status')
    ahead = value.get('ahead_by')
    behind = value.get('behind_by')
    counts_match = (type(ahead) is int and type(behind) is int and
                    ((status == 'ahead' and ahead > 0 and behind == 0)
                     or (status == 'identical' and ahead == 0 and behind == 0)))
    # GitHub's compare response has no `head_commit` field. The requested URL
    # binds the exact target SHA; status and the returned base/merge base prove
    # that the proof parent is its ancestor without inferring a substitute head.
    if (not counts_match
            or value.get('base_commit', {}).get('sha') != proof['parent']
            or value.get('merge_base_commit', {}).get('sha') != proof['parent']):
        fail('proof', 'proof_parent_not_ancestor')


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--proof-ref', required=True)
    args = parser.parse_args(argv)
    token = os.environ.get('PRIVATE_VAULT_TOKEN')
    if not token:
        fail('bindings', 'credential_missing')
    verify_formal_runtime_checkout()
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
        if runtime.validate_policy(policy, policy_sha, runtime.entry.BOOTSTRAP) != policy:
            fail('policy')
        parent = manual.Remote(None, token)
        parent_sha = manual.guard(parent)
        preflight_sha = os.environ.get('GET_PREFLIGHT_SOURCE_SHA', '')
        if not re.fullmatch(r'[0-9a-f]{40}', preflight_sha) or parent_sha != preflight_sha:
            fail('canonical_prepare', 'preflight_parent_mismatch')
        verify_proof_parent(api, proof, parent_sha)
        public.private_proof_source = runtime.private_proof.PrivateRecords(parent)
        public.private_proof_source.expected_head = parent_sha
        candidate = {}
        with tempfile.TemporaryDirectory(prefix='get-image-prepare-only-') as folder:
            root = Path(folder)
            local = manual.r.readback_checkout(
                root / 'objects', runtime.entry.BOOTSTRAP['capture_anchor']['capture_commit'], token)
            parent.local = local
            reader, evidence, _ = manual.load_evidence(
                local, parent, token, root, parent_sha,
                anchor=runtime.entry.BOOTSTRAP['capture_anchor'])
            trusted_root = root / 'trusted'
            trusted_root.mkdir(mode=0o700)
            replay = TimedReplaySource(source)
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                anchor, preview, history_witness = runtime.load_trusted_inputs(
                    local, reader, evidence, parent, token, trusted_root, parent_sha,
                    public, policy, bootstrap=runtime.entry.BOOTSTRAP,
                    exclude_run_id=None, exclude_run_attempt=None, source_id=source_id)
                plan, files = runtime.entry.prepare(
                    reader, evidence, parent_sha, replay,
                    approved_rules_sha256=policy['rules_sha256'], anchor=anchor,
                    preview=preview, bootstrap=runtime.entry.BOOTSTRAP,
                    trigger='manual', history_witness=history_witness,
                    source_id=source_id)
            replay.seal()
            if (plan.get('source_scope') != 'single_note'
                    or plan.get('single_source_id') != source_id
                    or plan.get('proposed_content_ids') != [source_id]
                    or replay.snapshot is None or replay.snapshot[0] != source_id
                    or source_id not in replay.details):
                fail('canonical_prepare', 'single_note_contract_mismatch')
            summaries, terminal = replay.snapshot[1]
            items = runtime.entry.engine.a.source_snapshot(summaries, terminal, only_id=source_id)
            detail = manual.detail_note(replay.details[source_id])
            summary = items.get(source_id)
            runtime.entry.engine.n.source_note(summary, detail)
            detail_version = runtime.entry.engine.raw_source_version(detail.get('updated_at'))
            summary_version = runtime.entry.engine.raw_source_version(summary.get('updated_at'))
            if detail_version != summary_version:
                fail('canonical_prepare', 'source_version_race')
            detail_received_at_utc = replay.detail_received_at_utc.get(source_id)
            if not isinstance(detail_received_at_utc, str):
                fail('canonical_prepare', 'source_detail_timestamp_missing')
            source_json_sha256 = hashlib.sha256(sync.json_text(detail).encode('utf-8')).hexdigest()
            source_content = detail.get('content') if isinstance(detail, dict) else None
            if not isinstance(source_content, str):
                fail('canonical_prepare', 'source_content_missing')
            source_content_sha256 = hashlib.sha256(source_content.encode('utf-8')).hexdigest()
            runtime.entry.verify_plan(
                reader, evidence, plan, files, policy['rules_sha256'],
                bootstrap=runtime.entry.BOOTSTRAP,
                history_witness=history_witness, source_id=source_id)
            candidate = {'plan': plan, 'files': files,
                'parent_tree': reader.local.run('rev-parse', parent_sha + '^{tree}').decode().strip(),
                'source_json_sha256': source_json_sha256,
                'source_content_sha256': source_content_sha256,
                'detail': detail,
                'summary': summary,
                'detail_received_at_utc': detail_received_at_utc}
            if manual.guard(parent) != parent_sha:
                fail('private_proof', 'main_moved')
        image, proof_matches, markdown_path = extract_candidate(
            candidate['plan'], candidate['files'], source_id, proof,
            candidate['source_json_sha256'], candidate['source_content_sha256'])
        run_id = os.environ.get('GITHUB_RUN_ID', '')
        attempt = os.environ.get('GITHUB_RUN_ATTEMPT', '')
        if (not re.fullmatch(r'[1-9][0-9]{0,19}', run_id)
                or not re.fullmatch(r'[1-9][0-9]{0,5}', attempt)):
            fail('private_proof', 'proof_run_identity_invalid')
        new_proof_ref = 'codex/get-asset-proof-' + run_id + '-' + attempt
        proof_created_at_utc = datetime.now(timezone.utc).replace(
            microsecond=0).isoformat().replace('+00:00', 'Z')
        current_proof = build_current_proof(
            proof_ref=new_proof_ref, source_id=source_id,
            old_proof_ref=args.proof_ref,
            parent=candidate['plan']['parent_commit'], detail=candidate['detail'],
            summary=candidate['summary'], image=image, markdown_path=markdown_path,
            files=candidate['files'], source_json_sha256=candidate['source_json_sha256'],
            source_content_sha256=candidate['source_content_sha256'],
            detail_received_at_utc=candidate['detail_received_at_utc'],
            created_at_utc=proof_created_at_utc)
        proof_ref, _ = publish_current_proof(
            api, token, candidate['plan']['parent_commit'], candidate['parent_tree'], current_proof)
        print(json.dumps({'status':'private_proof_readback_verified', **proof_matches,
            'detail_updated_at_matches_summary':True,'private_proof_ref_created':True,
            'proof_readback_verified':True,'main_updated':False,'notion_updated':False},sort_keys=True))
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
