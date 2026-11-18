"""Direct Get fan-out. No production defaults; injected transports for tests."""
import base64, copy, hashlib, http.client, json, re, sqlite3, urllib.parse, urllib.request, urllib.error
from retry import HTTPFailure,TransportFailure

class Hold(RuntimeError): pass

def canonical(x):
    return json.dumps(x, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
def digest(x): return hashlib.sha256(canonical(x).encode()).hexdigest()
def identity(note):
    ids = [str(note[k]) for k in ('id','note_id') if note.get(k) is not None]
    if not ids or len(set(ids)) != 1 or not ids[0].isascii() or not ids[0].isdigit(): raise Hold('invalid_source_identity')
    return ids[0]
def source_version(note):
    # Fingerprints ignore only the temporary signature shape verified on Get media.
    value=copy.deepcopy(note)
    for attachment in value.get('attachments',[]) or []:
        if not isinstance(attachment,dict) or attachment.get('type')=='link':continue
        url=attachment.get('url')
        if not isinstance(url,str):continue
        try:parts=urllib.parse.urlsplit(url)
        except ValueError:continue
        if parts.scheme!='https' or parts.netloc!='mediacdn.umiwi.com' or not parts.query:continue
        prefix,_,tail=url.partition('?');raw_query,fragment_mark,fragment=tail.partition('#')
        fields=raw_query.split('&');signature={'Expires','OSSAccessKeyId','Signature'}
        signed=[f for f in fields if f.partition('=')[0] in signature]
        if len(signed)!=3 or {f.partition('=')[0] for f in signed}!=signature or any(not f.partition('=')[2] for f in signed):continue
        query='&'.join(f for f in fields if f.partition('=')[0] not in signature)
        attachment['url']=prefix+('?' + query if query else '')+(fragment_mark+fragment if fragment_mark else '')
    return digest(value)
def snapshot(note):
    if note.get('truncated') or note.get('is_truncated'): raise Hold('source_truncated')
    if not isinstance(note.get('content'),str) or not note.get('updated_at'): raise Hold('incomplete_source')
    if note.get('note_type') in ('recorder_audio','recorder_flash_audio') and not (isinstance(note.get('audio'),dict) and isinstance(note['audio'].get('original'),str) and note['audio']['original'].strip()):raise Hold('recording_transcript_unavailable')
    return {'id':identity(note), 'version':source_version(note), 'note':note}

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs): raise Hold('redirect_blocked')
class HTTP:
    """Credentials never forwarded to redirects or dynamic attachment URLs."""
    def __init__(self, host, headers):
        if host not in ('https://openapi.biji.com','https://api.github.com','https://api.notion.com'): raise Hold('unapproved_host')
        self.host,self.headers=host,headers
        self.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
    def request(self, method,path,body=None):
        if not path.startswith('/') or path.startswith('//') or '#' in path: raise Hold('invalid_path')
        req=urllib.request.Request(self.host+path, data=None if body is None else canonical(body).encode(), method=method, headers={**self.headers,'Content-Type':'application/json'})
        try:
            with self.opener.open(req,timeout=30) as r:
                raw=r.read(16*1024*1024+1)
                if len(raw)>16*1024*1024: raise Hold('response_too_large')
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            code=e.code;retry_after=e.headers.get('Retry-After') if e.headers else None;e.close()
            if code==404: return None
            raise HTTPFailure(code,retry_after) from None
        except (urllib.error.URLError,TimeoutError,OSError,http.client.HTTPException): raise TransportFailure() from None

class GetSource:
    def __init__(self,http,max_pages=10000): self.http,self.max_pages=http,max_pages
    def scan(self,*,on_note_hold=None,select_note=None):
        cursor=None; cursors=set(); seen=set(); result=[];self.observed_count=0;self.summaries={};self.holds=[];self.scan_complete=False
        for _ in range(self.max_pages):
            q='' if cursor is None else '?'+urllib.parse.urlencode({'cursor':cursor})
            response=self.http.request('GET','/open/api/v1/resource/note/list'+q)
            if not response or response.get('success') is not True: raise Hold('list_failed')
            data=response.get('data',{}); notes=data.get('notes')
            if not isinstance(notes,list) or not isinstance(data.get('has_more'),bool): raise Hold('invalid_list')
            for summary in notes:
                nid=identity(summary)
                if nid in seen: raise Hold('duplicate_source_id')
                seen.add(nid);self.observed_count+=1;self.summaries[nid]=digest(summary)
                if select_note is not None and not select_note(summary):continue
                r=self.http.request('GET','/open/api/v1/resource/note/detail?'+urllib.parse.urlencode({'id':nid,'image_quality':'original'}))
                if not r or r.get('success') is not True: raise Hold('detail_failed')
                note=r.get('data',{}).get('note',{})
                if identity(note)!=nid or note.get('updated_at')!=summary.get('updated_at') or note.get('note_type')!=summary.get('note_type'): raise Hold('source_changed_during_scan')
                try:result.append({**snapshot(note),'summary_hash':self.summaries[nid]})
                except Hold as error:
                    if on_note_hold is None or str(error) not in ('recording_transcript_unavailable','source_truncated','incomplete_source'):raise
                    held={'id':nid,'version':source_version(note),'reason':str(error),'note':note,'summary_hash':self.summaries[nid]}
                    self.holds.append(held)
                    on_note_hold(held)
            if not data['has_more']:
                self.scan_complete=True
                return result
            if not notes: raise Hold('empty_page_claims_more')
            cursor=data.get('cursor',data.get('next_cursor'))
            if not cursor or str(cursor) in cursors: raise Hold('invalid_cursor')
            cursors.add(str(cursor))
        raise Hold('scan_limit')

def requires_binary_assets(snapshot):
    note=snapshot['note'];audio=note.get('audio') or {}
    return bool(note.get('note_type') in ('audio','local_audio','recorder_audio','recorder_flash_audio') or any(not isinstance(a,dict) or a.get('type')!='link' for a in note.get('attachments',[])) or isinstance(audio,dict) and (audio.get('play_url') or audio.get('url')) or '![' in note.get('content','') or '<img' in note.get('content','').lower())

class CheckpointFailure(Hold):
    """A remote checkpoint failure must stop every subsequent target write."""
    pass

class Store:
    """Private SQLite; each committed mutation reaches durable state before continuing."""
    def __init__(self,path,*,checkpoint=None):
        self.db=sqlite3.connect(path)
        self._persist,self.failed=checkpoint,False
        columns=self.db.execute('PRAGMA table_info(pending)').fetchall()
        if columns and [row[1] for row in columns]!=['id','version','snapshot']:raise Hold('state_schema_incompatible')
        self.db.executescript('''CREATE TABLE IF NOT EXISTS journal(k TEXT PRIMARY KEY,v TEXT); CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY,v TEXT); CREATE TABLE IF NOT EXISTS inventory(id TEXT PRIMARY KEY,version TEXT); CREATE TABLE IF NOT EXISTS pending(id TEXT,version TEXT,snapshot TEXT,PRIMARY KEY(id,version)); CREATE TABLE IF NOT EXISTS receipts(sink TEXT,id TEXT,value TEXT,PRIMARY KEY(sink,id)); CREATE TABLE IF NOT EXISTS held(sink TEXT,id TEXT,version TEXT,reason TEXT,PRIMARY KEY(sink,id,version)); CREATE TABLE IF NOT EXISTS delivered(id TEXT,version TEXT,PRIMARY KEY(id,version)); CREATE TABLE IF NOT EXISTS source_summaries(id TEXT PRIMARY KEY,summary_hash TEXT NOT NULL);''')
        self._checkpoint()
    def _checkpoint(self):
        if self.failed:raise CheckpointFailure('state_checkpoint_failed')
        if self._persist:
            try:self._persist(self.db)
            except CheckpointFailure:
                self.failed=True
                raise
            except Exception:
                self.failed=True
                raise CheckpointFailure('state_checkpoint_failed') from None
    def _ready(self):
        if self.failed:raise CheckpointFailure('state_checkpoint_failed')
    def bind(self,configuration):
        self._ready();fingerprint=digest(configuration)
        row=self.db.execute("SELECT v FROM meta WHERE k='target_fingerprint'").fetchone()
        if row and row[0]!=fingerprint:raise Hold('state_target_changed')
        if not row and self.db.execute("SELECT 1 FROM meta WHERE k='initialized'").fetchone():raise Hold('unbound_existing_state')
        with self.db:self.db.execute("INSERT OR IGNORE INTO meta VALUES('target_fingerprint',?)",(fingerprint,))
        self._checkpoint()
    def accept(self, snapshots, *, scan_state=None, held_summaries=()):
        self._ready()
        if len({s['id'] for s in snapshots})!=len(snapshots): raise Hold('duplicate_source_id')
        if any('summary_hash' in s and not re.fullmatch('[0-9a-f]{64}',s['summary_hash']) for s in [*snapshots,*held_summaries]):raise Hold('invalid_summary_hash')
        before=self.db.total_changes
        with self.db:
            initial=self.db.execute("SELECT v FROM meta WHERE k='initialized'").fetchone() is None
            for s in snapshots:
                old=self.db.execute('SELECT version FROM inventory WHERE id=?',(s['id'],)).fetchone()
                if not initial and (old is None or old[0]!=s['version']):
                    self.db.execute('INSERT OR IGNORE INTO pending VALUES(?,?,?)',(s['id'],s['version'],canonical(s)))
                if old is None or old[0]!=s['version']:
                    self.db.execute('INSERT OR REPLACE INTO inventory VALUES(?,?)',(s['id'],s['version']))
                if 'summary_hash' in s:
                    self.db.execute('INSERT INTO source_summaries VALUES(?,?) ON CONFLICT(id) DO UPDATE SET summary_hash=excluded.summary_hash WHERE summary_hash!=excluded.summary_hash',(s['id'],s['summary_hash']))
            for held in held_summaries:
                self.db.execute('INSERT INTO source_summaries VALUES(?,?) ON CONFLICT(id) DO UPDATE SET summary_hash=excluded.summary_hash WHERE summary_hash!=excluded.summary_hash',(held['id'],held['summary_hash']))
            if initial:self.db.execute("INSERT INTO meta VALUES('initialized','1')")
            if scan_state is not None:self._set('incremental_source_v1',scan_state)
        if self.db.total_changes!=before:self._checkpoint()
        return {'baseline_only':initial,'observed':len(snapshots)}
    def enqueue(self,snapshots):
        """Explicit history batch; never reset the inventory or existing receipts."""
        self._ready()
        if not self.db.execute("SELECT 1 FROM meta WHERE k='initialized'").fetchone():raise Hold('history_requires_baseline')
        if len({(s['id'],s['version']) for s in snapshots})!=len(snapshots):raise Hold('duplicate_history_snapshot')
        added=0
        with self.db:
            for s in snapshots:
                if self.db.execute('SELECT 1 FROM delivered WHERE id=? AND version=?',(s['id'],s['version'])).fetchone():continue
                old=self.db.execute('SELECT version FROM inventory WHERE id=?',(s['id'],)).fetchone()
                if old and old[0]!=s['version']:raise Hold('history_snapshot_not_current')
                receipts=[self.receipt(name,s['id']) or {} for name in ('github','notion')]
                if all(r.get('version')==s['version'] and (not requires_binary_assets(s) or r.get('binary_assets_verified') is True) for r in receipts):continue
                added+=self.db.execute('INSERT OR IGNORE INTO pending VALUES(?,?,?)',(s['id'],s['version'],canonical(s))).rowcount
                self.db.execute('INSERT OR IGNORE INTO inventory VALUES(?,?)',(s['id'],s['version']))
        self._checkpoint()
        return {'enqueued':added,'pending':self.pending_count()}
    def get(self,key):
        row=self.db.execute('SELECT v FROM journal WHERE k=?',(key,)).fetchone()
        return json.loads(row[0]) if row else None
    def _set(self,key,value):
        encoded=canonical(value)
        row=self.db.execute('SELECT v FROM journal WHERE k=?',(key,)).fetchone()
        if row and row[0]==encoded:return False
        self.db.execute('INSERT OR REPLACE INTO journal VALUES(?,?)',(key,encoded))
        return True
    def set(self,key,value):
        self._ready()
        with self.db:changed=self._set(key,value)
        if changed:self._checkpoint()
    def receipt(self,sink,nid):
        r=self.db.execute('SELECT value FROM receipts WHERE sink=? AND id=?',(sink,nid)).fetchone()
        return json.loads(r[0]) if r else None
    def pending_count(self):return self.db.execute('SELECT count(*) FROM pending').fetchone()[0]
    def _held(self,name,nid,version,error):
        reason='unexpected_sink_failure'
        message=str(error)
        safe_reasons={'unverified_receipt','binary_assets_not_verified','metadata_projection_incomplete','duplicate_source_pages',
            'unknown_existing_page_ownership','page_ownership_mismatch','page_parent_mismatch',
            'managed_content_modified','managed_title_modified','unresolved_prior_revision',
            'page_create_or_missing_requires_reconcile','title_write_requires_reconcile',
            'toggle_create_requires_reconcile','append_requires_reconcile','append_readback_mismatch',
            'final_readback_mismatch','notion_database_unavailable','notion_database_property_type_mismatch',
            'notion_database_schema_unavailable','notion_database_not_live','notion_multiple_data_sources_unsupported',
            'target_unreadable','target_ownership_or_edit_conflict','target_missing','github_readback_mismatch',
            'github_write_response_invalid','github_write_requires_reconcile',
            'media_git_transport_failed','media_git_ref_changed','media_github_readback_mismatch',
            'snapshot_exceeds_validated_limit','asset_url_not_allowed','asset_address_not_public',
            'asset_size_limit','asset_time_limit','asset_length_mismatch','asset_hash_mismatch',
            'media_embedded_coverage_unverified','media_embedded_image_unverified',
            'transport_uncertain','retry_attempt_limit','retry_elapsed_budget','retry_server_delay_exceeds_budget'}
        if message in safe_reasons or re.fullmatch('http_[1-5][0-9]{2}',message):reason=message
        row=self.db.execute('SELECT reason FROM held WHERE sink=? AND id=? AND version=?',(name,nid,version)).fetchone()
        if row and row[0]==reason:return
        with self.db:self.db.execute('INSERT OR REPLACE INTO held VALUES(?,?,?,?)',(name,nid,version,reason))
        self._checkpoint()
    def drain(self,sinks,limit=None,paused_ids=()):
        self._ready()
        if set(sinks)!={'github','notion'}:raise Hold('both_sinks_required')
        counts={k:{'verified':0,'held':0} for k in sinks}
        if limit is not None and (type(limit) is not int or limit<0):raise Hold('invalid_drain_limit')
        blocked=set();attempted=0
        paused=set(map(str,paused_ids))
        rows=[row for row in self.db.execute('SELECT rowid,id,version,snapshot FROM pending ORDER BY rowid').fetchall() if row[1] not in paused]
        if limit is not None:
            # Round-robin IDs, retaining strict version order within each ID.
            queues={}
            for row in rows:queues.setdefault(row[1],[]).append(row)
            cursor=self.get('drain_cursor_v1') or 0
            groups=sorted(queues.values(),key=lambda q:q[0][0])
            groups=[q for q in groups if q[0][0]>cursor]+[q for q in groups if q[0][0]<=cursor]
            rows=[]
            while groups:
                rows.extend(q.pop(0) for q in groups)
                groups=[q for q in groups if q]
        for rowid,nid,version,raw in rows:
            if limit is not None and attempted>=limit:break
            if nid in blocked:continue
            attempted+=1
            s=json.loads(raw)
            for name in ('github','notion'):
                self._checkpoint()
                previous=self.receipt(name,nid) or {}
                if (name=='github' and previous.get('version')==version
                        and (not requires_binary_assets(s) or previous.get('binary_assets_verified') is True)
                        and not self.db.execute('SELECT 1 FROM held WHERE sink=? AND id=? AND version=?',
                                                (name,nid,version)).fetchone()):
                    continue
                try:
                    receipt=sinks[name].sync(s,previous or None)
                    if receipt.get('version')!=s['version']: raise Hold('unverified_receipt')
                    before=self.db.total_changes
                    with self.db:
                        self.db.execute('INSERT INTO receipts VALUES(?,?,?) ON CONFLICT(sink,id) DO UPDATE SET value=excluded.value WHERE value!=excluded.value',(name,nid,canonical(receipt)))
                        self.db.execute('DELETE FROM held WHERE sink=? AND id=? AND version=?',(name,nid,version))
                    if self.db.total_changes!=before:self._checkpoint()
                    if requires_binary_assets(s) and receipt.get('binary_assets_verified') is not True:
                        self._held(name,nid,version,Hold('binary_assets_not_verified'))
                        blocked.add(nid);counts[name]['held']+=1
                    else:counts[name]['verified']+=1
                except CheckpointFailure:raise
                except Exception as exc:
                    self._held(name,nid,version,exc)
                    blocked.add(nid);counts[name]['held']+=1
                if nid in blocked:break
            if nid not in blocked and all((self.receipt(k,nid) or {}).get('version')==s['version'] for k in sinks):
                with self.db:
                    self.db.execute('INSERT OR IGNORE INTO delivered VALUES(?,?)',(nid,version))
                    self.db.execute('DELETE FROM pending WHERE id=? AND version=?',(nid,version))
                self._checkpoint()
            if limit is not None:self.set('drain_cursor_v1',rowid)
        return {**counts,'pending':self.pending_count()}

class GitHubSink:
    """Deterministic files, repository visibility gate, exact SHA compare-and-swap."""
    def __init__(self,http,repo,branch,namespace):
        parts=repo.split('/')
        if len(parts)!=2 or any(not p or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._' for c in p) for p in parts): raise Hold('invalid_repo')
        if not namespace or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789-_' for c in namespace): raise Hold('invalid_namespace')
        self.http,self.repo,self.branch,self.namespace=http,repo,branch,namespace
    def content(self,item):
        if item.get('encoding')=='none':
            sha=item.get('sha','')
            if not sha or any(c not in '0123456789abcdef' for c in sha):raise Hold('invalid_blob_identity')
            item=self.http.request('GET','/repos/'+self.repo+'/git/blobs/'+sha)
            if not item or item.get('sha')!=sha or item.get('encoding')!='base64':raise Hold('blob_readback_invalid')
        if item.get('encoding') not in (None,'base64'):raise Hold('unsupported_content_encoding')
        return base64.b64decode(item['content'])
    def put_verified(self,path,payload,desired,*,uncertain_reason='github_write_requires_reconcile',mismatch_reason='github_readback_mismatch'):
        expected_sha=hashlib.sha1(b'blob '+str(len(desired)).encode()+b'\0'+desired).hexdigest()
        try:response=self.http.request('PUT',path,payload)
        except CheckpointFailure:raise
        except Exception:
            # A lost write response gets one branch reconciliation, never another PUT.
            try:
                check=self.http.request('GET',path+'?'+urllib.parse.urlencode({'ref':self.branch}))
                if check and check.get('sha')==expected_sha and self.content(check)==desired:return check
            except CheckpointFailure:raise
            except Exception:pass
            raise Hold(uncertain_reason) from None
        commit=response.get('commit') if isinstance(response,dict) else None
        content=response.get('content') if isinstance(response,dict) else None
        commit_sha=commit.get('sha') if isinstance(commit,dict) else None
        if not isinstance(commit_sha,str) or not re.fullmatch('[0-9a-f]{40}',commit_sha) or not isinstance(content,dict) or content.get('sha')!=expected_sha:
            raise Hold('github_write_response_invalid')
        check=self.http.request('GET',path+'?'+urllib.parse.urlencode({'ref':commit_sha}))
        if not check or check.get('sha')!=expected_sha or self.content(check)!=desired:raise Hold(mismatch_reason)
        return check
    def sync(self,s,receipt=None):
        r=self.http.request('GET','/repos/'+self.repo)
        if not r or r.get('private') is not True: raise Hold('private_repository_required')
        path='/repos/'+self.repo+'/contents/get-direct/'+self.namespace+'/'+s['id']+'.json'
        query='?'+urllib.parse.urlencode({'ref':self.branch})
        expected={'owner':'get-direct-v1/'+self.namespace,**s}
        desired=canonical(expected).encode()
        if len(desired)>8*1024*1024:raise Hold('snapshot_exceeds_validated_limit')
        old=self.http.request('GET',path+query)
        if old:
            try: body=self.content(old); value=json.loads(body)
            except Exception: raise Hold('target_unreadable') from None
            if body==desired:return {'version':s['version'],'sha':old['sha'],'target':path,'body_hash':hashlib.sha256(body).hexdigest(),'content_scope':'text_and_metadata'}
            if not receipt or receipt.get('target')!=path or receipt.get('sha')!=old['sha'] or receipt.get('body_hash')!=hashlib.sha256(body).hexdigest():raise Hold('target_ownership_or_edit_conflict')
        elif receipt:raise Hold('target_missing')
        payload={'message':'Update Get snapshot','content':base64.b64encode(desired).decode(),'branch':self.branch}
        if old:payload['sha']=old['sha']
        check=self.put_verified(path,payload,desired)
        return {'version':s['version'],'sha':check['sha'],'target':path,'body_hash':hashlib.sha256(desired).hexdigest(),'content_scope':'text_and_metadata'}
