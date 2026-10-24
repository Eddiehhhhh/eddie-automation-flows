"""Public projection uses only synthetic data and fixed safe fields."""
from pathlib import Path
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import get_private_runtime_report as r
class Report(unittest.TestCase):
    def test_private_fields_removed(self):
        output=r.public({'status':'executed','page_id':'private-id','title':'private-title','members':[{'note_id':'secret'}],'receipt_commit':'a'*40,'reason':'unrecognized-private-reason','error':'secret-error','payload':'private-text'})
        for v in ('private','secret','a'*40):
            # Module's fixed contract contains private, but no actual content values.
            if v!='private':self.assertNotIn(v,str(output))
        self.assertNotIn('page_id',output);self.assertNotIn('members',output);self.assertNotIn('title',output)
    def test_unknown_status_never_echo(self):self.assertEqual(r.public({'status':'secret-title'})['status'],'blocked')
    def test_malformed_status(self):self.assertEqual(r.public({'status':[],'reason':[]},frozenset({'known'}))['status'],'blocked')
    def test_health_not_claimed(self):
        out=r.public({'status':'executed','complete_library_claimed':True,'business_health_asserted':True})
        self.assertFalse(out['complete_library_claimed']);self.assertFalse(out['business_health_asserted'])
    def test_fixed_error_only(self):
        self.assertEqual(r.public({'error':'known'},frozenset({'known'}))['error'],'known')
        self.assertNotIn('error',r.public({'error':'private-value'},frozenset({'known'})))
    def test_counts(self):
        self.assertEqual(r.public({'writes':2,'external_write_attempts':None})['writes'],2)
        self.assertIsNone(r.public({'external_write_attempts':None})['external_write_attempts'])
    def test_bad_counts(self):
        for v in (-1,True,'2',1000001,[]):self.assertNotIn('writes',r.public({'writes':v}))
    def test_status_codes(self):
        self.assertEqual(r.public({'http_statuses':[401]})['http_statuses'],[401])
        self.assertNotIn('http_statuses',r.public({'http_statuses':['private']}))
    def test_prepare_requires_all_fixed_evidence(self):
        self.assertNotEqual(r.public({'status':'recurring_candidate_verified'}),r.PREPARE)
    def test_invalid_result(self):self.assertEqual(r.public(None)['status'],'blocked')
if __name__=='__main__':unittest.main()
