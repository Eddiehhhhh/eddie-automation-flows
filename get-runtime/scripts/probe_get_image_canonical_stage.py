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
V1_PROOF_REF = 'codex/get-asset-proof-42297879789-1'
V2_PROOF_REF = 'codex/get-asset-proof-42327675228-1'
V2_PROOF_COMMIT = 'b5e514c7537f6d1c2f9ce0170643e00e84fab6fa'
PROOF_ROOT = '.sync/get-notion-asset-probes'
STAGES = {'proof', 'bindings', 'policy', 'source', 'canonical_prepare', 'candidate_publish', 'readback'}


class ProbeFailure(Exception):
    def __init__(self, stage, category='verification_failed'):
        self.stage = stage if stage in STAGES else 'readback'
        self.category = category if re.fullmatch(r'[a-z_]{1,64}', category or '') else 'verification_failed'
        super().__init__(self.category)


def fail(stage, category='verification_failed'):
    raise ProbeFailure(stage, category) from None


CANONICAL_SUBSTAGES = {
    'readback_checkout', 'evidence_load', 'trusted_inputs', 'entry_prepare',
    'source_snapshot', 'detail_parse', 'source_validation', 'source_version',
    'plan_verify', 'proof_compare', 'candidate_publish',
}
SAFE_EXCEPTION_KINDS = {
    'AttributeError':'attribute_error', 'AssertionError':'assertion_error',
    'KeyError':'key_error', 'OSError':'os_error', 'RuntimeError':'runtime_error',
    'TimeoutError':'timeout_error', 'TypeError':'type_error', 'ValueError':'value_error',
    'UnicodeDecodeError':'unicode_error', 'UnicodeEncodeError':'unicode_error',
    'JSONDecodeError':'json_decode_error', 'HTTPError':'http_error', 'URLError':'url_error',
}


def canonical_exception_category(substage, exc):
    if substage not in CANONICAL_SUBSTAGES:
        substage = 'readback_checkout'
    kind = SAFE_EXCEPTION_KINDS.get(type(exc).__name__, 'other')
    return 'unexpected_' + substage + '_' + kind


def fetch_proof(api, proof_ref):
    if proof_ref != V2_PROOF_REF:
        fail('proof')
    ref = api.call('GET', '/git/ref/heads/' + proof_ref, missing=True)
    ref_object = ref.get('object') if isinstance(ref, dict) else None
    if (not isinstance(ref, dict) or ref.get('ref') != 'refs/heads/' + proof_ref
            or not isinstance(ref_object, dict) or ref_object.get('type') != 'commit'):
        fail('proof')
    commit = ref_object.get('sha')
    if commit != V2_PROOF_COMMIT:
        fail('proof')
    rows = api.call('GET', '/contents/' + PROOF_ROOT + '?ref=' + commit, list_result=True)
    if not isinstance(rows, list) or len(rows) != 1:
        fail('proof')
    row = rows[0]
    path = row.get('path') if isinstance(row, dict) else None
    if (not isinstance(path, str) or row.get('type') != 'file'
            or not re.fullmatch(re.escape(PROOF_ROOT) + r'/[0-9]{19}\.json', path)):
        fail('proof')
    value = api.call('GET', '/contents/' + quote(path, safe='/') + '?ref=' + commit)
    if (not isinstance(value, dict) or value.get('path') != path
            or value.get('encoding') != 'base64' or not isinstance(value.get('content'), str)):
        fail('proof')
    try:
        raw = base64.b64decode(value['content'].replace('\n', ''), validate=True)
        proof = json.loads(raw)
    except (ValueError, UnicodeError, TypeError):
        fail('proof')
    ident = proof.get('source_id') if isinstance(proof, dict) else None
    expected_keys = {'contract','proof_version','ref','supersedes_ref','source_id','source_field',
        'source_json_sha256','source_content_sha256','source_detail_updated_at',
        'source_summary_updated_at','source_summary_sha256','source_detail_received_at_utc',
        'proof_created_at_utc','parent','note_path','note_sha256','assets'}
    if (not isinstance(proof, dict) or set(proof) != expected_keys
            or proof.get('contract') != PROOF_CONTRACT or proof.get('proof_version') != 2
            or proof.get('ref') != proof_ref or proof.get('supersedes_ref') != V1_PROOF_REF
            or type(ident) is not str or re.fullmatch(r'[0-9]{19}', ident) is None
            or path != PROOF_ROOT + '/' + ident + '.json'
            or proof.get('source_field') != 'content_embed'
            or re.fullmatch(r'[0-9a-f]{64}', proof.get('source_content_sha256', '')) is None
            or re.fullmatch(r'[0-9a-f]{64}', proof.get('source_json_sha256', '')) is None
            or re.fullmatch(r'[0-9a-f]{64}', proof.get('source_summary_sha256', '')) is None
            or re.fullmatch(r'[0-9a-f]{64}', proof.get('note_sha256', '')) is None
            or not isinstance(proof.get('assets'), list) or len(proof['assets']) != 1
            or not isinstance(proof.get('parent'), str) or re.fullmatch(r'[0-9a-f]{40}', proof['parent']) is None
            or not isinstance(proof.get('note_path'), str) or not proof['note_path'].startswith('Raw/03 Get/')):
        fail('proof')
    version = proof.get('source_detail_updated_at')
    if (not isinstance(version, dict) or set(version) != {'type','value'}
            or version.get('type') != 'string'
            or proof.get('source_summary_updated_at') != version):
        fail('proof')
    try:
        if runtime.entry.engine.raw_source_version(version['value']) != version:
            fail('proof')
        runtime.r.safe_path(proof['note_path'])
    except Exception:
        fail('proof')
    utc_pattern = r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ'
    if any(not re.fullmatch(utc_pattern, proof.get(key, '') or '')
           for key in ('source_detail_received_at_utc','proof_created_at_utc')):
        fail('proof')
    asset = proof['assets'][0]
    mime_suffix = {'image/png':'.png','image/jpeg':'.jpg','image/webp':'.webp','image/gif':'.gif'}
    if (not isinstance(asset, dict)
            or set(asset) != {'path','sha256','source_url_sha256','source_embed_sha256','bytes','mime'}
            or not isinstance(asset.get('path'), str)
            or not re.fullmatch(r'Raw/03 Get/_assets/images/[0-9a-f]{64}\.(?:png|jpg|webp|gif)', asset['path'])
            or not re.fullmatch(r'[0-9a-f]{64}', asset.get('sha256', ''))
            or not re.fullmatch(r'[0-9a-f]{64}', asset.get('source_url_sha256', ''))
            or not re.fullmatch(r'[0-9a-f]{64}', asset.get('source_embed_sha256', ''))
            or asset['path'] != 'Raw/03 Get/_assets/images/' + asset['sha256'] + mime_suffix.get(asset.get('mime'), '')
            or type(asset.get('bytes')) is not int or not 1 <= asset['bytes'] <= 16 * 1024 * 1024
            or asset.get('mime') not in mime_suffix):
        fail('proof')
    try:
        runtime.r.safe_path(asset['path'])
    except Exception:
        fail('proof')
    commit_obj = api.call('GET', '/git/commits/' + commit)
    tree_obj = commit_obj.get('tree') if isinstance(commit_obj, dict) else None
    if (not isinstance(commit_obj, dict) or commit_obj.get('sha') != commit
            or not isinstance(tree_obj, dict)
            or re.fullmatch(r'[0-9a-f]{40}', tree_obj.get('sha', '')) is None):
        fail('proof')
    parents = commit_obj.get('parents')
    if (not isinstance(parents, list) or len(parents) != 1
            or not isinstance(parents[0], dict) or parents[0].get('sha') != proof['parent']):
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


def extract_candidate(plan, files, source_id, proof, detail, summary):
    get_root = runtime.r.GET + '/'
    state_path = get_root + runtime.engine.n.STATE
    receipt_path = runtime.entry.FULL_RECEIPT
    if (plan.get('source_scope') != 'single_note' or plan.get('single_source_id') != source_id
            or plan.get('proposed_content_ids') != [source_id]
            or proof.get('source_id') != source_id
            or state_path not in files or receipt_path not in files
            or not isinstance(detail, dict) or not isinstance(summary, dict)):
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
    try:
        detail_version = runtime.entry.engine.raw_source_version(detail.get('updated_at'))
        summary_version = runtime.entry.engine.raw_source_version(summary.get('updated_at'))
        current_source_json_sha256 = hashlib.sha256(sync.json_text(detail).encode('utf-8')).hexdigest()
        current_source_content_sha256 = hashlib.sha256(detail['content'].encode('utf-8')).hexdigest()
        current_summary_sha256 = hashlib.sha256(sync.json_text(summary).encode('utf-8')).hexdigest()
        current_note_sha256 = hashlib.sha256(files[markdown]).hexdigest()
    except Exception:
        fail('canonical_prepare', 'current_source_hash_invalid')
    matches = {
        'source_json_match': current_source_json_sha256 == proof['source_json_sha256'],
        'source_content_match': current_source_content_sha256 == proof['source_content_sha256'],
        'source_summary_match': current_summary_sha256 == proof['source_summary_sha256'],
        'detail_updated_at_match': detail_version == proof['source_detail_updated_at'],
        'summary_updated_at_match': summary_version == proof['source_summary_updated_at'],
        'note_path_match': markdown == proof['note_path'],
        'note_sha256_match': current_note_sha256 == proof['note_sha256'],
        'image_path_match': asset_path == proof_asset['path'],
        'image_sha256_match': image.get('asset_sha256') == proof_asset['sha256'],
        'image_bytes_match': image.get('asset_bytes') == proof_asset['bytes'],
        'image_mime_match': image.get('asset_mime') == proof_asset['mime'],
        'image_url_hash_match': image.get('reference_sha256') == proof_asset['source_url_sha256'],
        'image_embed_hash_match': image.get('source_sha256') == proof_asset['source_embed_sha256'],
    }
    if not all(matches.values()):
        fail('canonical_prepare', 'v2_proof_source_mismatch')
    return image, matches, markdown


def publish_candidate_ref(api, token, reader, remote, root, plan, files, run_id, attempt, source_id):
    parent = plan.get('parent_commit')
    candidate = plan.get('candidate_commit')
    tree = plan.get('candidate_tree')
    if (plan.get('source_scope') != 'single_note' or plan.get('single_source_id') != source_id
            or plan.get('proposed_content_ids') != [source_id]
            or not isinstance(files, dict) or not files
            or not re.fullmatch(r'[0-9a-f]{40}', parent or '')
            or not re.fullmatch(r'[0-9a-f]{40}', candidate or '')
            or not re.fullmatch(r'[0-9a-f]{40}', tree or '')
            or type(run_id) is not int or type(attempt) is not int
            or not 1 <= run_id <= 10**20 or not 1 <= attempt <= 9999):
        fail('candidate_publish', 'candidate_plan_invalid')
    paths = [row.get('path') for row in plan.get('changes', []) if isinstance(row, dict)]
    if (not paths or len(paths) != len(set(paths)) or set(paths) != set(files)
            or any(not isinstance(data, bytes) for data in files.values())
            or sum(len(data) for data in files.values()) > staging.MAX_BYTES):
        fail('candidate_publish', 'candidate_scope_invalid')
    for path, data in files.items():
        if not isinstance(path, str) or not path.startswith(runtime.r.GET + '/') or not isinstance(data, bytes):
            fail('candidate_publish', 'candidate_scope_invalid')
        try:
            runtime.r.safe_path(path)
        except Exception:
            fail('candidate_publish', 'candidate_scope_invalid')
    ref = 'refs/heads/codex/get-image-candidate-' + str(run_id) + '-' + str(attempt)
    ref_path = '/git/ref/heads/' + ref.removeprefix('refs/heads/')
    if manual.guard(remote) != parent:
        fail('candidate_publish', 'main_moved')
    if api.call('GET', ref_path, missing=True) is not None:
        fail('candidate_publish', 'candidate_ref_exists')
    expected_parent_tree = reader.local.run('rev-parse', parent + '^{tree}').decode().strip()
    if (reader.local.run('rev-parse', candidate + '^{tree}').decode().strip() != tree
            or reader.local.run('rev-list', '--parents', '-n', '1', candidate).decode().strip().split() != [candidate, parent]):
        fail('candidate_publish', 'candidate_commit_invalid')
    raw_commit = reader.local.run('cat-file', 'commit', candidate)
    try:
        headers, message = raw_commit.split(b'\n\n', 1)
        rows = headers.splitlines()
        if len(rows) != 4 or not rows[0].startswith(b'tree ') or not rows[1].startswith(b'parent '):
            raise ValueError()
        tree_line, parent_line = rows[0][5:].decode('ascii'), rows[1][7:].decode('ascii')
        author_line, committer_line = rows[2], rows[3]
        pattern = re.compile(rb'^Get local proposal <get-proposal@example\.invalid> ([0-9]+) \+0000$')
        author_match = pattern.fullmatch(author_line.removeprefix(b'author '))
        committer_match = pattern.fullmatch(committer_line.removeprefix(b'committer '))
        if (tree_line != tree or parent_line != parent
                or not author_line.startswith(b'author ')
                or not committer_line.startswith(b'committer ')
                or not author_match or not committer_match
                or author_match.group(1) != committer_match.group(1)
                or message != b'Prepare canonical Get transaction with receipt\n'):
            raise ValueError()
        timestamp = int(author_match.group(1))
        parent_timestamp = reader.local.run('show', '-s', '--format=%ct', parent).decode().strip()
        if not parent_timestamp.isdigit() or str(timestamp) != parent_timestamp:
            raise ValueError()
    except Exception:
        fail('candidate_publish', 'candidate_commit_invalid')
    entries = []
    selected = {}
    for path, data in sorted(files.items()):
        blob_oid = manual.r.oid(data)
        result = api.call('POST', '/git/blobs', {
            'content': base64.b64encode(data).decode('ascii'), 'encoding': 'base64'})
        if result.get('sha') != blob_oid:
            fail('candidate_publish', 'candidate_blob_mismatch')
        entries.append({'path':path,'mode':'100644','type':'blob','sha':blob_oid})
        selected[path] = ('100644','blob',blob_oid,len(data))
    result = api.call('POST', '/git/trees', {'base_tree':expected_parent_tree,'tree':entries})
    if result.get('sha') != tree:
        fail('candidate_publish', 'candidate_tree_mismatch')
    timestamp_text = datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace('+00:00','Z')
    actor = {'name':'Get local proposal','email':'get-proposal@example.invalid','date':timestamp_text}
    commit_result = api.call('POST','/git/commits',{
        'message':message.decode('utf-8'),'tree':tree,'parents':[parent],
        'author':actor,'committer':actor})
    if commit_result.get('sha') != candidate:
        fail('candidate_publish', 'candidate_commit_mismatch')
    if manual.guard(remote) != parent:
        fail('candidate_publish', 'main_moved')
    try:
        api.call('POST','/git/refs',{'ref':ref,'sha':candidate})
    except Exception:
        observed = api.call('GET',ref_path,missing=True)
    else:
        observed = api.call('GET',ref_path)
    if (not isinstance(observed,dict) or observed.get('ref') != ref
            or observed.get('object',{}).get('type') != 'commit'
            or observed.get('object',{}).get('sha') != candidate):
        fail('candidate_publish', 'candidate_ref_unverified')
    commit_readback = remote.commit(candidate)
    if commit_readback != {'tree':tree,'parents':[parent]}:
        fail('candidate_publish', 'candidate_commit_readback_mismatch')
    if manual.fetch_selected(remote,selected,token,root/'candidate-readback') != files:
        fail('candidate_publish', 'candidate_blob_readback_mismatch')
    if manual.guard(remote) != parent:
        fail('candidate_publish', 'main_moved')
    return ref, candidate


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
    parser.add_argument('--diagnostic-only', action='store_true')
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
    canonical_substage = None
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
        run_id = os.environ.get('GITHUB_RUN_ID', '')
        attempt = os.environ.get('GITHUB_RUN_ATTEMPT', '')
        if (not re.fullmatch(r'[1-9][0-9]{0,19}', run_id)
                or not re.fullmatch(r'[1-9][0-9]{0,5}', attempt)):
            fail('candidate_publish', 'candidate_run_identity_invalid')
        with tempfile.TemporaryDirectory(prefix='get-image-prepare-only-') as folder:
            root = Path(folder)
            canonical_substage = 'readback_checkout'
            local = manual.r.readback_checkout(
                root / 'objects', runtime.entry.BOOTSTRAP['capture_anchor']['capture_commit'], token)
            parent.local = local
            canonical_substage = 'evidence_load'
            reader, evidence, _ = manual.load_evidence(
                local, parent, token, root, parent_sha,
                anchor=runtime.entry.BOOTSTRAP['capture_anchor'])
            trusted_root = root / 'trusted'
            trusted_root.mkdir(mode=0o700)
            replay = TimedReplaySource(source)
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                canonical_substage = 'trusted_inputs'
                anchor, preview, history_witness = runtime.load_trusted_inputs(
                    local, reader, evidence, parent, token, trusted_root, parent_sha,
                    public, policy, bootstrap=runtime.entry.BOOTSTRAP,
                    exclude_run_id=None, exclude_run_attempt=None, source_id=source_id)
                canonical_substage = 'entry_prepare'
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
            canonical_substage = 'source_snapshot'
            items = runtime.entry.engine.a.source_snapshot(summaries, terminal, only_id=source_id)
            canonical_substage = 'detail_parse'
            detail = manual.detail_note(replay.details[source_id])
            summary = items.get(source_id)
            canonical_substage = 'source_validation'
            runtime.entry.engine.n.source_note(summary, detail)
            canonical_substage = 'source_version'
            detail_version = runtime.entry.engine.raw_source_version(detail.get('updated_at'))
            summary_version = runtime.entry.engine.raw_source_version(summary.get('updated_at'))
            if detail_version != summary_version:
                fail('canonical_prepare', 'source_version_race')
            detail_received_at_utc = replay.detail_received_at_utc.get(source_id)
            if not isinstance(detail_received_at_utc, str):
                fail('canonical_prepare', 'source_detail_timestamp_missing')
            canonical_substage = 'plan_verify'
            runtime.entry.verify_plan(
                reader, evidence, plan, files, policy['rules_sha256'],
                bootstrap=runtime.entry.BOOTSTRAP,
                history_witness=history_witness, source_id=source_id)
            if manual.guard(parent) != parent_sha:
                fail('candidate_publish', 'main_moved')
            canonical_substage = 'proof_compare'
            image, proof_matches, markdown_path = extract_candidate(
                plan, files, source_id, proof, detail, summary)
            if args.diagnostic_only:
                print(json.dumps({'status':'canonical_prepare_verified_read_only', **proof_matches,
                    'detail_updated_at_matches_summary':True,'candidate_ref_created':False,
                    'candidate_readback_verified':False,'main_updated':False,
                    'notion_updated':False},sort_keys=True))
                return 0
            canonical_substage = 'candidate_publish'
            publish_candidate_ref(
                api, token, reader, parent, root, plan, files,
                int(run_id), int(attempt), source_id)
        print(json.dumps({'status':'private_candidate_readback_verified', **proof_matches,
            'detail_updated_at_matches_summary':True,'candidate_ref_created':True,
            'candidate_readback_verified':True,'main_updated':False,'notion_updated':False},sort_keys=True))
        return 0
    except ProbeFailure:
        raise
    except Exception as exc:
        if canonical_substage is not None:
            stage = 'candidate_publish' if canonical_substage == 'candidate_publish' else 'canonical_prepare'
            fail(stage, canonical_exception_category(canonical_substage, exc))
        stage = getattr(exc, 'runtime_stage', 'canonical_prepare')
        fail(stage, 'verification_failed')


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except ProbeFailure as exc:
        print(f'Get image private candidate stopped at stage={exc.stage} category={exc.category}', file=sys.stderr)
        raise SystemExit(1)
