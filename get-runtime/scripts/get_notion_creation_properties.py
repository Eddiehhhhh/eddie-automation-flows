"""Fixed four-field scope for NEW pages from verified canonical publications.

No historical selection, schema mutation, identity adoption or independent write.
The normal content/privacy gate still decides which immutable note can be sent.
"""
from copy import deepcopy
import get_notion_properties as props
import get_notion_property_transaction as tx

SCOPE = {'contract': 'get-notion-canonical-create-properties-1',
    'fields': sorted(props.FIELDS), 'target': 'new_pages_only',
    'source': 'verified_canonical_publication', 'schema_options': 'existing_only',
    'timestamps': 'explicit_offset_or_exact_bound_registry',
    'missing_values': 'hold_without_coercion', 'content_gate': 'unchanged',
    'registry_export': 'timestamps_only'}


def validate(scope):
    if scope != SCOPE: tx.engine().fail('approval_missing')
    return deepcopy(SCOPE)


def eligible(publication):
    d = publication.descriptor
    return (d.get('initial_cohort') is None and 'outbox_receipt_sha256' in d
        and bool(d.get('records')) and set(d['records']) <= set(d.get('changed_content_ids', []))
        and not any(key.startswith('historical_') for key in d))


def context(publication, schema, scope):
    e = tx.engine(); validate(scope)
    if not eligible(publication): e.fail('approval_missing')
    e.p.hex_hash(publication.descriptor['outbox_receipt_sha256'], 64)
    publication.check(publication.descriptor)
    projection = props.schema_projection(schema)
    selected = deepcopy(publication.descriptor['records'])
    binding = None
    blob = publication.descriptor['content_tree'].get(props.REGISTRY)
    if blob is not None:
        raw = publication.repo.read(publication.descriptor['commit'], props.REGISTRY, 16 * 1024 * 1024)
        if e.p.git_blob(raw) != blob: e.fail('property_source_invalid')
        binding = {'path': props.REGISTRY, 'blob': blob, 'sha256': e.digest(raw)}
    times = tx.registry_timestamps(publication, binding, selected)
    # This is an internal projection binding, not a fabricated privacy manifest.
    return projection, times, selected, {'registry': binding, 'schema_sha256': e.digest(projection)}
