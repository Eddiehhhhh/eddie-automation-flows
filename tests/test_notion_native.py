import copy
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import get_notion_native as n

P='1'*32; D='2'*32; DB='3'*32; B='4'*32; C='5'*32; N='1234567890123456789'
def listing(rows, more=False, cursor=None):return {'object':'list','results':rows,'has_more':more,'next_cursor':cursor}
def block(i,parent,child=False):return {'object':'block','id':i,'parent':{'type':'page_id' if parent==P else 'block_id', 'page_id' if parent==P else 'block_id':parent},'type':'paragraph','paragraph':{'rich_text':[{'type':'text','text':{'content':'synthetic'}}]},'has_children':child,'archived':False}
def page():return {'object':'page','id':P,'parent':{'type':'data_source_id','data_source_id':D},'archived':False,'last_edited_time':'2026-01-01T00:00:00Z','properties':{'笔记 ID':{'type':'rich_text','rich_text':[{'type':'text','text':{'content':N}}]}}}
class Fake:
 def __init__(self):self.calls=[];self.mutate=None;self.reads=0
 def __call__(self,m,p,b):
  self.calls.append((m,p,b))
  if p=='/search':x=listing([{'object':'page','id':P}])
  elif p=='/pages/'+P:self.reads+=1;x=page()
  elif p=='/data_sources/'+D:x={'object':'data_source','id':D,'parent':{'database_id':DB},'properties':{'笔记 ID':{'type':'rich_text'}}}
  elif p.endswith('/query'):x=listing([page()])
  elif p.startswith('/blocks/'+P):x=listing([block(B,P,True)])
  elif p.startswith('/blocks/'+B):x=listing([block(C,B)])
  else:raise AssertionError('unknown fixture route')
  return self.mutate(m,p,b,x) if self.mutate else copy.deepcopy(x)
class Test(unittest.TestCase):
 def run_collect(self,f):return n.collect(f,page_pins={n.sha(P):n.sha(N)},data_source_pin=n.sha(D),database_pin=n.sha(DB))
 def test_complete_recursive(self):
  f=Fake();r=self.run_collect(f);self.assertEqual(r['report']['blocks'],2);s=r['snapshots'][N];self.assertTrue(s['complete']);self.assertFalse(s['ownership_adopted']);self.assertEqual(s['recursive_blocks'][0]['children_ids'],[C]);self.assertFalse(any(m not in ('GET','POST') for m,_,_ in f.calls));self.assertNotIn(N,str(r['report']))
 def test_paginated_search_and_children(self):
  f=Fake()
  def mutate(m,p,b,x):
   if p=='/search' and not b.get('start_cursor'):return listing([],True,'next')
   if p.startswith('/blocks/'+P) and 'start_cursor' not in p:return listing([],True,'next')
   return x
  f.mutate=mutate;self.assertEqual(self.run_collect(f)['report']['pages'],1)
 def blocked(self,mutate,code):
  f=Fake();f.mutate=mutate
  with self.assertRaisesRegex(n.Blocked,'^'+code+'$'):self.run_collect(f)
 def test_duplicate(self):self.blocked(lambda m,p,b,x: listing([page(),page()]) if p.endswith('/query') else x,'duplicate_or_missing_id')
 def test_wrong_parent(self):
  def mutate(m,p,b,x):
   if p=='/pages/'+P:x['parent']['data_source_id']='a'*32
   return x
  self.blocked(mutate,'wrong_parent')
 def test_changed_page(self):
  f=Fake()
  def mutate(m,p,b,x):
   if p=='/pages/'+P and f.reads>1:x['last_edited_time']='2026-01-02T00:00:00Z'
   return x
  f.mutate=mutate
  with self.assertRaisesRegex(n.Blocked,'page_changed_during_read'):self.run_collect(f)
 def test_bad_cursor(self):self.blocked(lambda m,p,b,x:listing([],True,'repeat') if p=='/search' else x,'incomplete_pagination')
 def test_empty_children(self):self.blocked(lambda m,p,b,x:listing([]) if p.startswith('/blocks/'+B) else x,'incomplete_children')
 def test_wrong_block_parent(self):
  def mutate(m,p,b,x):
   if p.startswith('/blocks/'+B):x['results'][0]['parent']['block_id']='a'*32
   return x
  self.blocked(mutate,'wrong_block_parent')
 def test_permission_redaction(self):
  def request(*args):raise type('Forbidden',(Exception,),{'status':403})('PRIVATE TOKEN BODY')
  with self.assertRaises(n.Blocked) as e:self.run_collect(request)
  self.assertEqual(str(e.exception),'permission_denied')
 def test_unsupported(self):
  def mutate(m,p,b,x):
   if p.startswith('/blocks/'+B):x['results'][0]['type']='unsupported'
   return x
  self.blocked(mutate,'unsupported_block')
 def test_note_identity_mismatch(self):
  def mutate(m,p,b,x):
   if p=='/pages/'+P:x['properties']['笔记 ID']['rich_text'][0]['text']['content']='9'*19
   return x
  self.blocked(mutate,'wrong_note_identity')
 def test_malformed_property_containers(self):
  cases = [(None, 'id_property_invalid'), ({'笔记 ID':None}, 'id_property_invalid'), ({'笔记 ID':{'type':'rich_text','rich_text':None}}, 'id_property_invalid'), ({'笔记 ID':{'type':'rich_text','rich_text':[{'type':'text','text':None}]}}, 'id_property_invalid')]
  for props,code in cases:
   with self.subTest(properties=props):
    def mutate(m,p,b,x):
     if p=='/pages/'+P:x['properties']=props
     return x
    self.blocked(mutate,code)
 def test_malformed_unowned_property(self):
  def mutate(m,p,b,x):
   if p=='/pages/'+P:x['properties']['other']={'type':'rich_text','rich_text':None}
   return x
  self.blocked(mutate,'incomplete_properties')
 def test_malformed_schema(self):
  def mutate(m,p,b,x):
   if p=='/data_sources/'+D:x['properties']=None
   return x
  self.blocked(mutate,'wrong_schema')
 def test_malformed_parent(self):
  def mutate(m,p,b,x):
   if p=='/pages/'+P:x['parent']=None
   return x
  self.blocked(mutate,'wrong_parent')
 def test_top_level_request_id_is_not_edit(self):
  f=Fake()
  def mutate(m,p,b,x):
   if p=='/pages/'+P:x['request_id']='request-'+str(f.reads)
   return x
  f.mutate=mutate
  self.assertEqual(self.run_collect(f)['report']['status'],'native_read_verified')
  self.assertEqual(n.page_difference_categories({}, {'request_id':'anything'}), ())
 def test_real_edit_categories_are_value_free(self):
  for field,value,category in [('properties',{},'properties'),('last_edited_time','different','edit_metadata'),('cover',{'url':'PRIVATE'},'presentation'),('PRIVATE_FIELD','PRIVATE_VALUE','other_page_field')]:
   with self.subTest(field=field):
    f=Fake()
    def mutate(m,p,b,x):
     if p=='/pages/'+P:
      x['request_id']=str(f.reads)
      if f.reads>1:x[field]=value
     return x
    f.mutate=mutate
    with self.assertRaises(n.Blocked) as e:self.run_collect(f)
    self.assertEqual(str(e.exception),'page_changed_during_read')
    self.assertEqual(e.exception.difference_categories,(category,))
    self.assertNotIn('PRIVATE',str(e.exception.difference_categories))
 def test_nested_request_id_is_still_compared(self):
  self.assertEqual(n.page_difference_categories({'properties':{'request_id':'a'}},{'properties':{'request_id':'b'}}),('properties',))
 def test_missing_other_field_still_compared(self):
  self.assertEqual(n.page_difference_categories({'cover':None},{}),('presentation',))
 def test_json_type_changes_are_edits(self):
  self.assertEqual(n.page_difference_categories({'properties':{'value':1}}, {'properties':{'value':True}}), ('properties',))
  self.assertEqual(n.page_difference_categories({'extra':1}, {'extra':True}), ('other_page_field',))
  self.assertEqual(n.page_difference_categories({'extra':1}, {'extra':1.0}), ('other_page_field',))
  self.assertEqual(n.page_difference_categories({'request_id':1}, {'request_id':True}), ())
if __name__=='__main__':unittest.main()
