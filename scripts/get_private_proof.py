"""Opaque public proofs backed by existing private objects; no writes or tokens.

A public pointer is NOT trusted by itself. Call expansion only within the
existing authenticated run/job/controller verifier, which subsequently validates
the reconstructed original records. No current-state self-hash grants authority.
"""
from copy import deepcopy
import hashlib
import json
import re
import sys
from get_mirror_incremental_replay import capture_json_bytes as public_encoded

CONTRACT='get-private-proof-2'
MARKER='GET_PRIVATE_PROOF_V2 '
PREFIX={'publication':'GET_CANONICAL_PUBLICATION_V1 ','journal':'GET_NOTION_JOURNAL_V1 '}
REPOSITORY='Eddiehhhhh/eddie-llm-wiki'
REF='refs/heads/main'
PATHS={'journal':'.sync/get-notion/state.json','publication':'Raw/03 Get/03 State/canonical-sync-receipt.json'}
MAX_LOG=4*1024*1024
MAX_OBJECT=32*1024*1024
MAX_COMMITS=1024
MAX_PRIVATE_BYTES=256*1024*1024
MAX_RECORD=16384
TIMESTAMP=re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?Z ')

class PrivateProofError(ValueError):pass

def fail():raise PrivateProofError('private_proof_unverified') from None

def encoded(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
def sha(value):return hashlib.sha256(value if isinstance(value,bytes) else encoded(value)).hexdigest()
def oid(raw):return hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()
def checked(value,width):
    if not isinstance(value,str) or not re.fullmatch('[0-9a-f]{'+str(width)+'}',value):fail()
    return value

def unique(rows):
    result={}
    for key,value in rows:
        if key in result:fail()
        result[key]=value
    return result

def strict(raw,maximum,*,serializer=encoded):
    try:
        if not isinstance(raw,bytes) or not 0<len(raw)<=maximum:fail()
        value=json.loads(raw,object_pairs_hook=unique)
        if serializer(value)!=raw or not isinstance(value,dict):fail()
        return value
    except PrivateProofError:raise
    except Exception:fail()

def pointer(kind,record):
    try:
        if kind not in PREFIX or not isinstance(record,dict) or len(encoded(record))>MAX_RECORD:fail()
        sequence=record.get('sequence' if kind=='journal' else 'receipt_sequence')
        if type(sequence) is not int or not 1<=sequence<=1000000:fail()
        previous=record.get('previous_record_sha256') if kind=='journal' else None
        if previous is not None:checked(previous,64)
        if kind=='journal' and (sequence==1)!=(previous is None):fail()
        digest=hashlib.sha256(CONTRACT.encode()+b'\0'+kind.encode()+b'\0'+encoded(record)).hexdigest()
        object_digest=checked(record.get('state_sha256'),64) if kind=='journal' else None
        return {'contract':CONTRACT,'kind':kind,'sequence':sequence,'previous':previous,'digest':digest,'object_digest':object_digest}
    except PrivateProofError:raise
    except Exception:fail()

def checked_pointer(value):
    if (not isinstance(value,dict) or set(value)!={'contract','kind','sequence','previous','digest','object_digest'}
            or value['contract']!=CONTRACT or value['kind'] not in PREFIX
            or type(value['sequence']) is not int or not 1<=value['sequence']<=1000000):fail()
    checked(value['digest'],64)
    if value['kind']=='journal':checked(value['object_digest'],64)
    elif value['object_digest'] is not None:fail()
    if value['previous'] is not None:checked(value['previous'],64)
    if value['kind']=='publication' and value['previous'] is not None:fail()
    if value['kind']=='journal' and (value['sequence']==1)!=(value['previous'] is None):fail()
    return value

def line(kind,record):return MARKER+encoded(pointer(kind,record)).decode()

def expand(raw,resolver,kinds=('publication','journal')):
    """Preserve old logs exactly. Expand only authenticated v2 pointer bytes."""
    try:
        if not isinstance(raw,bytes) or len(raw)>MAX_LOG:fail()
        if MARKER.encode() not in raw:return raw
        if not callable(resolver):fail()
        out=[]; found=0
        for original in raw.decode('utf-8').splitlines(keepends=True):
            bare=original.rstrip('\r\n'); match=TIMESTAMP.match(bare)
            prefix=match.group(0) if match else ''; body=bare[len(prefix):]
            if not body.startswith(MARKER):out.append(original);continue
            p=checked_pointer(strict(body[len(MARKER):].encode(),2048))
            if p['kind'] not in kinds:out.append(original);continue
            found+=1
            if found>1025:fail()
            record=resolver(deepcopy(p))
            if pointer(p['kind'],record)!=p:fail()
            out.append(prefix+PREFIX[p['kind']]+encoded(record).decode()+'\n')
        result=''.join(out).encode()
        if len(result)>MAX_LOG:fail()
        return result
    except PrivateProofError:raise
    except Exception:fail()

class PrivateRecords:
    """Bounded fixed-path reads on the existing private main one-parent chain.

    Never CASes or creates objects. Public digest covers context and full record;
    reconstructed commit/tree/blob/bytes cannot authorize themselves. Values are
    returned privately to the original verifier, never printed.
    """
    def __init__(self,remote,*,max_commits=MAX_COMMITS):
        if type(max_commits) is not int or not 1<=max_commits<=MAX_COMMITS:fail()
        self.remote=remote;self.limit=max_commits;self.nodes=[];self.blobs={};self.head=None;self.bytes=0
        self._node_cache={}
        # One root plus the two fixed paths, with no shared root double-count.
        # At most three original state hints may be outside the ancestor chain.
        depth=sum(len(path.split('/')) for path in PATHS.values())-(len(PATHS)-1)
        self._tree_limit=depth*(self.limit+3)
        self._tree_cache={}
        self._diagnostic={'stage':'binding','reason':'unverified'}
    def _reject(self,stage,reason):
        self._diagnostic={'stage':stage,'reason':reason};fail()
    def _read(self,stage,method,*args):
        self._diagnostic={'stage':stage,'reason':'unverified'}
        try:return method(*args)
        except Exception as exc:
            # Only original fixed local error enums are classified. Never emit
            # exception text, private paths/URLs, hashes, IDs, tokens or bodies.
            code=str(exc)
            reason=('transport' if code in ('remote_read_failed','remote_redirect_blocked')
                or isinstance(exc,(OSError,TimeoutError)) else
                'size' if code=='remote_response_budget' else
                'shape' if code in ('invalid_json','remote_schema_invalid','invalid_source_tree') else
                'hash' if code in ('invalid_commit','remote_commit_invalid','remote_blob_invalid') else
                'ancestry' if code=='remote_ref_invalid' else 'unverified')
            self._diagnostic={'stage':stage,'reason':reason};raise
    def _report_failure(self):
        value=self._diagnostic
        stages={'binding','metadata','ref','commit','tree','blob','receipt','ancestry'}
        reasons={'unverified','transport','shape','size','hash','ancestry'}
        try:
            print(json.dumps({'contract':'get-private-proof-diagnostic-1',
                'stage':value['stage'] if value['stage'] in stages else 'binding',
                'reason':value['reason'] if value['reason'] in reasons else 'unverified'},sort_keys=True),file=sys.stderr)
        except Exception:pass  # Reporting cannot change rejection or authority.
        try:
            observer=getattr(self.remote,'read_diagnostic',None)
            detail=observer() if callable(observer) else None
            phases={'open','read','close','deadline','validation'}
            classes={'http','certificate','tls','timeout','connection','incomplete_read','url_error','validation','other','deadline'}
            counts={'logical_requests','physical_attempts','response_bytes','failed_read_bound','tree_extra_used','tree_extra_limit','tree_physical_attempts'}
            if (isinstance(detail,dict) and set(detail)==counts|{'phase','error_class','http_status'}
                    and detail['phase'] in phases and detail['error_class'] in classes
                    and (detail['http_status'] is None or type(detail['http_status']) is int and 100<=detail['http_status']<=599)
                    and all(type(detail[k]) is int and 0<=detail[k]<=2**53-1 for k in counts)
                    and detail['tree_extra_limit']==8 and detail['tree_extra_used']<=8):
                print(json.dumps({'contract':'get-private-transport-diagnostic-1',**detail},sort_keys=True),file=sys.stderr)
        except Exception:pass  # Observation cannot affect the original rejection.

    def _fixed_tree(self,tree):
        checked(tree,40);key=(REPOSITORY,tree)
        if key in self._tree_cache:return self._tree_cache[key]
        if len(self._tree_cache)>=self._tree_limit:self._reject('tree','size')
        entries=self._read('tree',self.remote.tree_entries,tree,False)
        if not isinstance(entries,list):self._reject('tree','shape')
        if len(entries)>100000:self._reject('tree','size')
        rows={};parts={part for path in PATHS.values() for part in path.split('/')}
        modes={'040000':'tree','100644':'blob','100755':'blob','120000':'blob','160000':'commit'}
        for row in entries:
            if not isinstance(row,dict):self._reject('tree','shape')
            name=row.get('path');mode=row.get('mode');kind=row.get('type');sha=row.get('sha')
            if (not isinstance(name,str) or not name or name in ('.','..')
                    or '/' in name or '\0' in name or name in rows
                    or mode not in modes or modes[mode]!=kind):self._reject('tree','shape')
            if not isinstance(sha,str) or not re.fullmatch('[0-9a-f]{40}',sha):self._reject('tree','hash')
            if kind=='blob' and (type(row.get('size')) is not int or row['size']<0):self._reject('tree','shape')
            rows[name]={k:row[k] for k in ('path','mode','type','sha','size') if k in row}
        try:
            ordered=sorted(rows.values(),key=lambda row:(row['path']+('/' if row['type']=='tree' else '')).encode('utf-8'))
            body=b''.join((row['mode'].lstrip('0')+' '+row['path']).encode('utf-8')+b'\0'+bytes.fromhex(row['sha']) for row in ordered)
        except (UnicodeError,ValueError):self._reject('tree','shape')
        if hashlib.sha1(b'tree '+str(len(body)).encode()+b'\0'+body).hexdigest()!=tree:self._reject('tree','hash')
        # Retain only tiny, SHA-verified metadata projections, never unrelated
        # whole-tree rows, URLs or any private body. Mutable guards never cache.
        result={name:row for name,row in rows.items() if name in parts}
        self._tree_cache[key]=result
        return result
    def _fixed_path_entries(self,tree):
        selected={}
        for path in PATHS.values():
            current=tree;components=path.split('/')
            for index,name in enumerate(components):
                row=self._fixed_tree(current).get(name)
                if row is None:break  # Original absence: never invent an object.
                if index<len(components)-1:
                    if row['type']!='tree' or row['mode']!='040000':self._reject('tree','shape')
                    current=row['sha']
                else:
                    if row['type']!='blob' or row['mode']!='100644':self._reject('tree','shape')
                    if type(row.get('size')) is not int or not 0<row['size']<=MAX_OBJECT:self._reject('tree','size')
                    selected[path]=(row['sha'],row['size'])
        return selected
    def guard(self):
        m=self._read('metadata',self.remote.metadata)
        if m.get('full_name')!=REPOSITORY or m.get('private') is not True:self._reject('metadata','shape')
        head=checked(self._read('ref',self.remote.ref,REF),40)
        if self.head is None:self.head=head
        elif head!=self.head:self._reject('ref','ancestry')
        return head
    def node(self,index):
        if index<len(self.nodes):return self.nodes[index]
        if index!=len(self.nodes) or index>=self.limit:self._reject('ancestry','ancestry')
        commit=self.head if index==0 else self.nodes[-1]['parent']
        if commit is None:return None
        if any(v['commit']==commit for v in self.nodes):self._reject('ancestry','ancestry')
        node=self.read_node(commit);self.nodes.append(node)
        return node
    def read_node(self,commit):
        checked(commit,40)
        # Per-resolver immutable-object cache only. Mutable repository/head
        # guards still execute through guard(), including before/after resolve.
        # Hints are locators, not proof: the original complete record checks
        # and ancestor traversal remain unchanged after this metadata lookup.
        key=(REPOSITORY,commit)
        if key in self._node_cache:
            return deepcopy(self._node_cache[key])
        paired=getattr(self.remote,'commit_with_locator',None)
        if callable(paired): metadata,locator=self._read('commit',paired,commit)
        else: metadata=self._read('commit',self.remote.commit,commit);locator=None
        tree=checked(metadata.get('tree'),40);parents=metadata.get('parents')
        if not isinstance(parents,list) or len(parents)>1:self._reject('ancestry','ancestry')
        parent=checked(parents[0],40) if parents else None
        selected=self._fixed_path_entries(tree)
        node={'commit':commit,'tree':tree,'parent':parent,'objects':selected}
        if locator is not None: node['locator']=deepcopy(locator)
        # Cache only after every original metadata validation succeeds. The
        # three fixed journal hint fields add at most three off-chain nodes.
        # A full cache merely declines retention; it never changes validation.
        if len(self._node_cache)<self.limit+3:
            self._node_cache[key]=deepcopy(node)
        return node
    def object(self,node,kind):
        row=node['objects'].get(PATHS[kind])
        if row is None:return None
        blob,size=row;key=(blob,size)
        if key not in self.blobs:
            if self.bytes+size>MAX_PRIVATE_BYTES:self._reject('blob','size')
            raw=self._read('blob',self.remote.blob,blob,size)
            if not isinstance(raw,bytes) or len(raw)!=size or oid(raw)!=blob:self._reject('blob','hash')
            self.blobs[key]=raw;self.bytes+=len(raw)
        return self.blobs[key]
    def receipt_origins(self,index,blob,receipt,*,hints_only=False):
        self._diagnostic={'stage':'ancestry','reason':'unverified'}
        parent=checked(receipt.get('parent_commit'),40);seen=set()
        # A private current-state reference is ONLY a search hint. Its target
        # still requires immutable object verification and the externally
        # authenticated complete-record digest. It grants no authority itself.
        head=self.node(0);state_raw=self.object(head,'journal')
        if state_raw is not None:
            state=strict(state_raw,MAX_OBJECT)
            for field in ('last_enqueued_publication','observed_publication_tip','origin'):
                hint=state.get(field)
                if not isinstance(hint,dict) or hint.get('sequence')!=receipt.get('sequence'):continue
                candidate=hint.get('commit')
                if not isinstance(candidate,str) or not re.fullmatch('[0-9a-f]{40}',candidate) or candidate in seen:continue
                seen.add(candidate)
                node=self.read_node(candidate);row=node['objects'].get(PATHS['publication'])
                if (node['parent']==parent and row is not None and row[0]==blob
                        and self.object(node,'publication') is not None):yield candidate
        if hints_only:return
        for i in range(index,self.limit):
            node=self.node(i)
            if node is None:break
            row=node['objects'].get(PATHS['publication'])
            if row is None or row[0]!=blob:break
            if node['parent']==parent:
                if node['commit'] not in seen:yield node['commit']
                return
        fail()
    def resolve(self,p,*,policy,run,notion_policy_sha256=None):
        try:
            self._diagnostic={'stage':'binding','reason':'unverified'}
            checked_pointer(p);self.guard()
            context={'run_id':run['id'],'run_attempt':run['run_attempt'],'code_sha':policy['code_sha'],
                     'controller_sha':run['head_sha'],'rules_sha256':policy['rules_sha256']}
            deferred=[]
            def publication_match(node,raw,receipt,origins):
                self._diagnostic={'stage':'binding','reason':'unverified'}
                for origin in origins:
                    if receipt.get('policy_sha256')!=policy['get_policy_sha256']:
                      # Reuse the original exact predecessor bridge only after
                      # authenticating the immutable receipt origin and bytes.
                      # This authorizes receipt identity under the executing verifier,
                      # not the historical wrapper policy. Preserve that policy
                      # below and require its exact external full-record digest.
                      import get_recurring_policy_recovery as recovery
                      if not recovery.accepts_receipt(origin,sha(raw),receipt,recovery.current_get_policy()):continue
                    for updated in (False,True):
                      if updated and origin!=node['commit']:continue
                      record={**context,'contract':'get-canonical-publication-anchor-1','status':'canonical_publication_verified',
                          'policy_sha256':policy['get_policy_sha256'],'observed_main_commit':node['commit'],
                          'receipt_commit':origin,'receipt_sha256':sha(raw),'receipt_sequence':receipt['sequence'],
                          'main_updated':updated,'cas_attempts':int(updated),'readback_verified':True}
                      record['record_sha256']=sha(public_encoded(record))
                      if pointer('publication',record)==p:self.guard();return record
                return None
            for i in range(self.limit):
                node=self.node(i)
                if node is None:break
                row=node['objects'].get(PATHS[p['kind']])
                if row is None:continue
                blob,size=row
                if p['kind']=='journal':
                    checked(notion_policy_sha256,64)
                    # Historical state bodies are not downloaded for every
                    # checkpoint. The opaque state digest is bound by the public
                    # full-record proof, never trusted independently. The final
                    # anchor MUST still pass the original actual blob/state readback.
                    for cas in (0,1):
                        if cas and node['parent'] is None:continue
                        record={**context,'contract':'get-notion-journal-checkpoint-1','status':'journal_readback_verified',
                            'notion_policy_sha256':notion_policy_sha256,'public_policy_sha256':sha(public_encoded(policy)),
                            'sequence':p['sequence'],'previous_record_sha256':p['previous'],
                            'parent_commit':node['parent'] if cas else None,'commit':node['commit'],'tree':node['tree'],
                            'state_blob':blob,'state_sha256':p['object_digest'],'state_bytes':size,'cas_attempts':cas,'readback_verified':True}
                        record['record_sha256']=sha(record)
                        if pointer('journal',record)==p:self.guard();return record
                else:
                    raw=self.object(node,'publication');self._diagnostic={'stage':'receipt','reason':'unverified'}
                    receipt=strict(raw,MAX_OBJECT,serializer=public_encoded)
                    if receipt.get('sequence')!=p['sequence']:continue
                    hints=list(self.receipt_origins(i,blob,receipt,hints_only=True))
                    if hints:
                        # A validated locator is not authority. Try all actual
                        # observed nodes against its full external digest first;
                        # retain the original ancestor search as a fallback.
                        deferred.append((node,i,blob,raw,receipt))
                        origins=hints
                    else:origins=self.receipt_origins(i,blob,receipt)
                    matched=publication_match(node,raw,receipt,origins)
                    if matched is not None:return matched
                    if deferred and node['parent']==receipt.get('parent_commit'):
                        # The original fallback stops at this exact origin.
                        # Resolve deferred older-observation candidates before
                        # reading any older/different receipt or unrelated node.
                        for old,index,old_blob,old_raw,old_receipt in deferred:
                            matched=publication_match(old,old_raw,old_receipt,self.receipt_origins(index,old_blob,old_receipt))
                            if matched is not None:return matched
                        deferred=[]
            for node,index,blob,raw,receipt in deferred:
                matched=publication_match(node,raw,receipt,self.receipt_origins(index,blob,receipt))
                if matched is not None:return matched
            fail()
        except PrivateProofError:
            self._report_failure();raise
        except Exception:
            self._report_failure();fail()

def source_for(client):
    for _ in range(5):
        value=getattr(client,'private_proof_source',None)
        if isinstance(value,PrivateRecords):return value
        client=getattr(client,'client',None)
        if client is None:break
    fail()

def expand_for(client,raw,*,policy,run,notion_policy_sha256=None,kinds=('publication','journal')):
    if MARKER.encode() not in raw:return raw
    source=source_for(client)
    return expand(raw,lambda p:source.resolve(p,policy=policy,run=run,notion_policy_sha256=notion_policy_sha256),kinds)
