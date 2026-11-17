import base64,unittest
from core import Store,snapshot,Hold
from test_core import note
from vault_sink import VaultGitHubSink,markdown
class Git:
    def __init__(self):self.files={};self.count=0
    def request(self,m,p,b=None):
        if '/contents/' not in p:return {'private':True}
        path=p.split('?')[0]
        if m=='GET':return self.files.get(path)
        self.count+=1
        self.files[path]={'sha':str(self.count),'encoding':'base64','content':b['content']}
        return {}
class Tests(unittest.TestCase):
    def test_readable_body_and_transcript_with_lossless_source_link(self):
        n=note(text='Summary');n['audio']={'original':'Full original transcript'}
        body=markdown(snapshot(n),'test','Raw/Get/current').decode()
        self.assertIn(n['content'],body);self.assertIn(n['audio']['original'],body)
        self.assertIn('get-direct/test/1.json',body);self.assertNotIn('# test',body)
    def test_repeat_does_not_rewrite_and_manual_edit_is_held(self):
        git=Git();journal=Store(':memory:');sink=VaultGitHubSink(git,'owner/private','proof','test',journal,raw_prefix='Raw/Get/current')
        s=snapshot(note());r=sink.sync(s);count=git.count
        self.assertEqual(sink.sync(s,r),r);self.assertEqual(git.count,count)
        path=r['markdown']['path'];git.files[path]['sha']='human';git.files[path]['content']=base64.b64encode(b'Human content').decode()
        with self.assertRaisesRegex(Hold,'human_edit'):sink.sync(snapshot(note(text='new')),r)
        self.assertEqual(base64.b64decode(git.files[path]['content']),b'Human content')
    def test_prefix_stays_inside_raw(self):
        for prefix in ['Wiki/notes','Raw/../Wiki','Raw/.git/config','/Raw/notes']:
            with self.assertRaises(Hold):VaultGitHubSink(Git(),'a/b','proof','test',Store(':memory:'),raw_prefix=prefix)
