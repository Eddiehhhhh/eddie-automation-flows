"""Bounded details with complete-list detection and persistent fair audits."""
import bisect
from core import Hold,digest,identity

class IncrementalSelector:
    def __init__(self,store,paused_ids=(),detail_budget=100,audit_reserve=25,window=False):
        if type(detail_budget) is not int or type(audit_reserve) is not int or not 0<audit_reserve<=detail_budget:raise Hold('invalid_source_budget')
        self.store=store;self.paused=set(map(str,paused_ids));self.budget=detail_budget
        self.previous=dict(store.db.execute('SELECT id,summary_hash FROM source_summaries'))
        self.state=store.get('incremental_source_v1') or {}
        self.window=window
        self.deferred=[nid for nid in self.state.get('deferred_ids',[]) if nid not in self.paused]
        self.window_result={}
        self.audit_order=[]
        if not window:
            known=set(self.previous)|{row[0] for row in store.db.execute('SELECT id FROM inventory')}
            known.update(row[0].split(':',1)[1] for row in store.db.execute("SELECT k FROM journal WHERE k LIKE 'source_hold:%'"))
            known=sorted(known-self.paused)
            offset=bisect.bisect(known,self.state.get('audit_cursor',''))
            self._set_audit((known[offset:]+known[:offset])[:audit_reserve])
        self.selected=set();self.changed=[];self.changed_seen=set();self.new_changed=0;self.committed=False

    def window_position(self):
        cursor = self.state.get('sweep_cursor')
        cycle = self.state.get('sweep_cycle', 0)
        if ((cursor is not None and (type(cursor) not in (str, int) or not 0 < len(str(cursor)) <= 256
                                    or not str(cursor).isprintable()))
                or type(cycle) is not int or cycle < 0):
            raise Hold('invalid_incremental_window')
        return cursor, cycle

    def prepare_window(self, observed, audit_order, cursor, cycle):
        self.window_observed = set(observed)
        self.window_result = {'sweep_cursor': cursor, 'sweep_cycle': cycle}
        held = [sid for sid in observed if sid not in self.paused and
                (self.store.get('source_hold:' + sid) or {}).get('status') == 'held']
        audit = [sid for sid in dict.fromkeys(held[:10] + audit_order) if sid not in self.paused]
        self._set_audit(audit[:self.budget])

    def _set_audit(self, audit_order):
        self.audit_order=audit_order;self.audit=set(audit_order)
        self.changed_limit=self.budget-len(self.audit)
        self.deferred=[nid for nid in self.deferred if nid not in self.audit]
        # Leave capacity for newly observed changes even while an old backlog exists.
        fresh_slots=min(25,max(1,self.changed_limit//3)) if self.changed_limit else 0
        deferred_slots=self.changed_limit-fresh_slots
        deferred = self.deferred if not self.window else [sid for sid in self.deferred if sid in self.window_observed]
        self.reserved=set(deferred[:deferred_slots])
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
        pending=[nid for nid in self.deferred+self.changed if nid not in self.selected and
                 (nid in self.changed_seen or self.window and nid not in self.window_observed)]
        pending=list(dict.fromkeys(pending))
        state={'audit_cursor':self.audit_order[-1] if self.audit_order else self.state.get('audit_cursor',''),
               'deferred_ids':pending, **self.window_result}
        result=self.store.accept(snapshots,scan_state=state,held_summaries=holds)
        self.committed=True
        return result
