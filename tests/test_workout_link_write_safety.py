import contextlib
import copy
import importlib.util
import io
from pathlib import Path
import unittest
from unittest.mock import patch
import urllib.error

spec=importlib.util.spec_from_file_location('link',Path(__file__).parents[1]/'scripts/link_notion_workout_to_diary.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
A='11111111-1111-1111-1111-111111111111';B='22222222-2222-2222-2222-222222222222';C='33333333-3333-3333-3333-333333333333'

class WriteSafetyTests(unittest.TestCase):
 def run_case(self,fresh, dry=False):
  workout={'object':'database','id':A,'properties':{'date':{'type':'date'},'title':{'type':'title'},'link':{'type':'relation','relation':{'database_id':B}}}}
  diary={'object':'database','id':B,'properties':{'date':{'type':'date'}}}
  initial={'id':A,'properties':{'date':{'type':'date','date':{'start':'2026-01-01'}},'link':{'type':'relation','relation':[]}}}
  with patch.dict(m.os.environ,{'NOTION_API_KEY':'token','NOTION_WORKOUT_DATABASE_ID':A,'NOTION_DIARY_DATABASE_ID':B,'NOTION_DRY_RUN':str(dry)},clear=True),patch.object(m.sys,'argv',['link']),patch.object(m,'resolve_database',side_effect=[workout,diary]),patch.object(m,'paginate_database',side_effect=[[initial],[{'id':B}]]),patch.object(m,'notion_request',return_value=fresh),patch.object(m,'update_page') as update,contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
   status=m.cli()
   return status,update.call_args_list
 def fresh(self,ids=()):
  return {'id':A,'properties':{'date':{'type':'date','date':{'start':'2026-01-01'}},'link':{'type':'relation','relation':[{'id':x} for x in ids]}}}
 def test_fresh_links_preserved(self):
  status,calls=self.run_case(self.fresh([C]));self.assertEqual(status,0);self.assertEqual(calls[0].args[2],{'link':{'relation':[{'id':C},{'id':B}]}})
 def test_already_present_no_write(self):
  status,calls=self.run_case(self.fresh([C,B]));self.assertEqual((status,len(calls)),(0,0))
 def test_changed_date_blocks_write(self):
  p=self.fresh();p['properties']['date']['date']['start']='2026-01-02';status,calls=self.run_case(p);self.assertEqual((status,len(calls)),(1,0))
 def test_truncated_relation_blocks_write(self):
  p=self.fresh([C]);p['properties']['link']['has_more']=True;status,calls=self.run_case(p);self.assertEqual((status,len(calls)),(1,0))
 def test_dry_run_never_writes(self):
  status,calls=self.run_case(self.fresh([C]),True);self.assertEqual((status,len(calls)),(0,0))
 def test_patch_never_blind_retries(self):
  for error in [urllib.error.URLError('private'),TimeoutError(),urllib.error.HTTPError('private',500,'private',{},io.BytesIO(b'private')),urllib.error.HTTPError('private',429,'private',{},io.BytesIO(b'private'))]:
   with patch.object(m.urllib.request,'urlopen',side_effect=error) as api,patch.object(m.time,'sleep'):
    with self.assertRaises(RuntimeError):m.notion_request('token','PATCH','/pages/'+A,{'properties':{}})
    self.assertEqual(api.call_count,1)
 def test_missing_cursor_blocks(self):
  with patch.object(m,'query_database',return_value={'results':[],'has_more':True}):
   with self.assertRaises(RuntimeError):list(m.paginate_database('token',A))
 def test_repeated_cursor_blocks(self):
  with patch.object(m,'query_database',return_value={'results':[],'has_more':True,'next_cursor':'same'}):
   with self.assertRaises(RuntimeError):list(m.paginate_database('token',A))

if __name__=='__main__':unittest.main()
