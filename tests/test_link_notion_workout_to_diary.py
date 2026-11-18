import contextlib
import importlib.util
import io
from pathlib import Path
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('link', Path(__file__).parents[1] / 'scripts/link_notion_workout_to_diary.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
A = '11111111-1111-1111-1111-111111111111'
B = '22222222-2222-2222-2222-222222222222'

class SafetyTests(unittest.TestCase):
    def test_relation_merge_preserves_existing(self):
        page={'properties':{'link':{'type':'relation','relation':[{'id':A}]}}}
        self.assertEqual(m.merged_relation_ids(page,'link',B),[A,B])
        self.assertEqual(m.merged_relation_ids(page,'link',A),[A])
        page['properties']['link']['has_more']=True
        with self.assertRaises(RuntimeError): m.merged_relation_ids(page,'link',B)

    def test_missing_identity_never_searches(self):
        with patch.object(m, 'notion_request') as api:
            for value in (None, '', 'private title', 'https://example.com/'+A):
                with self.assertRaises(SystemExit): m.resolve_database('token', value, 'private title')
            api.assert_not_called()

    def test_explicit_identity_verified(self):
        for db in ({'object':'database','id':B}, {'object':'page','id':A}, {'object':'database','id':A,'archived':True}):
            with patch.object(m, 'get_database', return_value=db), self.assertRaises(SystemExit):
                m.resolve_database('token', A, 'unused')
        with patch.object(m, 'get_database', return_value={'object':'database','id':A}):
            self.assertEqual(m.resolve_database('token', A, '')['id'], A)

    def test_wrong_or_unknown_relation_fails_closed(self):
        for relation in ({'database_id':B}, {}, {'data_source_id':A}):
            db={'properties':{'private relation':{'type':'relation','relation':relation}}}
            for configured in (None, 'private relation'):
                with self.assertRaises(SystemExit):
                    m.property_choice(db, configured, 'relation', relation_target_database_id=A)

    def test_matching_relation(self):
        db={'properties':{'link':{'type':'relation','relation':{'database_id':A}}}}
        self.assertEqual(m.property_choice(db,None,'relation',relation_target_database_id=A)[0],'link')

    def test_exception_boundary_redacts(self):
        for exc in (RuntimeError('private title '+A+' secret'), SystemExit('private title '+A+' secret')):
            out=io.StringIO()
            with patch.object(m,'main',side_effect=exc), contextlib.redirect_stderr(out):
                self.assertEqual(m.cli(),1)
            for value in ('private title', A, 'secret', 'Traceback'):
                self.assertNotIn(value,out.getvalue())

    def test_processing_logs_only_aggregate(self):
        workout={'object':'database','id':A,'properties':{'date':{'type':'date'},'title':{'type':'title'},'link':{'type':'relation','relation':{'database_id':B}}}}
        diary={'object':'database','id':B,'properties':{'date':{'type':'date'}}}
        page={'id':A,'properties':{'date':{'type':'date','date':{'start':'2026-01-01'}},'title':{'type':'title','title':[{'plain_text':'private title'}]},'link':{'type':'relation','relation':[]}}}
        for dry in (False,True):
            out=io.StringIO();err=io.StringIO()
            with patch.dict(m.os.environ,{'NOTION_API_KEY':'token','NOTION_WORKOUT_DATABASE_ID':A,'NOTION_DIARY_DATABASE_ID':B,'NOTION_DRY_RUN':str(dry)},clear=True), patch.object(m.sys,'argv',['link']), patch.object(m,'resolve_database',side_effect=[workout,diary]), patch.object(m,'paginate_database',side_effect=[[page],[{'id':B}]]), patch.object(m,'notion_request',return_value=page), patch.object(m,'update_page') as update, contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):
                self.assertEqual(m.cli(),0)
                self.assertEqual(update.call_count,0 if dry else 1)
            for value in (A,B,'private title','2026-01-01','token'):
                self.assertNotIn(value,out.getvalue()+err.getvalue())

if __name__ == '__main__': unittest.main()
