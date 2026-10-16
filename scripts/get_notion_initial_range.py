"""Pure, read-only historical preview range proposals. No transport or adoption.

The caller independently authenticates the evidence hash and the source's
committed path/blob membership. Prepared input plus historical readback is
matching evidence, NOT an existing native ownership receipt. Any returned range
requires a separate explicit ownership approval and fresh target verification.
"""
from copy import deepcopy
import json
import re
import xml.etree.ElementTree as ET

import get_notion_sink_plan as p
import get_notion_executor as engine

CONTRACT='get-notion-initial-range-candidate-1'
MAX_INPUT=2*1024*1024
REASONS=frozenset(('evidence_invalid','source_changed','unsupported_history','target_invalid',
    'duplicate_id','no_unique_range','noncontiguous_history','wrong_parent','missing_asset','other'))
class Blocked(ValueError):pass
def fail(code):raise Blocked(code) from None
def same(a,b):return p.encoded(a)==p.encoded(b)


def historical_roots(markdown, ident, *, h4_mapping, readback=False):
    """Strict known enhanced-Markdown grammar; no current-tree inference.

    h4_mapping must explicitly be heading_3. If the actual old writer rendered
    it differently, the unique native match fails rather than guessing a map.
    Historical callouts/images/dividers are protected, never proposed as owned.
    """
    if not isinstance(markdown,str) or len(markdown.encode())>MAX_INPUT or h4_mapping!='heading_3':fail('unsupported_history')
    lines=markdown.splitlines();entries=[];plain=[];index=0
    def flush():
        if not plain:return
        data=('---\nnote_id: '+json.dumps(ident)+'\n---\n'+'\n'.join(plain)+'\n').encode()
        try:blocks,_,_=p.render_markdown(data,ident,{},'Raw/03 Get/history.md',{})
        except Exception:fail('unsupported_history')
        entries.extend({'role':'protected' if b['type']=='divider' else 'candidate','block':b} for b in blocks);plain.clear()
    def xml_chunk(tag):
        nonlocal index
        start=index;end='</'+tag+'>'
        while index<len(lines) and end not in lines[index]:index+=1
        if index>=len(lines):fail('unsupported_history')
        text='\n'.join(lines[start:index+1]);index+=1
        if '<!' in text or '<?' in text or re.search(r'\\<[bB][rR]',text) or re.search(r'`[^`]*<[bB][rR][^`]*`',text):fail('unsupported_history')
        # Enhanced Markdown uses HTML's attribute-free void br in cells.
        text=text.replace('<br>','<br/>')
        try:node=ET.fromstring(text)
        except ET.ParseError:fail('unsupported_history')
        if node.tag!=tag or node.tail not in (None,''):fail('unsupported_history')
        return node
    def cell_text(node):
        if node.attrib:fail('unsupported_history')
        value=node.text or ''
        for child in node:
            if child.tag!='br' or child.attrib or list(child) or child.text not in (None,''):fail('unsupported_history')
            value+='\n'+(child.tail or '')
        return value
    while index<len(lines):
        line=lines[index]
        if readback and line in ('---','___','***'):
            flush();entries.append({'role':'protected','block':p.block('divider',{})});index+=1
        elif line.startswith('<callout'):
            flush();node=xml_chunk('callout')
            if set(node.attrib)!={'icon','color'} or node.attrib['color']!='gray_bg' or not 1<=len(node.attrib['icon'])<=16 or list(node):fail('unsupported_history')
            text=node.text or '';parts=text.splitlines()
            if len(parts)!=2 or parts[0]!='' or not parts[1].startswith('\t'):fail('unsupported_history')
            try:b=p.block('callout',{'rich_text':p.rich_text(parts[1][1:]),'color':'gray_background','icon':{'type':'emoji','emoji':node.attrib['icon']}})
            except Exception:fail('unsupported_history')
            entries.append({'role':'protected','block':b})
        elif line.startswith('<table'):
            flush();node=xml_chunk('table')
            if node.attrib!={'fit-page-width':'true','header-row':'true'} or (node.text or '').strip():fail('unsupported_history')
            rows=[];width=None
            for tr in node:
                if tr.tag!='tr' or tr.attrib or (tr.text or '').strip() or (tr.tail or '').strip():fail('unsupported_history')
                cells=[]
                for td in tr:
                    if td.tag!='td' or (td.tail or '').strip():fail('unsupported_history')
                    try:cells.append(p.rich_text(cell_text(td)))
                    except Exception:fail('unsupported_history')
                if width is None:width=len(cells)
                if len(cells)!=width or not 1<=width<=20:fail('unsupported_history')
                rows.append(p.block('table_row',{'cells':cells}))
            if not rows:fail('unsupported_history')
            entries.append({'role':'candidate','block':p.block('table',{'table_width':width,'has_column_header':True,'has_row_header':False},rows)})
        elif line.startswith('<image'):
            flush();node=xml_chunk('image')
            if set(node.attrib)!={'src'} or list(node) or not re.fullmatch(r'file-upload://[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',node.attrib['src']):fail('unsupported_history')
            # This upload-operation UUID is NOT a native file/block identity.
            entries.append({'role':'protected','kind':'image','historical_reference_sha256':p.digest(node.attrib['src']),'caption':node.text or ''})
        elif readback and re.fullmatch(r'!\[\]\(notion-file-block://[^\s)]+\)',line):
            flush();entries.append({'role':'protected','kind':'image','historical_reference_sha256':p.digest(line)});index+=1
        elif line.startswith('<'):
            fail('unsupported_history')
        else:
            if line.startswith('#### '):line='### '+line[5:]
            elif re.match(r'^#{4,}',line):fail('unsupported_history')
            plain.append(line);index+=1
    flush()
    if len(entries)>p.MAX_BLOCKS:fail('unsupported_history')
    return entries


def canonical_semantics(blocks):
    """Only same-label/same-address auto links are equivalent to bare URLs.

    This is an ownership-evidence comparison, not a mutation to native blocks.
    Actual receipt hashes retain the complete native payload and annotations.
    """
    def runs(items):
        value=deepcopy(items)
        for item in value:
            text=item.get('text',{});link=text.get('link')
            if isinstance(link,dict) and set(link)=={'url'} and link['url']==text.get('content') and re.fullmatch(r'https?://[^\s<>]+',text['content']):text['link']=None
        return p.normalize_rich(value)
    def walk(row):
        row=deepcopy(row);payload=row['payload']
        for key in ('rich_text','caption'):
            if key in payload:payload[key]=runs(payload[key])
        if 'cells' in payload:payload['cells']=[runs(x) for x in payload['cells']]
        row['children']=[walk(x) for x in row['children']];return row
    return [walk(x) for x in blocks]


def verify_historical_readback(prepared_entries,readback,ident,h4_mapping):
    text=readback['text']
    if text.count('<content>')!=1 or text.count('</content>')!=1:fail('evidence_invalid')
    content=text.split('<content>',1)[1].split('</content>',1)[0]
    # The enhanced readback's comparison-symbol escape is serialization, not
    # a new source character. Other escapes are not guessed or removed.
    lines=[];fence=False
    for line in content.splitlines():
        if line.startswith('```'):fence=not fence;lines.append(line);continue
        if fence:lines.append(line);continue
        # Only Markdown's single escape outside code is interpreted. A doubled
        # slash or a literal code span is never silently stripped.
        chunks=re.split(r'(`[^`]*`)',line)
        for i in range(0,len(chunks),2):chunks[i]=re.sub(r'(?<!\\)\\>', '>', chunks[i])
        lines.append(''.join(chunks))
    content='\n'.join(lines)
    entries=historical_roots(content,ident,h4_mapping=h4_mapping,readback=True)
    expected=canonical_semantics(owned_sequence(prepared_entries));actual=[]
    for row in entries:
        actual.append(canonical_semantics([row['block']])[0] if 'block' in row else None)
    matches=sum(same(actual[i:i+len(expected)],expected) for i in range(len(actual)-len(expected)+1))
    if matches!=1:fail('evidence_invalid')
    return prepared_entries


def evidence_roots(evidence,expected_evidence_sha,source_data):
    keys={'note_id','page_id','source','prepared_markdown','readback','h4_mapping'}
    if not isinstance(evidence,dict) or set(evidence)!=keys or len(p.encoded(evidence))>4*MAX_INPUT or p.digest(evidence)!=expected_evidence_sha:fail('evidence_invalid')
    try:
        ident=p.note_id(evidence['note_id']);page=p.notion_id(evidence['page_id']);source=evidence['source']
        if not isinstance(source,dict) or set(source)!={'commit','path','blob','sha256'}:fail('evidence_invalid')
        p.hex_hash(source['commit'],40);p.safe_path(source['path']);p.hex_hash(source['blob'],40);p.hex_hash(source['sha256'],64)
        if not isinstance(source_data,bytes) or len(source_data)>p.MAX_NOTE or p.git_blob(source_data)!=source['blob'] or p.digest(source_data)!=source['sha256']:fail('source_changed')
        p.source_body(source_data,ident)
        readback=evidence['readback']
        if not isinstance(readback,dict) or p.notion_id(readback.get('id'))!=page or not isinstance(readback.get('text'),str) or not readback['text']:fail('evidence_invalid')
    except Blocked:raise
    except Exception:fail('evidence_invalid')
    prepared=historical_roots(evidence['prepared_markdown'],ident,h4_mapping=evidence['h4_mapping'])
    return verify_historical_readback(prepared,readback,ident,evidence['h4_mapping'])


def raw_tree(snapshot,ident,data_source,page):
    """Complete topology; opaque protected payloads stay untouched and unowned."""
    if not isinstance(snapshot,dict) or snapshot.get('complete') is not True or snapshot.get('native_blocks_available') is not True or snapshot.get('identity_query_complete') is not True or len(p.encoded(snapshot))>p.MAX_TARGET:fail('target_invalid')
    matches=snapshot.get('exact_id_matches')
    if not isinstance(matches,list):fail('target_invalid')
    if len(matches)>1:fail('duplicate_id')
    try:
        if [p.notion_id(i) for i in matches]!=[page] or p.notion_id(snapshot.get('page_id'))!=page or p.note_id(snapshot.get('id_property'))!=ident or p.property_identity(snapshot.get('properties_raw'))!=ident:fail('target_invalid')
        if p.notion_id(snapshot.get('parent_data_source'))!=data_source:fail('wrong_parent')
        rows=snapshot.get('recursive_blocks')
        if not isinstance(rows,list) or len(rows)>p.MAX_BLOCKS or not isinstance(snapshot.get('last_edited_time'),str) or not snapshot['last_edited_time']:fail('target_invalid')
        nodes={};order=[]
        for row in rows:
            if not isinstance(row,dict) or not isinstance(row.get('payload'),dict) or not isinstance(row.get('type'),str):fail('target_invalid')
            bid=p.notion_id(row.get('id'));parent=p.notion_id(row.get('parent_id'));children=row.get('children_ids')
            if not isinstance(children,list) or bid in nodes or bid==page:fail('target_invalid')
            children=[p.notion_id(i) for i in children]
            if len(set(children))!=len(children) or type(row.get('has_children')) is not bool or row['has_children']!=bool(children):fail('target_invalid')
            nodes[bid]={'id':bid,'parent_id':parent,'type':row['type'],'payload':deepcopy(row['payload']),'children_ids':children};order.append(bid)
        roots=[i for i in order if nodes[i]['parent_id']==page];visited=[]
        def walk(bid,depth):
            if depth>32 or bid in visited or bid not in nodes:fail('target_invalid')
            visited.append(bid)
            for child in nodes[bid]['children_ids']:
                if child not in nodes or nodes[child]['parent_id']!=bid:fail('target_invalid')
                walk(child,depth+1)
        for bid in roots:walk(bid,0)
        if visited!=order:fail('target_invalid')
        return nodes,roots,order
    except Blocked:raise
    except Exception:fail('target_invalid')


def blank_result():
    return {'contract':CONTRACT,'decision':'blocked','reason':'other','ownership_adopted':False,
        'requires_explicit_scope_approval':True,'executor_scope':None,'managed_block_ids':[],
        'protected_block_ids':[],'pending_assets':[],'execution_ready':False}


def match_expected(expected,ident,page,source,snapshot,data_source_id,pins):
    result=blank_result();page=p.notion_id(page);ds=p.notion_id(data_source_id)
    nodes,roots,order=raw_tree(snapshot,ident,ds,page)
    def semantic(bid):
        node=nodes[bid]
        if node['type']=='image':raise ValueError('protected')
        return {'type':node['type'],'payload':p.normalize_payload(node['type'],node['payload']),'children':[semantic(x) for x in node['children_ids']]}
    matches=[]
    for start in range(len(roots)-len(expected)+1):
        selected=roots[start:start+len(expected)]
        try:actual=[semantic(i) for i in selected]
        except Exception:continue
        if same(canonical_semantics(actual),canonical_semantics(expected)):matches.append(selected)
    if len(matches)!=1:fail('no_unique_range')
    managed=[]
    def own(bid):
        managed.append(bid)
        for child in nodes[bid]['children_ids']:own(child)
    for bid in matches[0]:own(bid)
    protected=[i for i in order if i not in managed]
    pending=['native_image_binding_required'] if any(nodes[i]['type']=='image' for i in protected) else []
    scope=None
    try:scope={'page_id':page,'snapshot_sha256':engine.target_sha(snapshot,ident,ds),'managed_block_ids':managed}
    except Exception:pass
    result.update(decision='candidate',reason=None,managed_block_ids=managed,protected_block_ids=protected,
        executor_scope=scope,pending_assets=pending,source=deepcopy(source),raw_snapshot_sha256=p.digest(snapshot),
        expected_semantic_sha256=p.digest(expected),**pins)
    return result


def owned_sequence(entries):
    wanted=[i for i,row in enumerate(entries) if row['role']=='candidate']
    if not wanted or wanted!=list(range(wanted[0],wanted[-1]+1)):fail('noncontiguous_history')
    return canonical_semantics([entries[i]['block'] for i in wanted])


def propose(evidence,expected_evidence_sha,source_data,snapshot,data_source_id):
    result=blank_result()
    try:
        expected=owned_sequence(evidence_roots(evidence,expected_evidence_sha,source_data))
        return match_expected(expected,evidence['note_id'],evidence['page_id'],evidence['source'],snapshot,data_source_id,
            {'evidence_sha256':expected_evidence_sha,'prepared_markdown_sha256':p.digest(evidence['prepared_markdown'].encode()),'historical_readback_sha256':p.digest(evidence['readback'])})
    except (Blocked,p.Blocked) as exc:result['reason']=str(exc) if str(exc) in REASONS else 'other'
    except Exception:result['reason']='evidence_invalid'
    return result


PROOF_CONTRACT='get-notion-historical-semantic-proof-1'
TRANSFORM='get-api-body-without-footer-h4-to-h3-1'


def reconstructed_roots(source_data,ident,source_path):
    """Deterministic known Git export grammar, never a target-derived baseline.

    Only the exact API footer with the same ID is omitted. It remains protected
    on Notion. Images require a separate binding contract and are blocked here.
    """
    try:lines=p.source_body(source_data,ident).splitlines()
    except Exception:fail('source_changed')
    while lines and lines[-1]=='':lines.pop()
    if len(lines)<4 or lines[-4]!='---' or lines[-3]!='source: api' or not re.fullmatch(r'updated: [0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?Z',lines[-2]) or lines[-1]!='canonicalId: api:'+ident:fail('unsupported_history')
    lines=lines[:-4]
    if any('![[' in x or '![' in x or '<image' in x or '<img' in x for x in lines):fail('missing_asset')
    mapped=[]
    for line in lines:
        if line.startswith('#### '):line='### '+line[5:]
        elif re.match(r'^#{4,}',line):fail('unsupported_history')
        mapped.append(line)
    data=('---\nnote_id: '+json.dumps(ident)+'\n---\n'+'\n'.join(mapped)+'\n').encode()
    try:blocks,_,_=p.render_markdown(data,ident,{},source_path,{})
    except p.Blocked as exc:fail('missing_asset' if str(exc)=='missing_asset' else 'unsupported_history')
    if not blocks or any(x['type'] in ('callout','divider','image') for x in blocks):fail('noncontiguous_history')
    return canonical_semantics(blocks)


def build_hash_proof(evidence,expected_evidence_sha,source_data):
    """Offline proof production from the existing full private artifacts.

    Only identifiers, counts and hashes leave this function. The proof's hash
    must then be independently approved/pinned before a runtime consumes it.
    """
    historical=owned_sequence(evidence_roots(evidence,expected_evidence_sha,source_data))
    rebuilt=reconstructed_roots(source_data,evidence['note_id'],evidence['source']['path'])
    if not same(historical,rebuilt):fail('source_changed')
    return {'contract':PROOF_CONTRACT,'transform':TRANSFORM,'validation':'prepared-readback-logical-1','note_id':evidence['note_id'],
        'page_id':p.notion_id(evidence['page_id']),'source':deepcopy(evidence['source']),
        'historical_evidence_sha256':expected_evidence_sha,
        'prepared_markdown_sha256':p.digest(evidence['prepared_markdown'].encode()),
        'historical_readback_sha256':p.digest(evidence['readback']),
        'candidate_semantic_sha256':p.digest(historical),'candidate_roots':len(historical)}


def propose_from_git(proof,expected_proof_sha,source_data,snapshot,data_source_id):
    """Runtime needs only approved hash proof + exact committed Git MD bytes."""
    result=blank_result()
    try:
        keys={'contract','transform','validation','note_id','page_id','source','historical_evidence_sha256','prepared_markdown_sha256','historical_readback_sha256','candidate_semantic_sha256','candidate_roots'}
        if not isinstance(proof,dict) or set(proof)!=keys or len(p.encoded(proof))>16384 or p.digest(proof)!=expected_proof_sha or proof['contract']!=PROOF_CONTRACT or proof['transform']!=TRANSFORM or proof['validation']!='prepared-readback-logical-1':fail('evidence_invalid')
        ident=p.note_id(proof['note_id']);page=p.notion_id(proof['page_id']);source=proof['source']
        if not isinstance(source,dict) or set(source)!={'commit','path','blob','sha256'}:fail('evidence_invalid')
        p.hex_hash(source['commit'],40);p.safe_path(source['path']);p.hex_hash(source['blob'],40);p.hex_hash(source['sha256'],64)
        for key in ('historical_evidence_sha256','prepared_markdown_sha256','historical_readback_sha256','candidate_semantic_sha256'):p.hex_hash(proof[key],64)
        if not isinstance(source_data,bytes) or len(source_data)>p.MAX_NOTE or p.git_blob(source_data)!=source['blob'] or p.digest(source_data)!=source['sha256']:fail('source_changed')
        expected=reconstructed_roots(source_data,ident,source['path'])
        if type(proof['candidate_roots']) is not int or len(expected)!=proof['candidate_roots'] or p.digest(expected)!=proof['candidate_semantic_sha256']:fail('source_changed')
        return match_expected(expected,ident,page,source,snapshot,data_source_id,
            {'proof_sha256':expected_proof_sha,'prepared_markdown_sha256':proof['prepared_markdown_sha256'],'historical_readback_sha256':proof['historical_readback_sha256']})
    except (Blocked,p.Blocked) as exc:result['reason']=str(exc) if str(exc) in REASONS else 'other'
    except Exception:result['reason']='evidence_invalid'
    return result


def public_summary(result):
    if not isinstance(result,dict):result={}
    return {'contract':CONTRACT,'decision':'candidate' if result.get('decision')=='candidate' else 'blocked',
        'reason':result.get('reason') if isinstance(result.get('reason'),str) and result['reason'] in REASONS else None if result.get('decision')=='candidate' else 'other',
        'managed_blocks':len(result['managed_block_ids']) if isinstance(result.get('managed_block_ids'),list) and len(result['managed_block_ids'])<=p.MAX_BLOCKS else 0,
        'protected_blocks':len(result['protected_block_ids']) if isinstance(result.get('protected_block_ids'),list) and len(result['protected_block_ids'])<=p.MAX_BLOCKS else 0,
        'writes':0,'ownership_adopted':False}


def public_proof_template(proof,expected_proof_sha):
    if not isinstance(proof,dict) or p.digest(proof)!=expected_proof_sha:fail('evidence_invalid')
    return {key:deepcopy(proof[key]) for key in ('contract','transform','validation','historical_evidence_sha256','prepared_markdown_sha256','historical_readback_sha256','candidate_semantic_sha256','candidate_roots')} | {
        'proof_sha256':expected_proof_sha,'source_commit':proof['source']['commit'],'source_blob':proof['source']['blob'],'source_sha256':proof['source']['sha256'],
        'note_id_sha256':p.digest(proof['note_id']),'page_id_sha256':p.digest(proof['page_id']),'source_path_sha256':p.digest(proof['source']['path'])}


def reconstruct_proof(template,expected_template_sha,*,note_id,page_id,source_path):
    keys={'contract','transform','validation','historical_evidence_sha256','prepared_markdown_sha256','historical_readback_sha256','candidate_semantic_sha256','candidate_roots',
        'proof_sha256','source_commit','source_blob','source_sha256','note_id_sha256','page_id_sha256','source_path_sha256'}
    if not isinstance(template,dict) or set(template)!=keys or p.digest(template)!=expected_template_sha:fail('evidence_invalid')
    try:
        ident=p.note_id(note_id);page=p.notion_id(page_id);path=p.safe_path(source_path)
        if p.digest(ident)!=template['note_id_sha256'] or p.digest(page)!=template['page_id_sha256'] or p.digest(path)!=template['source_path_sha256']:fail('evidence_invalid')
        proof={key:deepcopy(template[key]) for key in ('contract','transform','validation','historical_evidence_sha256','prepared_markdown_sha256','historical_readback_sha256','candidate_semantic_sha256','candidate_roots')}
        proof.update(note_id=ident,page_id=page,source={'commit':template['source_commit'],'path':path,'blob':template['source_blob'],'sha256':template['source_sha256']})
        if p.digest(proof)!=template['proof_sha256']:fail('evidence_invalid')
        return proof
    except Blocked:raise
    except Exception:fail('evidence_invalid')
