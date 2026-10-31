"""Synthetic Git assets only; no Notion, private source or network is contacted."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import unittest
import uuid
from unittest import mock
import test_wiki_notion_bulk as fixtures
m=fixtures.m
assets=fixtures.assets


class JsonAttachmentTests(unittest.TestCase):
    def setUp(self):
        self.fx=fixtures.BulkTests(methodName='test_full_hierarchy_parent_first_original_root_retained_and_noop')
        self.fx.setUp();self.addCleanup(self.fx.doCleanups)
        self.root=self.fx.root
        subprocess.run(['git','-C',str(self.root),'remote','add','origin','https://github.com/example/private-fixture.git'],capture_output=True,check=True)
        self.publications=[]
        patch=mock.patch.object(assets.private_checkout,'REPOSITORY','example/private-fixture');patch.start();self.addCleanup(patch.stop)
        patch=mock.patch.object(assets,'publish',side_effect=self.publish);patch.start();self.addCleanup(patch.stop)

    def publish(self,asset,api_key,checkpoint,prior_receipt=None):
        self.publications.append((asset.path,asset.data))
        receipt=deepcopy(prior_receipt) if prior_receipt else {'version':1,'sha256':asset.sha256,'size':len(asset.data),'content_type':asset.content_type,'filename':asset.filename,'source_commit':asset.commit,'source_path':asset.path,'source_blob':asset.blob}
        ident=str(uuid.uuid5(uuid.NAMESPACE_URL,asset.sha256))
        receipt.setdefault('sources',{}).setdefault(asset.commit,{})[asset.path]=asset.blob
        receipt.update(phase='verified',upload_id=ident)
        checkpoint(receipt)
        return {'status':'ready','reason':None,'receipt':receipt,'block':{'object':'block','type':asset.block_type,asset.block_type:{'type':'file_upload','file_upload':{'id':ident},'caption':[]}}}

    def render(self,markdown,files,relative="Wiki/a.md"):
        for file_relative,raw in files.items():
            path=self.root/file_relative;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(raw)
        source=self.root/relative;source.parent.mkdir(parents=True,exist_ok=True);source.write_text(markdown)
        for args in [('add','--all'),('-c','user.name=Synthetic','-c','user.email=synthetic@example.invalid','commit','-qm','synthetic asset boundary')]:
            subprocess.run(['git','-C',str(self.root),*args],capture_output=True,check=True)
        commit=subprocess.check_output(['git','-C',str(self.root),'rev-parse','HEAD'],text=True).strip()
        return m.render_native_source('synthetic',relative,markdown,commit)

    def test_large_direct_json_is_one_verified_original_without_bundle_read_hold(self):
        raw=json.dumps({'data':'x'*3699400}).encode()
        blocks,_,counts=self.render('[Audit](../assets/audit.json)',{'assets/audit.json':raw})
        self.assertEqual(counts,{'assets_ready':1,'assets_held':0,'assets_fallback':0})
        self.assertEqual(self.publications,[('assets/audit.json',raw)])
        self.assertEqual(sum(b['type']=='file' for b in blocks),1)

    def test_direct_json_data_strings_do_not_collect_other_existing_sources(self):
        raw=b'{"evidence":["unrelated.bin","../Schema/b.md"],"text":"ordinary data"}'
        _,_,counts=self.render('[Audit](../assets/audit.json)',{'assets/audit.json':raw,'assets/unrelated.bin':b'Unrelated original'})
        self.assertEqual(counts['assets_ready'],1)
        self.assertEqual([p for p,_ in self.publications],['assets/audit.json'])

    def test_direct_nonparseable_json_is_still_preserved_as_original_file(self):
        raw=b'Original incomplete JSON syntax {'
        _,_,counts=self.render('[Source](../assets/source.json)',{'assets/source.json':raw})
        self.assertEqual(counts['assets_held'],0);self.assertEqual(self.publications,[('assets/source.json',raw)])

    def test_html_css_explicit_json_resource_chain_keeps_true_dependencies_and_cycles_bounded(self):
        files={'assets/view.html':b'<link href="style.css">','assets/style.css':b'@import "style.css"; body {background:url(config.json)}',
               'assets/config.json':b'{"resource":"data.bin","child":"second.json"}',
               'assets/second.json':b'{"cycle":"config.json"}','assets/data.bin':b'Synthetic bundle data'}
        _,_,counts=self.render('[View](../assets/view.html)',files)
        self.assertEqual(counts['assets_ready'],5);self.assertEqual(counts['assets_held'],0)
        self.assertEqual({p for p,_ in self.publications},set(files));self.assertEqual(len(self.publications),5)

    def test_direct_json_then_html_resource_upgrades_context_without_reupload(self):
        files={'assets/view.html':b'<a href="config.json">source</a>','assets/config.json':b'{"resource":"data.bin"}','assets/data.bin':b'Bundle resource'}
        _,_,counts=self.render('[Data](../assets/config.json)\n[View](../assets/view.html)',files)
        self.assertEqual({p for p,_ in self.publications},set(files));self.assertEqual(len(self.publications),3)
        self.assertEqual(counts['assets_held'],0)

    def test_inline_html_json_resource_retains_bundle_context(self):
        files={'assets/config.json':b'{"resource":"data.bin"}','assets/data.bin':b'Inline bundle resource'}
        _,_,counts=self.render('<link href="../assets/config.json">',files)
        self.assertEqual({p for p,_ in self.publications},set(files));self.assertEqual(counts['assets_held'],0)

    def test_explicit_bundle_json_keeps_read_budget_guard(self):
        raw=json.dumps({'data':'x'*(1024*1024)}).encode()
        _,_,counts=self.render('[View](../assets/view.html)',{'assets/view.html':b'<link href="config.json">','assets/config.json':raw})
        self.assertEqual(counts['assets_ready'],2);self.assertEqual(counts['assets_held'],1)

    def test_fenced_json_link_example_never_uploads_or_rewrites_literal(self):
        markdown='```markdown\n[Data](../assets/config.json)\n```\n'
        blocks,_,counts=self.render(markdown,{'assets/config.json':b'{"resource":"data.bin"}','assets/data.bin':b'Not referenced outside code'})
        self.assertEqual(self.publications,[]);self.assertEqual(counts['assets_ready'],0);self.assertEqual(counts['assets_held'],0)
        code=''.join(run['text']['content'] for b in blocks if b['type']=='code' for run in b['code']['rich_text'])
        self.assertIn('[Data](../assets/config.json)',code)

if __name__=='__main__':unittest.main()


class NavigationTests(unittest.TestCase):
    setUp=JsonAttachmentTests.setUp
    publish=JsonAttachmentTests.publish
    render=JsonAttachmentTests.render
    # Separate fixture class without rerunning the attachment tests below.
    def aliases(self,values):
        state=m.load_state();state['bulk_request']['source_root_aliases']=values;self.fx.persist(state)

    def links(self,blocks):
        import urllib.parse
        return [urllib.parse.unquote(part['text']['link']['url']) for b in blocks for part in b.get(b['type'],{}).get('rich_text',[]) if part.get('text',{}).get('link')]

    def test_wiki_root_relative_nested_reference_hits_exact_tree(self):
        blocks,_,counts=self.render('[[04 Projects/T/Target]]',{'Wiki/04 Projects/T/Target.md':b'Target'},relative='Wiki/01 People/Source.md')
        self.assertEqual(counts['assets_held'],0);self.assertTrue(any(url.endswith('/Wiki/04 Projects/T/Target.md') for url in self.links(blocks)))

    def test_wiki_root_relative_explicit_extension_is_supported(self):
        blocks,_,counts=self.render('[Target](04%20Projects/T/Target.md)',{'Wiki/04 Projects/T/Target.md':b'Target'})
        self.assertEqual(counts['assets_held'],0);self.assertTrue(self.links(blocks))

    def test_original_source_relative_match_wins_over_new_wiki_root_fallback(self):
        blocks,_,counts=self.render('[[04 Projects/T/Target]]',{'Wiki/Here/04 Projects/T/Target.md':b'Local target','Wiki/04 Projects/T/Target.md':b'Root target'},relative='Wiki/Here/Source.md')
        self.assertEqual(counts['assets_held'],0);self.assertTrue(any('/Wiki/Here/04 Projects/' in url for url in self.links(blocks)))

    def test_explicit_repository_root_match_wins(self):
        blocks,_,counts=self.render('[[Schema/Target]]',{'Schema/Target.md':b'Explicit root','Wiki/Schema/Target.md':b'Wiki fallback'},relative='Wiki/Deep/Source.md')
        self.assertEqual(counts['assets_held'],0);self.assertTrue(any(url.endswith('/Schema/Target.md') and '/Wiki/Schema/' not in url for url in self.links(blocks)))

    def test_new_wiki_root_fallback_does_not_apply_to_non_wiki_source(self):
        _,_,counts=self.render('[[04 Projects/T/Target]]',{'Wiki/04 Projects/T/Target.md':b'Target'},relative='Schema/Source.md')
        self.assertEqual(counts['assets_held'],1)

    def test_ambiguous_exact_wiki_root_candidates_remain_held(self):
        _,_,counts=self.render('[[04 Projects/T/Target]]',{'Wiki/04 Projects/T/Target':b'No suffix','Wiki/04 Projects/T/Target.md':b'Markdown'},relative='Wiki/Deep/Source.md')
        self.assertEqual(counts['assets_held'],1)

    def test_missing_navigation_is_not_guessed_or_uploaded(self):
        _,_,counts=self.render('[[04 Projects/Missing]]',{'Wiki/04 Projects/Other.md':b'Other'})
        self.assertEqual(counts['assets_held'],1);self.assertEqual(self.publications,[])

    def test_allowlisted_old_root_maps_only_exact_existing_git_path(self):
        self.aliases(['/synthetic/original-vault/'])
        blocks,_,counts=self.render('[Target](</synthetic/original-vault/project/05 Project/Target.md>)',{'project/05 Project/Target.md':b'Target'})
        self.assertEqual(counts['assets_held'],0);self.assertTrue(any(url.endswith('/project/05 Project/Target.md') for url in self.links(blocks)))

    def test_unconfigured_or_unknown_absolute_prefix_remains_held(self):
        self.aliases(['/synthetic/allowed/'])
        _,_,counts=self.render('[A](/synthetic/unknown/Target.md) [B](/synthetic/allowed-sibling/Target.md)',{'Target.md':b'Target'})
        self.assertEqual(counts['assets_held'],2);self.assertEqual(self.publications,[])

    def test_allowlisted_absolute_untracked_disk_file_is_not_a_source(self):
        self.aliases(['/synthetic/original-vault/'])
        (self.root/'untracked.bin').write_bytes(b'Never import this disk file')
        # Exclude the untracked file from the synthetic Git snapshot.
        subprocess.run(['git','-C',str(self.root),'config','user.name','Synthetic'],capture_output=True,check=True)
        subprocess.run(['git','-C',str(self.root),'config','user.email','synthetic@example.invalid'],capture_output=True,check=True)
        commit=subprocess.check_output(['git','-C',str(self.root),'rev-parse','HEAD'],text=True).strip()
        _,_,counts=m.render_native_source('synthetic','Wiki/a.md','[File](/synthetic/original-vault/untracked.bin)',commit)
        self.assertEqual(counts['assets_held'],1);self.assertEqual(self.publications,[])

    def test_parent_traversal_encoded_escape_and_backslash_do_not_map(self):
        self.aliases(['/synthetic/original-vault/'])
        markdown='[A](/synthetic/original-vault/../Target.md) [B](/synthetic/original-vault/%2e%2e/Target.md) [C](/synthetic/original-vault/folder%5cTarget.md)'
        _,_,counts=self.render(markdown,{'Target.md':b'Target'})
        self.assertEqual(counts['assets_held'],3);self.assertEqual(self.publications,[])

    def test_overlapping_aliases_with_two_existing_candidates_are_held(self):
        self.aliases(['/synthetic/original-vault/','/synthetic/original-vault/nested/'])
        _,_,counts=self.render('[Target](/synthetic/original-vault/nested/Target.md)',{'nested/Target.md':b'A','Target.md':b'B'})
        self.assertEqual(counts['assets_held'],1)

    def test_invalid_alias_configuration_fails_closed(self):
        for aliases in ['not-a-list',['/'],['relative/'],['/synthetic/../'],['/synthetic//root/'],['/synthetic/root']]:
            self.aliases(aliases)
            with self.assertRaisesRegex(m.MirrorHeld,'^source root alias configuration invalid$'):
                m.render_native_source('synthetic','Wiki/a.md','[Target](/synthetic/root/Target.md)',self.fx.snapshot)
