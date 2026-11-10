"""Best-effort, inclusive monotonic timings; never transaction authority.

Only fixed phase names, counts and elapsed nanoseconds are emitted. Nested
phase totals overlap and MUST NOT be added. No identifiers or payload bytes.
Clock/output failures never suppress, replace or retry a business operation.
"""
from functools import wraps
import json
import sys
import time

LABELS = frozenset(('cas_total', 'current', 'build', 'push', 'poll_ref',
    'readback', 'commit_load', 'state_validate', 'source_head_guard',
    'post_cas_guard', 'confirm', 'remote_state_blob', 'checkpoint_callback'))

class Timer:
    def __init__(self, clock=None):
        self.clock = clock if clock is not None else getattr(time, 'monotonic_ns', None)
        self.rows = {}

    def _now(self):
        try:
            value = self.clock()
            return value if type(value) is int and value >= 0 else None
        except Exception:
            return None

    def call(self, label, fn, *args, **kwargs):
        start = self._now()
        failed = True
        try:
            value = fn(*args, **kwargs)
            failed = False
            return value
        finally:
            # Observation is deliberately outside the business error contract.
            try:
                end = self._now()
                if label in LABELS:
                    row = self.rows.setdefault(label, {'calls': 0, 'errors': 0,
                        'timed_calls': 0, 'elapsed_ns': 0})
                    row['calls'] += 1
                    row['errors'] += int(failed)
                    if start is not None and end is not None and end >= start:
                        row['timed_calls'] += 1
                        row['elapsed_ns'] += end - start
            except Exception:
                pass

    def emit(self):
        try:
            value = {'contract': 'get-journal-timing-1', 'inclusive': True,
                'phases': {k: dict(v) for k, v in self.rows.items() if k in LABELS}}
            print('GET_JOURNAL_TIMING_V1 ' + json.dumps(value, sort_keys=True,
                separators=(',', ':')), file=sys.stderr, flush=True)
        except Exception:
            pass


def observed(label, *, emit=False):
    def decorate(fn):
        @wraps(fn)
        def wrapped(self, *args, **kwargs):
            timer = getattr(self, '_timing', None)
            if not isinstance(timer, Timer):
                return fn(self, *args, **kwargs)
            try:
                return timer.call(label, fn, self, *args, **kwargs)
            finally:
                if emit:
                    timer.emit()
        return wrapped
    return decorate
