"""Bounded, privacy-safe retry/rate pacing for an injected HTTP transport.

Only GET and explicitly enabled exact Notion read-query POST routes are retried.
Writes, including a 429 response, are never retried here: durable sink intent and
readback must decide whether they took effect. No request/response data is logged.
An elapsed budget bounds retries/sleeps, not an already in-flight socket request.
"""
from datetime import timezone
from email.utils import parsedate_to_datetime
import math
import random
import re
import threading
import time

_TRANSIENT = frozenset((429, 500, 502, 503, 504))
_UUID = r'(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})'
_NOTION_QUERY = re.compile(r'^/v1/databases/' + _UUID + r'/query$')

class HTTPFailure(RuntimeError):
    def __init__(self, status, retry_after=None):
        if type(status) is not int or not 100 <= status <= 599:
            raise ValueError('invalid_http_status')
        self.status = status
        # Header may be needed for timing, but never appears in str/repr/args.
        self.retry_after = retry_after if isinstance(retry_after, str) and len(retry_after) <= 256 else None
        super().__init__('http_' + str(status))

class TransportFailure(RuntimeError):
    def __init__(self):
        super().__init__('transport_uncertain')

class RetryStopped(RuntimeError):
    def __init__(self, reason, attempts):
        if reason not in ('attempt_limit', 'elapsed_budget', 'server_delay_exceeds_budget'):
            raise ValueError('invalid_retry_reason')
        self.reason, self.attempts = reason, attempts
        super().__init__('retry_' + reason)

def retry_after_seconds(value, now=None):
    """Parse RFC delay-seconds or HTTP-date; invalid/past values are ignored/zero."""
    if not isinstance(value, str) or len(value) > 256:
        return None
    value = value.strip()
    if value.isascii() and value.isdigit():
        try:
            result = float(value)
            return result if math.isfinite(result) else None
        except ValueError:
            return None
    try:
        parsed = parsedate_to_datetime(value)
        # HTTP dates are GMT; accepting a timezone-less date could depend on host TZ.
        if parsed.tzinfo is None:
            return None
        timestamp = parsed.astimezone(timezone.utc).timestamp()
        result = max(0.0, timestamp - (time.time() if now is None else now))
        return result if math.isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None

class RetryTransport:
    def __init__(self, transport, *, allow_notion_readonly_posts=False,
                 max_attempts=4, base_delay=0.5, max_delay=30.0, elapsed_budget=90.0,
                 min_interval=0.0, sleep=time.sleep, monotonic=time.monotonic,
                 wall_time=time.time, random_value=random.random):
        if type(max_attempts) is not int or not 1 <= max_attempts <= 10:
            raise ValueError('invalid_retry_attempt_limit')
        for n in (base_delay, max_delay, elapsed_budget, min_interval):
            if isinstance(n, bool) or not isinstance(n, (int, float)) or not math.isfinite(n) or n < 0:
                raise ValueError('invalid_retry_timing')
        if base_delay > max_delay or elapsed_budget <= 0 or type(allow_notion_readonly_posts) is not bool:
            raise ValueError('invalid_retry_configuration')
        self.transport = transport
        self.allow_notion_readonly_posts = allow_notion_readonly_posts
        self.max_attempts, self.base_delay, self.max_delay = max_attempts, base_delay, max_delay
        self.elapsed_budget, self.min_interval = elapsed_budget, min_interval
        self.sleep, self.monotonic, self.wall_time, self.random_value = sleep, monotonic, wall_time, random_value
        self._last_start = None
        self._lock = threading.Lock()

    def _readonly(self, method, path):
        if method == 'GET':
            return True
        return (method == 'POST' and self.allow_notion_readonly_posts
                and (path == '/v1/search' or bool(_NOTION_QUERY.fullmatch(path))))

    def request(self, method, path, body=None):
        # No method normalization: an unrecognized form never gains retry permission.
        readonly = self._readonly(method, path)
        with self._lock:
            started = self.monotonic()
            attempts = 0
            while True:
                now = self.monotonic()
                pace = 0.0 if self._last_start is None else max(0.0, self._last_start + self.min_interval - now)
                if now - started + pace >= self.elapsed_budget:
                    raise RetryStopped('elapsed_budget', attempts) from None
                if pace:
                    self.sleep(pace)
                self._last_start = self.monotonic()
                attempts += 1
                try:
                    return self.transport.request(method, path, body)
                except (HTTPFailure, TransportFailure) as error:
                    if not readonly:
                        # Including a write rejected by rate limiting: reconciliation
                        # belongs to the sink; no automatic second write is issued.
                        raise
                    if isinstance(error, HTTPFailure) and error.status not in _TRANSIENT:
                        raise
                    if attempts >= self.max_attempts:
                        raise RetryStopped('attempt_limit', attempts) from None
                    remaining = self.elapsed_budget - (self.monotonic() - started)
                    cap = min(self.max_delay, self.base_delay * (2 ** (attempts - 1)))
                    jitter = self.random_value()
                    if not isinstance(jitter, (int, float)) or not math.isfinite(jitter) or not 0 <= jitter <= 1:
                        raise ValueError('invalid_jitter_source') from None
                    delay = cap * (0.5 + 0.5 * jitter)
                    advised = retry_after_seconds(error.retry_after, self.wall_time()) if isinstance(error, HTTPFailure) else None
                    if advised is not None:
                        # Never cap a server's longer Retry-After and retry too soon.
                        if advised > self.max_delay or advised >= remaining:
                            raise RetryStopped('server_delay_exceeds_budget', attempts) from None
                        delay = max(delay, advised)
                    if delay >= remaining:
                        raise RetryStopped('elapsed_budget', attempts) from None
                    if delay:
                        self.sleep(delay)
