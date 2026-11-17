import copy
import json
import sys
import types
import unittest
from unittest.mock import patch
try:
    import render
except ImportError:
    sys.modules['render'] = types.SimpleNamespace(render_text=lambda s: s['note']['content'])
from notion_sink import NotionSink, Hold, rich, OWNER

class Journal:
    def __init__(self): self.data={}
    def get(self,k): return copy.deepcopy(self.data.get(k))
    def set(self,k,v): self.data[k]=copy.deepcopy(v)

class Fake:
    def __init__(self):
        self.pages={}; self.blocks={}; self.calls=[]; self.counter=0; self.timeout=None
        self.database={'object':'database','id':'db','properties':{'Get ID':{'type':'rich_text'},'Sync Owner':{'type':'rich_text'},'Name':{'type':'title'}}}
    def request(self,m,p,b=None):
        self.calls.append((m,p,copy.deepcopy(b)))
        if m=='GET' and p=='/v1/databases/db': return copy.deepcopy(self.database)
        if p.endswith('/query'): return {'results':list(copy.deepcopy(self.pages).values()),'has_more':False}
        if p=='/v1/pages':
            assert all(len(x['text']['content'].encode('utf-16-le'))//2<=2000 for x in b['properties']['Name']['title'])
            self.counter+=1; id='p'+str(self.counter)
            page={'id':id,'properties':b['properties'],'parent':b['parent']}
            self.pages[id]=page; self.blocks[id]=[]
            if self.timeout=='page': self.timeout=None; raise TimeoutError()
            return copy.deepcopy(page)
        if p.startswith('/v1/pages/'):
            page=self.pages[p.split('/')[-1]]
            if m=='PATCH':
                page['properties'].update(b['properties'])
                if self.timeout=='title': self.timeout=None; raise TimeoutError()
            return copy.deepcopy(page)
        id=p.split('/')[3]
        if m=='GET': return {'results':copy.deepcopy(self.blocks[id]),'has_more':False}
        self.assert_batch(b)
        out=[]
        for block in b['children']:
            self.counter+=1; block=copy.deepcopy(block); block['id']='b'+str(self.counter)
            self.blocks[block['id']]=[]; out.append(block)
        self.blocks[id].extend(out)
        if self.timeout=='append' and out[0]['type']=='paragraph': self.timeout=None; raise TimeoutError()
        if self.timeout=='toggle' and out[0]['type']=='toggle': self.timeout=None; raise TimeoutError()
        return {'results':copy.deepcopy(out)}
    def assert_batch(self,b):
        assert len(b['children'])<=100
        for child in b['children']:
            for item in child[child['type']]['rich_text']:
                assert len(item['text']['content'].encode('utf-16-le'))//2<=2000
        assert len(json.dumps(b).encode())<=400000

class Tests(unittest.TestCase):
    def setUp(self):
        self.http=Fake(); self.j=Journal(); self.sink=NotionSink(self.http,'db',self.j)
        self.snap={'id':'123','version':'a'*64,'note':{'title':'title','content':'hello'}}
        self.patcher=patch('notion_sink.render_text',lambda s:s['note']['content']); self.patcher.start(); self.addCleanup(self.patcher.stop)
    def test_multiple_matches_record_conflict_without_any_target_write(self):
        self.http.pages={'p1':{'id':'p1'},'p2':{'id':'p2'}}
        before=copy.deepcopy(self.http.pages)
        for _ in range(2):
            with self.assertRaisesRegex(Hold,'duplicate_source_pages'):self.sink.sync(self.snap)
        self.assertEqual(self.http.pages,before)
        self.assertFalse(any(m in ('PATCH','PUT','DELETE') or p=='/v1/pages' for m,p,b in self.http.calls))
        conflict=self.j.get('conflict:notion:db:default:123')
        self.assertEqual(conflict['observed_count'],2);self.assertFalse(conflict['automatic_resolution'])
        self.assertIsNone(self.j.get('notion:db:default:123'))
    def test_duplicate_match_preserves_existing_intent(self):
        state={'phase':'page_create_intent','version':self.snap['version']}
        self.j.set('notion:db:default:123',state)
        self.http.pages={'p1':{'id':'p1'},'p2':{'id':'p2'}}
        with self.assertRaisesRegex(Hold,'duplicate_source_pages'):self.sink.sync(self.snap)
        self.assertEqual(self.j.get('notion:db:default:123'),state)
    def test_paginated_match_is_held_not_silently_selected(self):
        original=self.http.request
        def query(m,p,b=None):
            if p.endswith('/query'):return {'results':[{'id':'p1'}],'has_more':True}
            return original(m,p,b)
        self.http.request=query
        with self.assertRaisesRegex(Hold,'duplicate_source_pages'):self.sink.sync(self.snap)
        self.assertTrue(self.j.get('conflict:notion:db:default:123')['additional_results'])
        self.assertFalse(any(m=='PATCH' or p=='/v1/pages' for m,p,b in self.http.calls))
    def test_create_and_repeat(self):
        r=self.sink.sync(self.snap); n=len([c for c in self.http.calls if c[0]=='PATCH'])
        self.assertEqual(r,self.sink.sync(self.snap,r))
        self.assertEqual(n,len([c for c in self.http.calls if c[0]=='PATCH']))
    def test_long_unicode_complete(self):
        self.snap['note']['content']='中文🧪'*100000
        r=self.sink.sync(self.snap)
        self.assertEqual(''.join(self.sink.block_text(r['block_id'])),self.snap['note']['content'])
        self.assertGreater(len(self.sink.block_text(r['block_id'])),100)
    def test_update_preserves_previous_and_human(self):
        r=self.sink.sync(self.snap)
        self.http.blocks[r['page_id']].append({'id':'human','type':'paragraph','paragraph':{'rich_text':rich('human')}})
        self.snap['version']='b'*64; self.snap['note']['content']='new'
        r2=self.sink.sync(self.snap,r)
        self.assertNotEqual(r['block_id'],r2['block_id'])
        self.assertEqual(len(self.http.blocks[r['page_id']]),3)
    def test_human_managed_edit_holds(self):
        r=self.sink.sync(self.snap)
        self.http.blocks[r['block_id']][0]['paragraph']['rich_text']=rich('changed')
        self.snap['version']='b'*64
        with self.assertRaisesRegex(Hold,'modified'): self.sink.sync(self.snap,r)
    def test_unknown_existing_holds(self):
        self.sink.sync(self.snap)
        with self.assertRaisesRegex(Hold,'unknown'): NotionSink(self.http,'db',Journal()).sync(self.snap)
    def test_ambiguous_page_recovered_by_query(self):
        self.http.timeout='page'
        with self.assertRaises(TimeoutError): self.sink.sync(self.snap)
        self.sink.sync(self.snap)
        self.assertEqual(len(self.http.pages),1)
    def test_ambiguous_toggle_recovered(self):
        self.http.timeout='toggle'
        with self.assertRaises(TimeoutError): self.sink.sync(self.snap)
        r=self.sink.sync(self.snap)
        self.assertEqual(len(self.http.blocks[r['page_id']]),1)
    def test_ambiguous_append_recovered(self):
        self.http.timeout='append'
        with self.assertRaises(TimeoutError): self.sink.sync(self.snap)
        r=self.sink.sync(self.snap)
        self.assertEqual(len(self.http.blocks[r['block_id']]),1)
    def test_ambiguous_missing_page_not_recreated(self):
        self.j.set('notion:db:default:123',{'phase':'page_create_intent','version':'a'*64})
        with self.assertRaisesRegex(Hold,'reconcile'): self.sink.sync(self.snap)
        self.assertFalse(self.http.pages)
    def test_ambiguous_missing_append_not_retried(self):
        self.http.timeout='append'
        with self.assertRaises(TimeoutError): self.sink.sync(self.snap)
        bid=self.j.get('notion:db:default:123')['block_id']; self.http.blocks[bid]=[]
        with self.assertRaisesRegex(Hold,'reconcile'): self.sink.sync(self.snap)

    def test_pending_old_version_holds_before_any_request(self):
        self.http.timeout='append'
        with self.assertRaises(TimeoutError): self.sink.sync(self.snap)
        before=len(self.http.calls)
        self.snap['version']='b'*64
        with self.assertRaisesRegex(Hold,'unresolved_prior_revision'): self.sink.sync(self.snap)
        self.assertEqual(before,len(self.http.calls))
    def test_old_create_intents_hold_before_any_request(self):
        for phase in ('page_create_intent','toggle_create_intent','page_known'):
            self.j.set('notion:db:default:123',{'phase':phase,'version':'old'})
            with self.assertRaisesRegex(Hold,'unresolved_prior_revision'): self.sink.sync(self.snap)
        self.assertEqual(self.http.calls,[])
    def test_namespace_isolates_owner_and_journal(self):
        sink=NotionSink(self.http,'db',self.j,namespace='other')
        r=sink.sync(self.snap)
        self.assertIn('notion:db:other:123',self.j.data)
        self.assertNotIn('notion:db:default:123',self.j.data)
        with self.assertRaisesRegex(Hold,'ownership'):
            self.sink.sync(self.snap,r)

    def test_title_update_and_ambiguous_recovery(self):
        r=self.sink.sync(self.snap)
        self.snap['note']['title']='new title'; self.snap['version']='b'*64
        self.http.timeout='title'
        with self.assertRaises(TimeoutError): self.sink.sync(self.snap,r)
        r2=self.sink.sync(self.snap,r)
        self.assertEqual(r2['title'],'new title')
        self.assertEqual(len(self.http.blocks[r['page_id']]),2)
    def test_human_title_edit_holds(self):
        r=self.sink.sync(self.snap)
        self.http.pages[r['page_id']]['properties']['Name']['title']=rich('human title')
        self.snap['version']='b'*64
        with self.assertRaisesRegex(Hold,'managed_title_modified'): self.sink.sync(self.snap,r)

    def test_stale_store_receipt_after_verified_journal(self):
        r=self.sink.sync(self.snap)
        self.snap['note']['title']='changed'; self.snap['version']='b'*64
        r2=self.sink.sync(self.snap,r)
        # Store crashed before committing r2 but adapter journal already verified.
        self.assertEqual(self.sink.sync(self.snap,r),r2)
    def test_astral_title_uses_utf16_limit(self):
        self.snap['note']['title']='🧪'*2000
        r=self.sink.sync(self.snap)
        self.assertEqual(len(r['title'].encode('utf-16-le'))//2,2000)

    def test_preflight_wrong_schema_no_writes(self):
        self.http.database['properties']['Get ID']['type']='number'
        with self.assertRaisesRegex(Hold,'property_type_mismatch'): self.sink.sync(self.snap)
        self.assertEqual([c[0] for c in self.http.calls],['GET'])
        self.assertEqual(self.j.data,{})
    def test_preflight_archived_no_writes(self):
        self.http.database['archived']=True
        with self.assertRaisesRegex(Hold,'not_live'): self.sink.sync(self.snap)
        self.assertEqual([c[0] for c in self.http.calls],['GET'])
    def test_preflight_multiple_sources_no_writes(self):
        self.http.database['data_sources']=[{'id':'a'},{'id':'b'}]
        with self.assertRaisesRegex(Hold,'multiple_data_sources'): self.sink.sync(self.snap)
        self.assertEqual([c[0] for c in self.http.calls],['GET'])
    def test_preflight_mapped_existing_chinese_properties(self):
        self.http.database['properties']={'笔记 ID':{'type':'rich_text'},'归档来源':{'type':'rich_text'},'标题':{'type':'title'}}
        sink=NotionSink(self.http,'db',self.j,id_property='笔记 ID',owner_property='归档来源',title_property='标题')
        # Read-only check proves mapped schema accepted without schema mutation.
        sink.preflight()
        self.assertEqual([c[0] for c in self.http.calls],['GET'])

if __name__=='__main__': unittest.main()
