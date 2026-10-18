"""Injected, bounded coordinator for the three externally reviewed PNGs.

No CLI, credential lookup, publication or policy migration lives here. The
caller supplies separately authenticated source/asset/state/policy anchors.
Default preparation has zero CAS and zero external writes. Full page shape,
privacy, identity and fresh source guards precede any upload creation.
"""
from copy import deepcopy
from collections import Counter
import json

import get_notion_executor as e
import get_notion_pipeline as pipeline
import get_notion_live_transport as live
import get_notion_outbox as outbox
import get_notion_png_evidence as evidence_module
import get_notion_png_store as storage
import get_notion_png_upload as upload
import get_notion_png_transport as multipart
import get_notion_uploaded_png as image
import get_notion_png_expired_recovery as expired_recovery

CONTRACT='get-notion-three-png-runtime-1'
REASONS=frozenset(e.REASONS)|{'asset_evidence_invalid','asset_current_changed','asset_reference_changed','asset_receipt_invalid','png_upload_unverified','upload_expired_or_near_expiry','upload_unusable','create_uncertain_no_retry','send_uncertain_read_only','request_uncertain','request_not_dispatched','permission_denied','durability_unconfirmed'}

def fail(reason):raise ValueError(reason) from None

class Session:
    def __init__(self,evidence,publication,store,request,approved_policy,expected_policy_sha256,*,current_commit,asset_guard,writers_guard,apply=False,now=None):
        if not isinstance(evidence,evidence_module.AssetEvidence) or not isinstance(request,live.Transport) or type(apply) is not bool:fail('approval_missing')
        if not all(callable(x) for x in (current_commit,asset_guard,writers_guard)):fail('approval_missing')
        self.policy=pipeline.policy(approved_policy,expected_policy_sha256)
        if not self.policy['allow_create'] or not self.policy['format_accepted']:fail('approval_missing')
        if apply and (not self.policy['allow_apply'] or self.policy['plan_authorization']!='standing_bounded'):fail('approval_missing')
        self.evidence=evidence;self.publication=publication;self.store=store;self.request=request;self.current=current_commit
        self.asset_guard=asset_guard;self.writers_guard=writers_guard;self.apply=apply;self.now=now
        self.ready={};self.blocked={};self.finished=False;self.upload_round_attempted=False
        publication.check(publication.descriptor);evidence.check_current(current_commit())
        if publication.descriptor['commit']!=evidence.descriptor['publication_commit']:fail('source_changed')

    def read(self):
        state,sha=self.store.read();outbox.check_state(state)
        if outbox.digest(state)!=sha:fail('state_changed')
        assets=state.get('png_assets')
        if assets is not None and (assets['evidence_sha256']!=self.evidence.evidence_sha256 or not e.same(assets['evidence_anchor'],self.evidence.descriptor)):fail('asset_receipt_invalid')
        return state,sha

    def fresh_guard(self):
        self.publication.check(self.publication.descriptor)
        self.evidence.check_current(self.current())
        if self.asset_guard() is not True:fail('asset_current_changed')
        if not e.p.writer_gate({'old_writer_evidence':self.writers_guard()}):fail('writer_gate_unknown')
        return True

    def _binding(self,ident,data):
        reference=evidence_module.exact_reference(data,ident)
        b=self.evidence.resolve(ident,data,reference,current_commit=self.current());a=b['asset']
        value={'note_id':ident,'md_sha256':b['source']['sha256'],'asset_path':a['path'],'git_blob':a['blob'],'sha256':a['sha256'],'bytes':a['bytes'],'mime':a['mime'],'data_source_id':self.policy['data_source_id'],'source_commit':b['source_commit'],'reference_sha256':b['reference_sha256']}
        raw=self.evidence.read_asset(b,current_commit=self.current())
        plan=upload.prepare(value,upload.digest(value),self.evidence.asset_tree,raw)
        return plan,raw

    def prepare(self):
        """Private plans remain in memory; expose only fixed aggregate counts."""
        state,_=self.read();self.ready={};self.blocked={}
        if state['engine_state']['journal'] is not None:fail('journal_busy')
        self.fresh_guard()
        preview=image.Resolver(self.evidence,{},current_commit=self.current,preview=True)
        ids=[i for i in sorted(self.publication.descriptor['records']) if upload.sha(i.encode()) in upload.PINS]
        if len(ids)>3:fail('request_budget')
        api=live.API(self.request,self.policy['data_source_id'],ids,data_source_pin=self.policy['data_source_pin'],database_pin=self.policy['database_pin']) if ids else None
        for ident in ids:
            try:
                engine=state['engine_state'];source=self.publication.descriptor['records'][ident]
                if ident in engine['receipts']:continue
                if e.plan_recovery.held(engine,ident):fail('unresolved_operation')
                data=self.publication.read(ident);reason=e.content_scope.reason(ident,data)
                if reason:fail(reason)
                pending=state['pending_by_id'].get(ident)
                if pending is None or any(pending[k]!=v for k,v in {'commit':self.publication.descriptor['commit'],'path':source['path'],'blob':source['blob'],'sha256':source['sha256']}.items()):fail('source_changed')
                plan,raw=self._binding(ident,data)
                rendered,bindings,warnings=e.p.render_markdown(data,ident,{},source['path'],self.publication.descriptor['content_tree'],reviewed_png=preview)
                counts=image.preflight_blocks(rendered)
                self.publication.check_assets({b['path']:b['git_blob'] for b in bindings.values()})
                header=data.decode().split('\n---\n',1)[0];titles=[x[7:] for x in header.splitlines() if x.startswith('title: ')]
                if len(titles)!=1:fail('source_invalid')
                try:title=json.loads(titles[0])
                except ValueError:title=titles[0]
                if not isinstance(title,str) or not title or len(title)>2000:fail('request_budget')
                properties={'笔记 ID':{'type':'rich_text','rich_text':[e.p.text_run(ident)]},
                    self.policy['title_property']:{'type':'title','title':[e.p.text_run(title)]}}
                if expired_recovery.only_note_id(state) is not None:
                    recovery=state['png_assets']['expired_recovery']
                    old=storage.receipt_for(recovery['old_slot'])
                    comparison=deepcopy(rendered)
                    def bind_comparison(nodes):
                        for node in nodes:
                            if node['type']=='image':
                                payload=node['payload']
                                if payload.get('kind')!='notion_png_preflight' or any(payload.get(k)!=old[k] for k in ('sha256','byte_count','mime')):fail('asset_receipt_invalid')
                                node['payload']=image.semantic(old,payload['caption'])
                            bind_comparison(node['children'])
                    bind_comparison(comparison)
                    expired_recovery.check_preflight(state,ident,comparison,source,properties)
                    # Discard the comparison copy. The actual preflight blocks
                    # remain unwritable and never contain the expired handle.

                # Every target must still be a uniquely missing new page.
                snapshot=api.read_note(ident);view=e.target(snapshot,ident,self.policy['data_source_id'])
                if view is not None:fail('missing_block_ownership')
                self.ready[ident]={'plan':plan,'data':raw,'counts':counts}
            except Exception as exc:
                reason=str(exc)
                if reason in ('permission_denied','asset_permission_denied','writer_gate_unknown'):raise
                self.blocked[ident]=reason if reason in REASONS else 'asset_evidence_invalid'
        self.fresh_guard()
        self.finished=True
        return self.report()

    def report(self):
        # This is a preparation report, not a write-outcome receipt. Once an
        # upload round starts, even an exception can follow durable/external
        # effects; this instance must never advertise zero writes again.
        if self.upload_round_attempted:fail('preparation_report_expired')
        return {'contract':CONTRACT,'selected':len(self.ready)+len(self.blocked),'upload_candidates':len(self.ready),'blocked':len(self.blocked),'reason_counts':dict(Counter(self.blocked.values())),'upload_bytes':sum(len(v['data']) for v in self.ready.values()),'remote_writes':0,'journal_writes':0,'prepared_only':True}

    def _kernel(self,ident,plan,raw,*,allow):
        slot=storage.UploadStore(self.store,ident,upload.digest(plan),allow_persist=allow)
        network=multipart.Transport(self.request,plan,upload.digest(plan),raw,slot,allow_writes=allow,now=self.now,before_write=self.fresh_guard if allow else None)
        return upload.Upload(plan,upload.digest(plan),raw,slot,network,allow_writes=allow,now=self.now) if self.now is not None else upload.Upload(plan,upload.digest(plan),raw,slot,network,allow_writes=allow)

    def advance_uploads(self):
        if not self.apply or not self.finished or self.upload_round_attempted:fail('approval_missing')
        self.upload_round_attempted=True
        # Re-prepare in the same invocation is not a bypass around a saved
        # uncertain write; the kernel recognizes all persisted phases.
        self.fresh_guard();state,sha=self.read()
        if state['engine_state']['journal'] is not None:fail('journal_busy')
        wanted=storage.initialize(state,self.evidence.descriptor,self.evidence.evidence_sha256)
        for ident,row in self.ready.items():wanted=storage.add_upload(wanted,row['plan'],upload.digest(row['plan']))
        if not e.same(wanted,state):state,sha=outbox.confirmed_compare_and_set(self.store,sha,wanted)
        for ident,row in self.ready.items():
            kernel=self._kernel(ident,row['plan'],row['data'],allow=True)
            # At most create + send + independent GET; a thrown operation ends
            # this round, never a loop that retries an uncertain request.
            for _ in range(3):
                outcome=kernel.advance()
                if outcome.get('phase')=='uploaded_verified':break
            else:fail('png_upload_unverified')
        state,_=self.read()
        return image.Resolver(self.evidence,storage.upload_receipts(state),current_commit=self.current)

    def verify_unattached(self):
        """Fresh saved-ID GET before page writes; attached joins stay stable."""
        self.fresh_guard();state,_=self.read();assets=state.get('png_assets',{})
        selected=set(self.publication.descriptor['records'])
        journal=state['engine_state']['journal']
        if journal is not None:
            plan=journal['plan']
            if not e.same(plan['publication'],self.publication.descriptor):fail('source_changed')
            index=journal['index'];rows=plan['records'];operation=journal['operation']
            selected=set()
            if operation is not None:
                valid_phases={'create':{'prepared','in_flight','acknowledged'},'append':{'prepared','in_flight','acknowledged'},'archive':{'prepared','in_flight'},'cleanup':{'verified'}}
                if operation.get('kind') not in valid_phases or operation.get('phase') not in valid_phases[operation['kind']] or index>=len(rows):fail('state_invalid')
            if index<len(rows):
                row=rows[index];ident=row['note_id']
                if ident not in self.publication.descriptor['records'] or not e.same(row['source'],self.publication.descriptor['records'][ident]):fail('source_changed')
                # A saved create response is read back, never re-dispatched.
                # Its native image bytes and full tree still need verification.
                # Do not let a later, unattached upload block this readback.
                acknowledged=(operation is not None and operation['kind']=='create'
                    and operation['phase']=='acknowledged' and operation.get('response') is not None)
                if acknowledged:
                    if row['action']!='create':fail('state_invalid')
                    e.p.notion_id(operation['response'])
                else:selected={ident}
        for ident,row in assets.get('uploads',{}).items():
            if ident in assets.get('attachments',{}):continue
            if ident not in selected:continue
            if row['state']['phase']!='uploaded_verified':fail('png_upload_unverified')
            data=self.publication.read(ident);plan,raw=self._binding(ident,data)
            if not e.same(plan,row['plan']):fail('asset_receipt_invalid')
            result=self._kernel(ident,plan,raw,allow=False).advance()
            if result.get('phase')!='uploaded_verified' or not e.same(result['receipt'],storage.receipt_for(row)):fail('asset_receipt_invalid')
        return self.writers_guard()
