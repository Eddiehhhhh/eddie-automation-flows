#!/usr/bin/env python3
"""Bounded Get list census against a pinned private manifest. No detail or writes.

Private IDs/paths and incidental fields returned by the list API remain in memory.
Only a fixed aggregate allowlist can reach public output. No source is published.
"""
import argparse
import json
import os
from pathlib import Path
import re
import sys
import urllib.parse

import source_sync_sample as sample
import sync_getnote_to_raw as sync

MAX_PAGES = 300
MAX_NOTES = 5000
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
COUNTS = ('pages', 'source_notes', 'source_response_bytes', 'prior_manifest_notes',
          'prior_api_notes', 'matched_prior_api_notes', 'new_api_notes',
          'prior_api_not_enumerated', 'prior_non_api_notes_preserved',
          'prior_missing_id_notes_preserved')
ERRORS = ('private_index_invalid', 'source_limit', 'source_incomplete', 'source_failed')


class SourceLimit(RuntimeError):
    pass


class ResponseBudget:
    def __init__(self, maximum=MAX_TOTAL_BYTES):
        self.maximum, self.used = maximum, 0

    def read_limit(self, per_response):
        remaining = self.maximum - self.used
        if remaining <= 0:
            raise SourceLimit('source byte limit')
        return min(remaining, per_response) if per_response is not None else remaining

    def consume(self, count):
        self.used += count
        if self.used > self.maximum:
            raise SourceLimit('source byte limit')


def validate_manifest(value):
    if (not isinstance(value, dict) or value.get('source_system') != 'getnote'
            or not isinstance(value.get('notes'), list)
            or type(value.get('count')) is not int or value['count'] != len(value['notes'])
            or not 1 <= value['count'] <= MAX_NOTES):
        raise sample.FixedSampleError('invalid private index')
    mapping, paths, opaque, missing = {}, set(), 0, 0
    for note in value['notes']:
        if not isinstance(note, dict):
            raise sample.FixedSampleError('invalid private index')
        path = str(sample.safe_relative_path(note.get('path')))
        if path in paths:
            raise sample.FixedSampleError('duplicate private path')
        paths.add(path)
        ident = note.get('note_id')
        if ident in (None, ''):
            missing += 1
        elif type(ident) is not str:
            raise sample.FixedSampleError('invalid private identity')
        elif re.fullmatch(r'[0-9]{1,64}', ident):
            if ident in mapping:
                raise sample.FixedSampleError('duplicate private API identity')
            mapping[ident] = path
        else:
            opaque += 1
    return mapping, {'prior_manifest_notes': value['count'], 'prior_api_notes': len(mapping),
                     'prior_non_api_notes_preserved': opaque, 'prior_missing_id_notes_preserved': missing}


def private_index():
    token = os.environ.get('PRIVATE_VAULT_TOKEN')
    if not token:
        raise sample.FixedSampleError('private index unavailable')
    metadata = json.loads(sample.github_read('', token, 65536))
    if metadata.get('private') is not True:
        raise sample.FixedSampleError('private index unavailable')
    ref = json.loads(sample.github_read('/git/ref/heads/main', token, 65536))
    sha = ref.get('object', {}).get('sha')
    if (ref.get('object', {}).get('type') != 'commit' or type(sha) is not str
            or re.fullmatch(r'[a-f0-9]{40}', sha) is None):
        raise sample.FixedSampleError('private index unavailable')
    endpoint = '/contents/' + urllib.parse.quote(sample.MANIFEST_PATH, safe='/') + '?ref=' + sha
    manifest = json.loads(sample.github_read(endpoint, token, sample.MANIFEST_MAX_BYTES, raw=True))
    return validate_manifest(manifest)


def census(mapping, metadata, api_key, client_id):
    ids = set()
    pages = 0
    budget = ResponseBudget()
    # fetch_note_pages enforces terminal has_more, unique IDs and cursor progress.
    for pages, notes, more in sync.fetch_note_pages(
            api_key, client_id, max_pages=MAX_PAGES, max_response_bytes=MAX_RESPONSE_BYTES,
            response_budget=budget):
        for note in notes:
            ident = sync.note_id_from(note)
            if not re.fullmatch(r'[0-9]{1,64}', ident):
                raise RuntimeError('invalid source identity')
            ids.add(ident)
            if len(ids) > MAX_NOTES:
                raise SourceLimit('source note limit')
    prior = set(mapping)
    return dict(metadata, pages=pages, source_notes=len(ids), source_response_bytes=budget.used,
                matched_prior_api_notes=len(ids & prior), new_api_notes=len(ids - prior),
                prior_api_not_enumerated=len(prior - ids), enumeration_complete=True,
                source_complete=False, published=False, scope='inventory', status='inventory_ok')


def safe_report(value):
    result = {'scope': 'inventory', 'status': 'failed', 'enumeration_complete': False,
              'source_complete': False, 'published': False}
    if not isinstance(value, dict):
        return dict(result, error_code='source_failed')
    if value.get('status') == 'inventory_ok' and value.get('enumeration_complete') is True:
        bounds = {k: MAX_NOTES for k in COUNTS}
        bounds.update(pages=MAX_PAGES, source_response_bytes=MAX_TOTAL_BYTES)
        if all(type(value.get(k)) is int and 0 <= value[k] <= bounds[k] for k in COUNTS):
            result.update(status='inventory_ok', enumeration_complete=True)
            result.update({k: value[k] for k in COUNTS})
    elif value.get('error_code') in ERRORS:
        result['error_code'] = value['error_code']
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--publish-report', type=Path)
    args = parser.parse_args()
    if args.publish_report:
        try:
            report = json.loads(args.publish_report.read_text())
        except (OSError, ValueError):
            report = None
        print(json.dumps(safe_report(report), indent=2))
        return 0
    try:
        mapping, metadata = private_index()
    except Exception:
        print(json.dumps(safe_report({'error_code': 'private_index_invalid'})))
        return 1
    # The private-vault credential is never forwarded to Get requests or children.
    try:
        api_key, client_id = sync.load_credentials()
        report = census(mapping, metadata, api_key, client_id)
    except SourceLimit:
        report = {'error_code': 'source_limit'}
    except (Exception, SystemExit) as error:
        report = {'error_code': 'source_incomplete' if str(error) in (
            'page cap reached; refusing incomplete sync',
            'repeated Get list IDs; enumeration is incomplete',
            'Get cursor did not advance', 'official cursor response missing has_more') else 'source_failed'}
    safe = safe_report(report)
    print(json.dumps(safe))
    return 0 if safe['status'] == 'inventory_ok' else 1


if __name__ == '__main__':
    raise SystemExit(main())
