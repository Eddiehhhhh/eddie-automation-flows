"""Local, injectable Notion transaction engine. No HTTP, credentials or CLI.

A future production adapter must implement the exact read/native-block contract
and privately publish each journal CAS BEFORE its corresponding external write.
The file store below is for local tests, not proof of remote journal durability.
Approval hashes, source/state anchors and reviewed ownership scopes MUST come
from separate trusted approval/committed evidence, never from self-hashing a
fresh observation. This module never treats an unowned existing page as owned.
"""
from copy import deepcopy
import fcntl
import hashlib
import json
import os
import stat
from pathlib import Path
import tempfile

import get_notion_sink_plan as p
import get_legacy_adapter as git

CONTRACT = 'get-notion-journal-engine-1'
STATE_PATH = '.sync/get-notion/state.json'
MAX_STATE = 32 * 1024 * 1024
MAX_RECORDS = 50
MAX_WRITE_BLOCKS = 100
MAX_REQUEST_BYTES = 400 * 1024
REASONS = frozenset(('source_changed', 'source_invalid', 'state_changed', 'state_invalid',
    'approval_missing', 'rules_changed', 'target_changed', 'target_invalid', 'duplicate_id',
    'missing_target', 'missing_block_ownership', 'human_edit_conflict', 'wrong_parent',
    'unsupported_source_format', 'missing_asset', 'asset_write_contract_missing',
    'archive_not_approved', 'create_not_approved', 'format_not_approved', 'writer_gate_unknown',
    'request_budget', 'journal_failed', 'journal_busy', 'uncertain_append', 'uncertain_create',
    'uncertain_archive', 'readback_mismatch', 'transport_read_failed', 'transport_write_uncertain',
    'recovery_not_approved', 'unsupported_prepend', 'other'))


class Blocked(ValueError):
    pass


def fail(reason):
    raise Blocked(reason if reason in REASONS else 'other') from None


def encoded(value):
    try: return p.encoded(value)
    except Exception: fail('state_invalid')


def digest(value):
    return hashlib.sha256(value if isinstance(value, bytes) else encoded(value)).hexdigest()


def same(left, right):
    return encoded(left) == encoded(right)


def initial_state():
    return {'contract': CONTRACT, 'receipts': {}, 'failures': {}, 'journal': None}


def rules_sha256():
    # Cover the complete local Python import closure, including future wire
    # adapters. An unrelated code change conservatively requires reapproval.
    root = Path(__file__).resolve().parent; paths=sorted(root.glob('*.py'))
    if not paths or len(paths)>500:fail('request_budget')
    hashes={};total=0
    for path in paths:
        data=git.bounded_file(path,2*1024*1024);total+=len(data)
        if total>16*1024*1024:fail('request_budget')
        hashes[path.name]=digest(data)
    return digest(hashes)


def check_state(value):
    try:
        if not isinstance(value, dict) or set(value) != {'contract', 'receipts', 'failures', 'journal'} or value['contract'] != CONTRACT or not isinstance(value['receipts'], dict) or not isinstance(value['failures'], dict): fail('state_invalid')
        if len(value['receipts'])>2500 or len(value['failures'])>2500: fail('request_budget')
        for ident,row in value['receipts'].items():
            p.note_id(ident)
            required={'note_id','page_id','data_source_id','source_commit','md_blob','renderer_version','managed_blocks','protected_blocks','properties_baseline','asset_bindings','readback_verified'}
            if not isinstance(row,dict) or not required<=set(row) or set(row)-required-{'creation_plan_sha256'} or row['note_id']!=ident or row['readback_verified'] is not True or row['renderer_version']!=p.RENDERER: fail('state_invalid')
            p.notion_id(row['page_id']);p.notion_id(row['data_source_id']);p.hex_hash(row['source_commit'],40);p.hex_hash(row['md_blob'],40)
            if 'creation_plan_sha256' in row:p.hex_hash(row['creation_plan_sha256'],64)
            if not isinstance(row['properties_baseline'],dict) or p.property_identity(row['properties_baseline'])!=ident or not isinstance(row['asset_bindings'],dict):fail('state_invalid')
            seen=set()
            for key in ('managed_blocks','protected_blocks'):
                if not isinstance(row[key],list):fail('state_invalid')
                for block in row[key]:
                    if not isinstance(block,dict) or set(block)!={'id','normalized_hash'}:fail('state_invalid')
                    block_id=p.notion_id(block['id']);p.hex_hash(block['normalized_hash'],64)
                    if block_id in seen:fail('state_invalid')
                    seen.add(block_id)
            if not row['managed_blocks'] and 'creation_plan_sha256' not in row:fail('state_invalid')
        for ident,row in value['failures'].items():
            p.note_id(ident)
            if not isinstance(row,dict) or set(row)!={'md_blob','source_commit','policy_sha256','reason'} or row['reason'] not in REASONS:fail('state_invalid')
            p.hex_hash(row['md_blob'],40);p.hex_hash(row['source_commit'],40);p.hex_hash(row['policy_sha256'],64)
        journal=value['journal']
        if journal is not None:
            if not isinstance(journal,dict) or set(journal)!={'plan_sha256','plan','index','operation'} or not isinstance(journal['plan'],dict) or digest(journal['plan'])!=journal['plan_sha256'] or type(journal['index']) is not int or not 0<=journal['index']<=len(journal['plan'].get('records',[])) or (journal['operation'] is not None and not isinstance(journal['operation'],dict)):fail('state_invalid')
        if len(encoded(value)) > MAX_STATE: fail('request_budget')
        return value
    except Blocked: raise
    except Exception: fail('state_invalid')


class LocalJournal:
    """Fsynced atomic local CAS. Does NOT publish private Git state remotely."""
    def __init__(self, root):
        root = Path(root).resolve(strict=True)
        self.path = root / STATE_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if any(x.is_symlink() for x in (self.path, *self.path.parents)): fail('state_invalid')
        self.lock = self.path.with_suffix('.lock')

    def read(self):
        if not self.path.exists(): return initial_state(), digest(initial_state())
        fd=os.open(self.path,os.O_RDONLY|os.O_NOFOLLOW)
        try:
            before=os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_size>MAX_STATE:fail('state_invalid')
            with os.fdopen(fd,'rb',closefd=False) as handle:data=handle.read(MAX_STATE+1)
            after=os.fstat(fd)
            if len(data)!=before.st_size or len(data)>MAX_STATE or (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns)!=(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns):fail('state_invalid')
        finally:os.close(fd)
        try: value = json.loads(data)
        except Exception: fail('state_invalid')
        check_state(value)
        if encoded(value) != data: fail('state_invalid')
        return value, digest(data)

    def compare_and_set(self, expected_sha, value):
        check_state(value); data = encoded(value)
        fd = os.open(self.lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            _, current = self.read()
            if current != expected_sha: fail('state_changed')
            handle, name = tempfile.mkstemp(dir=self.path.parent, prefix='.notion-journal-')
            try:
                with os.fdopen(handle, 'wb') as out:
                    out.write(data); out.flush(); os.fsync(out.fileno())
                os.replace(name, self.path)
                directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
                try: os.fsync(directory)
                finally: os.close(directory)
            finally:
                if os.path.exists(name): os.unlink(name)
            observed, new_sha = self.read()
            if observed != value or new_sha != digest(data): fail('journal_failed')
            return new_sha
        finally: os.close(fd)


class GitPublication:
    """Only actual changed committed Markdown IDs become queue candidates.

    expected_commit is an external confirmed-publication anchor. `current_ref`
    may advance through journal/state-only commits, but selected MD blobs must
    remain exactly unchanged and the publication must remain in its ancestry.
    """
    def __init__(self, root, commit, parent, expected_commit, current_ref='refs/heads/main', *,
                 initial_cohort=None, expected_initial_sha=None):
        self.repo = git.OfflineGit(root)
        if commit != expected_commit: fail('source_invalid')
        git.checked(commit, 40); git.checked(parent, 40)
        if current_ref != 'refs/heads/main': fail('source_invalid')
        self.current_ref = current_ref
        self.repo.local.run('merge-base', '--is-ancestor', parent, commit)
        parents=self.repo.local.run('rev-list','--parents','-n','1',commit).decode().split()[1:]
        if initial_cohort is None and parents!=[parent]:fail('source_invalid')
        if initial_cohort is not None and parent!=commit:fail('source_invalid')
        before = self.repo.tree(parent); after = self.repo.tree(commit)
        records = {}; changed = []; source_bytes = 0
        for path in sorted(set(before) | set(after)):
            if not path.startswith(git.retention.GET + '/') or not path.lower().endswith(('.md', '.markdown')) or before.get(path) == after.get(path): continue
            if path not in after or after[path][:2] != ('100644', 'blob'): fail('source_invalid')
            data = self.repo.read(commit, path, p.MAX_NOTE)
            source_bytes += len(data)
            if len(records) >= 2500 or source_bytes > 128*1024*1024: fail('request_budget')
            try:
                header = data.decode().split('\n---\n', 1)[0]
                ids = [line[9:] for line in header.splitlines() if line.startswith('note_id: ')]
                if len(ids) != 1: fail('source_invalid')
                ident = p.note_id(json.loads(ids[0])); p.source_body(data, ident)
            except Exception: fail('source_invalid')
            if ident in records: fail('duplicate_id')
            records[ident] = {'path': path, 'blob': after[path][2], 'sha256': digest(data)}
            changed.append(ident)
        # Initial acceptance is a separately approved source selection. It never
        # pretends that a state-only commit changed Markdown.
        cohort = None
        if initial_cohort is not None:
            if not isinstance(initial_cohort,dict) or set(initial_cohort)!={'commit','records'} or digest(initial_cohort)!=expected_initial_sha or initial_cohort['commit']!=commit or changed:
                fail('approval_missing')
            selected=initial_cohort['records']
            if not isinstance(selected,dict) or len(selected)!=2: fail('source_invalid')
            paths=set()
            for ident,row in selected.items():
                try:
                    p.note_id(ident)
                    if not isinstance(row,dict) or set(row)!={'path','blob','sha256'}: fail('source_invalid')
                    path=p.safe_path(row['path']); p.hex_hash(row['blob'],40); p.hex_hash(row['sha256'],64)
                    if not path.lower().endswith(('.md','.markdown')) or path in paths or after.get(path)!=('100644','blob',row['blob']): fail('source_invalid')
                    data=self.repo.read(commit,path,p.MAX_NOTE); p.source_body(data,ident)
                    if digest(data)!=row['sha256']: fail('source_invalid')
                    paths.add(path)
                except Blocked: raise
                except Exception: fail('source_invalid')
            records=deepcopy(selected); cohort=deepcopy(initial_cohort)
        content_tree={path:row[2] for path,row in after.items()
            if path.startswith(git.retention.GET+'/') and row[:2]==('100644','blob')}
        self.descriptor = {'repository': p.REPOSITORY, 'commit': commit, 'parent': parent,
            'changed_content_ids': sorted(changed), 'records': records,
            'initial_cohort':cohort, 'content_tree':content_tree}
        if len(encoded(self.descriptor))>8*1024*1024: fail('request_budget')
        self.check(self.descriptor)

    def check(self, descriptor):
        if descriptor != self.descriptor: fail('source_invalid')
        try:
            head = self.repo.local.run('rev-parse', self.current_ref).decode().strip()
            self.repo.local.run('merge-base', '--is-ancestor', descriptor['commit'], head)
            tree = self.repo.tree(head)
            if any(tree.get(row['path']) != ('100644', 'blob', row['blob']) for row in descriptor['records'].values()): fail('source_changed')
        except Blocked: raise
        except Exception: fail('source_changed')

    def check_assets(self, bindings):
        try:
            head=self.repo.local.run('rev-parse',self.current_ref).decode().strip();tree=self.repo.tree(head)
            if any(tree.get(path)!=('100644','blob',blob) for path,blob in bindings.items()):fail('source_changed')
        except Blocked:raise
        except Exception:fail('source_changed')

    def read(self, ident):
        row = self.descriptor['records'][ident]
        data = self.repo.read(self.descriptor['commit'], row['path'], p.MAX_NOTE)
        if p.git_blob(data) != row['blob'] or digest(data) != row['sha256']: fail('source_invalid')
        return data


def target(snapshot, ident, data_source):
    if not isinstance(snapshot, dict) or snapshot.get('complete') is not True or snapshot.get('identity_query_complete') is not True: fail('target_invalid')
    matches = snapshot.get('exact_id_matches')
    if not isinstance(matches, list): fail('target_invalid')
    if len(matches) > 1: fail('duplicate_id')
    if not matches: return None
    try:
        page = p.notion_id(snapshot.get('page_id'))
        if [p.notion_id(x) for x in matches] != [page] or p.note_id(snapshot.get('id_property')) != ident or p.property_identity(snapshot.get('properties_raw')) != ident: fail('target_invalid')
        if p.notion_id(snapshot.get('parent_data_source')) != data_source: fail('wrong_parent')
        nodes, roots, order, semantic = p.snapshot_tree(snapshot)
    except Blocked: raise
    except Exception: fail('target_invalid')
    if not isinstance(snapshot.get('last_edited_time'),str) or not snapshot['last_edited_time']: fail('target_invalid')
    return {'page_id': page, 'data_source_id': data_source, 'note_id': ident,
        'properties': deepcopy(snapshot['properties_raw']), 'nodes': nodes,
        'roots': roots, 'order': order, 'semantic': semantic,
        'last_edited_time': snapshot.get('last_edited_time')}


def target_sha(snapshot, ident, data_source):
    return digest(target(snapshot, ident, data_source))


def subtree(nodes, root):
    out = []
    def walk(ident):
        out.append(ident)
        for child in nodes[ident]['children_ids']: walk(child)
    walk(root)
    return out


def semantic(nodes, root):
    row = nodes[root]
    return {'type': row['type'], 'payload': deepcopy(row['payload']),
            'children': [semantic(nodes, child) for child in row['children_ids']]}


def receipt(snapshot, ident, data_source, managed, publication, source):
    view = target(snapshot, ident, data_source)
    if view is None or set(managed) - set(view['nodes']): fail('readback_mismatch')
    return {'note_id': ident, 'page_id': view['page_id'], 'data_source_id': data_source,
        'source_commit': publication['commit'], 'md_blob': source['blob'], 'renderer_version': p.RENDERER,
        'managed_blocks': [{'id': i, 'normalized_hash': p.block_hash(view['nodes'][i])} for i in view['order'] if i in managed],
        'protected_blocks': [{'id': i, 'normalized_hash': p.block_hash(view['nodes'][i])} for i in view['order'] if i not in managed],
        'properties_baseline': deepcopy(view['properties']), 'asset_bindings': {}, 'readback_verified': True}


def request_blocks(blocks):
    """Bound and split rich text; abstract transport still validates wire format.

    Images are not write-ready until an explicit persistent-file API adapter
    exists. An observed opaque file reference is never guessed into a UUID.
    """
    blocks = deepcopy(blocks); total = [0]
    def rich(runs):
        result = []
        for run in runs:
            content = run['text']['content']
            for start in range(0, len(content), 2000):
                item = deepcopy(run); item['text']['content'] = content[start:start+2000]; result.append(item)
            if not content: result.append(deepcopy(run))
        if len(result) > 100: fail('request_budget')
        return result
    def walk(rows, depth):
        if depth > 2 or len(rows) > 100: fail('request_budget')
        for row in rows:
            total[0] += 1
            if total[0] > MAX_WRITE_BLOCKS: fail('request_budget')
            if row['type'] == 'image': fail('asset_write_contract_missing')
            payload = row['payload']
            if 'rich_text' in payload: payload['rich_text'] = rich(payload['rich_text'])
            if 'caption' in payload: payload['caption'] = rich(payload['caption'])
            if 'cells' in payload: payload['cells'] = [rich(cell) for cell in payload['cells']]
            walk(row['children'], depth + 1) if row['children'] else None
    walk(blocks, 0)
    if len(encoded(blocks)) > MAX_REQUEST_BYTES: fail('request_budget')
    return blocks


def propose_initial_scope(publication, snapshots, data_source_id, previous_preview,
                          expected_preview_sha):
    """Propose a unique prior-preview block range, without adopting it.

    previous_preview is independently pinned historical prepared-input and
    successful-readback evidence: {ID: {page_id, blocks, evidence_sha256}}. `blocks` is the exact
    semantic root sequence derived from that historical input, not a sequence
    freshly copied from today's target. The caller verifies that provenance;
    the evidence does not itself establish native-block ownership. Hashing a
    new target is not historical evidence. Each candidate STILL
    needs explicit external scope approval before prepare() can consume it.
    """
    publication.check(publication.descriptor)
    if not publication.descriptor['initial_cohort'] or not isinstance(previous_preview,dict) or digest(previous_preview)!=expected_preview_sha or set(previous_preview)!=set(publication.descriptor['records']):fail('approval_missing')
    try: data_source=p.notion_id(data_source_id)
    except Exception:fail('wrong_parent')
    scope={};blocked={};proof={}
    for ident,old in previous_preview.items():
        try:
            if not isinstance(old,dict) or set(old)!={'page_id','blocks','evidence_sha256'} or not isinstance(old['blocks'],list) or not old['blocks']:fail('missing_block_ownership')
            p.hex_hash(old['evidence_sha256'],64)
            view=target(snapshots.get(ident),ident,data_source)
            if view is None or p.notion_id(old['page_id'])!=view['page_id']:fail('missing_target')
            width=len(old['blocks']);matches=[]
            for start in range(len(view['roots'])-width+1):
                roots=view['roots'][start:start+width]
                if same([semantic(view['nodes'],i) for i in roots],old['blocks']):matches.append(roots)
            if len(matches)!=1:fail('missing_block_ownership')
            managed=[i for root in matches[0] for i in subtree(view['nodes'],root)]
            scope[ident]={'page_id':view['page_id'],'snapshot_sha256':digest(view),'managed_block_ids':managed}
            proof[ident]={'historical_evidence_sha256':old['evidence_sha256'],'source':publication.descriptor['records'][ident],
                'protected_blocks':len(view['nodes'])-len(managed)}
        except (Blocked,p.Blocked) as exc:blocked[ident]=str(exc) if str(exc) in REASONS else 'missing_block_ownership'
        except Exception:blocked[ident]='missing_block_ownership'
    return {'scope':scope,'scope_sha256':digest(scope),'blocked':blocked,'proof':proof,
        'ownership_adopted':False,'requires_explicit_scope_approval':True}


def prepare(publication, state, expected_state_sha, snapshots, policy, *, assets=None,
            initial_scope=None, expected_scope_sha=None, retry_conflicts=False):
    """Create an exact private approval plan; existing unowned pages block.

    An initial_scope is optional and must itself be externally approved and
    hash-pinned. Each reviewed row names exact page/snapshot/managed block IDs.
    Approval of a newly observed hash alone is not implicit ownership approval.
    """
    check_state(state)
    if digest(state) != expected_state_sha or state['journal'] is not None: fail('state_changed')
    if not isinstance(policy, dict) or set(policy) != {'data_source_id','id_property','title_property','allow_create','allow_archive','format_accepted','old_writer_evidence'}: fail('approval_missing')
    try: data_source = p.notion_id(policy['data_source_id'])
    except Exception: fail('wrong_parent')
    if any(type(policy[k]) is not bool for k in ('allow_create','allow_archive','format_accepted')) or policy['id_property'] != '笔记 ID' or not isinstance(policy['title_property'], str) or not policy['title_property']: fail('approval_missing')
    if policy['title_property'] == policy['id_property']: fail('approval_missing')
    policy = {**policy, 'data_source_id':data_source}
    initial_scope = {} if initial_scope is None else initial_scope
    if not isinstance(initial_scope, dict) or (initial_scope and digest(initial_scope) != expected_scope_sha): fail('approval_missing')
    publication.check(publication.descriptor)
    if type(retry_conflicts) is not bool: fail('approval_missing')
    policy_sha = digest({'policy':policy,'initial_scope':initial_scope})
    pending = []; skipped_conflicts=[]
    for ident in sorted(publication.descriptor['records']):
        source = publication.descriptor['records'][ident]
        prior = state['receipts'].get(ident,{})
        if prior.get('md_blob')==source['blob'] and prior.get('source_commit')==publication.descriptor['commit']: continue
        failed = state['failures'].get(ident,{})
        if not retry_conflicts and failed.get('md_blob')==source['blob'] and failed.get('source_commit')==publication.descriptor['commit'] and failed.get('policy_sha256')==policy_sha:
            skipped_conflicts.append(ident);continue
        pending.append(ident)
    ids = pending[:MAX_RECORDS]
    records = []
    for ident in ids:
        source = publication.descriptor['records'][ident]
        row = {'note_id': ident, 'source': source, 'action': 'blocked', 'reason': None}
        try:
            snapshot = snapshots.get(ident); view = target(snapshot, ident, data_source)
            data = publication.read(ident)
            rendered, bindings, warnings = p.render_markdown(data, ident, assets or {}, source['path'],
                publication.descriptor['content_tree'])
            row.update(rendered=rendered, before_sha256=digest(view), warnings=warnings, asset_bindings_sha256=digest(bindings), source_assets={v['path']:v['git_blob'] for v in bindings.values()})
            publication.check_assets(row['source_assets'])
            if not policy['format_accepted']: fail('format_not_approved')
            if not p.writer_gate(policy): fail('writer_gate_unknown')
            if view is None:
                if ident in state['receipts']: fail('missing_target')
                if not policy['allow_create']: fail('create_not_approved')
                header = data.decode().split('\n---\n', 1)[0]
                titles = [x[7:] for x in header.splitlines() if x.startswith('title: ')]
                if len(titles) != 1: fail('source_invalid')
                try: title = json.loads(titles[0])
                except ValueError: title = titles[0]
                if not isinstance(title, str) or not title: fail('source_invalid')
                props = {policy['id_property']: {'type':'rich_text','rich_text':[p.text_run(ident)]},
                         policy['title_property']: {'type':'title','title':[p.text_run(title)]}}
                if len(title) > 2000: fail('request_budget')
                row.update(action='create', properties=props, request_blocks=request_blocks(rendered))
            else:
                prior = state['receipts'].get(ident)
                if prior is None and ident in initial_scope:
                    scope = initial_scope[ident]
                    if not isinstance(scope, dict) or set(scope) != {'page_id','snapshot_sha256','managed_block_ids'} or scope['page_id'] != view['page_id'] or scope['snapshot_sha256'] != digest(view): fail('missing_block_ownership')
                    managed = scope['managed_block_ids']
                    if not isinstance(managed, list) or not managed or len(managed) != len(set(managed)) or any(i not in view['nodes'] for i in managed): fail('missing_block_ownership')
                    prior = receipt(snapshot, ident, data_source, managed, publication.descriptor, source)
                if prior is None: fail('missing_block_ownership')
                if prior.get('managed_blocks') == []:
                    origin = prior.get('creation_plan_sha256')
                    if not isinstance(origin,str) or len(origin)!=64 or any(c not in '0123456789abcdef' for c in origin) or prior.get('readback_verified') is not True or prior.get('page_id')!=view['page_id'] or prior.get('data_source_id')!=data_source or prior.get('note_id')!=ident or prior.get('renderer_version')!=p.RENDERER:
                        fail('missing_block_ownership')
                    row.update(page_id=view['page_id'],managed=[],old_roots=[],before=deepcopy(snapshot),after_anchor=None,creation_plan_sha256=origin)
                    if not rendered: row['action']='noop'
                    else: row.update(action='update',request_blocks=request_blocks(rendered))
                    records.append(row); continue
                batch = {'commit': publication.descriptor['commit'], 'content_complete': {ident: True},
                    'content_tree': publication.descriptor['content_tree'], 'content_sha256': {source['path']: source['sha256']}}
                mapping = {'path': source['path'], 'md_blob': source['blob'], 'page_id': view['page_id'], 'data_source_id': data_source}
                validation_policy = {**policy, 'allowlist':[ident], 'ownership_sha256':{ident:digest(prior)}}
                validated = p.record_plan(ident, batch, mapping, {source['blob']:data}, assets or {}, snapshot, prior, validation_policy)
                if validated['decision'] == 'blocked': fail(validated['reason_codes'][0])
                managed = [x['id'] for x in prior['managed_blocks']]
                roots = [x for x in view['roots'] if x in managed]
                if not roots or set(managed) != {x for root in roots for x in subtree(view['nodes'], root)}: fail('missing_block_ownership')
                indices = [view['roots'].index(x) for x in roots]
                if indices != list(range(indices[0], indices[-1]+1)): fail('missing_block_ownership')
                row.update(page_id=view['page_id'], managed=managed, old_roots=roots,
                    before=deepcopy(snapshot), after_anchor=roots[-1])
                if validated['decision'] == 'noop_verified': row['action'] = 'noop'
                else:
                    if not policy['allow_archive']: fail('archive_not_approved')
                    if not rendered: fail('unsupported_source_format')
                    row.update(action='update', request_blocks=request_blocks(rendered))
        except (Blocked, p.Blocked) as exc:
            row.update(action='blocked', reason=str(exc) if str(exc) in REASONS else 'other')
        except Exception:
            row.update(action='blocked', reason='source_invalid')
        records.append(row)
    return {'contract':CONTRACT, 'publication':deepcopy(publication.descriptor),
        'state_sha256':expected_state_sha, 'rules_sha256':rules_sha256(),
        'policy':deepcopy(policy), 'policy_sha256':policy_sha, 'initial_scope_sha256':digest(initial_scope), 'retry_conflicts':retry_conflicts,
        'records':records, 'deferred_ids':pending[MAX_RECORDS:], 'skipped_conflicts':skipped_conflicts,
        'state_only':not publication.descriptor['changed_content_ids'],
        'source_mode':'initial_review' if publication.descriptor['initial_cohort'] else 'changed_content'}


def read_target(api, ident, data_source):
    try: snapshot = api.read_note(ident)
    except Exception: fail('transport_read_failed')
    target(snapshot, ident, data_source)
    return snapshot


def preserved(before, after):
    return all(same(before[k], after[k]) for k in ('page_id','data_source_id','note_id','properties'))


def appended(before_snapshot, after_snapshot, row, data_source, response_ids=None):
    before = target(before_snapshot, row['note_id'], data_source)
    after = target(after_snapshot, row['note_id'], data_source)
    if before is None or after is None or not preserved(before, after): fail('readback_mismatch')
    if any(not same(after['nodes'].get(i), value) for i,value in before['nodes'].items()): fail('readback_mismatch')
    position = 0 if row['after_anchor'] is None else before['roots'].index(row['after_anchor']) + 1
    count = len(row['rendered']); roots = after['roots'][position:position+count]
    if len(roots) != count or after['roots'] != before['roots'][:position] + roots + before['roots'][position:]: fail('readback_mismatch')
    if response_ids is not None and roots != response_ids: fail('readback_mismatch')
    if set(roots) & set(before['nodes']) or not same([semantic(after['nodes'], i) for i in roots], row['rendered']): fail('readback_mismatch')
    new_ids = {i for root in roots for i in subtree(after['nodes'], root)}
    if set(after['nodes']) != set(before['nodes']) | new_ids: fail('readback_mismatch')
    return roots, [i for i in after['order'] if i in new_ids]


def archived(before_snapshot, after_snapshot, ident, data_source, root):
    before = target(before_snapshot, ident, data_source); after = target(after_snapshot, ident, data_source)
    if before is None or after is None or not preserved(before, after): fail('readback_mismatch')
    removed = set(subtree(before['nodes'], root))
    if not same(after['nodes'], {i:v for i,v in before['nodes'].items() if i not in removed}) or after['roots'] != [i for i in before['roots'] if i != root]: fail('readback_mismatch')


def created(snapshot, row, data_source, expected_page):
    view = target(snapshot, row['note_id'], data_source)
    if view is None or view['page_id'] != expected_page or not same(view['semantic'], row['rendered']): fail('readback_mismatch')
    for key, submitted in row['properties'].items():
        actual = view['properties'].get(key)
        kind = submitted['type']
        try:
            if not isinstance(actual, dict) or actual.get('type') != kind or p.normalize_rich(actual.get(kind)) != p.normalize_rich(submitted[kind]): fail('readback_mismatch')
        except Exception: fail('readback_mismatch')
    return view['order']


def execute(plan, approved_plan_sha, publication, api, store, *, expected_store_sha, writers_guard, create_recovery=None, approved_recovery_sha=None):
    """Execute only injected methods: read_note, append_blocks, archive_block,
    create_page. There is NO real network adapter in this module.

    A durable prepared journal is never permission to repeat append/create.
    Uncertain create without a saved response ID requires separate exact-page
    recovery approval; a matching current observation alone cannot adopt it.
    """
    counts = {'created':0,'updated':0,'noop':0,'blocked':0,'write_calls':0,'deferred':0,'retained_conflicts':0}
    reason = None
    def guard_writers():
        try: evidence = writers_guard()
        except Exception: fail('writer_gate_unknown')
        if not p.writer_gate({'old_writer_evidence':evidence}): fail('writer_gate_unknown')
    try:
        if not isinstance(plan, dict) or plan.get('contract') != CONTRACT or digest(plan) != approved_plan_sha: fail('approval_missing')
        if plan.get('rules_sha256') != rules_sha256(): fail('rules_changed')
        publication.check(plan['publication'])
        counts.update(deferred=len(plan['deferred_ids']),retained_conflicts=len(plan['skipped_conflicts']))
        value, version = store.read(); check_state(value)
        if version != expected_store_sha: fail('state_changed')
        if not plan['records']:
            if plan['records'] or value['journal'] is not None or version != plan['state_sha256']: fail('state_changed')
            return {'status':'partial' if counts['retained_conflicts'] or counts['deferred'] else 'verified','reason':None,**counts,'state_sha256':version,'external_write_attempts':0,'write_state':'not_attempted'}
        journal = value['journal']
        if journal is None:
            if version != plan['state_sha256']: fail('state_changed')
            journal = {'plan_sha256':approved_plan_sha,'plan':deepcopy(plan),'index':0,'operation':None}
            value['journal'] = journal
            version = store.compare_and_set(version, value)
        elif journal.get('plan_sha256') != approved_plan_sha or journal.get('plan') != plan: fail('journal_busy')
        data_source = plan['policy']['data_source_id']
        while journal['index'] < len(plan['records']):
            row = plan['records'][journal['index']]; ident = row['note_id']
            publication.check(plan['publication'])
            if row['action'] == 'blocked':
                value['failures'][ident]={'md_blob':row['source']['blob'],'source_commit':plan['publication']['commit'],'policy_sha256':plan['policy_sha256'],'reason':row['reason']}
                counts['blocked'] += 1; journal['index'] += 1
                version = store.compare_and_set(version, value); continue
            publication.check_assets(row.get('source_assets',{}))
            operation = journal['operation']
            if operation is None:
                snapshot = read_target(api, ident, data_source)
                if target_sha(snapshot, ident, data_source) != row['before_sha256']: fail('target_changed')
                if row['action'] == 'noop':
                    value['receipts'][ident] = receipt(snapshot, ident, data_source, row['managed'], plan['publication'], row['source'])
                    if row.get('creation_plan_sha256'): value['receipts'][ident]['creation_plan_sha256']=row['creation_plan_sha256']
                    value['failures'].pop(ident,None)
                    counts['noop'] += 1; journal['index'] += 1
                    version = store.compare_and_set(version, value); continue
                operation = {'kind':'create' if row['action']=='create' else 'append',
                    'phase':'prepared','before':snapshot,'response':None,'archive_index':0}
                journal['operation'] = operation
                version = store.compare_and_set(version, value)
            if operation['kind'] in ('create','append') and operation['phase']=='prepared':
                publication.check(plan['publication'])
                # Prepared means no attempt; only durable in_flight permits a call.
                if target_sha(read_target(api, ident, data_source), ident, data_source) != row['before_sha256']: fail('target_changed')
                publication.check_assets(row.get('source_assets',{}))
                guard_writers()
                if operation['kind']=='append' and hasattr(api,'preflight_append'):
                    try:api.preflight_append(row['page_id'],row['request_blocks'],row['after_anchor'])
                    except Exception as exc:
                        capability=str(exc)
                        if capability not in ('unsupported_prepend','asset_write_contract_missing','request_budget'):fail('target_invalid')
                        # A pure preflight and durable prepared phase establish
                        # that no API attempt occurred. Isolate this record.
                        value['failures'][ident]={'md_blob':row['source']['blob'],'source_commit':plan['publication']['commit'],'policy_sha256':plan['policy_sha256'],'reason':capability}
                        counts['blocked']+=1;journal.update(index=journal['index']+1,operation=None)
                        version=store.compare_and_set(version,value)
                        continue
                operation['phase']='in_flight'
                version=store.compare_and_set(version,value)
                try:
                    counts['write_calls'] += 1
                    response = api.create_page(data_source, ident, row['properties'], row['request_blocks']) if operation['kind']=='create' else api.append_blocks(row['page_id'], row['request_blocks'], row['after_anchor'])
                except Exception:
                    fail('uncertain_create' if operation['kind']=='create' else 'uncertain_append')
                operation.update(phase='acknowledged',response=response)
                version = store.compare_and_set(version, value)
            snapshot = read_target(api, ident, data_source)
            if operation['kind'] == 'create':
                response = operation.get('response')
                if response is None:
                    recovery = create_recovery
                    if not isinstance(recovery, dict) or digest(recovery) != approved_recovery_sha or set(recovery) != {'plan_sha256','note_id','page_id','snapshot_sha256'} or recovery['plan_sha256'] != approved_plan_sha or recovery['note_id'] != ident or recovery['snapshot_sha256'] != target_sha(snapshot, ident, data_source): fail('uncertain_create')
                    response = recovery['page_id']
                try: response = p.notion_id(response)
                except Exception: fail('readback_mismatch')
                managed = created(snapshot, row, data_source, response)
                publication.check(plan['publication'])
                value['receipts'][ident] = receipt(snapshot, ident, data_source, managed, plan['publication'], row['source'])
                value['receipts'][ident]['creation_plan_sha256']=approved_plan_sha
                value['failures'].pop(ident,None)
                counts['created'] += 1; journal.update(index=journal['index']+1, operation=None)
                version = store.compare_and_set(version, value); continue
            if operation['kind'] == 'append':
                try:
                    roots, managed = appended(operation['before'], snapshot, row, data_source, operation.get('response'))
                except Blocked:
                    if operation['phase'] == 'in_flight' and target_sha(snapshot,ident,data_source) == target_sha(operation['before'],ident,data_source): fail('uncertain_append')
                    raise
                operation.update(kind='cleanup',phase='verified',working=snapshot,new_roots=roots,managed=managed)
                version = store.compare_and_set(version, value)
            while operation['archive_index'] < len(row['old_roots']):
                old_root = row['old_roots'][operation['archive_index']]
                if operation['kind'] == 'archive' and operation['phase'] != 'prepared':
                    snapshot = read_target(api,ident,data_source)
                    try: archived(operation['working'],snapshot,ident,data_source,old_root)
                    except Blocked:
                        if target_sha(snapshot,ident,data_source) == target_sha(operation['working'],ident,data_source): fail('uncertain_archive')
                        raise
                    operation.update(kind='cleanup',phase='verified',working=snapshot,archive_index=operation['archive_index']+1)
                    version = store.compare_and_set(version,value); continue
                publication.check(plan['publication'])
                snapshot = read_target(api,ident,data_source)
                if target_sha(snapshot,ident,data_source) != target_sha(operation['working'],ident,data_source): fail('target_changed')
                if operation['kind'] != 'archive':
                    operation.update(kind='archive',phase='prepared')
                    version = store.compare_and_set(version,value)
                publication.check(plan['publication'])
                if target_sha(read_target(api,ident,data_source),ident,data_source) != target_sha(snapshot,ident,data_source): fail('target_changed')
                publication.check_assets(row.get('source_assets',{}))
                guard_writers()
                if operation['kind']=='append' and hasattr(api,'preflight_append'):
                    try:api.preflight_append(row['page_id'],row['request_blocks'],row['after_anchor'])
                    except Exception as exc:
                        capability=str(exc)
                        if capability not in ('unsupported_prepend','asset_write_contract_missing','request_budget'):fail('target_invalid')
                        # A pure preflight and durable prepared phase establish
                        # that no API attempt occurred. Isolate this record.
                        value['failures'][ident]={'md_blob':row['source']['blob'],'source_commit':plan['publication']['commit'],'policy_sha256':plan['policy_sha256'],'reason':capability}
                        counts['blocked']+=1;journal.update(index=journal['index']+1,operation=None)
                        version=store.compare_and_set(version,value)
                        continue
                operation['phase']='in_flight'
                version=store.compare_and_set(version,value)
                try:
                    counts['write_calls'] += 1; api.archive_block(old_root)
                except Exception: fail('uncertain_archive')
                # Resume and normal paths share the same full post-write check.
            final = read_target(api,ident,data_source)
            if target_sha(final,ident,data_source) != target_sha(operation['working'],ident,data_source): fail('target_changed')
            publication.check(plan['publication'])
            value['receipts'][ident] = receipt(final,ident,data_source,operation['managed'],plan['publication'],row['source'])
            if row.get('creation_plan_sha256'): value['receipts'][ident]['creation_plan_sha256']=row['creation_plan_sha256']
            value['failures'].pop(ident,None)
            counts['updated'] += 1; journal.update(index=journal['index']+1,operation=None)
            version = store.compare_and_set(version,value)
        value['journal'] = None
        version = store.compare_and_set(version,value)
        return {'status':'partial' if counts['blocked'] or counts['retained_conflicts'] or counts['deferred'] else 'verified','reason':None,**counts,'state_sha256':version,'external_write_attempts':counts['write_calls'],'write_state':'confirmed' if counts['created']+counts['updated'] else 'not_attempted'}
    except Blocked as exc: reason = str(exc)
    except Exception: reason = 'journal_failed'
    return {'status':'blocked','reason':reason,**counts,'external_write_attempts':counts['write_calls'],'write_state':'uncertain' if reason.startswith('uncertain_') or counts['write_calls'] else 'unknown'}


def public_report(result):
    out = {'status':'blocked','reason':'other','created':None,'updated':None,'noop':None,'blocked':None,'write_calls':None,'external_write_attempts':None,'deferred':None,'retained_conflicts':None,'write_state':'unknown'}
    if not isinstance(result,dict): return out
    if result.get('status') not in ('verified','partial','blocked'): return out
    for key in ('created','updated','noop','blocked','write_calls','external_write_attempts','deferred','retained_conflicts'):
        if type(result.get(key)) is not int or not 0 <= result[key] <= 10000: return out
    out.update({key:result[key] for key in out if key not in ('status','reason','write_state')})
    if result.get('write_state') in ('confirmed','not_attempted','uncertain','unknown'): out['write_state']=result['write_state']
    reason=result.get('reason')
    out['status'] = result['status']; out['reason'] = reason if reason is None or (isinstance(reason,str) and reason in REASONS) else 'other'
    return out
