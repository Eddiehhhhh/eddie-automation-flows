"""Pinned historical cohorts. No enumeration, credential access, or publication.

A historical selection is neither a Git delta nor a successful body receipt.
Exact reviewed native placeholder leaves anchor additions and are retained. All blocks
and properties remain protected; fresh full-page reads precede every write.
"""
from copy import deepcopy
import re
import get_notion_executor as e

CONTRACT = 'get-notion-historical-cohort-1'
MAX_COHORT = 10
MAX_LIBRARY = 2500
PLACEHOLDERS = frozenset(('（无文本内容）', '(无文本内容)'))
REVIEW_KEYS = {'source_sha256', 'review_evidence_sha256', 'destination_data_source_id',
               'credential_free', 'transmission_approved', 'media_free'}
# Exact hashes only; unreviewed CLI input cannot authorize any apply operation.
APPROVED_MANIFESTS = frozenset()


def validate(manifest, expected, *, apply=False):
    try:
        if not isinstance(manifest, dict) or e.digest(manifest) != expected: e.fail('approval_missing')
        keys = {'contract', 'source_commit', 'data_source_id', 'database_id',
                'audit_sha256', 'user_scope_sha256', 'records'}
        if set(manifest) != keys or manifest['contract'] != CONTRACT: e.fail('approval_missing')
        e.p.hex_hash(expected,64)
        if apply and expected not in APPROVED_MANIFESTS: e.fail('approval_missing')
        e.p.hex_hash(manifest['source_commit'],40)
        for key in ('audit_sha256','user_scope_sha256'): e.p.hex_hash(manifest[key],64)
        for key in ('data_source_id','database_id'):
            if e.p.notion_id(manifest[key]) != manifest[key]: e.fail('wrong_parent')
        rows=manifest['records']
        if not isinstance(rows,dict) or not 1<=len(rows)<=MAX_COHORT: e.fail('request_budget')
        paths=set(); pages=set()
        for ident,row in rows.items():
            # Native adapter is intentionally narrower than the source parser.
            if not isinstance(ident,str) or not re.fullmatch('[0-9]{19}',ident): e.fail('source_invalid')
            if not isinstance(row,dict) or set(row)!={'path','blob','sha256','mode','review','target'}: e.fail('approval_missing')
            e.p.safe_path(row['path']);e.p.hex_hash(row['blob'],40);e.p.hex_hash(row['sha256'],64)
            if not row['path'].lower().endswith(('.md','.markdown')) or row['path'] in paths: e.fail('duplicate_id')
            paths.add(row['path'])
            review=row['review']
            if not isinstance(review,dict) or set(review)!=REVIEW_KEYS: e.fail('privacy_scope_required')
            if review['source_sha256']!=row['sha256'] or review['destination_data_source_id']!=manifest['data_source_id'] or any(review[k] is not True for k in ('credential_free','transmission_approved','media_free')): e.fail('privacy_scope_required')
            e.p.hex_hash(review['review_evidence_sha256'],64)
            target=row['target']
            if row['mode']=='new_numeric':
                if target is not None: e.fail('approval_missing')
            elif row['mode']=='existing_placeholder':
                if not isinstance(target,dict) or set(target)!={'page_id','snapshot_sha256','placeholder_blocks'}: e.fail('missing_block_ownership')
                page=e.p.notion_id(target['page_id'])
                if page != target['page_id'] or page in pages: e.fail('duplicate_id')
                pages.add(page);e.p.hex_hash(target['snapshot_sha256'],64);blocks=target['placeholder_blocks']
                if not isinstance(blocks,list) or len(blocks)!=1: e.fail('missing_block_ownership')
                for block in blocks:
                    if not isinstance(block,dict) or set(block)!={'id','normalized_hash'}: e.fail('missing_block_ownership')
                    if e.p.notion_id(block['id'])!=block['id']: e.fail('missing_block_ownership')
                    e.p.hex_hash(block['normalized_hash'],64)
            else: e.fail('approval_missing')
        return manifest
    except e.Blocked: raise
    except Exception: e.fail('approval_missing')


def source_check(data, ident):
    """Conservative credential/media backstop, in addition to exact review."""
    body=e.p.source_body(data,ident)
    if not body.strip(): e.fail('empty_source')
    # Do not silently export credential-like strings even in a reviewed scope.
    text=data.decode('utf-8')
    if re.search(r'(?i)-----BEGIN [A-Z ]*PRIVATE KEY-----|\b(?:api[_-]?key|access[_-]?token|authorization|password|passwd|secret)\s*[:=]\s*["\']?[^\s"\']{6,}|\b(?:gh[pousr]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_-]{20,})',text): e.fail('privacy_scope_required')
    if '![' in body or re.search(r'(?i)<(?:img|audio|video|iframe)\b',body): e.fail('missing_asset')
    # No attachment-bearing frontmatter in this first bounded writer.
    header=text.split('\n---\n',1)[0]
    for line in header.splitlines():
        key,sep,value=line.partition(':')
        if sep and key=='note_type' and value.strip().strip(chr(34)).strip(chr(39)) in e.content_scope.RECORDINGS: e.fail('missing_asset')
        if sep and key in ('attachments','audio_url') and value.strip() not in ('','null','[]','""',"''"):
            e.fail('missing_asset')
    reason=e.content_scope.reason(ident,data)
    if reason: e.fail(reason)
    return body


class Publication:
    def __init__(self,root,manifest,expected):
        validate(manifest,expected);self.manifest=deepcopy(manifest)
        self.repo=e.git.OfflineGit(root);self.current_ref='refs/heads/main'
        commit=manifest['source_commit'];tree=self.repo.tree(commit)
        records={ident:{k:row[k] for k in ('path','blob','sha256')} for ident,row in manifest['records'].items()}
        total=0
        for ident,row in records.items():
            if tree.get(row['path'])!=('100644','blob',row['blob']): e.fail('source_invalid')
            data=self.repo.read(commit,row['path'],e.p.MAX_NOTE);total+=len(data)
            if e.digest(data)!=row['sha256'] or e.p.git_blob(data)!=row['blob']: e.fail('source_invalid')
            source_check(data,ident)
        if total>10*e.p.MAX_NOTE: e.fail('request_budget')
        self.descriptor={'repository':e.p.REPOSITORY,'commit':commit,'parent':commit,
            'changed_content_ids':[],'records':records,'initial_cohort':None,
            'historical_manifest_sha256':expected,
            'content_tree':{path:row[2] for path,row in tree.items() if path.startswith('Raw/03 Get/') and row[:2]==('100644','blob')}}
        if len(e.encoded(self.descriptor))>8*1024*1024: e.fail('request_budget')
        self.check(self.descriptor)
    check=e.GitPublication.check
    check_assets=e.GitPublication.check_assets
    read=e.GitPublication.read


def is_placeholder(node,page):
    if node['type']!='paragraph' or node['parent_id']!=page or node['children_ids']: return False
    runs=node['payload'].get('rich_text',[])
    if not isinstance(runs,list) or not runs: return False
    return ''.join(run.get('text',{}).get('content','') for run in runs) in PLACEHOLDERS and all(run.get('type')=='text' and run.get('text',{}).get('link') is None for run in runs)


def target_pin(snapshot,ident,data_source):
    """Read-only proposal; callers must review/pin it before apply registration."""
    view=e.target(snapshot,ident,data_source)
    if view is None: e.fail('missing_target')
    placeholders=[i for i in view['roots'] if is_placeholder(view['nodes'][i],view['page_id'])]
    if len(placeholders)!=1: e.fail('missing_block_ownership')
    return {'page_id':view['page_id'],'snapshot_sha256':e.digest(snapshot),'placeholder_blocks':[{'id':i,'normalized_hash':e.p.block_hash(view['nodes'][i])} for i in placeholders]}


def prepare(publication,state,sha,snapshots,policy,manifest,expected,*,
            property_manifest=None,expected_property_manifest_sha256=None,property_schema=None):
    validate(manifest,expected)
    if publication.descriptor.get('historical_manifest_sha256')!=expected: e.fail('source_invalid')
    e.check_state(state)
    if e.digest(state)!=sha or state['journal'] is not None: e.fail('journal_busy')
    if len(set(state['receipts'])|set(manifest['records']))>MAX_LIBRARY: e.fail('request_budget')
    if policy['data_source_id']!=manifest['data_source_id']: e.fail('wrong_parent')
    scopes={};early={}
    for ident,row in manifest['records'].items():
        try:
            if e.plan_recovery.held(state,ident,row['target']['page_id'] if row['target'] else None): e.fail('unresolved_operation')
            source_check(publication.read(ident),ident)
            # Existing successful receipts remain authoritative; never re-adopt.
            if ident in state['receipts']:
                prior=state['receipts'][ident]
                if row['mode']=='existing_placeholder' and prior['page_id']!=row['target']['page_id']: e.fail('target_changed')
                # This historical permission is first-adoption only. A later
                # source version belongs to the normal managed-update path;
                # never append it and demote the prior managed body to protected.
                if prior['md_blob']!=row['blob']: e.fail('content_review_required')
                continue
            view=e.target(snapshots.get(ident),ident,manifest['data_source_id'])
            if row['mode']=='new_numeric':
                if property_manifest is None: e.fail('approval_missing')
                if view is not None: e.fail('duplicate_id')
                continue
            if view is None or view['page_id']!=row['target']['page_id']: e.fail('missing_target')
            if e.digest(snapshots[ident])!=row['target']['snapshot_sha256']: e.fail('target_changed')
            owned=row['target']['placeholder_blocks']
            for block in owned:
                node=view['nodes'].get(block['id'])
                if node is None or not is_placeholder(node,view['page_id']) or e.p.block_hash(node)!=block['normalized_hash']: e.fail('human_edit_conflict')
            # Entire reviewed observation is unchanged; only the exact placeholder
            # leaf is owned. Other approved blocks remain protected.
            scopes[ident]={'page_id':view['page_id'],'snapshot_sha256':e.digest(view),
                           'managed_block_ids':[block['id'] for block in owned]}
        except (e.Blocked,e.p.Blocked) as exc:
            early[ident]=str(exc) if str(exc) in e.REASONS else 'target_invalid'
    property_options={} if property_manifest is None else {'property_manifest':property_manifest,
        'expected_property_manifest_sha256':expected_property_manifest_sha256,'property_schema':property_schema}
    plan=e.prepare(publication,state,sha,snapshots,policy,initial_scope=scopes,
                   expected_scope_sha=e.digest(scopes) if scopes else None,**property_options)
    for row in plan['records']:
        if row['note_id'] in early: row.update(action='blocked',reason=early[row['note_id']])
        # Resolved discussion completeness is unavailable. Retain EVERY old
        # block, including the placeholder; only new content becomes managed.
        if row.get('action')=='update' and row['note_id'] not in state['receipts']:
            row['old_roots']=[]
            row['preserve_historical_roots']=True
        # No format can sneak media through the source backstop.
        if row.get('source_assets'): row.update(action='blocked',reason='missing_asset')
        if any(block.get('children') for block in row.get('rendered',[])):
            row.update(action='blocked',reason='unsupported_source_format')
    plan.update(historical_only=True,historical_manifest_sha256=expected,
                historical_manifest=deepcopy(manifest),source_mode='historical_cohort')
    return plan

CAPTURE_CONTRACT='get-notion-historical-capture-1'
APPROVED_CAPTURE_SCOPES=frozenset()


def validate_capture(candidate,expected,*,apply=False):
    if not isinstance(candidate,dict) or e.digest(candidate)!=expected: e.fail('approval_missing')
    if candidate.get('contract')!=CAPTURE_CONTRACT: e.fail('approval_missing')
    if apply and expected not in APPROVED_CAPTURE_SCOPES: e.fail('approval_missing')
    # Reuse all source, privacy, destination, cohort and identity bounds.
    probe=deepcopy(candidate);probe['contract']=CONTRACT
    for row in probe.get('records',{}).values():
        page=row.get('target')
        if row.get('mode')=='existing_placeholder':
            if not isinstance(page,dict) or set(page)!={'page_id'}: e.fail('approval_missing')
            row['target']={'page_id':page['page_id'],'snapshot_sha256':'0'*64,
                           'placeholder_blocks':[{'id':'0'*32,'normalized_hash':'0'*64}]}
    validate(probe,e.digest(probe))
    return probe


def check_captures(captures):
    if not isinstance(captures,dict) or len(captures)>250: e.fail('state_invalid')
    if len(e.encoded(captures))>16*1024*1024: e.fail('request_budget')
    for key,value in captures.items():
        if not isinstance(value,dict) or set(value)!={'candidate','manifest','snapshots'}: e.fail('state_invalid')
        validate_capture(value['candidate'],key)
        manifest=value['manifest'];validate(manifest,e.digest(manifest))
        if set(value['snapshots'])!=set(manifest['records']): e.fail('state_invalid')
        for ident,row in manifest['records'].items():
            if row['mode']=='existing_placeholder' and target_pin(value['snapshots'][ident],ident,manifest['data_source_id'])!=row['target']: e.fail('state_invalid')
        base=deepcopy(manifest);base['contract']=CAPTURE_CONTRACT
        for row in base['records'].values():
            if row['mode']=='existing_placeholder': row['target']={'page_id':row['target']['page_id']}
        if base!=value['candidate']: e.fail('state_invalid')


def preserve_captures(old,new):
    for key,value in old.get('historical_captures',{}).items():
        if new.get('historical_captures',{}).get(key)!=value: e.fail('state_changed')
