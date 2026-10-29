"""Synthetic fixed-enum projection regressions; no runtime capsule required."""
from pathlib import Path
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import get_private_runtime_report as report

class Diagnostics(unittest.TestCase):
    def test_failure_fields(self):
        result=report.public({'status':'recurring_failed','error':'known_error','stage':'known_stage','failure_kind':'invalid_value','write_state':'not_attempted'}, {'known_error'},allowed_stages={'known_stage'})
        self.assertEqual(result['status'],'recurring_failed')
        self.assertEqual(result['error'],'known_error')
        self.assertEqual(result['stage'],'known_stage')
        self.assertEqual(result['failure_kind'],'invalid_value')
        self.assertFalse(result['business_health_asserted'])
    def test_failure_kind_enum(self):
        for value in report.FAILURE_KINDS:
            self.assertEqual(report.public({'failure_kind':value})['failure_kind'],value)
    def test_untrusted_fields_never_echo(self):
        for value in ('private-note-title','/private/path','https://example.invalid/?token=secret',[],{},None,42):
            result=report.public({'stage':value,'failure_kind':value,'error':value,'reason':value,'exception':value,'traceback':value})
            for key in ('stage','failure_kind','error','reason','exception','traceback'):self.assertNotIn(key,result)
    def test_stage_requires_explicit_allowlist(self):
        self.assertNotIn('stage',report.public({'stage':'checkout'}))
    def test_write_uncertainty_preserved(self):
        for value in ('unknown','uncertain','not_attempted','confirmed'):
            self.assertEqual(report.public({'write_state':value})['write_state'],value)
    def test_reason_is_fixed(self):
        self.assertEqual(report.public({'reason':'known'},{'known'})['reason'],'known')
        self.assertNotIn('reason',report.public({'reason':'private-value'},{'known'}))
if __name__=='__main__':unittest.main()
