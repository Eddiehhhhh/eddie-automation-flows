"""Offline GitHub Actions context-availability check for this controller.

This validates expression placement, not server admission or a business run.
The caller parses YAML without executing tags. Unsupported expression locations
fail closed; the allowlists follow GitHub's context-availability reference.
"""
import re

EARLY = frozenset(('github', 'needs', 'strategy', 'matrix', 'vars', 'secrets', 'inputs'))
STEP = EARLY | frozenset(('job', 'runner', 'env', 'steps'))
EXPRESSIONS = re.compile(r'\$\{\{(.*?)\}\}', re.S)
STRINGS = re.compile(r"'(?:[^']|'')*'")
REFERENCES = re.compile(r'(?<![.A-Za-z_0-9])([A-Za-z_][A-Za-z_0-9]*)\s*(?:\.|\[)')


def allowed(path):
    if path[:1] == ('env',):
        return frozenset(('github', 'secrets', 'inputs', 'vars'))
    if len(path) >= 3 and path[0] == 'jobs':
        field = path[2]
        if field == 'env': return EARLY
        if field in ('name', 'runs-on', 'timeout-minutes', 'continue-on-error', 'concurrency'):
            return EARLY - {'secrets'}
        if field == 'if': return frozenset(('github', 'needs', 'vars', 'inputs'))
        if field == 'steps' and len(path) >= 5:
            step_field = path[4]
            if step_field == 'if': return STEP - {'secrets'}
            if step_field in ('name', 'run', 'env', 'with', 'continue-on-error', 'timeout-minutes', 'working-directory'):
                return STEP
    if path[:1] in (('run-name',), ('concurrency',)):
        return frozenset(('github', 'inputs', 'vars'))
    return frozenset()


def validate(workflow):
    errors = []
    def walk(value, path):
        if isinstance(value, dict):
            for key, child in value.items(): walk(child, path + (str(key),))
        elif isinstance(value, list):
            for index, child in enumerate(value): walk(child, path + (str(index),))
        elif isinstance(value, str):
            for expression in EXPRESSIONS.findall(value):
                refs = set(REFERENCES.findall(STRINGS.sub('', expression)))
                if not refs <= allowed(path):
                    errors.append({'path': '.'.join(path), 'unavailable_contexts': sorted(refs - allowed(path))})
    walk(workflow, ())
    return errors
