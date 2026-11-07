"""Exact text-only projection for one independently bound preserved image.

No I/O, downloads, uploads, native-file guesses or ownership adoption. Original
Markdown remains the publication identity. The projection replaces exactly one
approved whole source line; every other byte, including frontmatter/footer,
survives. The native image must separately be verified and remain protected.
"""
from copy import deepcopy
from pathlib import PurePosixPath
import re

import get_notion_initial_range as h
from get_notion_initial_range import p

CONTRACT = 'get-notion-preserved-image-source-binding-1'
PROOF_CONTRACT = 'get-notion-preserved-image-text-proof-1'
TRANSFORM = 'single-bound-wiki-image-to-plain-paragraph-1'
REPLACEMENT = '图片已展示在页首。'
BINDING_KEYS = {'contract', 'note_id', 'source_path', 'reference', 'asset_path',
                'asset_blob', 'asset_sha256', 'asset_bytes', 'image_line_sha256'}
PROOF_KEYS = {'contract', 'transform', 'validation', 'note_id', 'page_id', 'source',
              'historical_evidence_sha256', 'prepared_markdown_sha256',
              'historical_readback_sha256', 'candidate_semantic_sha256',
              'candidate_roots', 'source_binding', 'source_binding_sha256',
              'projection_sha256'}
HASH_KEYS = ('historical_evidence_sha256', 'prepared_markdown_sha256',
             'historical_readback_sha256', 'candidate_semantic_sha256',
             'source_binding_sha256', 'projection_sha256')


class Blocked(ValueError):
    pass


def fail(code):
    raise Blocked(code) from None


def source_image(data, ident, source_path):
    """Only a complete standalone bullet with one wiki image is eligible."""
    try:
        p.note_id(ident); p.safe_path(source_path)
        body = p.source_body(data, ident)
        lines = body.splitlines(keepends=True)
        candidates = [i for i, line in enumerate(lines)
                      if '![' in line or re.search(r'<(?:img|image)\b', line, re.I)]
        if len(candidates) != 1: fail('image_reference_changed')
        index = candidates[0]; line = lines[index]
        fenced = False
        for prior in lines[:index]:
            if prior.startswith(('```', '~~~')): fenced = not fenced
        if fenced: fail('image_reference_changed')
        ending = '\r\n' if line.endswith('\r\n') else '\n' if line.endswith('\n') else ''
        logical = line[:-len(ending)] if ending else line
        match = re.fullmatch(r'- [^!\[\]\r\n`\\<>]*!\[\[([^\[\]\r\n|#]+)\]\]', logical)
        if match is None: fail('image_reference_changed')
        reference = match[1]
        if reference.startswith('/') or any(c in reference for c in ('\\', ':', '%', '\x00')):
            fail('image_reference_changed')
        parts = list(PurePosixPath(source_path).parent.parts)
        for part in reference.split('/'):
            if part == '..':
                if len(parts) <= 2: fail('image_reference_changed')
                parts.pop()
            elif part in ('', '.'):
                fail('image_reference_changed')
            else:
                parts.append(part)
        path = p.safe_path('/'.join(parts))
        if not path.startswith('Raw/03 Get/_assets/') or not re.search(r'\.(?:jpeg|jpg)$', path, re.I):
            fail('image_reference_changed')
        # source_body returns the exact suffix, so this is an exact byte range.
        offset = len(data) - len(body.encode()) + len(''.join(lines[:index]).encode())
        raw = logical.encode()
        if data[offset:offset + len(raw)] != raw: fail('source_changed')
        return {'reference': reference, 'asset_path': path, 'offset': offset,
                'line_bytes': raw, 'image_line_sha256': p.digest(raw)}
    except Blocked:
        raise
    except Exception:
        fail('source_changed')


def check_binding(binding, expected_sha256):
    try:
        if not isinstance(binding, dict) or set(binding) != BINDING_KEYS or p.digest(binding) != p.hex_hash(expected_sha256, 64):
            fail('asset_binding_invalid')
        if binding['contract'] != CONTRACT: fail('asset_binding_invalid')
        p.note_id(binding['note_id']); p.safe_path(binding['source_path']); p.safe_path(binding['asset_path'])
        p.hex_hash(binding['asset_blob'], 40); p.hex_hash(binding['asset_sha256'], 64)
        p.hex_hash(binding['image_line_sha256'], 64)
        if not isinstance(binding['reference'], str) or type(binding['asset_bytes']) is not int or not 1 <= binding['asset_bytes'] <= p.MAX_ASSET:
            fail('asset_binding_invalid')
        return binding
    except Blocked:
        raise
    except Exception:
        fail('asset_binding_invalid')


def build_source_binding(data, ident, source_path, content_tree, asset_bytes):
    """Offline candidate only; caller must independently review/pin its hash."""
    image = source_image(data, ident, source_path)
    if (not isinstance(asset_bytes, bytes) or not 1 <= len(asset_bytes) <= p.MAX_ASSET
            or not asset_bytes.startswith(b'\xff\xd8\xff') or not asset_bytes.endswith(b'\xff\xd9')):
        fail('asset_binding_invalid')
    binding = {'contract': CONTRACT, 'note_id': ident, 'source_path': source_path,
               'reference': image['reference'], 'asset_path': image['asset_path'],
               'asset_blob': p.git_blob(asset_bytes), 'asset_sha256': p.digest(asset_bytes),
               'asset_bytes': len(asset_bytes), 'image_line_sha256': image['image_line_sha256']}
    project_image_source(data, ident, source_path, content_tree, binding, p.digest(binding))
    return binding


def project_image_source(data, ident, source_path, content_tree, binding, expected_binding_sha256):
    """Use original bytes for source guards, returned bytes only for text render.

    This proves the pinned Git asset, not a live Notion block or image download.
    A separately verified existing-block binding is mandatory before execution.
    """
    check_binding(binding, expected_binding_sha256)
    image = source_image(data, ident, source_path)
    if (ident != binding['note_id'] or source_path != binding['source_path']
            or any(image[k] != binding[k] for k in ('reference', 'asset_path', 'image_line_sha256'))):
        fail('image_reference_changed')
    if (not isinstance(content_tree, dict) or content_tree.get(source_path) != p.git_blob(data)
            or content_tree.get(binding['asset_path']) != binding['asset_blob']):
        fail('asset_changed')
    start = image['offset']; end = start + len(image['line_bytes'])
    projected = data[:start] + REPLACEMENT.encode() + data[end:]
    return {'markdown_bytes': projected, 'source_sha256': p.digest(data),
            'source_blob': p.git_blob(data), 'projection_sha256': p.digest(projected),
            'asset_path': binding['asset_path'], 'asset_blob': binding['asset_blob'],
            'asset_sha256': binding['asset_sha256'], 'asset_bytes': binding['asset_bytes'],
            'source_binding_sha256': expected_binding_sha256,
            'native_image_binding_required': True, 'image_owned': False,
            'image_write_allowed': False}


def build_hash_proof(evidence, expected_evidence_sha, source_data, content_tree, binding, expected_binding_sha):
    """Three-way original Git / independently retained prepared / readback proof."""
    historical = h.owned_sequence(h.evidence_roots(evidence, expected_evidence_sha, source_data))
    projected = project_image_source(source_data, evidence['note_id'], evidence['source']['path'], content_tree, binding, expected_binding_sha)
    rebuilt = h.reconstructed_roots(projected['markdown_bytes'], evidence['note_id'], evidence['source']['path'])
    if not h.same(historical, rebuilt): fail('source_changed')
    return {'contract': PROOF_CONTRACT, 'transform': TRANSFORM,
            'validation': 'prepared-readback-projected-logical-1',
            'note_id': evidence['note_id'], 'page_id': p.notion_id(evidence['page_id']),
            'source': deepcopy(evidence['source']), 'historical_evidence_sha256': expected_evidence_sha,
            'prepared_markdown_sha256': p.digest(evidence['prepared_markdown'].encode()),
            'historical_readback_sha256': p.digest(evidence['readback']),
            'candidate_semantic_sha256': p.digest(historical), 'candidate_roots': len(historical),
            'source_binding': deepcopy(binding), 'source_binding_sha256': expected_binding_sha,
            'projection_sha256': projected['projection_sha256']}


def validate_proof(proof, expected_proof_sha):
    try:
        if not isinstance(proof, dict) or set(proof) != PROOF_KEYS or len(p.encoded(proof)) > 16384 or p.digest(proof) != p.hex_hash(expected_proof_sha, 64):
            fail('evidence_invalid')
        if proof['contract'] != PROOF_CONTRACT or proof['transform'] != TRANSFORM or proof['validation'] != 'prepared-readback-projected-logical-1': fail('evidence_invalid')
        p.note_id(proof['note_id']); p.notion_id(proof['page_id'])
        source = proof['source']
        if not isinstance(source, dict) or set(source) != {'commit', 'path', 'blob', 'sha256'}: fail('evidence_invalid')
        p.hex_hash(source['commit'], 40); p.safe_path(source['path']); p.hex_hash(source['blob'], 40); p.hex_hash(source['sha256'], 64)
        for key in HASH_KEYS: p.hex_hash(proof[key], 64)
        if type(proof['candidate_roots']) is not int or not 1 <= proof['candidate_roots'] <= p.MAX_BLOCKS: fail('evidence_invalid')
        binding = check_binding(proof['source_binding'], proof['source_binding_sha256'])
        if binding['note_id'] != proof['note_id'] or binding['source_path'] != source['path']: fail('evidence_invalid')
        return proof
    except Blocked:
        raise
    except Exception:
        fail('evidence_invalid')


def propose_from_git(proof, expected_proof_sha, source_data, content_tree, snapshot, data_source_id):
    result = h.blank_result()
    try:
        validate_proof(proof, expected_proof_sha)
        source = proof['source']; ident = proof['note_id']
        if not isinstance(source_data, bytes) or p.git_blob(source_data) != source['blob'] or p.digest(source_data) != source['sha256']: fail('source_changed')
        projected = project_image_source(source_data, ident, source['path'], content_tree, proof['source_binding'], proof['source_binding_sha256'])
        roots = h.reconstructed_roots(projected['markdown_bytes'], ident, source['path'])
        if (projected['projection_sha256'] != proof['projection_sha256'] or len(roots) != proof['candidate_roots']
                or p.digest(roots) != proof['candidate_semantic_sha256']): fail('source_changed')
        result = h.match_expected(roots, ident, proof['page_id'], source, snapshot, data_source_id,
            {'proof_sha256': expected_proof_sha, 'prepared_markdown_sha256': proof['prepared_markdown_sha256'],
             'historical_readback_sha256': proof['historical_readback_sha256']})
        rows = snapshot['recursive_blocks']; images = [row for row in rows if row['type'] == 'image']
        if (len(images) != 1 or images[0]['id'] not in result['protected_block_ids']
                or p.notion_id(images[0]['parent_id']) != p.notion_id(proof['page_id'])
                or rows.index(images[0]) >= min(i for i, row in enumerate(rows) if row['id'] in result['managed_block_ids'])):
            fail('missing_asset')
        result.update(source_binding_sha256=proof['source_binding_sha256'], native_image_binding_required=True,
                      image_owned=False, image_write_allowed=False)
    except (Blocked, h.Blocked, p.Blocked) as exc:
        result = h.blank_result()
        result['reason'] = str(exc) if str(exc) in h.REASONS else 'missing_asset'
    except Exception:
        result = h.blank_result()
        result['reason'] = 'evidence_invalid'
    return result


def public_proof_template(proof, expected_proof_sha):
    validate_proof(proof, expected_proof_sha); binding = proof['source_binding']
    return {key: deepcopy(proof[key]) for key in ('contract', 'transform', 'validation', *HASH_KEYS, 'candidate_roots')} | {
        'proof_sha256': expected_proof_sha, 'source_commit': proof['source']['commit'],
        'source_blob': proof['source']['blob'], 'source_sha256': proof['source']['sha256'],
        'note_id_sha256': p.digest(proof['note_id']), 'page_id_sha256': p.digest(proof['page_id']),
        'source_path_sha256': p.digest(proof['source']['path']),
        'reference_sha256': p.digest(binding['reference']), 'asset_path_sha256': p.digest(binding['asset_path']),
        'asset_blob': binding['asset_blob'], 'asset_sha256': binding['asset_sha256'],
        'asset_bytes': binding['asset_bytes'], 'image_line_sha256': binding['image_line_sha256']}


def reconstruct_proof(template, expected_template_sha, *, note_id, page_id, source_path, source_data, content_tree):
    """Rebuild private IDs/paths from current anchored inputs, never public text."""
    try:
        if not isinstance(template, dict) or p.digest(template) != p.hex_hash(expected_template_sha, 64): fail('evidence_invalid')
        image = source_image(source_data, note_id, source_path)
        binding = {'contract': CONTRACT, 'note_id': note_id, 'source_path': source_path,
            'reference': image['reference'], 'asset_path': image['asset_path'],
            'asset_blob': template['asset_blob'], 'asset_sha256': template['asset_sha256'],
            'asset_bytes': template['asset_bytes'], 'image_line_sha256': image['image_line_sha256']}
        proof = {key: deepcopy(template[key]) for key in ('contract', 'transform', 'validation', *HASH_KEYS, 'candidate_roots')}
        proof.update(note_id=note_id, page_id=p.notion_id(page_id), source_binding=binding,
                     source={'commit': template['source_commit'], 'path': source_path,
                             'blob': template['source_blob'], 'sha256': template['source_sha256']})
        validate_proof(proof, template['proof_sha256'])
        if not h.same(public_proof_template(proof, template['proof_sha256']), template): fail('evidence_invalid')
        project_image_source(source_data, note_id, source_path, content_tree, binding, proof['source_binding_sha256'])
        if p.git_blob(source_data) != proof['source']['blob'] or p.digest(source_data) != proof['source']['sha256']: fail('source_changed')
        return proof
    except Blocked:
        raise
    except Exception:
        fail('evidence_invalid')
