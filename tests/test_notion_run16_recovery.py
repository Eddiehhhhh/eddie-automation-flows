"""Exact run16 public proof tests, synthetic readers only."""
from copy import deepcopy
import base64
import unittest
from unittest.mock import patch
import get_notion_run16_recovery as r
import get_notion_journal_runtime as c
import get_notion_outbox as o

RECORDS = [('2026-10-18T01:50:54.4582831Z', {'cas_attempts': 1, 'code_sha': '54977fd93e0f4130dc24dd2083ec2e80e089dbd9', 'commit': '02106fe4e283587e032c32bd1908f1b8aaa67916', 'contract': 'get-notion-journal-checkpoint-1', 'controller_sha': '07650fabcd856c9dbd04c1b6158c4154de5383d9', 'notion_policy_sha256': 'a6babaa293cbe19349bb46be82a4c097edafb423d08e76680a9b48290571d18c', 'parent_commit': 'f9ad7c0b66d8b50553e69b1aa3d85d0afc9180ea', 'previous_record_sha256': None, 'public_policy_sha256': 'c34865c7c37859dd9410e5c265c8a41c6c5798709e3df492cd172f9931495a0a', 'readback_verified': True, 'record_sha256': '5ec3ffb95facd35349aab632d1c5492d55ade70a57ad4a6aa529f71ed3923411', 'rules_sha256': '58933680530fb1855fdec2094284028f9ca160b1ac91caf21ede5309f3ec1dba', 'run_attempt': 1, 'run_id': 39087117510, 'sequence': 1, 'state_blob': '2ccb52603e05907014519350c4d9c57fcf11b93e', 'state_bytes': 673850, 'state_sha256': 'bf7f49a3145c13b5322ce13edc6ecc829499b4e2a7f8d5754aabc1898a41fd43', 'status': 'journal_readback_verified', 'tree': 'fb2d39e09bae19d7a773ac03da1e9ff104ebdb5d'}), ('2026-10-18T01:53:09.0995120Z', {'cas_attempts': 1, 'code_sha': '54977fd93e0f4130dc24dd2083ec2e80e089dbd9', 'commit': '841236588fa7b44fe11b919ab2a8293b2b69742e', 'contract': 'get-notion-journal-checkpoint-1', 'controller_sha': '07650fabcd856c9dbd04c1b6158c4154de5383d9', 'notion_policy_sha256': 'a6babaa293cbe19349bb46be82a4c097edafb423d08e76680a9b48290571d18c', 'parent_commit': '02106fe4e283587e032c32bd1908f1b8aaa67916', 'previous_record_sha256': '5ec3ffb95facd35349aab632d1c5492d55ade70a57ad4a6aa529f71ed3923411', 'public_policy_sha256': 'c34865c7c37859dd9410e5c265c8a41c6c5798709e3df492cd172f9931495a0a', 'readback_verified': True, 'record_sha256': 'f28d2a9fa7f3b9d5ef8e9342e2ba25f223abfc88bcb73bb3cbf9ebcf987c41b9', 'rules_sha256': '58933680530fb1855fdec2094284028f9ca160b1ac91caf21ede5309f3ec1dba', 'run_attempt': 1, 'run_id': 39087117510, 'sequence': 2, 'state_blob': '94b7b5c686a7fe6ab46e42463dcdfe6315b359c2', 'state_bytes': 688046, 'state_sha256': '438e2a52850b64b6f8720a6998da248d0016fe2ab01cb6d78a3e0f23ec5eb224', 'status': 'journal_readback_verified', 'tree': '8084e060eef9389f5efcfb275619d1beb1656ccb'}), ('2026-10-18T01:53:27.4287897Z', {'cas_attempts': 1, 'code_sha': '54977fd93e0f4130dc24dd2083ec2e80e089dbd9', 'commit': '427f68d9066ff0fa0cfe5e7aba5785115166f12f', 'contract': 'get-notion-journal-checkpoint-1', 'controller_sha': '07650fabcd856c9dbd04c1b6158c4154de5383d9', 'notion_policy_sha256': 'a6babaa293cbe19349bb46be82a4c097edafb423d08e76680a9b48290571d18c', 'parent_commit': '841236588fa7b44fe11b919ab2a8293b2b69742e', 'previous_record_sha256': 'f28d2a9fa7f3b9d5ef8e9342e2ba25f223abfc88bcb73bb3cbf9ebcf987c41b9', 'public_policy_sha256': 'c34865c7c37859dd9410e5c265c8a41c6c5798709e3df492cd172f9931495a0a', 'readback_verified': True, 'record_sha256': '7f7a87c9a6db9131b9183b5dc7204aee8407ccfdb929e5510820062464e8ad54', 'rules_sha256': '58933680530fb1855fdec2094284028f9ca160b1ac91caf21ede5309f3ec1dba', 'run_attempt': 1, 'run_id': 39087117510, 'sequence': 3, 'state_blob': '0b73977c64c1d75339706d5905afb481ab4e711f', 'state_bytes': 688249, 'state_sha256': 'dca881ff71adbb73fede0c42ed9eb68d5a3447b64eef62710d149a797d9a3146', 'status': 'journal_readback_verified', 'tree': 'b04348c954304a7d5ffe385d7c214c90034463dd'}), ('2026-10-18T01:54:28.5842574Z', {'cas_attempts': 0, 'code_sha': '54977fd93e0f4130dc24dd2083ec2e80e089dbd9', 'commit': '427f68d9066ff0fa0cfe5e7aba5785115166f12f', 'contract': 'get-notion-journal-checkpoint-1', 'controller_sha': '07650fabcd856c9dbd04c1b6158c4154de5383d9', 'notion_policy_sha256': 'a6babaa293cbe19349bb46be82a4c097edafb423d08e76680a9b48290571d18c', 'parent_commit': None, 'previous_record_sha256': '7f7a87c9a6db9131b9183b5dc7204aee8407ccfdb929e5510820062464e8ad54', 'public_policy_sha256': 'c34865c7c37859dd9410e5c265c8a41c6c5798709e3df492cd172f9931495a0a', 'readback_verified': True, 'record_sha256': '7b58828840d1ddcd56cb211d6e7121740cfa07f49df53f8a874d36abad9c31f8', 'rules_sha256': '58933680530fb1855fdec2094284028f9ca160b1ac91caf21ede5309f3ec1dba', 'run_attempt': 1, 'run_id': 39087117510, 'sequence': 4, 'state_blob': '0b73977c64c1d75339706d5905afb481ab4e711f', 'state_bytes': 688249, 'state_sha256': 'dca881ff71adbb73fede0c42ed9eb68d5a3447b64eef62710d149a797d9a3146', 'status': 'journal_readback_verified', 'tree': 'b04348c954304a7d5ffe385d7c214c90034463dd'})]

class Public:
    def __init__(self):
        p=r.PUBLIC_POLICY;repo={'id':p['repository_id'],'full_name':p['repository'],'private':False}
        self.run={'id':r.RUN,'run_attempt':1,'workflow_id':p['workflow_id'],'path':p['workflow_path'],
            'head_sha':r.CHECKPOINT['controller_sha'],'head_branch':'main','repository':repo,'head_repository':deepcopy(repo),
            'event':'workflow_dispatch','status':'completed','conclusion':'success','created_at':'2026-10-18T01:36:38Z','updated_at':'2026-10-18T01:54:34Z'}
        self.job={'id':r.JOB,'run_id':r.RUN,'run_attempt':1,'head_sha':self.run['head_sha'],'name':p['job_name'],
            'status':'completed','conclusion':'success','started_at':'2026-10-18T01:36:42Z','completed_at':'2026-10-18T01:54:34Z',
            'steps':[{'name':p['publication_step_name'],'number':5,'status':'completed','conclusion':'success',
              'started_at':'2026-10-18T01:36:47Z','completed_at':'2026-10-18T01:49:43Z'},
              {'name':c.NOTION_STEP,'number':6,'status':'completed','conclusion':'success',
              'started_at':'2026-10-18T01:49:43Z','completed_at':'2026-10-18T01:54:32Z'}]}
        self.calls=[];self.records=deepcopy(RECORDS);self.get=deepcopy(r.GET_RECORD)
    def json(self,path):
        self.calls.append(path);p=r.PUBLIC_POLICY;root='/actions/runs/'+str(r.RUN)
        if path=='':return deepcopy(self.run['repository'])
        if path in (root,root+'/attempts/1'):return deepcopy(self.run)
        if path==root+'/attempts/1/jobs?per_page=100&page=1':return {'total_count':1,'jobs':[deepcopy(self.job)]}
        if path=='/actions/workflows/'+str(p['workflow_id']):return {'id':p['workflow_id'],'path':p['workflow_path']}
        if path=='/actions/workflows/'+str(p['workflow_id'])+'/runs?per_page=5&page=1':return {'total_count':1,'workflow_runs':[deepcopy(self.run)]}
        if path=='/contents/'+p['workflow_path']+'?ref='+self.run['head_sha']:
            raw=c.controller.expected_bytes(p,scheduled=False)
            return {'type':'file','path':p['workflow_path'],'encoding':'base64','size':len(raw),'sha':c.journal.retention.oid(raw),'content':base64.b64encode(raw).decode()}
        raise AssertionError('unexpected fixture route')
    def logs(self,job):
        if job!=r.JOB:raise AssertionError('wrong job')
        self.calls.append('logs')
        return ('2026-10-18T01:49:42.0428225Z '+c.provenance.MARKER+o.encoded(self.get).decode()+'\n'
          +''.join(t+' '+c.MARKER+o.encoded(v).decode()+'\n' for t,v in self.records)).encode()

class Tests(unittest.TestCase):
    def test_exact_record_chain_and_shared_budget(self):
        api=Public();budget=c._ReadBudget(api);value=r.recover_checkpoint(budget,r.RUN,1)
        self.assertEqual(value['recovery_anchor'],r.TAG);self.assertEqual(value['anchor'],r.ANCHOR)
        self.assertEqual(value['checkpoint_count'],4);self.assertEqual(value['checkpoint'],r.CHECKPOINT)
        self.assertEqual(sum(x.endswith('/attempts/1') for x in api.calls),2)
        self.assertEqual(budget.requests,len(api.calls));self.assertLessEqual(budget.requests,40)
    def test_invalid_target_stops_before_reads(self):
        for run,attempt in ((True,1),(r.RUN,True),(r.RUN+1,1),(r.RUN,2)):
            api=Public()
            with self.assertRaises(c.CheckpointFailure):r.recover_checkpoint(api,run,attempt)
            self.assertEqual(api.calls,[])
    def test_mutated_checkpoint_or_get_marker_rejected(self):
        for key in ('state_sha256','commit','state_blob'):
            api=Public();v=api.records[-1][1];v[key]='f'*len(v[key]);v.pop('record_sha256');v['record_sha256']=o.digest(v)
            with self.assertRaises(c.CheckpointFailure):r.recover_checkpoint(api,r.RUN,1)
        api=Public();api.get['receipt_sha256']='f'*64;api.get.pop('record_sha256');api.get['record_sha256']=c.provenance.a.sha(c.provenance.a.encoded(api.get))
        with self.assertRaises(c.CheckpointFailure):r.recover_checkpoint(api,r.RUN,1)
    def test_identity_and_same_attempt_drift_rejected(self):
        api=Public();api.job['id']+=1
        with self.assertRaises(c.CheckpointFailure):r.recover_checkpoint(api,r.RUN,1)
        api=Public();old=api.json;seen=0
        def drift(path):
            nonlocal seen
            value=old(path)
            if path.endswith('/attempts/1'):
                seen+=1
                if seen==2:value['updated_at']='2026-10-18T01:54:35Z'
            return value
        api.json=drift
        with self.assertRaises(c.CheckpointFailure):r.recover_checkpoint(api,r.RUN,1)
    def test_discovery_only_other_controller_uses_exact_bridge(self):
        policy={**r.PUBLIC_POLICY,'code_sha':'9'*40,'rules_sha256':'8'*64,'get_policy_sha256':'7'*64}
        args={'public_policy':policy,'approved_public_policy_sha256':c.provenance.a.sha(c.provenance.a.encoded(policy)),'notion_policy_sha256':'6'*64}
        api=Public();value=c.discover_anchor(api,**args)
        self.assertEqual(value['recovery_anchor'],r.TAG);self.assertLessEqual(value['provenance_reads'],40)
        for reason in ('journal_provenance_failed','journal_provenance_record','journal_provenance_budget'):
            with patch.object(c,'recover_anchor',side_effect=c.CheckpointFailure(reason)),patch.object(r,'recover_checkpoint') as call:
                with self.assertRaises(c.CheckpointFailure):c.discover_anchor(Public(),**args)
                call.assert_not_called()

if __name__=='__main__':unittest.main()
