"""Keep full source JSON plus a readable Markdown mirror in the existing Raw tree."""
import base64,hashlib,json,posixpath,re,urllib.parse
from core import GitHubSink,Hold

def markdown(snapshot,namespace,raw_prefix,manifest=None):
    note=snapshot['note']
    fields={'source_system':'getnote','note_id':snapshot['id'],'title':note.get('title',''),
            'note_type':note.get('note_type',''),'created_at':note.get('created_at',''),
            'updated_at':note.get('updated_at',''),'source_version':snapshot['version'],
            'tags':note.get('tags',[])}
    current=raw_prefix+'/'+snapshot['id']+'.md'
    source=posixpath.relpath('get-direct/'+namespace+'/'+snapshot['id']+'.json',posixpath.dirname(current))
    fields['source_json']=source
    text='---\n'+''.join(k+': '+json.dumps(v,ensure_ascii=False)+'\n' for k,v in fields.items())+'---\n\n'+note['content'].rstrip()+'\n'
    original=(note.get('audio') or {}).get('original') if isinstance(note.get('audio'),dict) else None
    if isinstance(original,str) and original.strip() and original.strip()!=note['content'].strip():
        text+='\n## 录音原文\n\n'+original.rstrip()+'\n'
    if manifest and manifest.get('assets'):
        text+='\n## 附件\n\n'
        for index,asset in enumerate(manifest['assets'],1):
            target=posixpath.relpath('get-direct/'+namespace+'/assets/'+asset['sha256']+'.bin',posixpath.dirname(current))
            text+='- [附件 '+str(index)+']('+urllib.parse.quote(target,safe='/.-')+')\n'
    return text.encode()

class VaultGitHubSink:
    def __init__(self,http,repo,branch,namespace,journal,*,raw_prefix):
        parts=raw_prefix.split('/')
        if not raw_prefix.startswith('Raw/') or any(p in ('','..','.') or p.startswith('.') for p in parts) or any(ord(c)<32 for c in raw_prefix):
            raise Hold('invalid_raw_prefix')
        self.sink=GitHubSink(http,repo,branch,namespace)
        self.journal,self.raw_prefix=journal,raw_prefix
    def sync(self,snapshot,receipt=None):
        sink=self.sink
        result=sink.sync(snapshot,receipt)
        key='markdown:'+sink.repo+':'+sink.branch+':'+sink.namespace+':'+snapshot['id']
        baseline=self.journal.get(key) or {}
        manifest=self.journal.get('asset_manifest:'+snapshot['id']+':'+snapshot['version'])
        desired=markdown(snapshot,sink.namespace,self.raw_prefix,manifest)
        path='/repos/'+sink.repo+'/contents/'+urllib.parse.quote(self.raw_prefix+'/'+snapshot['id']+'.md',safe='/')
        query='?'+urllib.parse.urlencode({'ref':sink.branch})
        observed=sink.http.request('GET',path+query)
        sha256=hashlib.sha256(desired).hexdigest()
        if observed and sink.content(observed)==desired:
            md={'sha':observed['sha'],'sha256':sha256,'path':path,'version':snapshot['version']}
        else:
            previous=baseline.get('receipt')
            if observed:
                body=sink.content(observed)
                if not previous or previous.get('sha')!=observed['sha'] or previous.get('sha256')!=hashlib.sha256(body).hexdigest():
                    raise Hold('markdown_human_edit_conflict')
            elif previous:raise Hold('markdown_target_missing')
            self.journal.set(key,{'phase':'write_intent','version':snapshot['version'],'sha256':sha256,'receipt':previous})
            payload={'message':'Update readable Get source','content':base64.b64encode(desired).decode(),'branch':sink.branch}
            if observed:payload['sha']=observed['sha']
            sink.http.request('PUT',path,payload)
            check=sink.http.request('GET',path+query)
            if not check or sink.content(check)!=desired:raise Hold('markdown_readback_mismatch')
            md={'sha':check['sha'],'sha256':sha256,'path':path,'version':snapshot['version']}
        self.journal.set(key,{'phase':'verified','receipt':md})
        return {**result,'markdown':md}
