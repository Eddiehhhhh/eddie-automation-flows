"""Preserve an original unknown-source page durably before appending current Git."""
import copy
import json
import hashlib
from pathlib import Path
import subprocess
import unittest
from unittest import mock
import test_wiki_notion_bulk as fixtures
import notion_private_assets as assets
m=fixtures.m
CREATOR='33333333-3333-4333-8333-333333333333'


class ProtectedOriginalTests(unittest.TestCase):
    def setUp(self):
        self.fx=fixtures.BulkTests(methodName='test_full_hierarchy_parent_first_original_root_retained_and_noop')
        self.fx.setUp();self.addCleanup(self.fx.doCleanups)
        self.root_page=self.fx.legacy_root()
        self.directory=self.fx.add_page(self.root_page,'Wiki',m.directory_page_blocks('Wiki'))
        self.target=self.fx.add_page(self.directory,'a.md',[m.quote_block('来源路径：Wiki/a.md'),m.divider_block(),m.paragraph_block('an original manual version not known to Git')])
        for page in self.fx.pages.values():page['created_by']={'id':CREATOR}
        original=copy.deepcopy(self.fx.children[self.target]);baseline=m._baseline(original)
        state=m.load_state();state['bulk_request'].update(include_patterns=['Wiki/*.md'],follow_source_changes=True)
        state['entries']={
            '.':{'type':'root','signature':m.ROOT_SIGNATURE,'page_id':self.root_page,'parent_page_id':fixtures.OUTER,'managed_block_baseline':m._baseline([b for b in self.fx.children[self.root_page]if b['type']!='child_page'])},
            'Wiki':{'type':'directory','signature':m.DIRECTORY_SIGNATURE,'page_id':self.directory,'parent_page_id':self.root_page,'managed_block_baseline':m._baseline([b for b in self.fx.children[self.directory]if b['type']!='child_page'])},
            'Wiki/a.md':{'type':'file','signature':None,'page_id':self.target,'parent_page_id':self.directory,'managed_block_baseline':baseline,
                         'render_version':'unknown-original','revision_generation':0,'preserve_target_content':True,
                         'original_identity':{'source_path':'Wiki/a.md','source_quote_id':original[0]['id'],'creator_id':CREATOR,'mirror_root_id':self.root_page,'parent_page_id':self.directory,'title':'a.md'},
                         'preserved_original':{'target_page_id':self.target,'parent_page_id':self.directory,'title':'a.md','source_signature_known':False,'blocks':original,'baseline':baseline}}
        }
        self.fx.persist(state)
        self.concurrent_change=False
        patch=mock.patch.object(m,'persist_private_state',side_effect=self.durable);patch.start();self.addCleanup(patch.stop)

    def durable(self,state):
        self.fx.persist(state)
        relative=self.fx.state_path.relative_to(self.fx.root).as_posix()
        for args in [('add',relative),('-c','user.name=Synthetic','-c','user.email=synthetic@example.invalid','commit','-m','synthetic private checkpoint')]:
            result=subprocess.run(['git','-C',str(self.fx.root),*args],capture_output=True)
            if result.returncode and args[0]!='add' and b'nothing to commit'not in result.stdout:raise RuntimeError('synthetic checkpoint failed')
        if self.concurrent_change and any(i.get('status')=='dispatched'for i in state.get('write_intents',{}).values()):
            self.fx.children[self.target][2]['paragraph']['rich_text']=m.text_rich_text('human changed after checkpoint')

    def run_bulk(self):return m.execute_bulk('synthetic',fixtures.REQUEST)

    def proof_snapshot(self,entry):
        ref=entry['preserved_target_version']
        raw=subprocess.check_output(['git','-C',str(self.fx.root),'show',ref['commit']+':.github/state/notion-wiki-mirror-state.json'])
        state=json.loads(raw);snapshot=state['write_intents'][ref['operation_id']]['context']['preserved_target']
        self.assertEqual(snapshot['snapshot_hash'],ref['snapshot_hash'])
        self.assertIsNone(snapshot['source_signature'])
        return snapshot

    def test_null_source_appends_after_pinned_full_private_snapshot_and_then_noops(self):
        before=copy.deepcopy(self.fx.children[self.target])
        result=self.run_bulk();self.assertEqual((result['held'],result['deferred']),(0,0))
        self.assertEqual(len(self.fx.sends),1)
        entry=m.load_state()['entries']['Wiki/a.md']
        self.assertEqual(self.proof_snapshot(entry)['blocks'],before)
        self.assertEqual(self.fx.children[self.target][:len(before)],before)
        self.assertEqual(entry['signature'],m.file_signature(self.fx.root/'Wiki/a.md'))
        intent=next(iter(m.load_state()['write_intents'].values()))
        self.assertNotIn('preserved_target',intent['context'])
        self.assertTrue(intent['receipt_compacted'])
        self.assertEqual(self.run_bulk()['skipped'],3)
        self.assertEqual(len(self.fx.sends),1)

    def test_identity_or_unbound_marker_failure_prevents_backup_and_target_write(self):
        for change in ['creator','quote','marker']:
            with self.subTest(change=change):
                saved=copy.deepcopy(self.fx.children[self.target]);meta=copy.deepcopy(self.fx.pages[self.target])
                if change=='creator':self.fx.pages[self.target]['created_by']['id']='different-creator'
                if change=='quote':self.fx.children[self.target][0]['quote']['rich_text']=m.text_rich_text('a different source')
                if change=='marker':self.fx.append(self.target,[m.paragraph_block('eddie-mirror-operation:'+'a'*64)])
                self.assertEqual(self.run_bulk()['held'],1)
                self.assertEqual(self.fx.sends,[])
                self.fx.children[self.target]=saved;self.fx.pages[self.target]=meta

    def test_known_historical_source_with_interleaved_human_blocks_uses_same_protection(self):
        old_signature=m.file_signature(self.fx.root/'Wiki/a.md')
        old_blocks=m.file_page_blocks(self.fx.root,'Wiki/a.md')
        self.fx.children[self.target]=[]
        self.fx.append(self.target,[*old_blocks[:3],m.paragraph_block('interleaved manual insertion'),*old_blocks[3:]])
        original=copy.deepcopy(self.fx.children[self.target]);baseline=m._baseline(original)
        state=m.load_state();entry=state['entries']['Wiki/a.md'];entry.update(signature=old_signature,managed_block_baseline=baseline)
        entry['original_identity']['source_quote_id']=original[0]['id']
        entry['preserved_original'].update(blocks=original,baseline=baseline,source_signature_known=True,source_signature=old_signature)
        self.fx.persist(state)
        (self.fx.root/'Wiki/a.md').write_text('# newer Git source\n\nconfirmed current body\n')
        for args in [('add','Wiki/a.md'),('-c','user.name=Synthetic','-c','user.email=synthetic@example.invalid','commit','-m','synthetic newer source')]:
            subprocess.run(['git','-C',str(self.fx.root),*args],check=True,capture_output=True)
        self.fx.children[self.target][3]['paragraph']['rich_text']=m.text_rich_text('manual insertion changed after binding')
        before=copy.deepcopy(self.fx.children[self.target])
        self.assertEqual(self.run_bulk()['held'],0)
        final=m.load_state()['entries']['Wiki/a.md']
        self.assertEqual(final['preserved_original']['source_signature'],old_signature)
        self.assertEqual(self.proof_snapshot(final)['blocks'],before)
        self.assertEqual(self.fx.children[self.target][:len(before)],before)
        self.assertEqual(final['signature'],m.file_signature(self.fx.root/'Wiki/a.md'))

    def test_null_signature_without_protection_or_owned_history_is_held(self):
        state=m.load_state();state['entries']['Wiki/a.md'].pop('preserve_target_content');self.fx.persist(state)
        self.assertEqual(self.run_bulk()['held'],1)
        self.assertEqual(self.fx.sends,[])

    def test_missing_original_snapshot_or_unrelated_title_does_not_grant_authority(self):
        state=m.load_state();state['entries']['Wiki/a.md'].pop('preserved_original');self.fx.persist(state)
        self.assertEqual(self.run_bulk()['held'],1)
        self.assertEqual(self.fx.sends,[])

    def test_full_snapshot_is_in_acknowledged_git_before_the_actual_patch(self):
        before=copy.deepcopy(self.fx.children[self.target]);send=m._send_once
        def guarded_send(key,method,path,payload):
            self.assertEqual(method,'PATCH')
            self.assertEqual(path,'/blocks/'+self.target+'/children')
            self.assertEqual(set(payload),{'children'})
            durable=json.loads(self.fx.remote.read_text())
            intent=next(i for i in durable['write_intents'].values()if i['status']=='dispatched')
            ref=intent['context']['preserved_target_ref']
            raw=subprocess.check_output(['git','-C',str(self.fx.root),'show',ref['commit']+':.github/state/notion-wiki-mirror-state.json'])
            original=json.loads(raw)['write_intents'][ref['operation_id']]
            self.assertEqual(original['status'],'prepared')
            self.assertEqual(original['context']['preserved_target']['blocks'],before)
            return send(key,method,path,payload)
        with mock.patch.object(m,'_send_once',side_effect=guarded_send):
            self.assertEqual(self.run_bulk()['held'],0)

    def test_unreadable_manual_block_is_held_without_claiming_full_preservation(self):
        self.fx.append(self.target,[{'object':'block','type':'unsupported','unsupported':{}}])
        self.assertEqual(self.run_bulk()['held'],1)
        self.assertEqual(self.fx.sends,[])

    def test_failed_private_ack_prevents_any_target_send(self):
        with mock.patch.object(m,'persist_private_state',side_effect=m.MirrorHeld('synthetic ack failed','checkpoint_publication_failed')):
            with self.assertRaises(m.MirrorHeld):self.run_bulk()
        self.assertEqual(self.fx.sends,[])

    def test_manual_append_after_binding_is_in_the_actual_prewrite_snapshot(self):
        self.fx.append(self.target,[m.paragraph_block('human added after the initial binding backup')])
        before=copy.deepcopy(self.fx.children[self.target])
        self.assertEqual(self.run_bulk()['held'],0)
        self.assertEqual(self.proof_snapshot(m.load_state()['entries']['Wiki/a.md'])['blocks'],before)
        self.assertEqual(self.fx.children[self.target][:len(before)],before)

    def test_manual_replacement_before_first_send_preserves_both_observed_versions(self):
        original=copy.deepcopy(self.fx.children[self.target])
        self.fx.children[self.target][2]['paragraph']['rich_text']=m.text_rich_text('manual replacement after binding')
        before=copy.deepcopy(self.fx.children[self.target])
        self.assertEqual(self.run_bulk()['held'],0)
        entry=m.load_state()['entries']['Wiki/a.md']
        self.assertEqual(self.proof_snapshot(entry)['blocks'],before)
        self.assertEqual(entry['preserved_original']['blocks'],original)
        self.assertEqual(self.fx.children[self.target][:len(before)],before)

    def test_native_manual_file_needs_real_durable_git_bytes_not_only_expiring_url(self):
        data=b'synthetic binary content'
        self.fx.append(self.target,[{'object':'block','type':'file','file':{'type':'file','file':{'url':'https://s3.us-west-2.amazonaws.com/secure.notion-static.com/synthetic/file?signature=synthetic'},'caption':[],'name':'human.bin'}}])
        with mock.patch.object(assets,'_download_returned_asset',return_value=data):
            self.assertEqual(self.run_bulk()['held'],1)
            self.assertEqual(self.fx.sends,[])
            path=self.fx.root/'assets/human.bin';path.parent.mkdir();path.write_bytes(data)
            for args in [('add','assets/human.bin'),('-c','user.name=Synthetic','-c','user.email=synthetic@example.invalid','commit','-m','synthetic durable binary')]:
                subprocess.run(['git','-C',str(self.fx.root),*args],check=True,capture_output=True)
            subprocess.run(['git','-C',str(self.fx.root),'remote','add','origin','https://github.com/example/private-fixture.git'],check=True,capture_output=True)
            head=subprocess.check_output(['git','-C',str(self.fx.root),'rev-parse','HEAD'],text=True).strip()
            blob=subprocess.check_output(['git','-C',str(self.fx.root),'rev-parse','HEAD:assets/human.bin'],text=True).strip()
            digest=hashlib.sha256(data).hexdigest();state=m.load_state();state['assets']={digest:{'sha256':digest,'size':len(data),'source_blob':blob,'source_commit':head,'source_path':'assets/human.bin'}};self.fx.persist(state)
            wrong=copy.deepcopy(state);wrong['assets'][digest]['source_blob']='f'*40;self.fx.persist(wrong)
            self.assertEqual(self.run_bulk()['held'],1)
            self.assertEqual(self.fx.sends,[])
            self.fx.persist(state)
            self.assertEqual(self.run_bulk()['held'],0)
            snapshot=self.proof_snapshot(m.load_state()['entries']['Wiki/a.md'])
            self.assertEqual(next(iter(snapshot['durable_binary_refs'].values()))['sha256'],digest)
            self.assertEqual(len(self.fx.sends),1)

    def test_manual_edit_after_a_verified_version_is_preserved_and_current_git_appended(self):
        self.assertEqual(self.run_bulk()['held'],0)
        self.fx.children[self.target][2]['paragraph']['rich_text']=m.text_rich_text('a later real human edit')
        before=copy.deepcopy(self.fx.children[self.target])
        self.assertEqual(self.run_bulk()['held'],0)
        entry=m.load_state()['entries']['Wiki/a.md']
        self.assertEqual(entry['revision_generation'],2)
        self.assertEqual(self.proof_snapshot(entry)['blocks'],before)
        self.assertEqual(self.fx.children[self.target][:len(before)],before)
        self.assertEqual(len(self.fx.sends),2)

    def test_response_lost_preserves_raw_unknown_snapshot_and_recovers_without_send(self):
        self.fx.lose=1
        self.assertEqual(self.run_bulk()['held'],1)
        intent=next(iter(m.load_state()['write_intents'].values()))
        self.assertEqual(intent['status'],'unknown_write')
        self.assertIn('preserved_target',intent['context'])
        self.fx.state_path.write_bytes(self.fx.remote.read_bytes())
        with mock.patch.object(m,'_send_once',side_effect=AssertionError('unknown may not resend')):
            self.assertEqual(m.recover_intents('synthetic',persist=True)['verified'],1)
        self.assertEqual(self.run_bulk()['held'],0)
        self.assertEqual(len(self.fx.sends),1)

    def test_concurrent_human_change_is_not_overwritten_or_retried(self):
        self.concurrent_change=True
        self.assertEqual(self.run_bulk()['held'],1)
        self.assertEqual(self.fx.children[self.target][2]['paragraph']['rich_text'][0]['text']['content'],'human changed after checkpoint')
        self.assertEqual(len(self.fx.sends),1)
        self.assertEqual(self.run_bulk()['held'],1)
        self.assertEqual(len(self.fx.sends),1)

    def test_partial_protected_source_can_resume_own_verified_markers(self):
        (self.fx.root/'Wiki/a.md').write_text('\n\n'.join('synthetic paragraph '+str(i)for i in range(220)))
        for args in [('add','Wiki/a.md'),('-c','user.name=Synthetic','-c','user.email=synthetic@example.invalid','commit','-m','synthetic long source')]:
            subprocess.run(['git','-C',str(self.fx.root),*args],check=True,capture_output=True)
        state=m.load_state();state['bulk_request']['max_batches']=1;self.fx.persist(state)
        for _ in range(5):
            result=self.run_bulk()
            if not result['deferred']:break
        self.assertEqual((result['held'],result['deferred']),(0,0))
        self.assertEqual(len(self.fx.sends),3)
        self.assertEqual(m.load_state()['entries']['Wiki/a.md']['signature'],m.file_signature(self.fx.root/'Wiki/a.md'))

    def test_manual_change_during_partial_version_starts_a_fresh_full_generation(self):
        (self.fx.root/'Wiki/a.md').write_text('\n\n'.join('synthetic paragraph '+str(i)for i in range(220)))
        for args in [('add','Wiki/a.md'),('-c','user.name=Synthetic','-c','user.email=synthetic@example.invalid','commit','-m','synthetic long source')]:
            subprocess.run(['git','-C',str(self.fx.root),*args],check=True,capture_output=True)
        state=m.load_state();state['bulk_request']['max_batches']=1;self.fx.persist(state)
        self.assertGreater(self.run_bulk()['deferred'],0)
        self.fx.children[self.target][2]['paragraph']['rich_text']=m.text_rich_text('manual edit during partial generation')
        for _ in range(5):
            result=self.run_bulk()
            if not result['deferred']:break
        self.assertEqual((result['held'],result['deferred']),(0,0))
        self.assertEqual(m.load_state()['entries']['Wiki/a.md']['revision_generation'],2)
        self.assertEqual(len(self.fx.sends),4)  # old partial, then every batch of the new full generation
        self.assertEqual(self.fx.children[self.target][2]['paragraph']['rich_text'][0]['text']['content'],'manual edit during partial generation')

    def test_credential_snapshot_is_held_without_publishing_the_secret(self):
        credential='ghp_'+'A'*36
        self.fx.append(self.target,[m.paragraph_block(credential)])
        self.assertEqual(self.run_bulk()['held'],1)
        self.assertNotIn(credential,self.fx.remote.read_text())
        self.assertEqual(self.fx.sends,[])


if __name__=='__main__':unittest.main()
