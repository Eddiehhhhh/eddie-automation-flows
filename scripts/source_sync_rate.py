"""Opt-in, process-wide bounded pacing for the private full replay only."""
import email.utils
import math
import random
import threading
import time

REASONS = ('qps_global', 'qps_bucket', 'quota_day', 'quota_month', 'not_member')
STOP_REASONS = ('quota_day', 'quota_month', 'not_member')


class RateLimitFailure(RuntimeError):
    def __init__(self, reason='unknown', http_status=None, budget=False):
        self.reason = reason if reason in REASONS else 'unknown'
        self.http_status = 429 if http_status == 429 else None
        self.budget = bool(budget)
        super().__init__('Get rate limit stopped')


def retry_seconds(value, now):
    """Seconds or timezone-aware HTTP date; never truncate server wait."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return float('inf') if value > 10**18 else max(0, value)
    if isinstance(value, float):
        return max(0, value) if not math.isnan(value) else 0
    if not isinstance(value, str):
        return 0
    value = value.strip()
    if value.isascii() and value.isdigit():
        return float('inf') if len(value) > 18 else int(value)
    if len(value) > 128:
        return 0
    try:
        parsed = email.utils.parsedate_to_datetime(value)
        return max(0, parsed.timestamp() - now) if parsed.tzinfo else 0
    except (TypeError, ValueError, OverflowError):
        return 0


class RequestPolicy:
    """One-second shared start spacing; bounded shared cooldown on 429.

    Sleeping never holds the lock. All waits recheck stop/deadline every second,
    so queued worker threads unwind promptly after a quota or deadline failure.
    """
    def __init__(self, seconds=110*60, clock=None, wall=None, sleep=None, jitter=None):
        self.clock = clock or time.monotonic
        self.wall = wall or time.time
        self.sleep = sleep or time.sleep
        self.jitter = jitter or (lambda: random.uniform(0, 5))
        self.deadline = self.clock() + seconds
        self.next_start = 0
        self.cooldown = 0
        self.stopped = None
        self.lock = threading.Lock()

    def before_request(self):
        while True:
            with self.lock:
                if self.stopped:
                    raise self.stopped
                now = self.clock()
                target = max(self.next_start, self.cooldown)
                if now >= self.deadline or target >= self.deadline:
                    self.stopped = RateLimitFailure(budget=True)
                    raise self.stopped
                delay = target - now
                if delay <= 0:
                    self.next_start = now + 1.0
                    return
            self.sleep(min(delay, 1.0))

    def rate_limited(self, attempt, retry_after=None, data=None, http_status=None):
        error = data.get('error') if isinstance(data, dict) else None
        error = error if isinstance(error, dict) else {}
        reason = error.get('reason')
        reason = reason if isinstance(reason, str) and reason in REASONS else 'unknown'
        code = error.get('code')
        if code == 10201:
            reason = 'not_member'
        waits = [retry_seconds(retry_after, self.wall())]
        for container in (data, error):
            rate = container.get('rate_limit') if isinstance(container, dict) else None
            if isinstance(rate, dict):
                waits.append(retry_seconds(rate.get('retry_after'), self.wall()))
        delay = max([60 * (2 ** min(attempt, 3)) + self.jitter()] + waits)
        with self.lock:
            if self.stopped:
                raise self.stopped
            budget = self.clock() + delay >= self.deadline
            if reason in STOP_REASONS or code == 42900 or attempt >= 4 or budget:
                self.stopped = RateLimitFailure(reason, http_status, budget)
                raise self.stopped
            self.cooldown = max(self.cooldown, self.clock() + delay)
