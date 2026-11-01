"""Keep actual Notion Unicode loss held until a byte-exact original is attached."""
import copy
import hashlib
import contextlib
import io
import sys
import json
import subprocess
import time
import unittest
import uuid
from unittest import mock
import test_wiki_notion_bulk as fixtures
import notion_private_assets as assets
m=fixtures.m
UPLOAD='11111111-1111-4111-8111-111111111111'


class UnicodePreservationTests(unittest.TestCase):
    def setUp(self):
        self.fx=fixtures.BulkTests('test_full_hierarchy_parent_first_original_root_retained_and_noop');self.fx.setUp();self.addCleanup(self.fx.doCleanups)
        self.source=self.fx.root/'Wiki/a.md';self.source.write_text('# Synthetic\n\nbefore\u200b\u200bafter\n\n- list\u200b\u200bend\n')
        self.commit_source()
        subprocess.run(['git','-C',str(self.fx.root),'remote','add','origin','https://github.com/example/private-fixture.git'],check=True,capture_output=True)
        state=m.load_state();state['bulk_request'].update(include_patterns=['Wiki/a.md'],follow_source_changes=True);self.fx.persist(state);self.fx.legacy_root()
        self.old_send=self.fx.send;self.lose_repair=False;self.wrong_bytes=False;self.change_before_patch=False;self.upload_calls=[]
        def server(key,method,path,payload):
            # A remediation PATCH is distinct from reissuing the original create.
            proof=[i.get('unicode_preservation')for i in m.load_state()['write_intents'].values()if (i.get('unicode_preservation')or{}).get('phase')=='dispatched']
            if proof and method=='PATCH':
                durable=json.loads(self.fx.remote.read_text());p=next(i['unicode_preservation']for i in durable['write_intents'].values()if i.get('unicode_preservation'))
                self.assertEqual(p['phase'],'dispatched');self.assertTrue(p['before_blocks']);self.assertEqual(len(payload['children']),4)
            self.old_send(key,method,path,payload)
            for target,blocks in self.fx.children.items():
                for b in blocks:
                    if b['type']in{'paragraph','bulleted_list_item'}:
                        for run in b[b['type']].get('rich_text',[]):run.get('text',{})['content']=run.get('text',{}).get('content','').replace('\u200b','')
            if proof and method=='PATCH'and self.lose_repair:
                self.lose_repair=False;raise m.UnknownWrite('synthetic repair response loss')
        patch=mock.patch.object(m,'_send_once',side_effect=server);patch.start();self.addCleanup(patch.stop)
        patch=mock.patch.object(assets.private_checkout,'REPOSITORY','example/private-fixture');patch.start();self.addCleanup(patch.stop)
        patch=mock.patch.object(assets,'_request',side_effect=self.asset_api);patch.start();self.addCleanup(patch.stop)
        patch=mock.patch.object(assets,'_download_returned_asset',side_effect=lambda url:b'changed'if self.wrong_bytes else self.source.read_bytes());patch.start();self.addCleanup(patch.stop)
        result=m.execute_bulk('synthetic',fixtures.REQUEST);self.assertEqual(result['held'],1)
        self.operation,self.original=next((op,copy.deepcopy(i))for op,i in m.load_state()['write_intents'].items()if i['status']=='dispatched')
        self.target=next(p for p,v in self.fx.pages.items()if m.page_title_from_page(v)=='a.md')
        self.before=copy.deepcopy(self.fx.children[self.target]);self.initial_sends=len(self.fx.sends)
        self.request_id=str(uuid.uuid4())
        state=m.load_state();context=self.original['context'];raw=self.source.read_bytes()
        blob=subprocess.check_output(['git','-C',str(self.fx.root),'rev-parse',context['source_snapshot']+':Wiki/a.md'],text=True).strip()
        state['execution_request']={'request_id':self.request_id,'action':'preserve_unicode_source','allow_new_page':False,
            'operation_id':self.operation,'source_path':'Wiki/a.md','source_signature':context['source_signature'],
            'source_snapshot':context['source_snapshot'],'source_blob':blob,'source_sha256':hashlib.sha256(raw).hexdigest(),
            'source_size':len(raw),'render_version':'legacy-v1','target_page_id':self.target,'parent_page_id':self.original['parent_id'],
            'original_request_hash':m.sha256_text(json.dumps(context['request'],sort_keys=True,ensure_ascii=False))}
        self.fx.persist(state)

    def repair(self):
        try:return m.execute_unicode_request('synthetic',self.request_id)
        except m.MirrorHeld as exc:
            if exc.code!='held':raise
            return {'held':1,'attempted_batches':0,'source_attachment_complete':0}


    def commit_source(self):
        for args in [('add','Wiki/a.md'),('-c','user.name=Synthetic','-c','user.email=synthetic@example.invalid','commit','-m','synthetic source')]:subprocess.run(['git','-C',str(self.fx.root),*args],check=True,capture_output=True)

    def asset_api(self,key,method,path,body=None,content_type=None):
        self.upload_calls.append((method,path))
        if method=='POST':
            receipt=next(iter(json.loads(self.fx.remote.read_text())['assets'].values()))
            self.assertEqual(receipt['phase'],'dispatched'if path.endswith('/send')else'prepared')
        if path=='/users/me':return {'bot':{'workspace_limits':{'max_file_upload_size_in_bytes':1024*1024}}}
        if path=='/file_uploads':return {'object':'file_upload','id':UPLOAD,'status':'pending'}
        receipt=next(r for r in m.load_state()['assets'].values()if r['sha256']==hashlib.sha256(self.source.read_bytes()).hexdigest())
        return {'object':'file_upload','id':UPLOAD,'status':'uploaded','filename':receipt['filename'],'content_type':receipt['content_type'],'content_length':len(self.source.read_bytes()),'expiry_time':None}

    def test_exact_attachment_readback_qualifies_original_without_changing_payload_then_noops(self):
        result=self.repair();self.assertEqual(result['held'],0);self.assertEqual(result['source_attachment_complete'],1)
        state=m.load_state();record=state['write_intents'][self.operation];entry=state['entries']['Wiki/a.md']
        self.assertEqual(record['status'],'verified');self.assertEqual(record['expected'],self.original['expected']);self.assertEqual(record['context']['request'],self.original['context']['request'])
        self.assertEqual(record['unicode_preservation']['before_blocks'],self.before)
        self.assertEqual(self.fx.children[self.target][:len(self.before)],self.before)
        self.assertEqual(entry['fidelity']['mode'],'source_attachment_complete')
        self.assertEqual(record['unicode_preservation']['unicode_counts'],{'source_file':4,'requested_text_runs':4,'actual_text_runs':0})
        notice=self.fx.children[self.target][-3]['paragraph']['rich_text'][0]['text']['content']
        self.assertIn(hashlib.sha256(self.source.read_bytes()).hexdigest(),notice)
        self.assertEqual(entry['fidelity']['asset']['sha256'],hashlib.sha256(self.source.read_bytes()).hexdigest())
        self.assertNotIn('receipt_compacted',record)  # raw old source request remains available
        self.assertEqual(len(self.fx.sends),self.initial_sends+1)
        writes=len(self.fx.sends);uploads=len(self.upload_calls)
        self.assertEqual(m.execute_bulk('synthetic',fixtures.REQUEST)['held'],0)
        self.assertEqual(len(self.fx.sends),writes);self.assertEqual(len(self.upload_calls),uploads)

    def test_qualified_same_source_skips_before_changed_renderer_or_assets(self):
        self.assertEqual(self.repair()['held'],0)
        state=m.load_state();state['bulk_progress']['next_order']=None
        state['bulk_progress']['cycle_complete']=False
        state['bulk_progress']['renderer_revision']='older-renderer'
        self.fx.persist(state)
        before=copy.deepcopy(state['write_intents'][self.operation]);writes=len(self.fx.sends);uploads=len(self.upload_calls)
        with mock.patch.object(m,'render_native_source',side_effect=AssertionError('must not rerender qualified source')):
            result=m.execute_bulk('synthetic',fixtures.REQUEST)
        self.assertEqual(result['held'],0);self.assertEqual(result['source_attachment_complete'],1)
        self.assertEqual(len(self.fx.sends),writes);self.assertEqual(len(self.upload_calls),uploads)
        after=m.load_state()['write_intents'][self.operation]
        self.assertEqual(after['expected'],before['expected']);self.assertEqual(after['context']['request'],before['context']['request'])
        node=m.Node('Wiki/a.md',None,'a.md','file',m.file_signature(self.source))
        snapshot=subprocess.check_output(['git','-C',str(self.fx.root),'rev-parse','HEAD'],text=True).strip()
        self.assertEqual(m.execute_node_blocks('synthetic',node,[],self.original['parent_id'],False,snapshot,render_version='native-v1:changed')['skipped'],1)
        self.assertEqual(m.load_state()['entries']['Wiki/a.md']['render_version'],'legacy-v1')

    def test_merged_identical_runs_are_only_directional_unicode_evidence(self):
        expected=m.paragraph_block('')
        expected['paragraph']['rich_text']=[{'type':'text','text':{'content':'part'+str(i)+'\u200b\u200b'}}for i in range(6)]
        actual=copy.deepcopy(expected)
        actual['paragraph']['rich_text']=[{'type':'text','text':{'content':''.join(x['text']['content']for x in expected['paragraph']['rich_text']).replace('\u200b','')}}]
        intent={'expected':[m.block_identity(expected)],'context':{'request':{'children':[expected]}}}
        original=copy.deepcopy(intent)
        self.assertTrue(m.unicode_deletion_matches(intent,[actual]))
        self.assertFalse(m.intent_expected_matches(intent,[actual]))
        self.assertEqual(intent,original)
        for mutation in ('style','link','other_character','reverse'):
            changed=copy.deepcopy(actual)
            if mutation=='style':changed['paragraph']['rich_text'][0]['annotations']={'bold':True}
            elif mutation=='link':changed['paragraph']['rich_text'][0]['text']['link']={'url':'https://example.invalid/changed'}
            elif mutation=='other_character':changed['paragraph']['rich_text'][0]['text']['content']+='x'
            else:changed['paragraph']['rich_text'][0]['text']['content']+='\u200b'
            if mutation=='reverse':
                reverse={'expected':[m.block_identity(actual)],'context':{'request':{'children':[actual]}}}
                self.assertFalse(m.unicode_deletion_matches(reverse,[changed]))
            else:self.assertFalse(m.unicode_deletion_matches(intent,[changed]))

    def native_reuse_request(self):
        self.assertEqual(self.repair()['held'],0)
        self.qualified_original=copy.deepcopy(m.load_state()['write_intents'][self.operation])
        body=m.file_page_blocks(self.fx.root,'Wiki/a.md',self.source.read_text())
        for block in body:
            if block['type']not in {'paragraph','bulleted_list_item'}:continue
            runs=[]
            for run in block[block['type']]['rich_text']:
                text=run['text']['content'];half=len(text)//2
                for part in (text[:half],text[half:]):
                    item=copy.deepcopy(run);item['text']['content']=part;runs.append(item)
            block[block['type']]['rich_text']=runs
        version='native-v1:'+m.sha256_text(json.dumps([m.block_identity(b)for b in body]))
        state=m.load_state();state['bulk_progress']['next_order']=None;state['bulk_progress']['cycle_complete']=False;self.fx.persist(state)
        with mock.patch.object(m,'qualified_unchanged_source',return_value=False),mock.patch.object(m,'render_native_source',return_value=(body,version,{'assets_ready':0,'assets_held':0,'assets_fallback':0})):
            self.assertEqual(m.execute_bulk('synthetic',fixtures.REQUEST)['held'],1)
        state=m.load_state();operation,intent=next((op,i)for op,i in state['write_intents'].items()if i['status']!='verified')
        self.native_operation=operation;self.native_before=copy.deepcopy(intent)
        request=copy.deepcopy(state['execution_request']);request.update(request_id=str(uuid.uuid4()),operation_id=operation,source_snapshot=intent['context']['source_snapshot'],render_version=version,original_request_hash=m.sha256_text(json.dumps(intent['context']['request'],sort_keys=True,ensure_ascii=False)),reused_operation_id=self.operation)
        state['execution_request']=request;self.fx.persist(state);return request

    def test_native_equivalent_pending_reuses_old_exact_attachment_by_gets_only(self):
        request=self.native_reuse_request();sends=len(self.fx.sends);uploads=len(self.upload_calls);before=copy.deepcopy(self.fx.children[self.target])
        with mock.patch.object(m,'_send_once',side_effect=AssertionError('qualification must not write Notion')):
            result=m.execute_unicode_request('synthetic',request['request_id'])
            self.assertEqual(result['held'],0);self.assertEqual(result['attempted_batches'],0)
            self.assertEqual(m.execute_unicode_request('synthetic',request['request_id'])['held'],0)
        state=m.load_state();intent=state['write_intents'][self.native_operation]
        self.assertEqual(intent['status'],'verified');self.assertEqual(intent['unicode_preservation']['mode'],'reuse_verified_attachment')
        self.assertTrue(state['entries']['Wiki/a.md']['fidelity']['native_loss'])
        self.assertFalse(m.intent_expected_matches(intent,before[len(intent['baseline']):]))
        for field in ('expected','context','baseline'):self.assertEqual(intent[field],self.native_before[field])
        self.assertEqual(state['write_intents'][self.operation],self.qualified_original)
        self.assertEqual(self.fx.children[self.target],before);self.assertEqual(len(self.fx.sends),sends);self.assertEqual(len(self.upload_calls),uploads)
        # Every future readback rechecks the retained source attachment and full baseline.
        self.fx.children[self.target][-1]['paragraph']['rich_text'][0]['text']['content']+='human'
        self.assertEqual(m.recover_intent('synthetic',intent)['status'],'held')

    def test_native_reuse_rejects_different_profile_wrong_old_proof_and_old_prefix_change(self):
        request=self.native_reuse_request();state=m.load_state();intent=state['write_intents'][self.native_operation]
        wrong=copy.deepcopy(intent);wrong['context']['request']['children'][2]['quote']['rich_text'][0]['text']['content']+='changed'
        wrong['expected']=[m.block_identity(b)for b in wrong['context']['request']['children']]
        wrong['context']['render_version']='native-v1:'+m.sha256_text(json.dumps([m.block_identity(b)for b in wrong['context']['request']['children'][2:-1]]))
        self.assertFalse(m.native_legacy_equivalent_payload(wrong,self.source.read_text()))
        state['execution_request']['reused_operation_id']='f'*64;self.fx.persist(state)
        self.assertEqual(m.execute_unicode_request('synthetic',request['request_id'])['held'],1)
        state=m.load_state();state['execution_request']=request;self.fx.persist(state)
        self.fx.children[self.target][0]['paragraph']['rich_text'][0]['text']['content']+='changed-prefix'
        self.assertEqual(m.execute_unicode_request('synthetic',request['request_id'])['held'],1)
        self.assertEqual(m.load_state()['write_intents'][self.native_operation]['status'],'dispatched')

    def test_native_reuse_rechecks_asset_and_unique_old_proof_on_every_get(self):
        request=self.native_reuse_request()
        self.assertEqual(m.execute_unicode_request('synthetic',request['request_id'])['held'],0)
        intent=m.load_state()['write_intents'][self.native_operation]
        self.wrong_bytes=True
        self.assertEqual(m.recover_intent('synthetic',intent)['status'],'held')
        self.wrong_bytes=False
        changed=copy.deepcopy(intent);changed['unicode_preservation']['qualification_request']['original_request_hash']='0'*64
        self.assertEqual(m.recover_intent('synthetic',changed)['status'],'held')
        state=m.load_state();state['write_intents']['f'*64]=copy.deepcopy(state['write_intents'][self.operation]);self.fx.persist(state)
        self.assertEqual(m.recover_intent('synthetic',intent)['status'],'held')

    def test_lost_attachment_append_response_recovers_after_runner_rebuild_by_gets_only(self):
        self.lose_repair=True;result=self.repair();self.assertEqual(result['held'],1);self.assertEqual(result['attempted_batches'],1)
        state=m.load_state();self.assertEqual(state['write_intents'][self.operation]['unicode_preservation']['phase'],'dispatched')
        self.fx.state_path.write_bytes(self.fx.remote.read_bytes());sends=len(self.fx.sends);uploads=len(self.upload_calls)
        with mock.patch.object(m,'_send_once',side_effect=AssertionError('must not resend')):
            self.assertEqual(m.recover_intents('synthetic',persist=True)['held'],0)
        self.assertEqual(len(self.fx.sends),sends);self.assertEqual(len(self.upload_calls),uploads)

    def test_changed_complete_source_evidence_cannot_authorize_qualified_recovery(self):
        self.lose_repair=True;self.assertEqual(self.repair()['held'],1)
        original=m.load_state()
        for change in ['bytes','commit','path']:
            state=copy.deepcopy(original);proof=state['write_intents'][self.operation]['unicode_preservation']
            if change=='bytes':proof['original_source_base64']='Y2hhbmdlZA=='
            elif change=='commit':proof['asset']['source_commit']='0'*40
            else:proof['asset']['source_path']='Wiki/wrong.md'
            self.fx.persist(state)
            self.assertEqual(m.recover_intents('synthetic',persist=True)['held'],1)
        self.fx.persist(original)
        self.assertEqual(m.recover_intents('synthetic',persist=True)['held'],0)

    def test_upload_response_loss_uses_known_id_get_recovery_never_duplicate_posts(self):
        api=self.asset_api;lost=[False]
        def losing(*args,**kwargs):
            if args[2].endswith('/send')and not lost[0]:
                lost[0]=True;api(*args,**kwargs);raise assets.UnknownAssetWrite('asset_write_unknown')
            return api(*args,**kwargs)
        with mock.patch.object(assets,'_request',side_effect=losing):
            self.assertEqual(self.repair()['held'],1)
            posts=sum(method=='POST'for method,path in self.upload_calls)
            self.assertEqual(self.repair()['held'],0)
            self.assertEqual(sum(method=='POST'for method,path in self.upload_calls),posts)

    def test_failed_private_checkpoint_prevents_attachment_or_target_write(self):
        with mock.patch.object(m,'persist_private_state',side_effect=m.MirrorHeld('synthetic ack failed','checkpoint_publication_failed')):
            with self.assertRaises(m.MirrorHeld):self.repair()
        self.assertEqual(len(self.fx.sends),self.initial_sends);self.assertEqual(self.upload_calls,[])

    def test_wrong_attachment_bytes_holds_and_unknown_never_resends(self):
        self.wrong_bytes=True;self.assertEqual(self.repair()['held'],1)
        sends=len(self.fx.sends)
        self.assertEqual(self.repair()['held'],1)
        self.assertEqual(len(self.fx.sends),sends)
        self.wrong_bytes=False;m._ASSET_READBACK_CACHE.clear()
        self.assertEqual(m.recover_intents('synthetic',persist=True)['held'],0)

    def test_changed_source_or_style_cannot_use_the_unicode_escape_hatch(self):
        for change in ['source','style','content','extra_block']:
            with self.subTest(change=change):
                before=copy.deepcopy(self.fx.children[self.target]);source=self.source.read_bytes()
                if change=='source':self.source.write_text('changed source');self.commit_source()
                elif change=='style':self.fx.children[self.target][5]['paragraph']['color']='red'
                elif change=='content':self.fx.children[self.target][5]['paragraph']['rich_text'][0]['text']['content']+='changed'
                else:self.fx.append(self.target,[m.paragraph_block('human addition')])
                self.assertEqual(self.repair()['held'],1)
                self.assertEqual(self.upload_calls,[]);self.assertEqual(len(self.fx.sends),self.initial_sends)
                self.fx.children[self.target]=before
                if change=='source':self.source.write_bytes(source);self.commit_source()

    def test_not_all_unicode_changes_are_accepted_even_with_exact_old_hash(self):
        expected=[m.paragraph_block('a\u200b\u200bb')];record={'context':{'request':{'children':expected}},'expected':[m.block_identity(b)for b in expected]}
        self.assertTrue(m.unicode_deletion_matches(record,[m.paragraph_block('ab')]))
        for text in ['a\u200b\u200b\u200bb','a\u200cb','changed','a b']:
            self.assertFalse(m.unicode_deletion_matches(record,[m.paragraph_block(text)]))
        self.assertFalse(m.intent_expected_matches(record,[m.paragraph_block('ab')]))
        code=[m.code_block('a\u200bb','python')];code_record={'context':{'request':{'children':code}},'expected':[m.block_identity(b)for b in code]}
        self.assertFalse(m.unicode_deletion_matches(code_record,[m.code_block('ab','python')]))

    def test_unknown_upload_create_without_id_is_not_repeated(self):
        def unknown(*args,**kwargs):
            if args[1]=='POST':
                self.upload_calls.append((args[1],args[2]));raise assets.UnknownAssetWrite('asset_write_unknown')
            return self.asset_api(*args,**kwargs)
        with mock.patch.object(assets,'_request',side_effect=unknown):
            self.assertEqual(self.repair()['held'],1)
            posts=sum(method=='POST'for method,path in self.upload_calls)
            self.assertEqual(self.repair()['held'],1)
            self.assertEqual(sum(method=='POST'for method,path in self.upload_calls),posts)
            self.assertEqual(len(self.fx.sends),self.initial_sends)

    def test_fixed_pin_fetch_verifies_private_remote_and_does_not_move_head_index_or_source(self):
        original_run=subprocess.run;events=[];pin=self.original['context']['source_snapshot']
        before_head=subprocess.check_output(['git','-C',str(self.fx.root),'rev-parse','HEAD'])
        before_source=self.source.read_bytes()
        def run(argv,*args,**kwargs):
            if argv[-3:]==['cat-file','-e',pin+'^{commit}']:return subprocess.CompletedProcess(argv,1,b'',b'')
            if 'fetch'in argv:
                events.append('fetch');self.assertEqual(argv[-5:],['fetch','--no-tags','--depth=1','origin',pin])
                return subprocess.CompletedProcess(argv,0,b'',b'')
            return original_run(argv,*args,**kwargs)
        with mock.patch.object(m.subprocess,'run',side_effect=run),mock.patch.object(m,'verify_private_checkout',side_effect=lambda *a:events.append('verify')):
            self.assertEqual(self.repair()['held'],0)
        self.assertEqual(events,['verify','fetch'])
        self.assertEqual(subprocess.check_output(['git','-C',str(self.fx.root),'rev-parse','HEAD']),before_head)
        self.assertEqual(self.source.read_bytes(),before_source)
        self.assertEqual(subprocess.run(['git','-C',str(self.fx.root),'diff','--cached','--quiet']).returncode,0)

    def test_fixed_pin_fetch_cannot_bypass_private_checkout_failure(self):
        original_run=subprocess.run;pin=self.original['context']['source_snapshot'];fetches=[]
        def run(argv,*args,**kwargs):
            if argv[-3:]==['cat-file','-e',pin+'^{commit}']:return subprocess.CompletedProcess(argv,1,b'',b'')
            if 'fetch'in argv:fetches.append(argv);raise AssertionError('private validation failed')
            return original_run(argv,*args,**kwargs)
        with mock.patch.object(m.subprocess,'run',side_effect=run),mock.patch.object(m,'verify_private_checkout',side_effect=ValueError('synthetic wrong repo')):
            self.assertEqual(self.repair()['held'],1)
        self.assertEqual(fetches,[]);self.assertEqual(self.upload_calls,[])

    def test_changed_old_request_or_renderer_cannot_authorize_repair(self):
        saved=m.load_state()
        for kind in ['request','profile','nonfinal']:
            state=copy.deepcopy(saved);record=state['write_intents'][self.operation]
            if kind=='request':record['expected'][0]='0'*64
            if kind=='profile':record['context']['render_version']='native-unknown'
            if kind=='nonfinal':record['context']['final_batch']=False
            self.fx.persist(state)
            self.assertEqual(self.repair()['held'],1)
            self.assertEqual(self.upload_calls,[]);self.assertEqual(len(self.fx.sends),self.initial_sends)
        self.fx.persist(saved)

    def test_human_change_after_repair_send_stays_held_without_resending(self):
        send=m._send_once
        def concurrent(*args,**kwargs):
            result=send(*args,**kwargs)
            if args[1]=='PATCH':self.fx.children[self.target][5]['paragraph']['rich_text'][0]['text']['content']='human post-dispatch edit'
            return result
        with mock.patch.object(m,'_send_once',side_effect=concurrent):
            self.assertEqual(self.repair()['held'],1)
        sends=len(self.fx.sends)
        self.assertEqual(self.repair()['held'],1)
        self.assertEqual(len(self.fx.sends),sends)

    def test_target_change_after_prepared_checkpoint_never_appends_or_retries(self):
        persist=self.fx.persist;changed=[False]
        def concurrent(state):
            persist(state)
            proof=(state.get('write_intents',{}).get(self.operation)or{}).get('unicode_preservation')or{}
            if proof.get('phase')=='prepared'and not changed[0]:
                changed[0]=True;self.fx.append(self.target,[m.paragraph_block('new human block')])
        with mock.patch.object(m,'persist_private_state',side_effect=concurrent):
            self.assertEqual(self.repair()['held'],1)
        self.assertEqual(len(self.fx.sends),self.initial_sends)
        self.assertEqual(self.repair()['held'],1)
        self.assertEqual(len(self.fx.sends),self.initial_sends)

    def test_source_revision_after_qualified_delivery_is_not_skipped(self):
        self.assertEqual(self.repair()['held'],0)
        original_bytes=self.source.read_bytes();writes=len(self.fx.sends)
        self.source.write_text('# Actually new revision\n\nnew visible content\n');self.commit_source()
        with mock.patch.object(assets,'_download_returned_asset',return_value=original_bytes):
            self.assertEqual(m.execute_bulk('synthetic',fixtures.REQUEST)['held'],0)
            entry=m.load_state()['entries']['Wiki/a.md']
            self.assertEqual(entry['signature'],m.file_signature(self.source))
            self.assertNotIn('fidelity',entry)
            self.assertEqual(len(self.fx.sends),writes+1)

    def test_bulk_does_not_consume_explicit_preservation_request(self):
        self.assertEqual(m.execute_bulk('synthetic',fixtures.REQUEST)['held'],1)
        self.assertEqual(self.upload_calls,[]);self.assertEqual(len(self.fx.sends),self.initial_sends)
        with self.assertRaises(m.MirrorHeld):m.execute_revision('synthetic','Wiki/a.md',self.original['parent_id'])
        self.assertEqual(self.upload_calls,[])

    def test_readonly_recovery_cli_never_uploads_patches_or_persists(self):
        before=self.fx.state_path.read_bytes()
        with mock.patch.object(sys,'argv',['sync','--recover-intents']),mock.patch.object(m,'load_notion_api_key',return_value='synthetic'),mock.patch.object(m,'persist_private_state',side_effect=AssertionError('read only')),mock.patch.object(assets,'publish',side_effect=AssertionError('read only')),mock.patch.object(m,'_send_once',side_effect=AssertionError('read only')),contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(m._main(),2)
        self.assertEqual(self.fx.state_path.read_bytes(),before)
        self.assertEqual(self.upload_calls,[])
        self.lose_repair=True;self.assertEqual(self.repair()['held'],1)
        before=self.fx.state_path.read_bytes();uploads=len(self.upload_calls);sends=len(self.fx.sends)
        with mock.patch.object(sys,'argv',['sync','--recover-intents']),mock.patch.object(m,'load_notion_api_key',return_value='synthetic'),mock.patch.object(m,'persist_private_state',side_effect=AssertionError('read only')),mock.patch.object(assets,'publish',side_effect=AssertionError('read only')),mock.patch.object(m,'_send_once',side_effect=AssertionError('read only')),contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(m._main(),0)
        self.assertEqual(self.fx.state_path.read_bytes(),before)
        self.assertEqual((len(self.upload_calls),len(self.fx.sends)),(uploads,sends))

    def test_explicit_opaque_route_and_exact_target_source_request_locks(self):
        original=m.load_state()
        for field,value in [('operation_id','0'*64),('source_blob','0'*40),('source_sha256','0'*64),('source_snapshot','0'*40),('source_size',123),('target_page_id',str(uuid.uuid4())),('original_request_hash','0'*64),('render_version','changed')]:
            state=copy.deepcopy(original);state['execution_request'][field]=value;self.fx.persist(state)
            self.assertEqual(self.repair()['held'],1,field)
            self.assertEqual(self.upload_calls,[]);self.assertEqual(len(self.fx.sends),self.initial_sends)
        self.fx.persist(original)
        with mock.patch.object(sys,'argv',['sync','--execute-request',self.request_id]),mock.patch.object(m,'load_notion_api_key',return_value='synthetic'),contextlib.redirect_stdout(io.StringIO())as output:
            self.assertEqual(m._main(),0)
        self.assertEqual(json.loads(output.getvalue())['status'],'revision_verified')
        self.assertEqual(self.repair()['attempted_batches'],0)

    def test_current_native_profile_change_holds_before_upload(self):
        native=m.native_markdown_to_blocks
        with mock.patch.object(m,'native_markdown_to_blocks',side_effect=lambda text:native(text)+[m.paragraph_block('new format')]):
            self.assertEqual(self.repair()['held'],1)
        self.assertEqual(self.upload_calls,[]);self.assertEqual(len(self.fx.sends),self.initial_sends)

    def test_foreign_old_notion_key_in_manual_snapshot_is_never_published(self):
        token='ntn_'+'Q'*40
        self.fx.append(self.target,[m.paragraph_block(token)])
        self.assertEqual(self.repair()['held'],1)
        self.assertNotIn(token,self.fx.remote.read_text());self.assertEqual(self.upload_calls,[])


if __name__=='__main__':unittest.main()
