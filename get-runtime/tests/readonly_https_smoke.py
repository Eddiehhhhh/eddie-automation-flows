"""One-time cloud read-only smoke; output is fixed and contains no private data."""
import base64, hashlib, json, os, sys, tempfile, time, urllib.request
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import get_private_runtime_bindings as bindings
import get_private_preview_retention as retention
import get_private_proof as proof
import get_exact_source_proof as exact

def main():
 token=os.environ['PRIVATE_VAULT_TOKEN'];digest='e2b66cdd767e78346f169a7c2603884d4dccea7ff319502c6eab71732405a608'
 url='https://api.github.com/repos/Eddiehhhhh/eddie-llm-wiki/contents/.sync/get-notion/runtime-bindings-'+digest+'.json?ref=main'
 req=urllib.request.Request(url,headers={'Authorization':'Bearer '+token,'Accept':'application/vnd.github+json','User-Agent':'get-proof-readonly-smoke'})
 with urllib.request.build_opener(retention.NoRedirect()).open(req,timeout=30) as response: body=response.read(256*1024+1)
 if len(body)>256*1024: raise ValueError('smoke_payload')
 envelope=json.loads(body);raw=base64.b64decode(envelope['content'],validate=False)
 values=bindings.parse(raw,digest);record=values['get_recurring_policy_recovery']['SEQUENCE_TWO_RECORD'];origin=record['receipt_commit']
 remote=retention.GitHubRemote(None,token);source=proof.PrivateRecords(remote);source.guard();head=source.head
 started=time.monotonic()
 with tempfile.TemporaryDirectory(prefix='get-proof-smoke-') as temp:
  roots=exact._chain(Path(temp),remote,head,origin,origin)
  total=sum(p.stat().st_size for p in Path(temp).rglob('*') if p.is_file())
  if total>exact.MAX_PACK_BYTES: raise ValueError('smoke_size')
 node=source.read_node(origin)
 if node['tree']!=roots[origin]: raise ValueError('smoke_tree')
 receipt=source.object(node,'publication')
 if receipt is None or proof.sha(receipt)!=record['receipt_sha256']: raise ValueError('smoke_receipt')
 parsed=proof.strict(receipt,proof.MAX_OBJECT,serializer=proof.public_encoded)
 if parsed.get('sequence')!=2 or parsed.get('parent_commit')!=node['parent']: raise ValueError('smoke_receipt')
 source.guard()
 print(json.dumps({'status':'verified','fixed_repository_private':True,'head_stable':True,'bounded_commit_chain':True,'fixed_receipt_tree_and_blob_verified':True,'temporary_bytes':total,'elapsed_seconds':round(time.monotonic()-started,1)}))

if __name__=='__main__':
 try: main()
 except Exception as exc:
  print(json.dumps({'status':'failed','error_type':type(exc).__name__}),file=sys.stderr);raise SystemExit(1)
