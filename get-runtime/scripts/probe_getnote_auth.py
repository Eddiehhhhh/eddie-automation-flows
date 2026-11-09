#!/usr/bin/env python3
"""One fixed-host Get list request; emit only a bounded, non-content verdict.

Linux/macOS CLI for a manually dispatched GitHub Actions probe. No secret
lookup, persistence, redirects, retries, pagination, note-detail or asset calls.
"""
import contextlib
import json
import os
import signal
import ssl
import sys
import urllib.error
import urllib.request

URL = 'https://openapi.biji.com/open/api/v1/resource/note/list'
SOCKET_TIMEOUT_SECONDS = 10
DEADLINE_SECONDS = 20
MAX_RESPONSE_BYTES = 1024 * 1024


class RedirectBlocked(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Reject even same-host redirects; never forward credential headers.
        fp.close()
        raise RedirectBlocked()


@contextlib.contextmanager
def deadline():
    def expired(signum, frame):
        raise TimeoutError()
    previous = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, DEADLINE_SECONDS)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def verdict(category, http_status=None, count=None):
    result = {'service': 'getnote', 'authentication': 'unknown', 'category': category}
    if category == 'ok':
        result['authentication'] = 'accepted'
    elif category == 'auth_rejected':
        result['authentication'] = 'rejected'
    if type(http_status) is int and 100 <= http_status <= 599:
        result['http_status'] = http_status
    if type(count) is int and count >= 0:
        result['count'] = count
    return result


def probe():
    api_key = os.environ.get('GETNOTE_API_KEY')
    client_id = os.environ.get('GETNOTE_CLIENT_ID')
    if not api_key or not client_id:
        return verdict('missing_configuration')
    status = None
    try:
        with deadline():
            # No proxy lookup or credentials from the runner's local config.
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}), NoRedirect(),
                urllib.request.HTTPSHandler(context=ssl.create_default_context()),
            )
            request = urllib.request.Request(URL, method='GET', headers={
                'Authorization': api_key,
                'X-Client-ID': client_id,
                'Accept': 'application/json',
                'User-Agent': 'getnote-readonly-auth-probe/1',
            })
            with opener.open(request, timeout=SOCKET_TIMEOUT_SECONDS) as response:
                status = response.status
                if status != 200:
                    return verdict('unexpected_http_status', status)
                body = response.read(MAX_RESPONSE_BYTES + 1)
            if len(body) > MAX_RESPONSE_BYTES:
                return verdict('response_too_large', status)
            payload = json.loads(body)
            if not isinstance(payload, dict):
                return verdict('invalid_response', status)
            if payload.get('success') is False:
                error = payload.get('error')
                code = error.get('code') if isinstance(error, dict) else None
                category = {10001: 'auth_rejected', 10201: 'membership_required',
                            10202: 'rate_limited', 42900: 'rate_limited'}.get(code, 'api_error') if type(code) is int else 'api_error'
                return verdict(category, status)
            data = payload.get('data')
            if payload.get('success') is not True or not isinstance(data, dict) or not isinstance(data.get('notes'), list):
                return verdict('invalid_response', status)
            return verdict('ok', status, len(data['notes']))
    except RedirectBlocked:
        return verdict('redirect_blocked')
    except urllib.error.HTTPError as exc:
        code = exc.code
        exc.close()
        category = ('auth_rejected' if code in (401, 403) else
                    'rate_limited' if code == 429 else
                    'redirect_blocked' if 300 <= code < 400 else 'http_error')
        return verdict(category, code)
    except TimeoutError:
        return verdict('timeout', status)
    except urllib.error.URLError as exc:
        return verdict('timeout' if isinstance(exc.reason, TimeoutError) else 'transport_error', status)
    except (ValueError, UnicodeError):
        return verdict('invalid_response', status)
    except Exception:
        # Never print repr/str of exceptions: URLs, response text and header
        # validation exceptions can contain signed query strings or secrets.
        return verdict('request_failed', status)


def main():
    result = probe()
    print(json.dumps(result, sort_keys=True))
    return 0 if result['category'] == 'ok' else 1


if __name__ == '__main__':
    sys.exit(main())
