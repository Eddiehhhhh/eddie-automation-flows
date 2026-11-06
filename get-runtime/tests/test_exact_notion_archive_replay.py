import os
import contextlib
import io
import json
import subprocess
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from test_notion_single_id_input import runtime, bindings


def setUpModule():
    global replay, modules
    modules = patch.dict(sys.modules)
    modules.start()
    for key in list(sys.modules):
        if key.startswith("get_"): del sys.modules[key]
    sys.modules["get_private_runtime_bindings"] = bindings
    import get_notion_exact_replay as module
    replay = module


def tearDownModule():
    modules.stop()


class ExactArchiveReplay(unittest.TestCase):
    def setUp(self):
        self.nid = '1234567890123456789'
        self.b = {'selection': {'run_id': 123, 'run_attempt': 1, 'receipt_commit':'a'*40,
            'receipt_sha256':'a'*64, 'record_sha256':'c'*64}, 'sequence':3, 'job_id':101,
            'source_policy': {'get_policy_sha256':'b'*64}, 'source_policy_sha256':'c'*64,
            'parent':'d'*40, 'previous_commit':'e'*40, 'previous_sha256':'f'*64, 'note_id':self.nid}
        self.ctx = {'run_id':456}
        self.proof = {'record':{'main_updated':True, 'cas_attempts':1,
            'observed_main_commit':'a'*40, 'receipt_commit':'a'*40, 'receipt_sha256':'a'*64, 'receipt_sequence':3}, 'source_job_id':101, 'notion_outcome':'failure'}

    def test_distinct_get_origin_and_notion_writer(self):
        with patch.object(replay.replay, 'verify', return_value=self.proof) as verifier:
            self.assertEqual(replay.origin(object(), self.ctx, self.b), {
                'commit':'a'*40, 'receipt_sha256':'a'*64, 'sequence':3})
            self.assertEqual(verifier.call_args.args[1:],
                (self.b['source_policy'], self.b['source_policy_sha256'], self.b['selection']))
            with self.assertRaises(ValueError):
                replay.origin(object(), {'run_id':123}, self.b)
            for key, value in [('source_job_id',102),('notion_outcome','success')]:
                with patch.object(replay.replay,'verify',return_value={**self.proof,key:value}), self.assertRaises(ValueError):
                    replay.origin(object(), self.ctx, self.b)

    def test_receipt_scope_binds_exact_origin_and_changed_id(self):
        values = {'parent_commit':self.b['parent'], 'previous_receipt_commit':self.b['previous_commit'],
            'previous_receipt_sha256':self.b['previous_sha256'], 'changed_content_ids':[self.nid],
            'sequence':3, 'source_scope':'single_note', 'policy_sha256':'b'*64}
        self.assertTrue(replay.receipt_matches('a'*40,'a'*64,values,self.b))
        for key, wrong in [('changed_content_ids', ['987']), ('source_scope','complete_catalog'),
                           ('sequence',2), ('previous_receipt_sha256','0'*64)]:
            self.assertFalse(replay.receipt_matches('a'*40,'a'*64,{**values,key:wrong},self.b))
        self.assertFalse(replay.receipt_matches('0'*40,'a'*64,values,self.b))
        self.assertFalse(replay.receipt_matches('a'*40,'0'*64,values,self.b))

    def test_rechecks_native_target_and_retains_uncertain_rows(self):
        state = {'engine_state':{'unresolved_by_id':{'hold':'unchanged'},'journal':None,
                                 'receipts':{'other':{'data_source_id':'source'}}}}
        target = {'complete':True,'identity_query_complete':True,'exact_id_matches':[]}
        with patch.object(replay.bindings,'value',return_value={'hold':'unchanged'}), \
             patch('get_notion_live_transport.API') as api:
            snapshot = api.return_value.read_note
            snapshot.return_value = target
            replay.target_guard(state, object(), self.b, "e"*40)
            snapshot.assert_called_once_with(self.nid)
            for matches in (['page'],['page','dup']):
                snapshot.return_value = {**target,'exact_id_matches':matches}
                with self.assertRaises(ValueError): replay.target_guard(state, object(), self.b, "e"*40)
            receipt = {'data_source_id':'source','page_id':'page','source_commit':'a'*40,'md_blob':'e'*40}
            state['engine_state']['receipts'][self.nid] = {**receipt,'properties_baseline':{'title':{'type':'title'}},'managed_blocks':[{'id':'block','normalized_hash':'good'}]}
            state['engine_state']['property_receipts'] = {self.nid: {**receipt,'readback_verified':True,'held':{},'managed':{'name':'correct'}}}
            snapshot.return_value = {**target,'exact_id_matches':['page']}
            view = {'order':['block'],'nodes':{'block':{}},'properties':{'name':{},'title':{'type':'title'}}}
            with patch.object(replay.e.properties,'FIELDS',{'name':None}), patch.object(replay.e,'target',return_value=view), patch.object(replay.e.p,'block_hash',return_value='good') as block, patch.object(replay.e.properties,'value',return_value='correct') as props:
                replay.target_guard(state, object(), self.b, 'e'*40)
                with patch.dict(replay.e.properties.FIELDS, {'missing':None}):
                    with self.assertRaisesRegex(ValueError,'readback_mismatch'):
                        replay.target_guard(state, object(), self.b, 'e'*40, final=True)
                with patch.dict(view['properties'], {'title':{'type':'title','title':['edited']}}):
                    with self.assertRaisesRegex(ValueError,'readback_mismatch'):
                        replay.target_guard(state, object(), self.b, 'e'*40, final=True)
                props.return_value = 'manual edit'
                with self.assertRaisesRegex(ValueError,'readback_mismatch'):
                    replay.target_guard(state, object(), self.b, 'e'*40, final=True)
                props.return_value = 'correct'; block.return_value = 'changed'
                with self.assertRaisesRegex(ValueError,'readback_mismatch'):
                    replay.target_guard(state, object(), self.b, 'e'*40, final=True)
            snapshot.return_value = target
            with self.assertRaises(ValueError): replay.target_guard(state, object(), self.b, "e"*40)
            with self.assertRaises(ValueError): replay.target_guard({'engine_state':{**state['engine_state'],'unresolved_by_id':{}}}, object(), self.b,'e'*40)

    def test_original_controller_route_and_journal_replay_marker(self):
        import get_notion_controller_route as route
        import get_notion_fixed_source as fixed
        import get_notion_route_selection as plain
        env={'APPLY':'true','NOTION_MODE':'historical-apply','NOTION_INPUT_SHA256':replay.SELECTION,
             'GITHUB_EVENT_NAME':'workflow_dispatch','GET_SINGLE_NOTE_MODE':'false','GET_SINGLE_NOTE_ID':''}
        args=SimpleNamespace(fixed_source=True,publication_file=None,apply=True,scope_from_policy=True,policy=Path('/policy'),approved_policy_sha256='a'*64)
        ctx={'run_id':456,'run_attempt':1,'code_sha':'a'*40,'controller_sha':'b'*40,
             'rules_sha256':'c'*64,'notion_policy_sha256':'d'*64,'public_policy_sha256':'e'*64}
        with patch.dict(os.environ,env), patch.object(replay,'binding',return_value=self.b), \
             patch('get_notion_runtime.main',return_value=0) as run:
            self.assertTrue(plain.selection())
            selected=route.selection()
            self.assertEqual(selected,('historical-apply',replay.SELECTION,replay.CONTRACT))
            self.assertEqual(route.dispatch(*selected,args),0)
            self.assertIn('--exact-archive-replay',run.call_args.args[0])
            self.assertNotIn('--publication-file',run.call_args.args[0])
            marker=fixed.record(ctx,selected[0],selected[1])
            self.assertEqual(fixed.parse(runtime.e.encoded(marker),ctx,{'event':'workflow_dispatch'}),marker)
            with self.assertRaises(ValueError): fixed.parse(runtime.e.encoded(marker),{**ctx,'run_id':123},{'event':'workflow_dispatch'})
            with patch.dict(os.environ,{'GET_SINGLE_NOTE_ID':'999'}), self.assertRaises(ValueError):
                replay.admission()
            args.fixed_source=False
            with self.assertRaises(ValueError): route.dispatch(*selected,args)

    def test_all_mainline_and_prehistory_fork_trees_proven_with_real_git(self):
        path='state.json'
        with tempfile.TemporaryDirectory() as d:
            root=Path(d).resolve()
            class Git:
                vault=root
                def run(self,*args):
                    return subprocess.check_output(['git','-C',str(root),*args],stderr=subprocess.DEVNULL)
            git=Git()
            git.run('init','-q','-b','main')
            git.run('config','user.name','test')
            git.run('config','user.email','test@example.invalid')
            def commit(file,body):
                (root/file).write_text(body)
                git.run('add',file);git.run('commit','-qm','fixture')
                return git.run('rev-parse','HEAD').decode().strip()
            base=commit('readme','one')
            old=commit(path,'authorized')
            anchor={'commit':old,'state_blob':git.run('rev-parse',old+':'+path).decode().strip()}
            git.run('checkout','-qb','older-fork',base)
            commit('older','two')
            git.run('checkout','-q','main')
            git.run('merge','--no-ff','-qm','merge older fork','older-fork')
            good=git.run('rev-parse','HEAD').decode().strip()
            replay.unchanged_journal(git,anchor,good,path)
            commit(path,'temporary edit')
            reverted=commit(path,'authorized')
            with self.assertRaises(ValueError):replay.unchanged_journal(git,anchor,reverted,path)
            git.run('checkout','-qb','bad-fork',base)
            commit(path,'foreign journal')
            git.run('rm','-q',path);git.run('commit','-qm','remove foreign')
            git.run('checkout','-qb','another-main',old)
            git.run('merge','--no-ff','-qm','merge changed fork','bad-fork')
            bad=git.run('rev-parse','HEAD').decode().strip()
            with self.assertRaises(ValueError):replay.unchanged_journal(git,anchor,bad,path)

    def test_shallow_journal_fork_rehydrates_but_keeps_change_and_span_gates(self):
        import get_confirmed_source as source
        def git(root, *args, data=None):
            return subprocess.check_output(['git','-C',str(root),*args],input=data,stderr=subprocess.DEVNULL)
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder).resolve()
            origin=root/'repo.git'
            git(root,'init','--bare','-q',str(origin))
            for key in ('uploadpack.allowFilter','uploadpack.allowAnySHA1InWant'):
                git(origin,'config',key,'true')
            commands=[]
            def commit(ref,parent=None,*,merge=None,body=None):
                mark=len(commands)+1
                commands.append(f'commit refs/heads/{ref}\nmark :{mark}\ncommitter Test <test@example.invalid> {mark} +0000\ndata 1\nx\n'
                    +(f'from :{parent}\n' if parent else '')+(f'merge :{merge}\n' if merge else '')
                    +(f'M 100644 inline state.json\ndata {len(body)}\n{body}\n' if body else '')+'\n')
                return mark
            base=None
            for _ in range(560):base=commit('early',base)
            side=commit('side',base)
            main=base
            for _ in range(600):main=commit('old',main)
            anchor_mark=commit('anchor',main,body='approved')
            good=commit('good',commit('good',anchor_mark),merge=side)
            dirty=commit('bad',good,body='foreign')
            commit('bad',dirty,body='approved')
            long=good
            for _ in range(513):long=commit('long',long)
            git(origin,'fast-import','--quiet',data=''.join(commands).encode())
            anchor={'commit':git(origin,'rev-parse','anchor').decode().strip(),
                    'state_blob':git(origin,'rev-parse','anchor:state.json').decode().strip()}
            for ref,allowed in (('good',True),('bad',False),('long',False)):
                with self.subTest(ref=ref):
                    dest=root/ref
                    git(root,'init','-q',str(dest))
                    git(dest,'remote','add','origin',origin.as_uri())
                    fetches=[]
                    class Local:
                        vault=dest
                        def run(self,*args):
                            if args[0]=='fetch':fetches.append(args)
                            return git(dest,'-c','protocol.file.allow=always',*args)
                    local=Local()
                    tip=git(origin,'rev-parse',ref).decode().strip()
                    for depth,oid in ((2,tip),(512,tip),(512,anchor['commit']),(512,tip)):
                        local.run('fetch','--depth='+str(depth),'--filter=blob:none','--no-tags',
                                  '--no-write-fetch-head','origin',oid)
                    self.assertTrue(all(local.run('cat-file','-t',c)==b'commit\n' for c in (anchor['commit'],tip)))
                    with self.assertRaisesRegex(ValueError,'approval_missing'):
                        replay.unchanged_journal(local,anchor,tip,'state.json')
                    if allowed:
                        failed=Mock(side_effect=RuntimeError('fixture_fetch_failed'))
                        with self.assertRaisesRegex(RuntimeError,'fixture_fetch_failed'):
                            replay.unchanged_journal(local,anchor,tip,'state.json',hydrate_fixed_history=failed)
                        failed.assert_called_once_with(tip)
                        unchanged=Mock()
                        with self.assertRaisesRegex(ValueError,'approval_missing'):
                            replay.unchanged_journal(local,anchor,tip,'state.json',hydrate_fixed_history=unchanged)
                        unchanged.assert_called_once_with(tip)
                    fetches.clear()
                    loader=object.__new__(source.Loader)
                    loader.local=local;loader.fetches=0
                    def prove():
                        replay.unchanged_journal(local,anchor,tip,'state.json',hydrate_fixed_history=loader.fetch)
                    if allowed:prove()
                    else:
                        with self.assertRaisesRegex(ValueError,'approval_missing'):prove()
                    self.assertEqual(loader.fetches,1)
                    self.assertEqual(len(fetches),1)
                    self.assertEqual(fetches[0][-2:],('origin',tip))
                    self.assertIn('--depth=8194',fetches[0])
                    self.assertIn('--filter=blob:none',fetches[0])
                    if ref=='long':
                        self.assertFalse((dest/'.git/shallow').exists())
                        unused=Mock()
                        with self.assertRaisesRegex(ValueError,'approval_missing'):
                            replay.unchanged_journal(local,anchor,tip,'state.json',hydrate_fixed_history=unused)
                        unused.assert_not_called()
                    self.assertNotIn(b'blob',local.run('cat-file','--batch-all-objects','--batch-check=%(objecttype)').splitlines())

    def test_registered_interval_reconnects_two_shallow_endpoint_slices(self):
        import get_confirmed_source as source
        def git(root, *args):
            return subprocess.check_output(['git', '-C', str(root), *args], stderr=subprocess.DEVNULL)
        with tempfile.TemporaryDirectory() as folder:
            origin=Path(folder).resolve()/'original'
            origin.mkdir()
            git(origin, 'init', '-q', '-b', 'main')
            git(origin, 'config', 'user.name', 'fixture')
            git(origin, 'config', 'user.email', 'fixture@example.invalid')
            path=origin/'Raw'/'03 Get'/'source.md'
            path.parent.mkdir(parents=True)
            for i in range(7):
                path.write_text('unchanged' if i!=3 else 'temporary foreign edit')
                (origin/'other').write_text(str(i))
                git(origin, 'add', '.')
                git(origin, 'commit', '-qm', 'fixture')
                if i == 0: ancestor=git(origin,'rev-parse','HEAD').decode().strip()
            descendant=git(origin,'rev-parse','HEAD').decode().strip()
            dest=origin.parent/'receiver'
            git(origin, 'clone', '--depth=2', '--no-checkout', origin.as_uri(), str(dest))
            git(dest,'fetch','--depth=1','origin',ancestor)
            run=lambda *args:git(dest,*args)
            reader=object.__new__(source.entry.a.OfflineGit)
            reader.local=SimpleNamespace(run=run)
            reader.cache={}
            loader=object.__new__(source.Loader)
            loader.local=SimpleNamespace(vault=dest,run=run)
            loader.reader=reader
            loader.commit=lambda c:run('cat-file','-t',c)
            loader.fetches=0
            self.assertNotIn(ancestor.encode(),run('rev-list','--first-parent',descendant).splitlines())
            with self.assertRaisesRegex(source.entry.EntryFailure,'entry_foreign_get_change'):
                loader._registered_interval(ancestor,descendant)
            self.assertEqual(loader.fetches,1)
            self.assertIn(ancestor.encode(),run('rev-list','--first-parent',descendant).splitlines())
            good=run('rev-parse',descendant+'~2').decode().strip()
            loader._registered_interval(good,descendant)
            self.assertEqual(loader.fetches,1)

    def test_workflow_cli_empty_single_id_reaches_current_writer_verification(self):
        import get_notion_controller_route as route
        import get_notion_runtime as receiver
        policy={'workflow_path':'.github/workflows/example.yml','head_branch':'main'}
        env={'APPLY':'true','NOTION_MODE':'historical-apply','NOTION_INPUT_SHA256':replay.SELECTION,
             'GET_SINGLE_NOTE_MODE':'false','GET_SINGLE_NOTE_ID':'','GITHUB_EVENT_NAME':'workflow_dispatch',
             'GITHUB_REPOSITORY':receiver.provenance.REPOSITORY,'GITHUB_REF':'refs/heads/main',
             'GITHUB_WORKFLOW_REF':receiver.provenance.REPOSITORY+'/'+policy['workflow_path']+'@refs/heads/main',
             'GITHUB_RUN_ID':'456','GITHUB_RUN_ATTEMPT':'1','GITHUB_SHA':'a'*40,
             'NOTION_API_KEY':'fixture','GITHUB_TOKEN':'fixture','PRIVATE_VAULT_TOKEN':'fixture'}
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'policy.json';path.write_text(json.dumps(policy))
            args=['--policy',str(path),'--approved-policy-sha256','a'*64,'--fixed-source','--apply','--scope-from-policy']
            with patch.dict(os.environ,env), patch.object(replay,'binding',return_value=self.b), \
                 patch.object(receiver.recurring,'executed_code_sha',return_value='b'*40), \
                 patch.object(receiver.recurring,'validate_policy',return_value=policy), \
                 patch.object(receiver.recurring,'context'), \
                 patch.object(receiver.provenance,'verify_execution') as verify, \
                 patch.object(receiver.old_writers.WriterGuard,'__call__',side_effect=ValueError('writer_gate_unknown')) as guard, \
                 contextlib.redirect_stdout(io.StringIO()) as log:
                self.assertEqual(route.main(args),1)
                result=json.loads(log.getvalue())
                self.assertEqual((result['stage'],result['reason']),('writers','writer_gate_unknown'))
                verify.assert_called_once();guard.assert_called_once()
                with patch.dict(os.environ,{'GET_SINGLE_NOTE_ID':self.nid}):
                    self.assertEqual(route.main(args),1)
                with patch.dict(os.environ,{'GET_SINGLE_NOTE_MODE':'true','GET_SINGLE_NOTE_ID':''}):
                    self.assertEqual(route.main(args),1)
                self.assertEqual((verify.call_count,guard.call_count),(1,1))

    def test_runtime_verifies_new_writer_and_original_get_separately(self):
        import get_notion_runtime as receiver
        policy={'rules_sha256':'a'*64}
        current={'run_id':456,'code_sha':'f'*40}
        with patch.dict(os.environ,{'GITHUB_EVENT_NAME':'workflow_dispatch'}), \
             patch.object(replay,'admission',return_value=self.b), \
             patch.object(receiver.recurring,'validate_policy',return_value=policy), \
             patch.object(receiver.recurring,'context') as context, \
             patch.object(receiver.provenance,'verify_execution') as writer, \
             patch.object(receiver.provenance,'parse_record',side_effect=AssertionError('wrong source identity')), \
             patch.object(receiver.m,'Remote'), patch.object(receiver.m,'guard',return_value='0'*40), \
             patch.object(replay,'origin',side_effect=ValueError('approval_missing')) as origin:
            client=object()
            result=receiver.run('synthetic',None,lambda:True,apply=True,execution=current,
                public_policy=policy,public_client=client,checkpoint_stream=object(),exact_archive_replay=True)
            self.assertEqual((result['stage'],result['reason']),('source','approval_missing'))
            context.assert_called_once_with(policy,current)
            writer.assert_called_once_with(client,policy,current,'manual')
            origin.assert_called_once_with(client,current,self.b)

    def test_runtime_cannot_combine_replay_with_direct_source_selection(self):
        import get_notion_runtime as receiver
        for extra in ({'fixed_source':True},{'publication_bytes':b'faked'},{'single_note_id':self.nid},
                      {'property_manifest':{}},{'historical_capture':{}},{'historical_manifest':{}}):
            with self.subTest(extra=extra), patch.object(replay,'admission') as admission:
                result=receiver.run(None,None,None,apply=True,exact_archive_replay=True,**extra)
                self.assertEqual((result['stage'],result['reason']),('input','notion_scope'))
                admission.assert_not_called()
