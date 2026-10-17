import io,json,sys,unittest,urllib.error
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import get_notion_writer_guard as g
class Response(io.BytesIO):
 def __init__(self,value,status=200,headers=None):super().__init__(value if isinstance(value,bytes) else json.dumps(value).encode());self.status=status;self.headers=headers or {}
class Opener:
 def __init__(self):self.calls=[];self.change=None
 def open(self,req,timeout):
  self.calls.append(req);url=req.full_url
  wid=int(url.split('/workflows/')[1].split('/')[0]);value={'id':wid,'path':'.github/workflows/sync.yml','state':'disabled_manually'} if '/runs?' not in url else {'total_count':0,'workflow_runs':[]}
  return self.change(req,value) if self.change else Response(value)
class Tests(unittest.TestCase):
 def make(self):
  op=Opener();return g.WriterGuard('synthetic-token',opener=op),op
 def test_fresh_twelve_stable_shape(self):
  guard,op=self.make();a=guard();b=guard();self.assertEqual(a,b);self.assertEqual(len(op.calls),24)
  self.assertEqual(set(a),{'fresh_readback_verified','writers'});self.assertEqual(set(a['writers']),{x[0] for x in g.WRITERS})
  for req in op.calls:self.assertEqual(req.get_method(),'GET');self.assertTrue(req.full_url.startswith(g.HOST+'/repos/Eddiehhhhh/'));self.assertEqual(req.get_header('Authorization'),'Bearer synthetic-token')
  self.assertEqual(guard.diagnostics['requests'],12);self.assertTrue(guard.diagnostics['complete'])
 def test_missing_token_never_requests(self):
  for token in (None,'','x\ny','x y',2):
   with self.assertRaisesRegex(g.Blocked,'missing_credentials'):g.WriterGuard(token)
 def test_permission_quota_no_retry_or_fallback(self):
  for code in (401,403,429,500,302):
   guard,op=self.make()
   def change(req,value):raise urllib.error.HTTPError(req.full_url,code,'PRIVATE',{},None)
   op.change=change
   with self.assertRaises(g.Blocked) as ctx:guard()
   self.assertNotIn('PRIVATE',str(ctx.exception));self.assertEqual(len(op.calls),1);self.assertFalse(guard.diagnostics['complete'])
 def test_response_codes_and_exhausted_quota(self):
  for code,headers in ((403,{}),(429,{}),(200,{'X-RateLimit-Remaining':'0'})):
   guard,op=self.make();op.change=lambda req,v:Response(v,code,headers)
   with self.assertRaises(g.Blocked):guard()
   self.assertEqual(len(op.calls),1)
 def test_target_identity_state_and_active_runs(self):
  for field,value in (('id',True),('id',0),('path','wrong'),('state','active'),('state','deleted')):
   guard,op=self.make();op.change=lambda req,v:Response({**v,field:value})
   with self.assertRaisesRegex(g.Blocked,'writer_gate_unknown'):guard()
  for count,rows in ((True,[]),(1,[]),(0,[{}]),(-1,[])):
   guard,op=self.make();op.change=lambda req,v:Response({'total_count':count,'workflow_runs':rows}) if '/runs?' in req.full_url else Response(v)
   with self.assertRaisesRegex(g.Blocked,'writer_gate_unknown'):guard()
   self.assertEqual(len(op.calls),2)
 def test_json_budget_and_private_error(self):
  for payload in (b'{"id":1,"id":2}',b'null',b'x'*(g.MAX_RESPONSE+1)):
   guard,op=self.make();op.change=lambda req,v:Response(payload)
   with self.assertRaises(g.Blocked):guard()
   self.assertEqual(len(op.calls),1)
  guard,op=self.make()
  def fail(req,v):raise RuntimeError('PRIVATE token text')
  op.change=fail
  with self.assertRaisesRegex(g.Blocked,'^writer_guard_transport$'):guard()
 def test_deadline(self):
  guard,op=self.make();ticks=iter([0,0,121,121]);guard._clock=lambda:next(ticks)
  with self.assertRaisesRegex(g.Blocked,'writer_guard_budget'):guard()
  self.assertEqual(len(op.calls),1)
 def test_redirect_rejected(self):
  self.assertIsNone(g.NoRedirect().redirect_request(None,None,302,'',{},'https://evil.invalid'))
if __name__=='__main__':unittest.main()
