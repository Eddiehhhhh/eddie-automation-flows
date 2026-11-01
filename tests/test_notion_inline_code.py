"""Inline examples stay literal through source linking and native rich-text output."""
import unittest
import subprocess
import test_notion_json_attachments as assets_fixture
m=assets_fixture.m


def content(blocks):
    return ''.join(run.get('text',{}).get('content','') for block in blocks for run in block.get(block['type'],{}).get('rich_text',[]))


class InlineCodeTests(unittest.TestCase):
    setUp=assets_fixture.JsonAttachmentTests.setUp
    publish=assets_fixture.JsonAttachmentTests.publish
    render=assets_fixture.JsonAttachmentTests.render

    def test_inline_wikilink_label_and_original_spelling_remain_literal(self):
        literal='`[[../../Missing-baseline|Baseline original]]`'
        blocks,_,counts=self.render('Use '+literal+' here.',{})
        self.assertEqual(counts['assets_held'],0);self.assertEqual(self.publications,[])
        self.assertIn('Use '+literal+' here.',content(blocks))

    def test_inline_markdown_image_html_and_external_links_are_not_interpreted(self):
        literal='`[Doc](missing.pdf) ![Image](missing.png) <img src="missing.png"> [URL](https://example.invalid/doc)`'
        blocks,_,counts=self.render(literal,{})
        self.assertEqual(counts['assets_held'],0);self.assertEqual(self.publications,[]);self.assertIn(literal,content(blocks))
        self.assertFalse(any(run.get('text',{}).get('link') for b in blocks for run in b.get(b['type'],{}).get('rich_text',[])))

    def test_multiple_code_spans_preserve_unprotected_active_reference(self):
        source='`[[Missing-one]]` and ``[Doc](missing.pdf)`` then [[Missing-real]]'
        blocks,_,counts=self.render(source,{})
        self.assertEqual(counts['assets_held'],1);self.assertIn('`[[Missing-one]]`',content(blocks));self.assertIn('``[Doc](missing.pdf)``',content(blocks))

    def test_matching_delimiter_widths_include_shorter_backticks_as_literal(self):
        for literal in ['``literal ` [[Missing]]``','```literal `` [Doc](missing.pdf)```']:
            blocks,_,counts=self.render('Before '+literal+' after.',{})
            self.assertEqual(counts['assets_held'],0);self.assertIn(literal,content(blocks))

    def test_escaped_and_unclosed_openers_do_not_hide_actual_references(self):
        for source in ['\\`[[Missing-real]]','`unclosed [[Missing-real]]','`unclosed\n\n[[Missing-real]] `other']:
            _,_,counts=self.render(source,{})
            self.assertEqual(counts['assets_held'],1)

    def test_long_unicode_literal_crossing_rich_text_chunks_keeps_every_character(self):
        literal='`'+('中文🙂  '*700)+'[[Missing]] [Doc](https://example.invalid/doc)  `'
        blocks,_,counts=self.render('Before '+literal+' after.',{})
        self.assertEqual(counts['assets_held'],0);self.assertIn('Before '+literal+' after.',content(blocks))
        self.assertTrue(all(len(run['text']['content'].encode('utf-16-le'))//2<=1800 for b in blocks for run in b.get(b['type'],{}).get('rich_text',[])))

    def test_inline_marker_restoration_is_profile_stable_and_never_leaks_tokens(self):
        source='Before `[[Missing]]` after.'
        blocks,version,_=self.render(source,{})
        # Render the same pinned content twice; random protection tokens are internal.
        commit=subprocess.check_output(['git','-C',str(self.root),'rev-parse','HEAD'],text=True).strip()
        again,again_version,_=m.render_native_source('synthetic','Wiki/a.md',source,commit)
        self.assertEqual(version,again_version);self.assertEqual(blocks,again);self.assertNotIn('NATIVEINLINE',content(blocks))

    def test_inline_code_inside_active_link_label_keeps_its_link(self):
        blocks,_,counts=self.render('[Use `literal`](../Schema/b.md)',{})
        self.assertEqual(counts['assets_held'],0)
        runs=[run for b in blocks for run in b.get(b['type'],{}).get('rich_text',[]) if run.get('text',{}).get('link')]
        self.assertEqual(''.join(r['text']['content'] for r in runs),'Use `literal`')

    def test_frontmatter_code_and_existing_fenced_examples_remain_literal(self):
        source='---\nexample: "`[[Missing]]`"\n---\n```markdown\n[[Missing-fenced]]\n```\n\n`[[Missing-inline]]`'
        blocks,_,counts=self.render(source,{})
        self.assertEqual(counts['assets_held'],0);self.assertIn('`[[Missing]]`',content(blocks));self.assertIn('[[Missing-fenced]]',content(blocks));self.assertIn('`[[Missing-inline]]`',content(blocks));self.assertNotIn('NATIVEINLINE',content(blocks))

    def test_real_asset_outside_code_still_uploads_while_inline_example_is_ignored(self):
        source='`[Example](../assets/example.bin)` [Actual](../assets/actual.bin)'
        blocks,_,counts=self.render(source,{'assets/example.bin':b'Not an active reference','assets/actual.bin':b'Real referenced file'})
        self.assertEqual([p for p,_ in self.publications],['assets/actual.bin']);self.assertEqual(counts['assets_ready'],1);self.assertEqual(counts['assets_held'],0);self.assertIn('`[Example](../assets/example.bin)`',content(blocks))

if __name__=='__main__':unittest.main()
