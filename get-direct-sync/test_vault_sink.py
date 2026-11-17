import base64,copy,hashlib,unittest
from urllib.parse import parse_qs,urlsplit
from core import Store,snapshot,Hold
from test_core import note
from vault_sink import VaultGitHubSink,markdown
class Git:
    def __init__(self):self.files={};self.count=0;self.commits={};self.stale={};self.stale_after_write=False;self.calls=[]
    def request(self,m,p,b=None):
        self.calls.append((m,p))
        if '/contents/' not in p:return {'private':True}
        path=p.split('?')[0]
        if m=='GET':
            ref=parse_qs(urlsplit(p).query).get('ref',[''])[0]
            files=self.commits.get(ref,self.stale if self.stale_after_write and self.count else self.files)
            return copy.deepcopy(files.get(path))
        if path in self.files and b.get('sha')!=self.files[path]['sha']:raise Hold('http_409')
        self.count+=1
        self.stale=copy.deepcopy(self.files)
        raw=base64.b64decode(b['content']);sha=hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()
        self.files[path]={'sha':sha,'encoding':'base64','content':b['content']}
        body=('tree '+sha+'\nauthor Test <test@example.invalid> 0 +0000\ncommitter Test <test@example.invalid> 0 +0000\n\nWrite '+str(self.count)+'\n').encode()
        commit=hashlib.sha1(b'commit '+str(len(body)).encode()+b'\0'+body).hexdigest();self.commits[commit]=copy.deepcopy(self.files)
        return {'commit':{'sha':commit},'content':{'sha':sha}}
class Tests(unittest.TestCase):
    def test_all_three_source_writes_read_their_immutable_commits(self):
        git=Git();git.stale_after_write=True;journal=Store(':memory:')
        sink=VaultGitHubSink(git,'owner/private','proof','test',journal,raw_prefix='Raw/Get/current')
        result=sink.sync(snapshot(note()))
        self.assertEqual(git.count,3)
        self.assertEqual(len([path for method,path in git.calls if method=='GET' and parse_qs(urlsplit(path).query).get('ref',[''])[0] in git.commits]),3)
        self.assertEqual(result['source_json']['version'],result['version'])
    def test_native_display_title_and_tag_names_remain_readable(self):
        n=note();n['title']='';n['display_title']='Native source display';n['tags']=[{'id':'2','name':'Source tag'}]
        body=markdown(snapshot(n),'test','Raw/Get/current').decode()
        self.assertIn('title: "'+n['display_title']+'"',body)
        self.assertIn('tags: ["'+n['tags'][0]['name']+'"]',body)

    def test_readable_body_and_transcript_with_lossless_source_link(self):
        n=note(text='Summary');n['audio']={'original':'Full original transcript'}
        body=markdown(snapshot(n),'test','Raw/Get/current').decode()
        self.assertIn(n['content'],body);self.assertIn(n['audio']['original'],body)
        self.assertIn('get-direct/test/versions/1/'+snapshot(n)['version']+'.json',body);self.assertNotIn('# test',body)
    def test_repeat_does_not_rewrite_and_manual_edit_is_held(self):
        git=Git();journal=Store(':memory:');sink=VaultGitHubSink(git,'owner/private','proof','test',journal,raw_prefix='Raw/Get/current')
        s=snapshot(note());r=sink.sync(s);count=git.count
        self.assertEqual(sink.sync(s,r),r);self.assertEqual(git.count,count)
        path=r['markdown']['path'];git.files[path]['sha']='human';git.files[path]['content']=base64.b64encode(b'Human content').decode()
        with self.assertRaisesRegex(Hold,'human_edit'):sink.sync(snapshot(note(text='new')),r)
        self.assertEqual(base64.b64decode(git.files[path]['content']),b'Human content')
    def test_readable_path_is_bound_once_and_versions_are_immutable(self):
        git=Git();journal=Store(':memory:')
        sink=VaultGitHubSink(git,'owner/private','proof','test',journal,raw_prefix='Raw/Get/current')
        n=note();n['title']='首次标题'
        first=sink.sync(snapshot(n))
        self.assertEqual(first['markdown']['relative_path'],'Raw/Get/current/1/首次标题.md')
        old_json=base64.b64decode(git.files[first['source_json']['path']]['content'])
        n['title']='Updated title';n['content']='new body';n['updated_at']='2026-11-18'
        second=sink.sync(snapshot(n),first)
        self.assertEqual(second['markdown']['relative_path'],first['markdown']['relative_path'])
        self.assertNotEqual(second['source_json']['relative_path'],first['source_json']['relative_path'])
        self.assertEqual(base64.b64decode(git.files[first['source_json']['path']]['content']),old_json)
        self.assertEqual(second['source_title'],n['title'])

    def test_chinese_emoji_filename_limit_and_same_title_distinct_ids(self):
        git=Git();journal=Store(':memory:')
        sink=VaultGitHubSink(git,'owner/private','proof','test',journal,raw_prefix='Raw/Get/current')
        n=note();n['title']='中🧪/标题?\\'*100
        first=sink.sync(snapshot(n))
        path=first['markdown']['relative_path']
        filename=path.split('/')[-1]
        self.assertLessEqual(len(filename.encode()),240)
        self.assertTrue(filename.endswith('.md'))
        self.assertNotIn('?',filename)
        n['note_id']='2'
        other=sink.sync(snapshot(n))
        self.assertNotEqual(first['markdown']['relative_path'],other['markdown']['relative_path'])
        self.assertEqual(filename,other['markdown']['relative_path'].split('/')[-1])

    def test_immutable_source_corruption_is_never_overwritten(self):
        git=Git();journal=Store(':memory:')
        sink=VaultGitHubSink(git,'owner/private','proof','test',journal,raw_prefix='Raw/Get/current')
        s=snapshot(note());receipt=sink.sync(s)
        path=receipt['source_json']['path']
        git.files[path]={'sha':'human','content':base64.b64encode(b'corrupt immutable data').decode()}
        with self.assertRaisesRegex(Hold,'immutable_source_conflict'):sink.sync(s,receipt)
        self.assertEqual(base64.b64decode(git.files[path]['content']),b'corrupt immutable data')

    def test_immutable_write_response_loss_reads_back_without_second_write(self):
        journal=Store(':memory:')
        class Lost(Git):
            def request(self,method,path,body=None):
                if method=='PUT' and '/versions/' in path:
                    intent=[value for key,value in journal.db.execute('SELECT k,v FROM journal') if key.startswith('immutable:')]
                    self.asserted=bool(intent)
                    result=super().request(method,path,body)
                    if not getattr(self,'lost',False):self.lost=True;raise TimeoutError('synthetic response loss')
                    return result
                return super().request(method,path,body)
        git=Lost();sink=VaultGitHubSink(git,'owner/private','proof','test',journal,raw_prefix='Raw/Get/current')
        s=snapshot(note());receipt=sink.sync(s);writes=git.count
        self.assertTrue(git.asserted)
        self.assertEqual(sink.sync(s,receipt),receipt)
        self.assertEqual(git.count,writes)

    def test_unknown_absent_immutable_write_is_not_blindly_retried(self):
        class Lost(Git):
            attempts=0
            def request(self,method,path,body=None):
                if method=='PUT' and '/versions/' in path:
                    self.attempts+=1;raise TimeoutError('synthetic unknown dispatch')
                return super().request(method,path,body)
        git=Lost();journal=Store(':memory:')
        sink=VaultGitHubSink(git,'owner/private','proof','test',journal,raw_prefix='Raw/Get/current')
        for _ in range(2):
            with self.assertRaisesRegex(Hold,'source_write_requires_reconcile'):sink.sync(snapshot(note()))
        self.assertEqual(git.attempts,1)

    def test_prefix_stays_inside_raw(self):
        for prefix in ['Wiki/notes','Raw/../Wiki','Raw/.git/config','/Raw/notes']:
            with self.assertRaises(Hold):VaultGitHubSink(Git(),'a/b','proof','test',Store(':memory:'),raw_prefix=prefix)
