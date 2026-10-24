"""Synthetic timing tests: no accounts, paths, note IDs or production evidence."""
import contextlib
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import get_notion_journal_timing as t

class Tests(unittest.TestCase):
    def test_result_identity_and_single_call(self):
        timer=t.Timer(iter((10, 35)).__next__); value=object(); calls=[]
        self.assertIs(timer.call('push', lambda: (calls.append(1), value)[1]), value)
        self.assertEqual(calls,[1]); self.assertEqual(timer.rows['push'],
            {'calls':1,'errors':0,'timed_calls':1,'elapsed_ns':25})

    def test_original_exception_identity_no_retry(self):
        timer=t.Timer(iter((10, 35)).__next__); error=RuntimeError('synthetic'); calls=[]
        def fail(): calls.append(1); raise error
        with self.assertRaises(RuntimeError) as got: timer.call('push',fail)
        self.assertIs(got.exception,error); self.assertEqual(calls,[1])
        self.assertEqual(timer.rows['push']['errors'],1)

    def test_absent_clock(self):
        with patch.object(t.time,'monotonic_ns',None): timer=t.Timer()
        self.assertEqual(timer.call('push',lambda:7),7)
        self.assertEqual(timer.rows['push']['timed_calls'],0)

    def test_raising_clock_does_not_mask_failure(self):
        def bad(): raise ValueError('clock')
        timer=t.Timer(bad); error=RuntimeError('business')
        def fail():raise error
        with self.assertRaises(RuntimeError) as got:timer.call('push',fail)
        self.assertIs(got.exception,error)

    def test_clock_backwards_and_invalid_values(self):
        for values in ((20,10),(False,20),(10,float('nan'))):
            timer=t.Timer(iter(values).__next__)
            self.assertEqual(timer.call('push',lambda:7),7)
            self.assertEqual(timer.rows['push']['timed_calls'],0)

    def test_output_failure_cannot_replace_business_failure(self):
        error=RuntimeError('business')
        class Example:
            _timing=t.Timer(iter((10,20)).__next__)
            @t.observed('cas_total',emit=True)
            def run(self):raise error
        with patch('builtins.print',side_effect=OSError('output')):
            with self.assertRaises(RuntimeError) as got:Example().run()
        self.assertIs(got.exception,error)

    def test_missing_timer_preserves_legacy_fixture(self):
        class Example:
            @t.observed('cas_total',emit=True)
            def run(self):return 7
        self.assertEqual(Example().run(),7)

    def test_nested_totals_are_explicitly_inclusive(self):
        timer=t.Timer(iter((0,10,30,50)).__next__)
        timer.call('readback',lambda:timer.call('remote_state_blob',lambda:7))
        self.assertEqual(timer.rows['readback']['elapsed_ns'],50)
        self.assertEqual(timer.rows['remote_state_blob']['elapsed_ns'],20)
        stream=io.StringIO()
        with contextlib.redirect_stderr(stream):timer.emit()
        value=json.loads(stream.getvalue().split(' ',1)[1])
        self.assertTrue(value['inclusive']);self.assertEqual(set(value),{'contract','inclusive','phases'})

    def test_unknown_label_is_not_logged(self):
        timer=t.Timer(iter((0,10)).__next__);timer.call('synthetic-secret',lambda:7)
        self.assertEqual(timer.rows,{})

class JournalFailureIntegrationTests(unittest.TestCase):
    def test_missing_clock_keeps_unknown_poison_and_one_attempt(self):
        from test_notion_remote_journal import JournalTests
        with patch.object(t.time, 'monotonic_ns', None):
            result=JournalTests('test_readback_transport_failure_is_unknown_without_retry').run()
        self.assertTrue(result.wasSuccessful(), str(result.failures)+str(result.errors))

    def test_output_failure_does_not_unlock_external_write(self):
        from test_notion_remote_journal import ExecutorOrderingTests
        with patch('builtins.print', side_effect=OSError('synthetic output failure')):
            result=ExecutorOrderingTests('test_failed_inflight_confirmation_prevents_notion_api_write').run()
        self.assertTrue(result.wasSuccessful(), str(result.failures)+str(result.errors))

    def test_clock_exception_keeps_stale_cas_no_retry(self):
        from test_notion_remote_journal import JournalTests
        with patch.object(t.time, 'monotonic_ns', side_effect=RuntimeError('synthetic clock failure')):
            result=JournalTests('test_rejected_or_forever_stale_never_retries').run()
        self.assertTrue(result.wasSuccessful(), str(result.failures)+str(result.errors))

if __name__=='__main__':unittest.main()
