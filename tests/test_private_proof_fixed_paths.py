"""Synthetic Git objects and original read-only transport; no private capsule."""
from copy import deepcopy
import base64
import contextlib
import hashlib
import io
import json
from pathlib import Path
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import get_private_proof as p
import get_private_preview_retention as retention


def build_tree(flat):
    """Build genuine nonrecursive Git tree objects from synthetic flat rows."""
    root={};trees={}
    for row in flat:
        parts=row['path'].split('/');node=root
        for name in parts[:-1]:node=node.setdefault(name,{})
        if parts[-1] in node:raise ValueError('synthetic_duplicate')
        node[parts[-1]]=dict(row,path=parts[-1])
    def make(node):
        rows=[]
        for name,value in node.items():
            if 'mode' in value:rows.append(value)
            else:rows.append({'path':name,'mode':'040000','type':'tree','sha':make(value)})
        rows.sort(key=lambda row:(row['path']+('/' if row['type']=='tree' else '')).encode())
        raw=b''.join((row['mode'].lstrip('0')+' '+row['path']).encode()+b'\0'+bytes.fromhex(row['sha']) for row in rows)
        sha=hashlib.sha1(b'tree '+str(len(raw)).encode()+b'\0'+raw).hexdigest();trees[sha]=rows;return sha
    return make(root),trees

POLICY={'code_sha':'1'*40,'rules_sha256':'2'*64,'get_policy_sha256':'3'*64}
RUN={'id':11,'run_attempt':1,'head_sha':'4'*40}
HEAD='a'*40;PARENT='b'*40
STATE=p.encoded({'origin':{'commit':HEAD,'sequence':1}})
RECEIPT=p.public_encoded({'parent_commit':PARENT,'sequence':1,'policy_sha256':'3'*64})

def row(kind,raw):return {'path':p.PATHS[kind],'type':'blob','mode':'100644','sha':p.oid(raw),'size':len(raw)}

class Response:
    def __init__(self,raw):self.status=200;self.headers={};self.raw=raw
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def read(self,maximum):return self.raw[:maximum]

class Opener:
    def __init__(self):
        self.root,self.trees=build_tree([row('journal',STATE),row('publication',RECEIPT)])
        empty,trees=build_tree([]);self.trees.update(trees)
        self.commits={HEAD:{'sha':HEAD,'tree':{'sha':self.root},'parents':[{'sha':PARENT}]},PARENT:{'sha':PARENT,'tree':{'sha':empty},'parents':[]}}
        self.blobs={p.oid(STATE):STATE,p.oid(RECEIPT):RECEIPT};self.calls=[];self.fault=None;self.head=HEAD
    def open(self,request,timeout):
        assert request.get_method()=='GET' and 0 < timeout <= 60  # shared hard deadline, never reset
        prefix='https://api.github.com/repos/'+p.REPOSITORY;assert request.full_url.startswith(prefix)
        path=request.full_url[len(prefix):];self.calls.append(path)
        if path=='':value={'full_name':p.REPOSITORY,'private':True}
        elif path=='/git/ref/heads/main':value={'ref':p.REF,'object':{'type':'commit','sha':self.head}}
        elif path.startswith('/git/commits/'):value=deepcopy(self.commits[path.rsplit('/',1)[1]])
        elif path.startswith('/git/trees/'):
            assert '?' not in path  # Whole-root recursion is forbidden.
            tree=path.rsplit('/',1)[1]
            if self.fault=='transport':raise TimeoutError('synthetic secret URL/token/private-path')
            if self.fault=='size':return Response(b'x'*(16*1024*1024+1))
            value={'sha':tree,'truncated':False,'tree':deepcopy(self.trees[tree])}
            if self.fault=='truncated':value['truncated']=True
            if self.fault=='duplicate':value['tree']*=2
            if self.fault=='wrong_sha':value['tree'][0]['sha']='f'*40
            if self.fault=='wrong_type':value['tree'][0]['type']='blob'
            if self.fault=='slash':value['tree'][0]['path']='wrong/path'
            if self.fault=='unknown_mode':value['tree'][0]['mode']='000000'
        elif path.startswith('/git/blobs/'):
            blob=path.rsplit('/',1)[1];raw=self.blobs[blob]
            value={'sha':blob,'encoding':'base64','size':len(raw),'content':base64.b64encode(raw).decode()}
        else:raise AssertionError(path)
        return Response(p.encoded(value))

class Remote(retention.GitHubRemote):
    def __init__(self,opener):self.local=None;self.token='synthetic-offline';self.opener=opener


def publication():
    value={'run_id':11,'run_attempt':1,'code_sha':'1'*40,'controller_sha':'4'*40,'rules_sha256':'2'*64,
        'contract':'get-canonical-publication-anchor-1','status':'canonical_publication_verified',
        'policy_sha256':'3'*64,'observed_main_commit':HEAD,'receipt_commit':HEAD,
        'receipt_sha256':p.sha(RECEIPT),'receipt_sequence':1,'main_updated':True,'cas_attempts':1,'readback_verified':True}
    value['record_sha256']=p.sha(p.public_encoded(value));return value

class FixedPathTests(unittest.TestCase):
    def setUp(self):self.opener=Opener();self.source=p.PrivateRecords(Remote(self.opener))
    def resolve(self):return self.source.resolve(p.pointer('publication',publication()),policy=POLICY,run=RUN)
    def rejection(self,reason=None):
        stream=io.StringIO()
        with contextlib.redirect_stderr(stream),self.assertRaisesRegex(p.PrivateProofError,'^private_proof_unverified$'):self.resolve()
        event=json.loads(stream.getvalue());self.assertEqual(set(event),{'contract','stage','reason'})
        self.assertEqual(event['contract'],'get-private-proof-diagnostic-1')
        if reason:self.assertEqual(event['reason'],reason)
        for secret in ('token','private-path','https:','synthetic secret',HEAD,p.PATHS['publication']):self.assertNotIn(secret,stream.getvalue())
        return event
    def test_exact_original_resolver_and_original_http(self):
        self.assertEqual(self.resolve(),publication())
        self.assertEqual(len(self.source._tree_cache),6)
        self.assertEqual(self.source._tree_limit,6*(p.MAX_COMMITS+3))
        self.assertFalse(any('recursive=' in path for path in self.opener.calls))
    def test_immutable_tree_and_blob_cache_but_fresh_guard(self):
        self.resolve();start=len(self.opener.calls);self.resolve();calls=self.opener.calls[start:]
        self.assertEqual(calls,['','/git/ref/heads/main','','/git/ref/heads/main'])
    def test_cached_projection_retains_no_urls_or_unrelated_metadata(self):
        self.resolve()
        for rows in self.source._tree_cache.values():
            for entry in rows.values():self.assertLessEqual(set(entry),{'path','mode','type','sha','size'})
    def test_changed_head_still_fails(self):
        self.resolve();self.opener.head=PARENT;self.rejection()
    def test_truncation_fails_shape(self):self.opener.fault='truncated';self.rejection('shape')
    def test_timeout_is_safe_transport_diagnostic(self):self.opener.fault='transport';self.rejection('transport')
    def test_original_response_limit_fails_size(self):self.opener.fault='size';self.rejection('size')
    def test_duplicate_path_fails(self):self.opener.fault='duplicate';self.rejection('shape')
    def test_changed_tree_oid_fails(self):self.opener.fault='wrong_sha';self.rejection('hash')
    def test_type_mode_mismatch_fails(self):self.opener.fault='wrong_type';self.rejection('shape')
    def test_recursive_or_foreign_path_fails(self):self.opener.fault='slash';self.rejection('shape')
    def test_unknown_mode_fails(self):self.opener.fault='unknown_mode';self.rejection('shape')
    def test_tree_object_bound_before_new_read(self):
        self.source._tree_limit=0;self.rejection('size')
        self.assertFalse(any('/git/trees/' in path for path in self.opener.calls))
    def test_missing_publication_is_not_a_proof(self):
        root,trees=build_tree([row('journal',STATE)]);self.opener.trees.update(trees);self.opener.commits[HEAD]['tree']['sha']=root;self.rejection()
    def test_wrong_leaf_mode_rejected_even_valid_tree_oid(self):
        bad=row('publication',RECEIPT);bad['mode']='120000';root,trees=build_tree([row('journal',STATE),bad]);self.opener.trees.update(trees);self.opener.commits[HEAD]['tree']['sha']=root;self.rejection('shape')
    def test_wrong_blob_bytes_rejected(self):
        self.opener.blobs[p.oid(RECEIPT)]=RECEIPT+b' ';self.rejection('hash')
    def test_missing_journal_preserves_original_publication_semantics(self):
        root,trees=build_tree([row('publication',RECEIPT)]);self.opener.trees.update(trees);self.opener.commits[HEAD]['tree']['sha']=root
        self.assertEqual(self.resolve(),publication())
    def test_original_caps_unchanged(self):
        self.assertEqual((p.MAX_COMMITS,p.MAX_OBJECT,p.MAX_PRIVATE_BYTES),(1024,32*1024*1024,256*1024*1024))

if __name__=='__main__':unittest.main()
