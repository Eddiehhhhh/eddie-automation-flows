from copy import deepcopy
import unittest
import get_notion_executor as e
import get_notion_live_transport as t
import get_notion_native as n
import test_notion_executor as f

DB="e"*32
class OfficialFixture:
 def __init__(self,api):self.api=api;self.calls=[]
 def page(self,s):return {"object":"page","id":s["page_id"],"parent":{"type":"data_source_id","data_source_id":f.DS},"archived":False,"properties":deepcopy(s["properties_raw"]),"last_edited_time":s["last_edited_time"]}
 def listing(self,rows):return {"object":"list","results":rows,"has_more":False,"next_cursor":None}
 def semantic(self,rows):
  out=[]
  for row in rows:
   kind=row["type"];p=deepcopy(row[kind]);children=p.pop("children",[])
   out.append({"type":kind,"payload":p,"children":self.semantic(children)})
  return out
 def __call__(self,method,path,body=None):
  self.calls.append((method,path))
  if path=="/data_sources/"+f.DS:return {"object":"data_source","id":f.DS,"parent":{"database_id":DB},"properties":{"笔记 ID":{"type":"rich_text"}}}
  if path.endswith("/query"):
   ident=body["filter"]["rich_text"]["equals"];return self.listing([self.page(self.api.pages[ident])] if ident in self.api.pages else [])
  if method=="GET" and path.startswith("/pages/"):return self.page(next(s for s in self.api.pages.values() if s["page_id"]==path.rsplit("/",1)[1]))
  if method=="GET" and path.startswith("/blocks/"):
   parent=path.split("/")[2];rows=[]
   for s in self.api.pages.values():
    for row in s["recursive_blocks"]:
     if row["parent_id"]==parent:rows.append({"object":"block","id":row["id"],"parent":{"type":"page_id" if parent==s["page_id"] else "block_id",("page_id" if parent==s["page_id"] else "block_id"):parent},"archived":False,"type":row["type"],row["type"]:deepcopy(row["payload"]),"has_children":row["has_children"]})
   return self.listing(rows)
  if method=="PATCH" and path.endswith("/children"):
   ids=self.api.append_blocks(path.split("/")[2],self.semantic(body["children"]),body.get("after"));return self.listing([{"object":"block","id":i} for i in ids])
  if method=="PATCH":
   ident=path.rsplit("/",1)[1];self.api.archive_block(ident);return {"object":"block","id":ident,"archived":True}
  if method=="POST" and path=="/pages":
   ident=n.note_id({"properties":body["properties"]});self.api.create_page(f.DS,ident,body["properties"],self.semantic(body["children"]));return self.page(self.api.pages[ident])
  raise AssertionError("Unknown fixed fixture route")

class Combined(unittest.TestCase):
 def test_actual_collector_wire_executor_long_update_and_repeat(self):
  case=f.Tests(methodName="test_real_git_update_journal_before_writes_preserves_unowned_and_properties");case.setUp();self.addCleanup(case.doCleanups)
  case.publish("x"*4200);transport=OfficialFixture(case.api);adapter=t.API(transport,f.DS,[f.N],data_source_pin=n.sha(f.DS),database_pin=n.sha(DB))
  state,sha=case.store.read();snap=adapter.read_note(f.N)
  plan=e.prepare(case.publication,state,sha,{f.N:snap},case.policy)
  self.assertEqual(plan["records"][0]["action"],"update");row=plan["records"][0]
  req=adapter.preflight_append(row["page_id"],row["request_blocks"],row["after_anchor"])
  keys=[t.fingerprint(req["method"],req["path"],req["body"])]+[t.fingerprint("PATCH","/blocks/"+i,{"archived":True}) for i in row["old_roots"]]
  adapter.approved=frozenset(keys)
  result=e.execute(plan,e.digest(plan),case.publication,adapter,case.store,expected_store_sha=sha,writers_guard=lambda:case.policy["old_writer_evidence"])
  self.assertEqual(result["status"],"verified",result);self.assertEqual(result["updated"],1)
  current,sha=case.store.read();repeat=e.prepare(case.publication,current,sha,{},case.policy);self.assertEqual(repeat["records"],[])
  self.assertEqual(len([x for x in transport.calls if x[0]=="PATCH"]),2)
if __name__=="__main__":unittest.main()
