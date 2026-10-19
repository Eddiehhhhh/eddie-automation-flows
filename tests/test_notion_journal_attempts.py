"""Synthetic exact Actions attempt recovery, including failed/ongoing reruns."""
from copy import deepcopy
import io
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import get_notion_journal_runtime as rt
import test_notion_journal_runtime as journal_fixture
from test_notion_journal_runtime import LogsAPI, public_fixture, receipt, o

class AttemptsAPI:
    def __init__(self, attempts):
        self.attempts={(api.row['id'],api.row['run_attempt']):api for api in attempts}
        self.latest={};self.called=[];self.overrides={}
        for api in attempts:
            ident=api.row['id']
            if ident not in self.latest or api.row['run_attempt']>self.latest[ident].row['run_attempt']:
                self.latest[ident]=api
    def json(self,path):
        self.called.append(path)
        if path in self.overrides:
            value=self.overrides[path];return value() if callable(value) else deepcopy(value)
        if '/runs?per_page=30' in path:
            rows=[deepcopy(api.row) for api in self.latest.values()]
            return {'total_count':len(rows),'workflow_runs':rows}
        if path.startswith('/actions/runs/'):
            fields=path.split('/');ident=int(fields[3]);attempt=int(fields[5]) if len(fields)>5 and fields[4]=='attempts' else None
            api=self.attempts[(ident,attempt)] if attempt is not None else self.latest[ident]
            return api.json(path)
        return next(iter(self.latest.values())).json(path)
    def logs(self,job_id):
        self.called.append('logs:'+str(job_id))
        return next(api.raw for api in self.attempts.values() if api.job['id']==job_id)

class AttemptTests(unittest.TestCase):
    setUp=journal_fixture.CheckpointTests.setUp
    def api(self,ident=1001,attempt=1,checkpoint=True,ongoing=False,noop=False):
        out=io.StringIO();emitter=rt.CheckpointEmitter(out,context={**self.context,'run_id':ident,'run_attempt':attempt})
        if checkpoint:
            emitter(receipt())
            if noop:emitter({**receipt(),'parent_commit':None,'cas_attempts':0})
        api=LogsAPI(self.policy,'')
        api.row.update(id=ident,run_attempt=attempt,updated_at='2026-10-16T10:'+str(5+attempt).zfill(2)+':00Z')
        if ongoing:api.row.update(status='in_progress',conclusion=None)
        api.job.update(id=ident*10+attempt,run_id=ident,run_attempt=attempt)
        get=public_fixture.record(api.row)
        api.raw=('2026-10-16T10:00:59Z '+rt.provenance.publication_line(get)+'\n'+''.join('2026-10-16T10:02:00Z '+line+'\n' for line in out.getvalue().splitlines())).encode()
        return api
    def discover(self,api,**kwargs):
        return rt.discover_anchor(api,public_policy=self.policy,approved_public_policy_sha256=self.context['public_policy_sha256'],notion_policy_sha256='d'*64,**kwargs)
    def recover(self,api,attempt=1):
        return rt.recover_anchor(api,run_id=1001,run_attempt=attempt,public_policy=self.policy,approved_public_policy_sha256=self.context['public_policy_sha256'],notion_policy_sha256='d'*64)
    def test_attempt_one_checkpoint_survives_failed_two_for_new_run(self):
        api=AttemptsAPI([self.api(),self.api(attempt=2,checkpoint=False),self.api(1002,checkpoint=False,ongoing=True)])
        found=self.discover(api,exclude_run_id=1002,exclude_run_attempt=1)
        self.assertEqual((found['run_id'],found['run_attempt']),(1001,1))
        self.assertEqual(found['latest_attempt'],2)
        self.assertEqual(found['candidate_attempts_checked'],2)
        self.assertLessEqual(found['provenance_reads'],40)
        self.assertEqual(sum(path.startswith('/contents/') for path in api.called),1)
    def test_prior_attempt_uses_its_own_time_and_success_stops_more_reads(self):
        old=self.api();latest=self.api(attempt=2,checkpoint=False);newer=self.api(1002)
        api=AttemptsAPI([old,latest,newer])
        found=self.discover(api)
        self.assertEqual((found['run_id'],found['run_attempt']),(1002,1))
        self.assertNotIn('logs:10011',api.called)
        self.assertEqual(api.called[-1],'/actions/runs/1002')

    def test_current_attempt_two_may_recover_same_run_attempt_one(self):
        api=AttemptsAPI([self.api(),self.api(attempt=2,checkpoint=False,ongoing=True)])
        found=self.discover(api,exclude_run_id=1001,exclude_run_attempt=2)
        self.assertEqual(found['run_attempt'],1)
        self.assertFalse(any('/attempts/2/jobs?' in path for path in api.called))
    def test_other_ongoing_rerun_does_not_hide_old_terminal_attempt(self):
        api=AttemptsAPI([self.api(),self.api(attempt=2,checkpoint=False,ongoing=True)])
        found=self.discover(api)
        self.assertEqual(found['run_attempt'],1)
        self.assertEqual(found['latest_attempt'],2)

    def test_same_commit_noop_checkpoint_requires_no_new_parent(self):
        found=self.recover(AttemptsAPI([self.api(noop=True),self.api(attempt=2,checkpoint=False)]))
        self.assertEqual(found['checkpoint_count'],2)
        self.assertEqual(found['checkpoint']['cas_attempts'],0)
        self.assertIsNone(found['checkpoint']['parent_commit'])
        self.assertEqual(found['anchor']['commit'],'1'*40)
    def test_original_attempt_changed_on_second_read_stops(self):
        api=AttemptsAPI([self.api(),self.api(attempt=2,checkpoint=False)])
        reads=[];original=deepcopy(api.attempts[(1001,1)].row)
        def drift():
            reads.append(1);return original if len(reads)==1 else {**original,'updated_at':'2026-10-16T10:09:00Z'}
        api.overrides['/actions/runs/1001/attempts/1']=drift
        with self.assertRaisesRegex(rt.CheckpointFailure,'journal_provenance_changed'):self.recover(api)
    def test_later_attempt_wrong_identity_or_attempt_regression_stops(self):
        for change in ({'head_sha':'f'*40},{'id':1002},{'run_attempt':0},{'created_at':'2026-10-16T09:00:00Z'}):
            api=AttemptsAPI([self.api(),self.api(attempt=2,checkpoint=False)])
            api.overrides['/actions/runs/1001']={**api.latest[1001].row,**change}
            with self.assertRaises(rt.CheckpointFailure):self.recover(api)
    def test_four_attempt_budget_does_not_reach_fifth_older_marker(self):
        api=AttemptsAPI([self.api(attempt=i,checkpoint=i==1) for i in range(1,6)])
        with self.assertRaisesRegex(rt.CheckpointFailure,'journal_provenance_missing'):self.discover(api)
        self.assertFalse(any('/attempts/1' in path for path in api.called))
        self.assertEqual(sum(path.startswith('logs:') for path in api.called),4)
    def test_exclusion_requires_exact_run_and_attempt_pair(self):
        api=AttemptsAPI([self.api()])
        for changes in ({'exclude_run_id':1001},{'exclude_run_attempt':1}):
            with self.assertRaisesRegex(rt.CheckpointFailure,'journal_provenance_context'):self.discover(api,**changes)
        self.assertEqual(api.called,[])

if __name__=='__main__':unittest.main()
