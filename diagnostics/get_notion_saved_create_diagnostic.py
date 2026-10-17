"""Pure aggregate diagnostics for one externally authenticated saved create.

The caller must authenticate the immutable journal/plan and saved response page,
then supply a fresh native snapshot normalized by the existing asset binder.
This module never grants ownership, changes a comparison, or authorizes retry.
It performs no I/O and emits no identifiers, property names, URLs or text.
"""
from collections import Counter

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
