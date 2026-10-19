"""Native PNG readback joins only after a complete batched new page."""
from copy import deepcopy
import unittest
from urllib.parse import urlsplit, parse_qs
from unittest.mock import patch
import get_notion_executor as e
import get_notion_pipeline as pipeline
import get_notion_create_batches as batches
import get_notion_png_runtime as runtime
import get_notion_png_transport as multipart
import get_notion_uploaded_png as image
import test_notion_executor as notes
import test_png_pipeline as png_fixture


class Tests(unittest.TestCase):
    def fixture(self, image_late=False):
        fixture=png_fixture.Tests(methodName='test_real_wire_native_bytes_create_and_second_zero_write')
        fixture.setUp();self.addCleanup(fixture.doCleanups)
        original=e.p.render_markdown
        def render(*args,**kwargs):
            rows,bindings,warnings=original(*args,**kwargs)
            if args[1]==notes.N:
                root=next(row for row in rows if row['type']=='image')
                rows=[e.p.block('paragraph',{'rich_text':[e.p.text_run('Synthetic paragraph %03d'%i)]}) for i in range(268)]
                rows[130 if image_late else 3]=root
            return rows,bindings,warnings
        patcher=patch.object(e.p,'render_markdown',side_effect=render);patcher.start();self.addCleanup(patcher.stop)
        def before(kind):
            state,_=fixture.store.read();op=state['journal']['operation']
            assert op['kind']=='create_batches' and op['phase']=='in_flight'
            assert kind==('create' if op['batch_index']==0 else 'append')
            fixture.api.calls.append((kind,))
            if fixture.api.hook:fixture.api.hook(kind)
            if fixture.api.throw==kind+'_before':raise TimeoutError('synthetic interruption')
        fixture.api.before=before
        def append(page,blocks,after):
            before('append')
            snapshot=next(s for s in fixture.api.pages.values() if s['page_id']==page)
            added,roots=fixture.api.rows(blocks,page);rows=snapshot['recursive_blocks']
            position=next(i for i,r in enumerate(rows) if r['id']==after)+1 if after else 0
            rows[position:position]=added;fixture.api.edited(snapshot)
            if fixture.api.throw=='append_after':raise TimeoutError('synthetic lost append response')
            return roots
        fixture.api.append_blocks=append
        class PaginatedTransport(png_fixture.Transport):
            def __call__(self,method,path,body=None):
                parsed=urlsplit(path)
                if method=='GET' and parsed.path.startswith('/blocks/') and parsed.path.endswith('/children'):
                    ident=parsed.path.split('/')[2]
                    for snapshot in self.api.pages.values():
                        rows=[self.block(r,snapshot['page_id']) for r in snapshot['recursive_blocks'] if r['parent_id']==ident]
                        if rows or ident==snapshot['page_id']:
                            self.calls.append((method,path))
                            offset=int(parse_qs(parsed.query).get('start_cursor',['0'])[0]);end=offset+100
                            return {'object':'list','results':rows[offset:end],'has_more':end<len(rows),
                                    'next_cursor':str(end) if end<len(rows) else None}
                return super().__call__(method,path,body)
        fixture.transport=PaginatedTransport(fixture.api,fixture.g.images[notes.N])
        fixture.adapter.request=fixture.transport
        return fixture

    def session(self,fixture):
        return runtime.Session(fixture.ev,fixture.pub,fixture.outer,fixture.transport,fixture.policy,e.digest(fixture.policy),
            current_commit=lambda:fixture.g.asset_commit,asset_guard=lambda:True,writers_guard=lambda:deepcopy(fixture.writers))

    def test_image_first_prefix_verified_before_final_atomic_attachment(self):
        fixture=self.fixture()
        with patch.object(batches,'resources',side_effect=lambda api,store,value,row,index:index==0):
            result=fixture.run_it(apply=True)
        self.assertEqual(result['execution']['reason'],'batch_boundary_yield',result)
        before=deepcopy(fixture.outer.value);op=before['engine_state']['journal']['operation']
        self.assertEqual(op['phase'],'verified');self.assertEqual(len(fixture.api.pages[notes.N]['recursive_blocks']),100)
        self.assertNotIn(notes.N,before['engine_state']['receipts'])
        self.assertNotIn(notes.N,before['png_assets']['attachments'])
        self.assertIn(notes.N,before['pending_by_id'])
        self.assertGreater(fixture.adapter.png_downloads,0)
        with patch.object(multipart,'Transport',side_effect=AssertionError('already attached image needs native readback, not upload expiry GET')):
            self.session(fixture).verify_unattached()
        fixture.adapter=image.SnapshotAdapter(fixture.resolver,fixture.outer.read,request=fixture.transport,
            asset_get=lambda _:fixture.g.images[notes.N])
        resumed=fixture.run_it(apply=True)
        self.assertEqual(resumed['execution']['created'],1,resumed)
        after=fixture.outer.value
        self.assertEqual(after['engine_state']['receipts'][notes.N]['page_id'],op['page_id'])
        self.assertEqual(len(after['engine_state']['receipts'][notes.N]['managed_blocks']),268)
        self.assertIn(notes.N,after['png_assets']['attachments']);self.assertNotIn(notes.N,after['pending_by_id'])
        self.assertEqual([c for c in fixture.api.calls if c[0]=='create'],[('create',)])
        self.assertEqual([c for c in fixture.api.calls if c[0]=='append'],[('append',),('append',)])

    def test_prefix_before_image_needs_no_candidate_then_binds_later(self):
        fixture=self.fixture(image_late=True)
        with patch.object(batches,'resources',side_effect=lambda api,store,value,row,index:index==0):
            result=fixture.run_it(apply=True)
        self.assertEqual(result['execution']['reason'],'batch_boundary_yield',result)
        self.assertEqual(fixture.adapter.png_downloads,0);self.assertEqual(fixture.adapter.candidates,{})
        resumed=fixture.run_it(apply=True)
        self.assertEqual(resumed['execution']['created'],1,resumed)
        self.assertGreater(fixture.adapter.png_downloads,0)
        self.assertIn(notes.N,fixture.outer.value['png_assets']['attachments'])

    def test_known_page_uncertain_append_readback_never_duplicates(self):
        fixture=self.fixture();fixture.api.throw='append_after'
        first=fixture.run_it(apply=True)
        state=fixture.outer.value['engine_state'];op=state['journal']['operation']
        self.assertEqual((op['batch_index'],op['phase']),(1,'in_flight'),first)
        self.assertNotIn(notes.N,state['receipts'])
        fixture.api.throw=None
        resumed=fixture.run_it(apply=True)
        self.assertEqual(resumed['execution']['created'],1,resumed)
        self.assertEqual(len([c for c in fixture.api.calls if c[0]=='create']),1)
        self.assertEqual(len([c for c in fixture.api.calls if c[0]=='append']),2)

    def test_unknown_first_create_with_expired_png_and_exact_recovery_stays_held(self):
        fixture=self.fixture();fixture.api.throw='create_after'
        first=fixture.run_it(apply=True);fixture.api.throw=None
        self.assertEqual(first['execution']['reason'],'uncertain_create')
        before=deepcopy(fixture.outer.value);journal=before['engine_state']['journal']
        operation=journal['operation'];plan=journal['plan']
        self.assertEqual((operation['batch_index'],operation['phase'],operation['page_id']),(0,'in_flight',None))
        self.assertEqual(len(fixture.api.pages[notes.N]['recursive_blocks']),100)
        # Model separately approved exact-page evidence. The remote first
        # prefix already contains the image, despite the lost create response.
        approved_snapshot=deepcopy(fixture.api.pages[notes.N])
        value,_=fixture.resolver.receipts[notes.N]
        for node in approved_snapshot['recursive_blocks']:
            if node['type']=='image':node['payload']=image.semantic(value,node['payload'].get('caption',[]))
        recovery={'plan_sha256':e.digest(plan),'note_id':notes.N,
            'page_id':approved_snapshot['page_id'],'snapshot_sha256':e.target_sha(approved_snapshot,notes.N,notes.DS)}
        upload_requests=[]
        class ExpiredTransport:
            def __init__(self,base,plan,approved,data,store,**kwargs):self.plan=plan;self.data=data
            def __call__(self,request):
                if request['method']!='GET':raise AssertionError('recovery must never upload again')
                upload_requests.append(request['method'])
                return 200,{'object':'file_upload','id':'a'*32,'status':'expired',
                    'filename':self.plan['filename'],'content_type':'image/png','content_length':len(self.data),
                    'expiry_time':'2000-01-01T00:00:00Z'}
        session=self.session(fixture)
        with patch.object(multipart,'Transport',ExpiredTransport):
            result=pipeline.run(fixture.pub,fixture.store,fixture.transport,fixture.policy,e.digest(fixture.policy),
                apply=True,writers_guard=session.verify_unattached,create_recovery=recovery,
                approved_recovery_sha=e.digest(recovery),snapshot_adapter=fixture.adapter,reviewed_png=fixture.resolver)
        self.assertEqual(result['status'],'failed');self.assertEqual(result['stage'],'writers')
        self.assertEqual(upload_requests,['GET']);self.assertEqual(fixture.outer.value,before)
        # The PNG adapter also refuses to adopt an unknown page even when the
        # generic pipeline is given the exact recovery. This is an intentional
        # hold, not supported automatic PNG recovery or permission to recreate.
        direct=fixture.run_it(apply=True,create_recovery=recovery,approved_recovery_sha=e.digest(recovery))
        # The omitted planning read was unused; the same adapter refusal now
        # occurs in the executor's required fresh uncertain-outcome read.
        self.assertEqual(direct['status'],'executed')
        self.assertEqual(direct['execution']['status'],'blocked')
        self.assertEqual(direct['execution']['reason'],'transport_read_failed')
        self.assertEqual(direct['external_write_attempts'],0)
        self.assertEqual(fixture.outer.value,before)
        self.assertEqual([c for c in fixture.api.calls if c[0]=='create'],[('create',)])
        self.assertEqual([c for c in fixture.api.calls if c[0]=='append'],[])
        after=fixture.outer.value
        self.assertEqual(after['engine_state']['journal'],journal)
        self.assertIsNotNone(after['engine_state']['journal'])
        self.assertNotIn(notes.N,after['engine_state']['receipts'])
        self.assertNotIn(notes.N,after['png_assets']['attachments'])
        self.assertIn(notes.N,after['pending_by_id'])


if __name__=='__main__':unittest.main()
