"""Durable Notion outbox codec and bounded dispatcher. No transport or ref writes.

The chain digest and initial cursor are EXTERNAL confirmed-publication trust.
The caller authenticates the Get receipt provenance first; this module also
reads each fixed-path receipt and rederives its parent, linkage and Markdown
changes from Git. Queue and engine journal share ONE state CAS. A production
store must use this exact codec and publish/read back the CAS before returning.
"""
from copy import deepcopy
import json

import get_notion_executor as e
import get_recurring_entry as entry
import get_notion_png_store as png_store

p=e.p;git=e.git
STATE_PATH=e.STATE_PATH
CONTRACT='get-notion-outbox-1'
MAX_RECORDS=2500
MAX_CHAIN=500
MAX_STATE=e.MAX_STATE
encoded=e.encoded
digest=e.digest

class BoundedSourceReader(git.OfflineGit):
    """Only derived validated trees are evicted; immutable objects stay in ODB."""
    def tree(self, commit):
        if commit not in self.cache and len(self.cache) >= 2:
            del self.cache[next(iter(self.cache))]
        result = super().tree(commit)
        # Keep the current and previous full tree, not one per ancestor.
        self.cache.pop(commit, None)
        self.cache[commit] = result
        while len(self.cache) > 2:
            del self.cache[next(iter(self.cache))]
        return result


def witness_parent(reader, cursor, history_witness):
    if history_witness is None:
        return None
    if type(history_witness) is not entry.VerifiedHistoryWitness:
        entry.fail('entry_history_witness')
    if cursor != history_witness.observed_commit:
        return None
    # Recheck the exact receipt and both Get-tree endpoints. Authority is the
    # caller's freshly authenticated typed witness, never endpoint equality.
    entry.unchanged_get_history(reader, history_witness.receipt_commit, cursor,
                                history_witness=history_witness)
    return history_witness.receipt_commit


def source_ancestry(reader, ancestor, descendant, history_witness=None):
    if history_witness is None:
        reader.local.run('merge-base', '--is-ancestor', ancestor, descendant)
        return
    # Reuse raw OID, single-parent and 16384-node validation; no alternative
    # network transport is supplied. The source loader must hydrate first.
    import get_confirmed_source as source
    loader = source.Loader(reader.local, None, None, None, '0' * 64,
                           history_witness=history_witness)
    loader.ancestry(ancestor, descendant)


class Blocked(ValueError):pass
def fail(reason):raise Blocked(reason) from None

def initial_state():
    return {'contract':CONTRACT,'origin':None,'policy_sha256':None,'last_enqueued_publication':None,
        'observed_publication_tip':None,'pending_by_id':{},'rotation_order':[],'engine_state':e.initial_state()}

def anchor(value):
    if not isinstance(value,dict) or set(value)!={'commit','receipt_sha256','sequence'}:fail('outbox_state_invalid')
    p.hex_hash(value['commit'],40)
    if type(value['sequence']) is not int or not 0<=value['sequence']<=1000000:fail('outbox_state_invalid')
    if value['sequence']==0:
        if value['receipt_sha256'] is not None:fail('outbox_state_invalid')
    else:p.hex_hash(value['receipt_sha256'],64)
    return value

def check_state(value):
    try:
        if not isinstance(value,dict) or not set(initial_state()) <= set(value) <= set(initial_state()) | set(png_store.ASSET_KEYS) | {'historical_captures', 'source_state_holds'} or value['contract']!=CONTRACT:fail('outbox_state_invalid')
        e.check_state(value['engine_state'])
        e.metadata_pending.check_source_state_holds(value.get('source_state_holds', {}))
        png_store.check_outer_assets(value)
        import get_notion_historical as historical
        historical.check_captures(value.get('historical_captures',{}))
        pending=value['pending_by_id'];order=value['rotation_order'];origin=value['origin'];cursor=value['last_enqueued_publication'];tip=value['observed_publication_tip']
        if not isinstance(pending,dict) or not isinstance(order,list) or len(pending)>MAX_RECORDS or len(order)!=len(pending) or any(not isinstance(i,str) for i in order) or len(set(order))!=len(order) or set(order)!=set(pending):fail('outbox_state_invalid')
        if origin is None:
            if cursor is not None or tip is not None or value['policy_sha256'] is not None or pending:fail('outbox_state_invalid')
        else:
            anchor(origin);anchor(cursor);anchor(tip);p.hex_hash(value['policy_sha256'],64)
            if cursor['sequence']<origin['sequence'] or tip['sequence']<cursor['sequence'] or tip['sequence']==cursor['sequence'] and tip!=cursor:fail('outbox_state_invalid')
        for event in value.get('source_state_holds', {}).values():
            if origin is None or not origin['sequence'] < event['sequence'] <= cursor['sequence']:fail('outbox_state_invalid')
            if event['sequence'] == cursor['sequence'] and any(event[k] != cursor[k] for k in ('commit','receipt_sha256')):fail('outbox_state_invalid')
        paths=set()
        for ident,row in pending.items():
            p.note_id(ident)
            if not isinstance(row,dict) or set(row)!={'commit','parent','receipt_sha256','sequence','path','blob','sha256'}:fail('outbox_state_invalid')
            for k in ('commit','parent','blob'):p.hex_hash(row[k],40)
            for k in ('receipt_sha256','sha256'):p.hex_hash(row[k],64)
            p.safe_path(row['path'])
            if not row['path'].lower().endswith(('.md','.markdown')) or row['path'] in paths or type(row['sequence']) is not int or not origin['sequence']<row['sequence']<=cursor['sequence']:fail('outbox_state_invalid')
            paths.add(row['path'])
        if len(encoded(value))>MAX_STATE:fail('outbox_budget')
        return value
    except Blocked:raise
    except Exception:fail('outbox_state_invalid')

def markdown_delta(reader,parent,commit,budget=None):
    budget={'bytes':0,'events':0,'receipt_bytes':0} if budget is None else budget
    before=reader.tree(parent);after=reader.tree(commit);records={};total=0
    for path in sorted(set(before)|set(after)):
        if not path.startswith(git.retention.GET+'/') or not path.lower().endswith(('.md','.markdown')) or before.get(path)==after.get(path):continue
        if after.get(path,())[:2]!=('100644','blob'):fail('outbox_source_invalid')
        size=reader.local.run('cat-file','-s',after[path][2]).decode().strip()
        if not size.isdigit() or int(size)>p.MAX_NOTE or budget['bytes']+int(size)>128*1024*1024 or budget['events']>=MAX_RECORDS:fail('outbox_budget')
        data=reader.read(commit,path,p.MAX_NOTE);total+=len(data);budget['bytes']+=len(data);budget['events']+=1
        if len(records)>=MAX_RECORDS or total>128*1024*1024:fail('outbox_budget')
        try:
            header=data.decode().split('\n---\n',1)[0];ids=[x[9:] for x in header.splitlines() if x.startswith('note_id: ')]
            if len(ids)!=1:fail('outbox_source_invalid')
            ident=p.note_id(json.loads(ids[0]));p.source_body(data,ident)
        except Exception:fail('outbox_source_invalid')
        if ident in records:fail('outbox_duplicate_id')
        records[ident]={'path':path,'blob':after[path][2],'sha256':digest(data)}
    return records

def checked_receipt(reader,commit,receipt_sha,policy,budget):
    blob=reader.tree(commit).get(entry.FULL_RECEIPT)
    if blob is None or blob[:2]!=('100644','blob'):fail('outbox_chain_invalid')
    size=reader.local.run('cat-file','-s',blob[2]).decode().strip()
    if not size.isdigit() or int(size)>entry.MAX_RECEIPT or budget['receipt_bytes']+int(size)>16*1024*1024:fail('outbox_budget')
    budget['receipt_bytes']+=int(size)
    return entry.read_receipt(reader,commit,receipt_sha,policy)

def verified_publication(reader,row,policy,previous=None,budget=None,*,history_witness=None):
    budget={'bytes':0,'events':0,'receipt_bytes':0} if budget is None else budget
    if not isinstance(row,dict) or set(row)!={'commit','parent','receipt_sha256'}:fail('outbox_chain_invalid')
    for key in ('commit','parent'):p.hex_hash(row[key],40)
    p.hex_hash(row['receipt_sha256'],64)
    receipt=checked_receipt(reader,row['commit'],row['receipt_sha256'],policy,budget)
    if entry.single_parent(reader,row['commit'])!=row['parent'] or receipt['parent_commit']!=row['parent'] or reader.source_get_tree(row['parent'])!=receipt['before_get_tree']:fail('outbox_chain_invalid')
    if previous is not None:
        if receipt['previous_receipt_commit']!=previous['commit'] or receipt['previous_receipt_sha256']!=previous['receipt_sha256'] or receipt['sequence']!=previous['sequence']+1:fail('outbox_chain_invalid')
        entry.unchanged_get_history(reader,previous['commit'],row['parent'],history_witness=history_witness)
    records=markdown_delta(reader,row['parent'],row['commit'],budget)
    if sorted(records)!=receipt['changed_content_ids'] or entry.a.sha(entry.a.encoded(entry.delta(reader,row['parent'],row['commit'])))!=receipt['delta_sha256']:fail('outbox_chain_invalid')
    return receipt,records

def enqueue(root,state,expected_state_sha,chain,expected_chain_sha,*,history_witness=None):
    """Pure state transition; caller MUST durably CAS this before planning.

    A state-only confirmed receipt advances the traversal cursor but adds zero
    IDs. No-op Get runs simply leave the existing pending queue available.
    """
    check_state(state)
    if digest(state)!=expected_state_sha:fail('outbox_state_changed')
    if state['engine_state']['journal'] is not None:fail('journal_busy')
    if not isinstance(chain,dict) or set(chain)!={'repository','policy_sha256','origin','after','tip','publications'} or digest(chain)!=expected_chain_sha or chain['repository']!=p.REPOSITORY:fail('outbox_chain_invalid')
    anchor(chain['origin']);anchor(chain['after']);anchor(chain['tip']);p.hex_hash(chain['policy_sha256'],64)
    rows=chain['publications']
    if not isinstance(rows,list) or len(rows)>MAX_CHAIN or len(encoded(chain))>512*1024:fail('outbox_budget')
    value=deepcopy(state);reader=BoundedSourceReader(root);budget={'bytes':0,'events':0,'receipt_bytes':0}
    if value['origin'] is None:
        if chain['after']!=chain['origin']:fail('outbox_chain_invalid')
        start=chain['origin'];reader.tree(start['commit'])
        if start['receipt_sha256'] is None and entry.FULL_RECEIPT in reader.tree(start['commit']):fail('outbox_chain_invalid')
        if start['receipt_sha256'] is not None:
            old=checked_receipt(reader,start['commit'],start['receipt_sha256'],chain['policy_sha256'],budget)
            if old['sequence']!=start['sequence']:fail('outbox_chain_invalid')
        value.update(origin=deepcopy(start),last_enqueued_publication=deepcopy(start),observed_publication_tip=deepcopy(start),policy_sha256=chain['policy_sha256'])
    elif chain['origin']!=value['origin'] or chain['policy_sha256']!=value['policy_sha256'] or chain['after']!=value['last_enqueued_publication']:fail('outbox_chain_invalid')
    tip=chain['tip'];old_tip=value['observed_publication_tip']
    if tip['sequence']<old_tip['sequence'] or tip['sequence']==old_tip['sequence'] and tip!=old_tip:fail('outbox_chain_invalid')
    source_ancestry(reader,old_tip['commit'],tip['commit'],history_witness)
    if tip['receipt_sha256'] is not None:
        tip_receipt=checked_receipt(reader,tip['commit'],tip['receipt_sha256'],value['policy_sha256'],budget)
        if tip_receipt['sequence']!=tip['sequence'] or entry.single_parent(reader,tip['commit'])!=tip_receipt['parent_commit'] or reader.source_get_tree(tip_receipt['parent_commit'])!=tip_receipt['before_get_tree']:fail('outbox_chain_invalid')
    elif tip!=chain['origin']:fail('outbox_chain_invalid')
    value['observed_publication_tip']=deepcopy(tip)
    previous=value['last_enqueued_publication']
    for row in rows:
        receipt,records=verified_publication(reader,row,value['policy_sha256'],previous,budget,history_witness=history_witness)
        e.metadata_pending.retain_source_state_signal(value,row,receipt)
        for ident,source in records.items():
            if ident not in value['pending_by_id']:value['rotation_order'].append(ident)
            value['pending_by_id'][ident]={**source,**row,'sequence':receipt['sequence']}
        previous={'commit':row['commit'],'receipt_sha256':row['receipt_sha256'],'sequence':receipt['sequence']}
        value['last_enqueued_publication']=previous
    head=reader.local.run('rev-parse','refs/heads/main').decode().strip()
    source_ancestry(reader,previous['commit'],head,history_witness)
    if previous['sequence']>tip['sequence'] or previous['sequence']==tip['sequence'] and previous!=tip:fail('outbox_chain_invalid')
    source_ancestry(reader,previous['commit'],tip['commit'],history_witness)
    # Bounded prefix traversal may persist, but cannot execute before reaching
    # this independently trusted, current confirmed-publication tip.
    entry.unchanged_get_history(reader,tip['commit'],head,history_witness=history_witness)
    check_state(value);return value

class QueuePublication:
    """One oldest queued real publication, max 50 IDs, preserving provenance.

    records is a selected subset of that commit's genuine changed_content_ids.
    It NEVER relabels old pending IDs as changes in a newer/state-only commit.
    """
    def __init__(self,root,state,expected_state_sha,*,resume=False,max_records=e.MAX_RECORDS,history_witness=None):
        if type(max_records) is not int or not 1<=max_records<=e.MAX_RECORDS:fail('outbox_budget')
        check_state(state)
        if digest(state)!=expected_state_sha:fail('outbox_state_changed')
        if state['last_enqueued_publication']!=state['observed_publication_tip']:fail('outbox_chain_incomplete')
        self.history_witness=history_witness
        self.repo=BoundedSourceReader(root);self.current_ref='refs/heads/main';self.state=deepcopy(state)
        journal=state['engine_state']['journal']
        if journal is not None:
            if not resume:fail('journal_busy')
            desc=deepcopy(journal['plan']['publication'])
            row={'commit':desc['commit'],'parent':desc['parent'],'receipt_sha256':desc['outbox_receipt_sha256']}
            wanted=list(desc['records'])
        else:
            if resume:fail('outbox_state_invalid')
            order=state['rotation_order']
            if not order:fail('outbox_empty')
            import get_notion_png_expired_recovery as expired
            selected=expired.only_note_id(state)
            if selected is not None:
                if selected not in state['pending_by_id']:fail('outbox_source_invalid')
                order=[selected]
            first=state['pending_by_id'][order[0]];row={k:first[k] for k in ('commit','parent','receipt_sha256')}
            wanted=[i for i in order if state['pending_by_id'][i]['commit']==row['commit']][:max_records]
        receipt,actual=verified_publication(self.repo,row,state['policy_sha256'],history_witness=history_witness)
        if not wanted or any(i not in actual for i in wanted):fail('outbox_source_invalid')
        if journal is None:
            for ident in wanted:
                pending=state['pending_by_id'][ident]
                if {k:pending[k] for k in actual[ident]}!=actual[ident] or pending['sequence']!=receipt['sequence'] or pending['receipt_sha256']!=row['receipt_sha256']:fail('outbox_source_invalid')
        tree=self.repo.tree(row['commit'])
        self.descriptor={'repository':p.REPOSITORY,'commit':row['commit'],'parent':row['parent'],
            'changed_content_ids':sorted(actual),'records':{i:actual[i] for i in wanted},'initial_cohort':None,
            'content_tree':{path:r[2] for path,r in tree.items() if path.startswith(git.retention.GET+'/') and r[:2]==('100644','blob')},
            'outbox_receipt_sha256':row['receipt_sha256'],'outbox_state_sha256':expected_state_sha if journal is None else desc['outbox_state_sha256']}
        if journal is not None and not e.same(desc,self.descriptor):fail('outbox_source_invalid')
        self.check(self.descriptor)
    def check(self, descriptor):
        if self.history_witness is None:
            return e.GitPublication.check(self, descriptor)
        if descriptor != self.descriptor: e.fail('source_invalid')
        try:
            head = self.repo.local.run('rev-parse', self.current_ref).decode().strip()
            source_ancestry(self.repo, descriptor['commit'], head, self.history_witness)
            tree = self.repo.tree(head)
            if any(tree.get(row['path']) != ('100644', 'blob', row['blob'])
                   for row in descriptor['records'].values()): e.fail('source_changed')
        except e.Blocked: raise
        except Exception: e.fail('source_changed')
    check_assets=e.GitPublication.check_assets
    read=e.GitPublication.read

def confirmed_compare_and_set(store, expected_sha, value):
    """Consume an atomic verified CAS when supplied; keep the generic fallback."""
    verified_cas = getattr(store, 'compare_and_set_verified', None)
    if callable(verified_cas):
        observed, version = verified_cas(expected_sha, value)
    else:
        version = store.compare_and_set(expected_sha, value)
        observed, observed_version = store.read()
        if observed_version != version: fail('outbox_journal_failed')
    check_state(observed)
    if version != digest(value) or not e.same(observed, value): fail('outbox_journal_failed')
    return deepcopy(observed), version


class EngineStore:
    """Fixed envelope adapter. Underlying CAS must be durable and exact-head.

    Keeps its outer version across engine calls: unrelated concurrent queue or
    journal changes fail the CAS, even when the nested engine hash is unchanged.
    Completion/dequeue/rotation are in the SAME CAS as verified receipt/index.
    """
    def __init__(self,store,*,attachment_provider=None):
        if attachment_provider is not None and not callable(attachment_provider):fail('outbox_state_invalid')
        self.store=store;self.outer_version=None;self.outer=None;self.attachment_provider=attachment_provider
    def read(self):
        value,version=self.store.read();check_state(value)
        if version!=digest(value):fail('outbox_state_invalid')
        self.outer=deepcopy(value);self.outer_version=version
        return deepcopy(value['engine_state']),digest(value['engine_state'])
    def compare_and_set(self,expected_sha,value):
        e.check_state(value)
        if self.outer is None or digest(self.outer['engine_state'])!=expected_sha:fail('outbox_state_changed')
        next_value=deepcopy(self.outer);old=self.outer['engine_state']
        e.plan_recovery.preserve_holds(old,value)
        next_value['engine_state']=deepcopy(value)
        journal=value['journal'] or old['journal']
        if journal is not None and not (journal['plan'].get('property_only') or journal['plan'].get('historical_only')):
            plan=journal['plan'];desc=plan['publication']
            if desc.get('initial_cohort') is None:
                if not desc.get('outbox_receipt_sha256'):fail('outbox_source_invalid')
                if old['journal'] is None and desc.get('outbox_state_sha256')!=self.outer_version:fail('outbox_state_changed')
                for row in plan['records']:
                    ident=row['note_id'];pending=self.outer['pending_by_id'].get(ident)
                    prior=value['receipts'].get(ident,{})
                    if pending is None:
                        if prior.get('md_blob')!=row['source']['blob'] or prior.get('source_commit')!=desc['commit']:fail('outbox_source_invalid')
                    elif pending['commit']!=desc['commit'] or pending['receipt_sha256']!=desc['outbox_receipt_sha256'] or any(pending[k]!=row['source'][k] for k in ('path','blob','sha256')):fail('outbox_source_invalid')
            done=len(plan['records']) if value['journal'] is None else value['journal']['index']
            old_done=old['journal']['index'] if old['journal'] is not None else 0
            for row in plan['records'][old_done:done]:
                ident=row['note_id'];pending=next_value['pending_by_id'].get(ident);prior=value['receipts'].get(ident,{})
                if pending is None:continue
                if ident in next_value['rotation_order']:next_value['rotation_order'].remove(ident)
                if prior.get('md_blob')==pending['blob'] and prior.get('source_commit')==pending['commit'] and e.metadata_pending.complete(value,plan,row):
                    del next_value['pending_by_id'][ident]
                else:next_value['rotation_order'].append(ident)
            # Same-source conflicts skipped by the engine are still pending;
            # rotate them when finishing this batch instead of starving peers.
            if value['journal'] is None:
                for ident in plan['skipped_conflicts']:
                    if ident in next_value['rotation_order']:
                        next_value['rotation_order'].remove(ident);next_value['rotation_order'].append(ident)
        candidates={} if self.attachment_provider is None else self.attachment_provider(deepcopy(old),deepcopy(value))
        png_store.join_engine(self.outer,next_value,candidates)
        check_state(next_value)
        observed,new_version=confirmed_compare_and_set(self.store,self.outer_version,next_value)
        self.outer=deepcopy(next_value);self.outer_version=new_version
        return digest(value)

def settle_empty_plan(store,expected_outer_sha,plan,approved_plan_sha):
    """Persist rotation for an all-skipped plan (engine otherwise has no CAS)."""
    if digest(plan)!=approved_plan_sha or plan.get('rules_sha256')!=e.rules_sha256():fail('outbox_approval_invalid')
    value,version=store.read();check_state(value)
    if version!=expected_outer_sha or version!=digest(value) or value['engine_state']['journal'] is not None or plan.get('records') or plan.get('state_sha256')!=digest(value['engine_state']) or plan.get('publication',{}).get('outbox_state_sha256')!=version:fail('outbox_state_changed')
    updated=deepcopy(value)
    # A trusted exact prior readback receipt can already cover a pending row
    # (for example an explicitly approved initial-scope delivery). Do not keep
    # such rows stuck forever just because prepare correctly skipped them.
    for ident,source in plan['publication']['records'].items():
        pending=updated['pending_by_id'].get(ident);prior=updated['engine_state']['receipts'].get(ident,{})
        if not e.metadata_pending.enabled(plan) and pending is not None and pending['commit']==plan['publication']['commit'] and pending['blob']==source['blob'] and prior.get('source_commit')==pending['commit'] and prior.get('md_blob')==pending['blob']:
            del updated['pending_by_id'][ident];updated['rotation_order'].remove(ident)
    for ident in plan['skipped_conflicts']:
        if ident not in updated['pending_by_id']:fail('outbox_source_invalid')
        updated['rotation_order'].remove(ident);updated['rotation_order'].append(ident)
    check_state(updated)
    if e.same(updated,value):return version
    observed,new_sha=confirmed_compare_and_set(store,version,updated)
    return new_sha
