#!/usr/bin/env python3
"""Read at most two Get details from one list page; output metrics, never notes."""
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import probe_getnote_auth as auth

DETAIL_URL = 'https://openapi.biji.com/open/api/v1/resource/note/detail'
DETAIL_MAX_BYTES = 4 * 1024 * 1024
KINDS = {'ordinary': {'plain_text', 'text', 'img_text', 'link'},
         'recording': {'recorder_audio', 'recorder_flash_audio'}}


def identity(note):
    values = [note[key] for key in ('note_id', 'id') if note.get(key) is not None]
    if not values or any(isinstance(v, (float, bool)) or not str(v).isdigit() for v in values):
        raise ValueError('invalid identity')
    values = [str(v) for v in values]
    if len(set(values)) != 1:
        raise ValueError('conflicting identity')
    return values[0]


def request(opener, headers, note_id=None):
    # Callers can select only list or fixed-host detail, never an upstream URL.
    url = auth.URL if note_id is None else DETAIL_URL + '?' + urllib.parse.urlencode({'id':note_id, 'image_quality':'original'})
    maximum = auth.MAX_RESPONSE_BYTES if note_id is None else DETAIL_MAX_BYTES
    status = None
    started = time.monotonic()
    payload = None
    result = {'category':'request_failed'}
    try:
        with auth.deadline():
            req = urllib.request.Request(url, method='GET', headers=headers)
            with opener.open(req, timeout=auth.SOCKET_TIMEOUT_SECONDS) as response:
                status = response.status
                if status != 200:
                    raise ValueError('unexpected status')
                body = response.read(maximum + 1)
            if len(body) > maximum:
                result = {'category':'response_too_large'}
            else:
                value = json.loads(body)
                if isinstance(value, dict) and value.get('success') is True and isinstance(value.get('data'), dict):
                    payload = value['data']
                    result = {'category':'ok','response_bytes':len(body)}
                else:
                    result = {'category':'api_or_shape_error'}
    except auth.RedirectBlocked:
        result = {'category':'redirect_blocked'}
    except urllib.error.HTTPError as exc:
        status = exc.code
        exc.close()
        result = {'category':'request_not_authorized' if status in (401,403) else 'rate_limited' if status==429 else 'http_error'}
    except TimeoutError:
        result = {'category':'timeout'}
    except urllib.error.URLError:
        result = {'category':'transport_error'}
    except (ValueError, UnicodeError):
        result = {'category':'invalid_response'}
    except Exception:
        result = {'category':'request_failed'}
    result['elapsed_ms'] = round((time.monotonic()-started)*1000)
    if type(status) is int and 100 <= status <= 599:
        result['http_status'] = status
    return payload, result


def inspect_detail(summary, detail):
    matched = identity(summary) == identity(detail)
    content = detail.get('content')
    audio = detail.get('audio')
    original = audio.get('original') if isinstance(audio, dict) else None
    attachments = detail.get('attachments')
    updated = detail.get('updated_at')
    truncated = bool(detail.get('truncated') or detail.get('is_truncated'))
    recording = summary.get('note_type') in KINDS['recording']
    metrics = {
        'stable_id_matches':matched,
        'note_type_matches':detail.get('note_type') == summary.get('note_type'),
        'updated_at_present':isinstance(updated,str) and bool(updated.strip()),
        'updated_at_matches_list':isinstance(updated,str) and bool(updated) and updated == summary.get('updated_at'),
        'content_field_present':isinstance(content,str),
        'content_chars':len(content) if isinstance(content,str) else 0,
        'transcript_field_present':isinstance(original,str),
        'transcript_chars':len(original) if isinstance(original,str) else 0,
        'attachments_field_present':isinstance(attachments,list),
        'attachments_count':len(attachments) if isinstance(attachments,list) else 0,
        'attachment_urls_present':isinstance(attachments,list) and all(isinstance(a,dict) and isinstance(a.get('original_url') or a.get('url'),str) and bool(a.get('original_url') or a.get('url')) for a in attachments),
        'upstream_declares_truncation':truncated,
    }
    usable = matched and metrics['note_type_matches'] and metrics['updated_at_present'] and metrics['updated_at_matches_list'] and isinstance(content,str) and not truncated
    if recording and not (isinstance(original,str) and original.strip()):
        usable = False
    metrics['category'] = 'fields_available' if usable else 'incomplete_or_changed_sample'
    return metrics


def probe():
    key, client = os.environ.get('GETNOTE_API_KEY'), os.environ.get('GETNOTE_CLIENT_ID')
    report = {'service':'getnote','mode':'bounded_details','requests_attempted':0,
              'timing_scope':'single_request_including_response_read_and_json_parse',
              'sample_selection':'first_matching_types_not_relevance_search',
              'ai_context_end_to_end_measured':False,
              'cross_destination_comparison':False, 'source_completeness_claimed':False}
    if not key or not client:
        return dict(report,category='missing_configuration')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),auth.NoRedirect(),urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    headers = {'Authorization':key,'X-Client-ID':client,'Accept':'application/json','User-Agent':'getnote-bounded-detail-probe/1'}
    page, result = request(opener,headers)
    report['requests_attempted'] += 1
    report['list'] = result
    if page is None or not isinstance(page.get('notes'),list):
        return dict(report,category='list_failed')
    report['list']['count'] = len(page['notes'])
    report['samples'] = []
    failed = False
    for kind, types in KINDS.items():
        summary = next((n for n in page['notes'] if isinstance(n,dict) and n.get('note_type') in types), None)
        sample = {'kind':kind}
        if summary is None:
            sample['category'] = 'not_in_first_page'
        else:
            try:
                nid = identity(summary)
                payload, detail_result = request(opener,headers,nid)
                report['requests_attempted'] += 1
                sample.update(detail_result)
                if payload is not None and isinstance(payload.get('note'),dict):
                    sample.update(inspect_detail(summary,payload['note']))
                elif detail_result['category'] == 'ok':
                    sample['category'] = 'invalid_detail_shape'
            except Exception:
                sample['category'] = 'invalid_sample'
            failed |= sample['category'] != 'fields_available'
        report['samples'].append(sample)
    report['samples_available'] = sum(s.get('category') == 'fields_available' for s in report['samples'])
    category = 'sample_failed' if failed else 'bounded_probe_finished' if report['samples_available'] else 'no_samples_selected'
    return dict(report,category=category)


def main():
    try:
        result = probe()
    except Exception:
        result = {'service':'getnote','mode':'bounded_details','category':'request_failed'}
    print(json.dumps(result,sort_keys=True))
    return 0 if result['category'] == 'bounded_probe_finished' else 1


if __name__ == '__main__':
    sys.exit(main())
