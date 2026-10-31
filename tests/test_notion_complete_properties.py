from copy import deepcopy
from pathlib import Path
import sys
import unittest
import urllib.parse
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from notion_complete_properties import hydrate_page


def rich(value):
    return {'type':'text','text':{'content':value,'link':None},'annotations':{'bold':True,'italic':False,'color':'red'},'plain_text':value,'href':None}


def envelope(kind,values,*,ident='p',more=False,cursor=None,rollup=None):
    meta={'id':ident,'type':kind,'next_url':None}
    if rollup is not None:meta['rollup']=rollup
    return {'object':'list','type':'property_item','results':[{'object':'property_item','id':ident,'type':kind,kind:deepcopy(v)} for v in values],
            'has_more':more,'next_cursor':cursor,'property_item':meta}


def page(prop):return {'id':'source-page','created_time':'2026-10-30T13:14:15.123Z','last_edited_time':'2026-11-01T09:08:07.456Z','properties':{'Name':prop}}


class CompletePropertiesTests(unittest.TestCase):
    def test_title_and_rich_text_keep_order_annotations_mentions_equation_and_duplicate_ids(self):
        mention={'type':'mention','mention':{'type':'page','page':{'id':'target'}},'annotations':{'bold':False,'color':'blue'},'plain_text':'Target','href':'https://example.invalid/target'}
        equation={'type':'equation','equation':{'expression':'x+y'},'annotations':{'code':False},'plain_text':'x+y','href':None}
        for kind in ('title','rich_text'):
            original=page({'id':'p','type':kind,kind:[rich('prefix')]});before=deepcopy(original)
            raw=[envelope(kind,[rich('same')]*25,more=True,cursor='opaque?+/cursor'),envelope(kind,[mention,equation,rich('same')])]
            queue=deepcopy(raw);calls=[]
            def request(method,path,payload=None):calls.append((method,path,payload));return queue.pop(0)
            result=hydrate_page(original,request)
            self.assertEqual(result['hydrated_page']['properties']['Name'][kind],[rich('same')]*25+[mention,equation,rich('same')])
            self.assertEqual(result['property_responses'],{'p':raw});self.assertEqual(result['original_page'],before);self.assertEqual(original,before)
            self.assertEqual(result['proof_version'],1);self.assertTrue(all(c[0]=='GET' and c[2] is None for c in calls))
            self.assertEqual(urllib.parse.parse_qs(urllib.parse.urlsplit(calls[1][1]).query)['start_cursor'],['opaque?+/cursor'])
            self.assertEqual(result['hydrated_page']['created_time'],original['created_time'])

    def test_people_even_small_prefix_uses_property_endpoint_and_preserves_partial_users(self):
        users=[{'object':'user','id':str(i)} for i in range(31)]
        queue=[envelope('people',users[:25],more=True,cursor='next'),envelope('people',users[25:])]
        result=hydrate_page(page({'id':'p','type':'people','people':users[:1]}),lambda *a:queue.pop(0))
        self.assertEqual(result['hydrated_page']['properties']['Name']['people'],users)

    def test_empty_title_is_explicitly_proven_by_empty_terminal_list(self):
        calls=[]
        def request(*args):calls.append(args);return envelope('title',[])
        result=hydrate_page(page({'id':'p','type':'title','title':[]}),request)
        self.assertEqual(len(calls),1);self.assertEqual(result['hydrated_page']['properties']['Name']['title'],[])

    def test_relation_false_uses_api_visible_page_value_but_missing_flag_hydrates(self):
        original=page({'id':'p','type':'relation','relation':[{'id':'page'}],'has_more':False})
        self.assertEqual(hydrate_page(original,lambda *a:self.fail('unneeded'))['property_responses'],{})
        original['properties']['Name'].pop('has_more')
        result=hydrate_page(original,lambda *a:envelope('relation',[{'id':'page'},{'id':'second'}]))
        self.assertEqual(len(result['hydrated_page']['properties']['Name']['relation']),2)
        self.assertFalse(result['hydrated_page']['properties']['Name']['has_more'])

    def test_scalar_button_place_and_native_dates_preserve_every_field_without_requests(self):
        original={'id':'source','properties':{'Button':{'id':'button','type':'button','button':{}},'Place':{'id':'place','type':'place','place':{'name':'Source place','lat':1.2}},'Date':{'id':'d','type':'date','date':{'start':'2026-10-30T13:14:15+08:00','end':None,'time_zone':None}},'Files':{'id':'f','type':'files','files':[{'name':'asset','type':'file','file':{'url':'https://files.example.invalid/a?expires=synthetic','expiry_time':'2026-11-02T00:00:00Z'}}]}}}
        self.assertEqual(hydrate_page(original,lambda *a:self.fail('unneeded'))['hydrated_page'],original)

    def test_rollup_uses_only_terminal_number_not_adding_partial(self):
        first=envelope('rollup',[],more=True,cursor='next',rollup={'type':'incomplete','incomplete':{},'function':'sum'})
        last=envelope('rollup',[],rollup={'type':'number','number':17,'function':'sum'})
        queue=[first,last];result=hydrate_page(page({'id':'p','type':'rollup','rollup':{'type':'number','number':3,'function':'sum'}}),lambda *a:queue.pop(0))
        self.assertEqual(result['hydrated_page']['properties']['Name']['rollup']['number'],17)
        self.assertEqual(result['property_responses']['p'],[first,last])

    def test_rollup_show_original_reconstructs_all_ordered_typed_rows(self):
        response=envelope('rollup',[],rollup={'type':'array','array':[],'function':'show_original'})
        response['results']=[{'object':'property_item','id':'target-property','type':'rich_text','rich_text':rich('A')},{'object':'property_item','id':'target-property','type':'number','number':None},{'object':'property_item','id':'target-property','type':'rich_text','rich_text':rich('A')}]
        result=hydrate_page(page({'id':'p','type':'rollup','rollup':{'type':'array','array':[]}}),lambda *a:response)
        self.assertEqual(result['hydrated_page']['properties']['Name']['rollup']['array'],[{'type':'rich_text','rich_text':rich('A')},{'type':'number','number':None},{'type':'rich_text','rich_text':rich('A')}])

    def test_formula_single_item_terminal_preserves_native_date(self):
        value={'type':'date','date':{'start':'2026-10-31T01:02:03+08:00','end':None,'time_zone':None}}
        result=hydrate_page(page({'id':'p','type':'formula','formula':{'type':'date','date':None}}),lambda *a:{'object':'property_item','id':'p','type':'formula','formula':value})
        self.assertEqual(result['hydrated_page']['properties']['Name']['formula'],value)

    def test_unsupported_formula_and_rollup_are_held_not_coerced(self):
        for kind in ('formula','rollup'):
            for subtype in ('unsupported','incomplete','unknown'):
                value={'type':subtype,subtype:{},'function':'sum'}
                response={'object':'property_item','id':'p','type':'formula','formula':value} if kind=='formula' else envelope(kind,[],rollup=value)
                original=page({'id':'p','type':kind,kind:{'type':'number','number':9}});before=deepcopy(original)
                with self.assertRaisesRegex(ValueError,'^property_(formula|rollup)_(unsupported|incomplete)$'):
                    hydrate_page(original,lambda *a:response)
                self.assertEqual(original,before)

    def test_budget_exception_propagates_same_instance_without_wrapping_or_input_change(self):
        class BudgetExceeded(Exception):pass
        error=BudgetExceeded('caller budget');original=page({'id':'p','type':'rich_text','rich_text':[]});before=deepcopy(original)
        def request(*args):raise error
        with self.assertRaises(BudgetExceeded) as caught:hydrate_page(original,request)
        self.assertIs(caught.exception,error);self.assertEqual(original,before)

    def test_encoding_once_and_never_follow_next_url(self):
        calls=[];queue=[envelope('title',[rich('A')],ident='a%3Ab',more=True,cursor='next'),envelope('title',[rich('B')],ident='a:b')]
        queue[0]['property_item']['next_url']='https://untrusted.example.invalid/credential-sink'
        def request(method,path):calls.append(path);return queue.pop(0)
        hydrate_page(page({'id':'a%3Ab','type':'title','title':[]}),request)
        self.assertTrue(all('/properties/a%3Ab?' in p and 'https://' not in p for p in calls))
        self.assertNotIn('%253A',calls[0])

    def test_bad_cursor_and_list_shapes_are_fixed_errors_not_partial_success(self):
        variants=[{'object':'list','type':'property_item','results':[],'has_more':'false'},envelope('title',[],more=True,cursor=None),envelope('title',[],cursor='unexpected')]
        for response in variants:
            with self.assertRaisesRegex(ValueError,'^property_(list_shape|cursor_incomplete|terminal_inconsistent)$'):
                hydrate_page(page({'id':'p','type':'title','title':[]}),lambda *a:response)
        repeating=envelope('title',[],more=True,cursor='repeat')
        with self.assertRaisesRegex(ValueError,'^property_cursor_incomplete$'):
            hydrate_page(page({'id':'p','type':'title','title':[]}),lambda *a:repeating)

    def test_unknown_properties_missing_values_and_duplicate_ids_fail_closed(self):
        for prop in [{'id':'p','type':'future_array','future_array':[1]},{'id':'p','type':'title'},{'type':'title','title':[]}]:
            with self.assertRaises(ValueError):hydrate_page(page(prop),lambda *a:self.fail('unneeded'))
        original={'id':'page','properties':{'A':{'id':'p','type':'number','number':1},'B':{'id':'p','type':'number','number':2}}}
        with self.assertRaisesRegex(ValueError,'^property_duplicate_id$'):hydrate_page(original,lambda *a:self.fail('unneeded'))

    def test_wrong_response_type_or_id_and_unknown_rich_text_type_fail_closed(self):
        variants=[envelope('title',[rich('A')],ident='other'),envelope('people',[{'id':'user'}]),envelope('title',[{'type':'future','future':{}}])]
        for response in variants:
            with self.assertRaises(ValueError):hydrate_page(page({'id':'p','type':'title','title':[]}),lambda *a:response)

if __name__=='__main__':unittest.main()
