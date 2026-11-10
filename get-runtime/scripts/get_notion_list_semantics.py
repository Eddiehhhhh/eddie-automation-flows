"""Context-bound default numbering, never a general list-reset rewrite."""
from copy import deepcopy

UNKNOWN='unknown'

def defaults(rows, previous_type=UNKNOWN, *, wire=False):
    """Only a proven segment start permits absent == integer 1.

    Inputs are normalized semantic blocks. Mid-list explicit resets stay exact
    for comparisons and are refused by the current REST write schema.
    """
    result=deepcopy(rows)
    previous=previous_type
    for row in result:
        kind=row['type'];payload=row['payload']
        if kind=='numbered_list_item':
            if 'list_format' in payload:
                if payload['list_format']!='numbers':raise ValueError('unsupported_source_format')
                payload.pop('list_format')
            if 'list_start_index' in payload:
                start=payload['list_start_index']
                if type(start) is not int or start!=1:raise ValueError('unsupported_source_format')
                if previous not in (UNKNOWN,'numbered_list_item'):
                    payload.pop('list_start_index')
                elif wire:raise ValueError('unsupported_source_format')
        row['children']=defaults(row['children'],None,wire=wire)
        previous=kind
    return result
