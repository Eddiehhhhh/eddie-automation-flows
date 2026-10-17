"""Pure aggregate diagnostics for one externally authenticated saved create.

The caller must authenticate the immutable journal/plan and saved response page,
then supply a fresh native snapshot normalized by the existing asset binder.
This module never grants ownership, changes a comparison, or authorizes retry.
It performs no I/O and emits no identifiers, property names, URLs or text.
"""
from collections import Counter
import re
from urllib.parse import urlsplit

import get_notion_executor as engine
import get_notion_sink_plan as planner

CONTRACT = 'get-notion-saved-create-diagnostic-1'
KINDS = ('paragraph', 'heading_1', 'heading_2', 'heading_3', 'heading_4',
         'bulleted_list_item', 'numbered_list_item', 'to_do', 'quote', 'code',
         'divider', 'image', 'table', 'table_row', 'callout')
CATEGORIES = ('type', 'heading', 'text', 'text_whitespace_only', 'annotations',
              'link', 'rich_text_runs', 'table', 'image', 'children', 'payload_other')
MAX_BYTES = 16 * 1024 * 1024
MAX_NODES = 10000


def _counts(rows):
    counts = {kind: 0 for kind in KINDS}
    def visit(items):
        for row in items:
            counts[row['type']] += 1
            visit(row['children'])
    visit(rows)
    return {'roots': len(rows), 'total': sum(counts.values()),
            'root_types': {kind: sum(r['type'] == kind for r in rows) for kind in KINDS},
            'total_types': counts}


def _differences(expected, actual):
    """Count differing normalized fields on paired nodes, never output values."""
    counts = {key: 0 for key in CATEGORIES}
    def rich(left, right, flags):
        if engine.same(left, right):
            return
        lt = ''.join(x['text']['content'] for x in left)
        rt = ''.join(x['text']['content'] for x in right)
        if lt != rt:
            flags.add('text')
            if ''.join(lt.split()) == ''.join(rt.split()):
                flags.add('text_whitespace_only')
        if len(left) != len(right): flags.add('rich_text_runs')
        if not engine.same([x['annotations'] for x in left], [x['annotations'] for x in right]):
            flags.add('annotations')
        if not engine.same([x['text']['link'] for x in left], [x['text']['link'] for x in right]):
            flags.add('link')
    def walk(left, right):
        for a, b in zip(left, right):
            flags = set(); ka, kb = a['type'], b['type']; pa, pb = a['payload'], b['payload']
            if ka != kb:
                flags.add('type')
                if ka.startswith('heading_') or kb.startswith('heading_'): flags.add('heading')
            for field in ('rich_text', 'caption'):
                rich(pa.get(field, []), pb.get(field, []), flags)
            ca, cb = pa.get('cells', []), pb.get('cells', [])
            if not engine.same(ca, cb):
                flags.add('table')
                for x, y in zip(ca, cb): rich(x, y, flags)
            excluded = {'rich_text', 'caption', 'cells'}
            if not engine.same({k:v for k,v in pa.items() if k not in excluded},
                               {k:v for k,v in pb.items() if k not in excluded}):
                flags.add('payload_other')
                if ka == 'image' or kb == 'image': flags.add('image')
                if ka in ('table', 'table_row') or kb in ('table', 'table_row'): flags.add('table')
            if len(a['children']) != len(b['children']): flags.add('children')
            for key in flags: counts[key] += 1
            walk(a['children'], b['children'])
    walk(expected, actual)
    return counts


def _alignment(expected, actual):
    a = [engine.digest(x) for x in expected]; b = [engine.digest(x) for x in actual]
    width = min(len(a), len(b))
    if not width:
        return {'compared_roots': 0, 'best_exact': 0, 'best_mismatched': 0,
                'best_candidate_count': 0, 'unique_best': False}
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    scores = [sum(x == y for x,y in zip(shorter, longer[start:start+width]))
              for start in range(len(longer)-width+1)]
    best = max(scores); ties = scores.count(best)
    return {'compared_roots': width, 'best_exact': best, 'best_mismatched': width-best,
            'best_candidate_count': ties, 'unique_best': ties == 1}


def _link_differences(expected, actual):
    """Classify only exact aligned text/annotation pairs; never infer auto-links.

    Counts describe observed values, not semantic equivalence or API behavior.
    No URL canonicalization, case-folding, percent decoding or text trimming is
    used. Explicit target changes remain explicit even if equal to run text.
    """
    report = {'pairing_complete': True, 'changed_pairs': 0, 'affected_nodes': 0,
              'affected_roots': 0, 'paired_fields': 0, 'unpaired_fields': 0,
              'unpaired_nodes': 0,
              'directions': {k:0 for k in ('absent_to_present', 'present_to_absent', 'both_targets_changed')},
              'classifications': {k:0 for k in ('exact_text_http_url', 'exact_text_mailto_email', 'explicit_target_changed', 'other')},
              'actual_schemes': {k:0 for k in ('none', 'http', 'https', 'mailto', 'tel', 'other')},
              'explicit_target_shapes': {k:0 for k in ('empty_path_to_slash', 'slash_to_empty', 'scheme_case_only', 'host_case_only', 'default_port_added', 'default_port_removed', 'other')}}
    changes = []; affected_nodes = set(); affected_roots = set()
    email = re.compile(r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,}")
    def scheme(url):
        if url is None: return 'none'
        for name in ('http', 'https', 'mailto', 'tel'):
            if url.startswith(name + ':'): return name
        return 'other'
    def exact_http(url, text):
        if url != text or any(c.isspace() or ord(c)<32 or ord(c)==127 for c in text): return False
        try:
            value = urlsplit(text)
            return value.scheme in ('http', 'https') and bool(value.hostname) and value.username is None and value.password is None
        except ValueError: return False
    def explicit_shape(before, after):
        # These are observational categories, never URL-equivalence rules.
        if any(c.isspace() or ord(c)<32 or ord(c)==127 for c in before+after): return 'other'
        try:
            x,y=urlsplit(before),urlsplit(after)
            sx,sy=before.partition(':')[0],after.partition(':')[0]
            if x.scheme not in ('http','https') or y.scheme not in ('http','https') or not x.netloc or not y.netloc: return 'other'
            if sx==sy and x.netloc==y.netloc and x.query==y.query and x.fragment==y.fragment:
                end=len(sx)+3+len(x.netloc)
                if x.path=='' and y.path=='/' and after==before[:end]+'/'+before[end:]: return 'empty_path_to_slash'
                if x.path=='/' and y.path=='' and before==after[:end]+'/'+after[end:]: return 'slash_to_empty'
            if x.netloc==y.netloc and (x.path,x.query,x.fragment)==(y.path,y.query,y.fragment) and sx!=sy and sx.lower()==sy.lower() and after==sy+before[len(sx):]: return 'scheme_case_only'
            if sx==sy and (x.path,x.query,x.fragment)==(y.path,y.query,y.fragment):
                if (x.username is None and y.username is None and x.password is None and y.password is None
                    and x.netloc!=y.netloc and x.netloc.lower()==y.netloc.lower()
                    and after==sx+'://'+y.netloc+before[len(sx)+3+len(x.netloc):]): return 'host_case_only'
                default='80' if sx=='http' else '443' if sx=='https' else None
                if default is not None and x.username is None and y.username is None:
                    same_rest=after==sx+'://'+y.netloc+before[len(sx)+3+len(x.netloc):]
                    if same_rest and x.netloc+':'+default==y.netloc: return 'default_port_added'
                    if same_rest and y.netloc+':'+default==x.netloc: return 'default_port_removed'
        except ValueError: pass
        return 'other'
    def rich(left, right, path):
        # Array/range correspondence is proven; mismatched chunks cannot be
        # zipped and accidentally labeled as a target-only change.
        if (len(left) != len(right) or any(a['text']['content'] != b['text']['content']
                or not engine.same(a['annotations'], b['annotations']) for a,b in zip(left,right))):
            report['unpaired_fields'] += 1; report['pairing_complete'] = False; return
        report['paired_fields'] += 1
        for a,b in zip(left,right):
            before,after = a['text']['link'],b['text']['link']
            if engine.same(before,after): continue
            text = a['text']['content']; target = after['url'] if after is not None else None
            direction = ('absent_to_present' if before is None else
                         'present_to_absent' if after is None else 'both_targets_changed')
            category = 'other'
            if before is not None and after is not None:
                category = 'explicit_target_changed'
                report['explicit_target_shapes'][explicit_shape(before['url'],after['url'])] += 1
            elif after is not None:
                if exact_http(target,text): category = 'exact_text_http_url'
                elif (target == 'mailto:' + text and email.fullmatch(text)
                      and '..' not in text and not text.startswith('.') and '.@' not in text):
                    category = 'exact_text_mailto_email'
            report['changed_pairs'] += 1; report['directions'][direction] += 1
            report['classifications'][category] += 1; report['actual_schemes'][scheme(target)] += 1
            affected_nodes.add(path); affected_roots.add(path[0])
            # Only the aggregate hash leaves this function, never these values.
            changes.append({'expected_link':before,'actual_link':after,'text':text,'annotations':a['annotations']})
    def walk(left,right,parent=()):
        if len(left) != len(right):
            report['unpaired_nodes'] += max(len(left),len(right)); report['pairing_complete'] = False; return
        for index,(a,b) in enumerate(zip(left,right)):
            path=parent+(index,)
            if a['type'] != b['type']:
                report['unpaired_nodes'] += 1; report['pairing_complete'] = False; continue
            for field in ('rich_text','caption'):
                rich(a['payload'].get(field,[]),b['payload'].get(field,[]),path)
            ca,cb=a['payload'].get('cells',[]),b['payload'].get('cells',[])
            if len(ca)!=len(cb):
                report['unpaired_fields'] += 1; report['pairing_complete'] = False
            else:
                for x,y in zip(ca,cb): rich(x,y,path)
            walk(a['children'],b['children'],path)
    walk(expected,actual)
    report['affected_nodes']=len(affected_nodes);report['affected_roots']=len(affected_roots)
    report['paired_changes_sha256']=engine.digest(changes)
    return report


def diagnose_created(row, normalized_snapshot, saved_page, data_source_id):
    """Return aggregate evidence only; this result cannot be used as a receipt.

    All semantic/property equality uses the pinned engine's existing created()
    rules. Caller authentication is required even if this reports a full match.
    """
    report = {'contract': CONTRACT, 'status': 'blocked', 'stage': 'input',
              'reason': 'input_invalid', 'ownership_adopted': False,
              'diagnostic_external_write_attempts': 0}
    try:
        if not isinstance(row, dict) or row.get('action') != 'create': return report
        if len(engine.encoded(row)) > MAX_BYTES or len(engine.encoded(normalized_snapshot)) > MAX_BYTES: return report
        if not isinstance(row.get('properties'), dict) or not 1 <= len(row['properties']) <= 20: return report
        for key, value in row['properties'].items():
            if not isinstance(key, str) or not isinstance(value, dict) or value.get('type') not in ('title', 'rich_text'): return report
            planner.normalize_rich(value.get(value['type']))
        planner.note_id(row['note_id']); planner.notion_id(data_source_id)
        if planner.notion_id(saved_page) != saved_page: return report
        report['stage'] = 'expected_semantics'; report['reason'] = 'expected_invalid'
        expected = planner.comparison_semantics(row['rendered'], None)
        expected_counts = _counts(expected)
        if expected_counts['total'] > MAX_NODES: return report
        report.update(expected=expected_counts, row_sha256=engine.digest(row), expected_semantics_sha256=engine.digest(expected))
        report['stage'] = 'target'; report['reason'] = 'target_invalid'
        view = engine.target(normalized_snapshot, row['note_id'], data_source_id)
        if view is None:
            report['reason'] = 'target_missing'; return report
        report['page_matches_saved'] = view['page_id'] == saved_page
        report['stage'] = 'actual_semantics'; report['reason'] = 'actual_invalid'
        actual = planner.comparison_semantics(view['semantic'], None)
        actual_counts = _counts(actual)
        if actual_counts['total'] > MAX_NODES: return report
        report.update(actual=actual_counts, actual_semantics_sha256=engine.digest(actual),
                      semantics_equal=engine.same(expected, actual),
                      positional_differences=_differences(expected, actual),
                      link_differences=_link_differences(expected, actual),
                      whole_root_alignment=_alignment(expected, actual))
        ah = [engine.digest(x) for x in expected]; bh = [engine.digest(x) for x in actual]
        report['root_order_only_difference'] = ah != bh and Counter(ah) == Counter(bh)
        report['positional_exact_roots'] = sum(x == y for x,y in zip(ah,bh))
        report['stage'] = 'properties'; report['reason'] = 'properties_invalid'
        properties = {'submitted': len(row['properties']), 'matched': 0,
                      'title_mismatched': 0, 'id_mismatched': 0, 'other_mismatched': 0}
        for key, value in row['properties'].items():
            observed = view['properties'].get(key); kind = value['type']
            try:
                same = (isinstance(observed, dict) and observed.get('type') == kind and
                        engine.same(planner.normalize_rich(observed.get(kind)), planner.normalize_rich(value[kind])))
            except Exception: same = False
            if same: properties['matched'] += 1
            else: properties['title_mismatched' if kind == 'title' else 'id_mismatched' if key == '笔记 ID' else 'other_mismatched'] += 1
        report['properties'] = properties
        equal = report['page_matches_saved'] and report['semantics_equal'] and properties['matched'] == properties['submitted']
        # An exact cross-check prevents diagnostic logic from silently disagreeing
        # with the unchanged production acceptance function.
        try: engine.created(normalized_snapshot, row, data_source_id, saved_page); accepted = True
        except Exception: accepted = False
        if equal != accepted:
            report.update(stage='acceptance_crosscheck', reason='acceptance_disagrees'); return report
        report.update(status='matched' if accepted else 'mismatch', stage='complete',
                      reason='exact_match' if accepted else 'readback_mismatch', created_accepts=accepted)
        return report
    except Exception:
        # Never echo exception text, type, caller keys or private values.
        return report
