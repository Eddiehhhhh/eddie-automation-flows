import unittest
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from capture_tencent_complex import all_pages, export

class ExportTests(unittest.TestCase):
    def test_pagination_and_computed(self):
        calls=[]
        def call(op, args):
            calls.append(args.copy()); n=args['offset']; return {'records': [{'record_id': str(n)}], 'total': 2, 'has_more': n == 0, 'next': 1}
        self.assertEqual(len(all_pages(call,'op','f','s','records',include_computed_values=True)),2)
        self.assertTrue(all(x['include_computed_values'] for x in calls))
    def test_inconsistent_total_fails(self):
        def call(op, args):
            n=args['offset'];return {'records':[{}], 'total': 3-n, 'has_more': n==0, 'next':1}
        with self.assertRaises(ValueError): all_pages(call,'op','f','s','records')
    def test_both_sheet_types_including_hidden(self):
        def call(op, args):
            if op=='sheet.get_sheet_info': return {'sheets':[{'sheet_id':'a','sheet_type':'worksheet','row_count':1,'col_count':1,'hidden':True},{'sheet_id':'b','sheet_type':'smartsheet','hidden':True}]}
            if op=='sheet.get_cell_data': return {'cells':[{'row':0,'col':0,'value_type':'STRING','string_value':'x'}]}
            if op=='sheet.get_merged_cells': return {'merged_cells':[]}
            name=op.split('.')[-1].replace('list_', ''); key=name[:-1]+'_id'; item={key:'x'}; item.update({'field_title':'field'} if name=='fields' else {'field_values':[]} if name=='records' else {}); return {name:[item], 'total':1,'has_more':False,'next':0}
        result=export(call,'f');self.assertEqual(len(result['sheets']),2);self.assertTrue(all(s['metadata']['hidden'] for s in result['sheets']))
if __name__=='__main__': unittest.main()

class SafetyTests(unittest.TestCase):
    def test_redacts_temporary_image_url_only(self):
        from capture_tencent_complex import redact_temporary_urls
        value = {'image_url':'https://docimg1.docs.qq.com/a?w=300&token=secret#x','link':'https://example.org/?needed=yes'}
        self.assertEqual(redact_temporary_urls(value), {'image_url':'https://docimg1.docs.qq.com/a?w=300','link':'https://example.org/?needed=yes'})

    def test_empty_source_is_not_success(self):
        with self.assertRaises(ValueError):
            export(lambda op, args: {'sheets': []}, 'f')

    def test_export_inventory_and_traversal(self):
        import tempfile, zipfile
        from capture_tencent_complex import unpack_official_export
        with tempfile.TemporaryDirectory() as d:
            archive=Path(d)/'sample.zip'
            with zipfile.ZipFile(archive,'w') as z:z.writestr('xl/data.xml','safe')
            result=unpack_official_export(archive,Path(d)/'out',{'sha256':'known','size':1})
            self.assertEqual(result['entries'][0]['size'],4)
            self.assertEqual((Path(d)/'out/xl/data.xml').read_text(),'safe')
            malicious=Path(d)/'bad.zip'
            with zipfile.ZipFile(malicious,'w') as z:z.writestr('../escape','bad')
            with self.assertRaises(ValueError):unpack_official_export(malicious,Path(d)/'bad',{})

    def test_sheet_list_must_match_export(self):
        import tempfile
        from capture_tencent_complex import verify_sheet_names
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'xl';p.mkdir();(p/'workbook.xml').write_text('<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheets><sheet name="A"/></sheets></workbook>')
            verify_sheet_names(d,{'sheets':[{'metadata':{'sheet_name':'A'}}]})
            with self.assertRaises(ValueError):verify_sheet_names(d,{'sheets':[{'metadata':{'sheet_name':'B'}}]})

    def test_reorders_record_fields_to_definition_order(self):
        def call(op, args):
            if op=='sheet.get_sheet_info':return {'sheets':[{'sheet_id':'s','sheet_type':'smartsheet'}]}
            if op=='smartsheet.list_fields':return {'fields':[{'field_id':'a','field_title':'A'},{'field_id':'b','field_title':'B'}], 'total':2,'has_more':False,'next':0}
            if op=='smartsheet.list_views':return {'views':[],'total':0,'has_more':False,'next':0}
            return {'records':[{'record_id':'r','field_values':[{'field':'B','text_value':{'items':[{'text':'b'}]}},{'field':'A','text_value':{'items':[]}}]}], 'total':1,'has_more':False,'next':0}
        record=export(call,'f')['sheets'][0]['records'][0]
        self.assertEqual([v['field'] for v in record['field_values']],['A','B'])

    def test_prior_complete_detects_tampering(self):
        import tempfile, hashlib, json
        from capture_tencent_complex import prior_complete
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);path=root/'excel/xl/workbook.xml';path.parent.mkdir(parents=True)
            data=b'<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheets><sheet name="A"/></sheets></workbook>'
            path.write_bytes(data);snap={'sheets':[{'metadata':{'sheet_name':'A'}}]}
            (root/'snapshot.json').write_text(json.dumps(snap));(root/'excel-manifest.json').write_text(json.dumps({'entries':[{'path':'xl/workbook.xml','size':len(data),'sha256':hashlib.sha256(data).hexdigest()}]}))
            self.assertTrue(prior_complete(root,snap));path.write_bytes(data+b'x');self.assertFalse(prior_complete(root,snap))

    def test_sensitive_signed_urls_rejected(self):
        from capture_tencent_complex import has_sensitive_query
        self.assertTrue(has_sensitive_query({'value':'https://example.invalid/a?q-signature=do-not-store'}))
        self.assertFalse(has_sensitive_query({'value':'https://example.invalid/a?w=300&h=200'}))

    def test_missing_previous_record_is_not_accepted(self):
        from capture_tencent_complex import protect_prior
        old={'sheets':[{'metadata':{'sheet_id':'s','sheet_type':'smartsheet'},'fields':[{'field_id':'f'}],'views':[],'records':[{'record_id':'r'}]}]}
        newer={'sheets':[{'metadata':{'sheet_id':'s','sheet_type':'smartsheet'},'fields':[{'field_id':'f'}],'views':[],'records':[]}]}
        with self.assertRaises(ValueError):protect_prior(old,newer)

    def test_blank_cells_do_not_make_empty_capture_successful(self):
        def call(op,args):
            if op=='sheet.get_sheet_info':return {'sheets':[{'sheet_id':'s','sheet_type':'worksheet','row_count':1,'col_count':1}]}
            if op=='sheet.get_cell_data':return {'cells':[{'row':0,'col':0,'value_type':''}]}
            if op=='sheet.get_merged_cells':return {'merged_cells':[]}
        with self.assertRaises(ValueError):export(call,'fixture')

    def test_missing_pagination_metadata_rejected(self):
        valid={'records':[{'record_id':'r'}],'total':1,'has_more':False,'next':0}
        for key in ('total','has_more','next'):
            payload=dict(valid);payload.pop(key)
            with self.subTest(missing=key),self.assertRaises(ValueError):all_pages(lambda *_:payload,'op','f','s','records')

    def test_missing_cell_payload_rejected(self):
        def call(op,args):
            if op=='sheet.get_sheet_info':return {'sheets':[{'sheet_id':'s','sheet_type':'worksheet','row_count':1,'col_count':1}]}
            if op=='sheet.get_cell_data':return {'cells':[{'row':0,'col':0,'value_type':'STRING'}]}
        with self.assertRaises(ValueError):export(call,'fixture')

    def test_missing_record_fields_rejected_even_if_other_record_valid(self):
        def call(op,args):
            if op=='sheet.get_sheet_info':return {'sheets':[{'sheet_id':'s','sheet_type':'smartsheet'}]}
            if op=='smartsheet.list_fields':return {'fields':[{'field_id':'a','field_title':'A'}],'total':1,'has_more':False,'next':0}
            if op=='smartsheet.list_views':return {'views':[],'total':0,'has_more':False,'next':0}
            return {'records':[{'record_id':'r1','field_values':[{'field':'A','text_value':{'items':[{'text':'x'}]}}]},{'record_id':'r2'}],'total':2,'has_more':False,'next':0}
        with self.assertRaises(ValueError):export(call,'fixture')

    def test_prior_values_and_image_references_may_not_disappear(self):
        import copy
        from capture_tencent_complex import protect_prior
        old={'sheets':[{'metadata':{'sheet_id':'s','sheet_type':'smartsheet'},'fields':[{'field_id':'f'}],'views':[],'records':[{'record_id':'r','field_values':[{'field':'A','image_value':{'items':[{'image_id':'a'}]}}]}]}]}
        for replacement in ([],[{'field':'A','image_value':{'items':[]}}],[{'field':'A','image_value':{'items':[{'image_id':'b'}]}}]):
            newer=copy.deepcopy(old);newer['sheets'][0]['records'][0]['field_values']=replacement
            with self.subTest(replacement=replacement),self.assertRaises(ValueError):protect_prior(old,newer)

    def test_prior_cell_value_may_not_become_empty(self):
        import copy
        from capture_tencent_complex import protect_prior
        old={'sheets':[{'metadata':{'sheet_id':'s','sheet_type':'worksheet'},'cells':[{'row':0,'col':0,'value_type':'STRING','string_value':'x'}]}]}
        for val in ('',None):
            newer=copy.deepcopy(old);newer['sheets'][0]['cells'][0]['string_value']=val
            with self.assertRaises(ValueError):protect_prior(old,newer)

    def test_excel_is_rechecked_when_structured_snapshot_is_unchanged(self):
        import tempfile, json, os
        from unittest.mock import patch
        import capture_tencent_complex as m
        snapshot={'file_id':'f','sheets':[{'metadata':{'sheet_id':'s','sheet_name':'A','sheet_type':'worksheet'},'cells':[]}]}
        old_manifest={'entries':[{'path':'xl/image.png','size':1,'sha256':'a'}]}
        for changed in (False,True):
            with self.subTest(excel_changed=changed),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);previous=root/'previous';previous.mkdir();out=root/'out'
                (previous/'snapshot.json').write_text(json.dumps(snapshot));(previous/'excel-manifest.json').write_text(json.dumps(old_manifest));(previous/'阅读入口.md').write_text(m.render_snapshot(snapshot))
                new_manifest={'entries':[{'path':'xl/image.png','size':1,'sha256':'b' if changed else 'a'}]}
                argv=['capture','--output-dir',str(out),'--previous-dir',str(previous)]
                with patch.object(sys,'argv',argv),patch.dict(os.environ,{'TENCENT_COMPLEX_FILE_ID':'f'}),patch.object(m.api,'load_token',return_value='fixture'),patch.object(m,'export',return_value=snapshot),patch.object(m,'prior_complete',return_value=True),patch.object(m,'protect_prior'),patch.object(m,'download_official_export',return_value={'size':1,'sha256':'fixture'}) as download,patch.object(m,'unpack_official_export',return_value=new_manifest),patch.object(m,'verify_sheet_names'):
                    m.main()
                download.assert_called_once();self.assertEqual(out.exists(),changed)

    def test_prior_formula_or_computed_value_cannot_disappear(self):
        import copy
        from capture_tencent_complex import protect_prior
        old={'sheets':[{'metadata':{'sheet_id':'s','sheet_type':'worksheet'},'cells':[{'row':0,'col':0,'value_type':'NUMBER','number_value':3}],'formula_cells':[{'row':0,'col':0,'formula':'=1+2'}]}]}
        newer=copy.deepcopy(old);newer['sheets'][0]['formula_cells']=[]
        with self.assertRaises(ValueError):protect_prior(old,newer)
        old={'sheets':[{'metadata':{'sheet_id':'s','sheet_type':'smartsheet'},'fields':[{'field_id':'f'}],'views':[],'records':[{'record_id':'r','field_values':[{'field':'A','text_value':{'items':[{'text':'x'}]},'computed_value':{'text':'y'}}]}]}]}
        newer=copy.deepcopy(old);del newer['sheets'][0]['records'][0]['field_values'][0]['computed_value']
        with self.assertRaises(ValueError):protect_prior(old,newer)

    def test_empty_source_ids_are_rejected(self):
        with self.assertRaises(ValueError):export(lambda *_:{'sheets':[{'sheet_id':'','sheet_type':'worksheet'}]},'fixture')
        def call(op,args):
            if op=='sheet.get_sheet_info':return {'sheets':[{'sheet_id':'s','sheet_type':'smartsheet'}]}
            if op=='smartsheet.list_fields':return {'fields':[{'field_id':'','field_title':'A'}],'total':1,'has_more':False,'next':0}
            if op=='smartsheet.list_views':return {'views':[],'total':0,'has_more':False,'next':0}
            return {'records':[{'record_id':'r','field_values':[{'field':'A','number_value':1}]}],'total':1,'has_more':False,'next':0}
        with self.assertRaises(ValueError):export(call,'fixture')

    def test_grid_requires_every_cell_or_confirmed_merged_interior(self):
        from capture_tencent_complex import verify_grid_coverage
        cells=[{'row':r,'col':c} for r in range(2) for c in range(2)]
        verify_grid_coverage(2,2,cells,[],'s')
        with self.assertRaises(ValueError):verify_grid_coverage(2,2,cells[:-1],[],'s')
        verify_grid_coverage(2,2,cells[:-1],['s$A1:B2'],'s')
        with self.assertRaises(ValueError):verify_grid_coverage(2,2,cells[1:],['s$A1:B2'],'s')

    def test_cross_chunk_merge_and_invalid_ranges(self):
        from capture_tencent_complex import verify_grid_coverage
        cells=[{'row':r,'col':0} for r in range(51) if r!=50]
        verify_grid_coverage(51,1,cells,['s$A50:A51'],'s')
        for ranges in (['s$A50:A52'],['other$A50:A51'],['s$A51:A50'],['s$A50:A51','s$A50:A51']):
            with self.subTest(ranges=ranges),self.assertRaises(ValueError):verify_grid_coverage(51,1,cells,ranges,'s')

    def test_formula_mode_empty_cannot_hide_populated_cell(self):
        def call(op,args):
            if op=='sheet.get_sheet_info':return {'sheets':[{'sheet_id':'s','sheet_type':'worksheet','row_count':1,'col_count':1}]}
            if op=='sheet.get_merged_cells':return {'merged_cells':[]}
            if op=='sheet.get_cell_data':return {'cells':[] if args['include_formula'] else [{'row':0,'col':0,'value_type':'STRING','string_value':'x'}]}
        with self.assertRaises(ValueError):export(call,'fixture')
