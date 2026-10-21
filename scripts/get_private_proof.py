"""Opaque public proofs backed by existing private objects; no writes or tokens.

A public pointer is NOT trusted by itself. Call expansion only within the
existing authenticated run/job/controller verifier, which subsequently validates
the reconstructed original records. No current-state self-hash grants authority.
"""
from copy import deepcopy
import hashlib
import json
import re
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
    def guard(self):
        m=self.remote.metadata()
        if m.get('full_name')!=REPOSITORY or m.get('private') is not True:fail()
        head=checked(self.remote.ref(REF),40)
        if self.head is None:self.head=head
        elif head!=self.head:fail()
        return head
    def node(self,index):
        if index<len(self.nodes):return self.nodes[index]
        if index!=len(self.nodes) or index>=self.limit:fail()
        commit=self.head if index==0 else self.nodes[-1]['parent']
        if commit is None:return None
        if any(v['commit']==commit for v in self.nodes):fail()
        node=self.read_node(commit);self.nodes.append(node)
        return node
    def read_node(self,commit):
        checked(commit,40)
        metadata=self.remote.commit(commit);tree=checked(metadata.get('tree'),40);parents=metadata.get('parents')
        if not isinstance(parents,list) or len(parents)>1:fail()
        parent=checked(parents[0],40) if parents else None
        entries=self.remote.tree_entries(tree,True)
        if not isinstance(entries,list) or len(entries)>100000:fail()
        selected={}
        for row in entries:
            if not isinstance(row,dict):fail()
            path=row.get('path')
            if path not in PATHS.values():continue
            if (path in selected or row.get('type')!='blob' or row.get('mode')!='100644'
                    or type(row.get('size')) is not int or not 0<row['size']<=MAX_OBJECT):fail()
            selected[path]=(checked(row.get('sha'),40),row['size'])
        return {'commit':commit,'tree':tree,'parent':parent,'objects':selected}
    def object(self,node,kind):
        row=node['objects'].get(PATHS[kind])
        if row is None:return None
        blob,size=row;key=(blob,size)
        if key not in self.blobs:
            if self.bytes+size>MAX_PRIVATE_BYTES:fail()
            raw=self.remote.blob(blob,size)
            if not isinstance(raw,bytes) or len(raw)!=size or oid(raw)!=blob:fail()
            self.blobs[key]=raw;self.bytes+=len(raw)
        return self.blobs[key]
    def receipt_origins(self,index,blob,receipt):
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
            checked_pointer(p);self.guard()
            context={'run_id':run['id'],'run_attempt':run['run_attempt'],'code_sha':policy['code_sha'],
                     'controller_sha':run['head_sha'],'rules_sha256':policy['rules_sha256']}
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
                    raw=self.object(node,'publication');receipt=strict(raw,MAX_OBJECT,serializer=public_encoded)
                    if receipt.get('sequence')!=p['sequence'] or receipt.get('policy_sha256')!=policy['get_policy_sha256']:continue
                    for origin in self.receipt_origins(i,blob,receipt):
                      for updated in (False,True):
                        if updated and origin!=node['commit']:continue
                        record={**context,'contract':'get-canonical-publication-anchor-1','status':'canonical_publication_verified',
                            'policy_sha256':policy['get_policy_sha256'],'observed_main_commit':node['commit'],
                            'receipt_commit':origin,'receipt_sha256':sha(raw),'receipt_sequence':receipt['sequence'],
                            'main_updated':updated,'cas_attempts':int(updated),'readback_verified':True}
                        record['record_sha256']=sha(public_encoded(record))
                        if pointer('publication',record)==p:self.guard();return record
            fail()
        except PrivateProofError:raise
        except Exception:fail()

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
