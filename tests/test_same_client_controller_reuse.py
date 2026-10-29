"""Synthetic immutable-controller cache boundaries; no network/private data."""
import ast,base64,hashlib,json,sys,unittest
from pathlib import Path
from types import SimpleNamespace as NS
from weakref import WeakKeyDictionary
from unittest.mock import patch

class Failure(ValueError):pass
def fail(code):raise Failure(code)
def sha(raw):return hashlib.sha256(raw).hexdigest()
def oid(raw):return hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()
def checked(v,n):
 if type(v)is not str or len(v)!=n or set(v)-set('abcdef0123456789'):fail('invalid')
 return v
class Public:
 def __init__(self,raw=b'scheduled'):self.raw=raw;self.calls=[];self.error=None
 def json(self,path):
  self.calls.append(path)
  if self.error:raise self.error
  return {'type':'file','path':path.split('?')[0][10:],'encoding':'base64','size':len(self.raw),'sha':oid(self.raw),'content':base64.b64encode(self.raw).decode()}
class Anonymous(Public):pass
class Budget:
 def __init__(self,client):self.client=client;self.calls=[]
 def json(self,path):self.calls.append(path);return self.client.json(path)
class Tests(unittest.TestCase):
 def setUp(self):
  self.p=NS(PublicGithub=Public,AnonymousGithub=Anonymous,REPOSITORY='synthetic/repository',fail=fail)
  self.j=NS(_ReadBudget=Budget);self.cache=WeakKeyDictionary()
  self.env={'base64':base64,'_VERIFIED_CONTROLLER_BYTES':self.cache,'a':NS(checked=checked,sha=sha),'r':NS(oid=oid),
   'MAX_CONTROLLER':32768,'expected_bytes':lambda policy,scheduled=True:policy['scheduled'] if scheduled else policy['manual']}
  path=Path(__file__).resolve().parents[1]/'scripts/get_recurring_controller.py'
  f=next(x for x in ast.parse(path.read_text()).body if isinstance(x,ast.FunctionDef)and x.name=='verify_remote')
  exec(compile(ast.Module(body=[f],type_ignores=[]),str(path),'exec'),self.env)
  self.fn=self.env['verify_remote'];self.policy={'workflow_path':'.github/workflows/synthetic.yml','scheduled':b'scheduled','manual':b'manual'};self.commit='a'*40
  self.patch=patch.dict(sys.modules,{'get_publication_provenance':self.p,'get_notion_journal_runtime':self.j});self.patch.start();self.addCleanup(self.patch.stop)
 def call(self,c,**kw):return self.fn(c,self.commit,self.policy,**kw)
 def test_same_client_original_wrapper_reuses_only_success(self):
  c=Public();self.call(c);b=Budget(c);self.call(b)
  self.assertEqual(len(c.calls),1);self.assertEqual(b.calls,[])
  self.assertEqual(next(iter(self.cache[c])),('synthetic/repository',self.commit,self.policy['workflow_path'],oid(b'scheduled'),sha(b'scheduled')))
 def test_different_client_does_not_reuse_and_failure_no_fallback(self):
  c=Public();self.call(c);other=Public();other.error=Failure('transport')
  with self.assertRaisesRegex(Failure,'transport'):self.call(Budget(other))
  self.assertEqual(len(other.calls),1);self.assertNotIn(other,self.cache)
 def test_subclass_and_nested_wrappers_do_not_share(self):
  class Sub(Budget):pass
  for wrap in (Sub,lambda c:Budget(Budget(c))):
   c=Public();self.call(c);self.call(wrap(c));self.assertEqual(len(c.calls),2)
 def test_subclass_transport_never_caches(self):
  class Sub(Public):pass
  c=Sub();self.call(c);self.call(c);self.assertEqual(len(c.calls),2);self.assertNotIn(c,self.cache)
 def test_changed_policy_still_rejected_on_hit(self):
  c=Public();self.call(c);self.policy['scheduled']=b'changed'
  with self.assertRaisesRegex(Failure,'provenance_controller_mismatch'):self.call(Budget(c))
  self.assertEqual(len(c.calls),1)
 def test_manual_rendering_never_authorizes_scheduled_event(self):
  c=Public(b'manual');self.call(c,event='workflow_dispatch')
  with self.assertRaisesRegex(Failure,'provenance_controller_mismatch'):self.call(Budget(c),event='schedule')
  self.assertEqual(len(c.calls),1)
 def test_wrong_event_rejected_before_any_read(self):
  c=Public()
  with self.assertRaises(Failure):self.call(c,event='push')
  self.assertEqual(c.calls,[])
 def test_other_repository_sha_or_path_never_reuses(self):
  for field in ('repository','sha','path'):
   c=Public();self.call(c)
   if field=='repository':self.p.REPOSITORY='synthetic/other'
   elif field=='sha':self.commit='b'*40
   else:self.policy['workflow_path']='.github/workflows/other.yml'
   self.call(Budget(c));self.assertEqual(len(c.calls),2)
 def test_corrupt_digest_oid_or_raw_rejects_not_fallback(self):
  for kind in ('digest','oid','raw'):
   c=Public();self.call(c);key=next(iter(self.cache[c]));saved=self.cache[c].pop(key)
   if kind=='digest':key=key[:4]+('f'*64,)
   elif kind=='oid':key=key[:3]+('f'*40,key[4])
   else:saved=b'tampered'
   self.cache[c][key]=saved
   with self.assertRaisesRegex(Failure,'provenance_controller'):self.call(Budget(c))
   self.assertEqual(len(c.calls),1)
 def test_uncached_malformed_response_never_enters_cache(self):
  c=Public();c.json=lambda p:{'type':'file','path':self.policy['workflow_path'],'encoding':'base64','size':9,'sha':'f'*40,'content':base64.b64encode(b'scheduled').decode()}
  with self.assertRaises(Failure):self.call(c)
  self.assertNotIn(c,self.cache)
 def test_dynamic_queries_are_not_served_by_controller_cache(self):
  c=Public();self.call(c);b=Budget(c)
  for path in ('/actions/runs/1','/actions/runs/1/attempts/1','/actions/runs/1/jobs','/git/ref/heads/main'):
   b.json(path)
  self.assertEqual(len(c.calls),5)

if __name__=='__main__':unittest.main()
