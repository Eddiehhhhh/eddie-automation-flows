"""Bounded details with complete-list detection and persistent fair audits."""
import bisect
from core import Hold,digest,identity

class IncrementalSelector:
    def __init__(self,store,paused_ids=(),detail_budget=100,audit_reserve=25):
        if type(detail_budget) is not int or type(audit_reserve) is not int or not 0<audit_reserve<=detail_budget:raise Hold('invalid_source_budget')
        self.store=store;self.paused=set(map(str,paused_ids));self.budget=detail_budget
        self.previous=dict(store.db.execute('SELECT id,summary_hash FROM source_summaries'))
        state=store.get('incremental_source_v1') or {}
        known=set(self.previous)|{row[0] for row in store.db.execute('SELECT id FROM inventory')}
        known.update(row[0].split(':',1)[1] for row in store.db.execute("SELECT k FROM journal WHERE k LIKE 'source_hold:%'"))
        known=sorted(known-self.paused)
        cursor=state.get('audit_cursor','');offset=bisect.bisect(known,cursor)
        audit_order=known[offset:]+known[:offset]
        self.audit_order=audit_order[:audit_reserve];self.audit=set(self.audit_order)
        self.changed_limit=detail_budget-len(self.audit)
        self.deferred=[nid for nid in state.get('deferred_ids',[]) if nid not in self.paused and nid not in self.audit]
        # Leave capacity for newly observed changes even while an old backlog exists.
        fresh_slots=min(25,max(1,self.changed_limit//3)) if self.changed_limit else 0
        deferred_slots=self.changed_limit-fresh_slots
        self.reserved=set(self.deferred[:deferred_slots])
        self.selected=set();self.changed=[];self.changed_seen=set();self.new_changed=0;self.committed=False
    def __call__(self,summary):
        nid=identity(summary)
        if nid in self.paused:return False
        changed=self.previous.get(nid)!=digest(summary)
        if changed and nid not in self.changed_seen:self.changed.append(nid);self.changed_seen.add(nid)
        chosen=nid in self.audit
        if not chosen and changed:
            if nid in self.reserved:chosen=True
            elif nid not in self.deferred and self.new_changed<self.changed_limit-len(self.reserved):
                self.new_changed+=1;chosen=True
        if chosen:self.selected.add(nid)
        return chosen
    def commit(self,source,snapshots):
        if self.committed or not getattr(source,'scan_complete',False):raise Hold('incomplete_source_scan')
        holds=getattr(source,'holds',[])
        finished=[*snapshots,*holds]
        if {s['id'] for s in finished}!=self.selected or len(finished)!=len(self.selected):raise Hold('source_selection_mismatch')
        if any(s.get('summary_hash')!=source.summaries.get(s['id']) for s in finished):raise Hold('source_summary_mismatch')
        pending=[nid for nid in self.deferred+self.changed if nid in self.changed_seen and nid not in self.selected]
        pending=list(dict.fromkeys(pending))
        old=self.store.get('incremental_source_v1') or {}
        state={'audit_cursor':self.audit_order[-1] if self.audit_order else old.get('audit_cursor',''),'deferred_ids':pending}
        result=self.store.accept(snapshots,scan_state=state,held_summaries=holds)
        self.committed=True
        return result
