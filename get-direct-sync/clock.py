"""Rearm GitHub's server-side wait timer without note credentials or idle runners."""
import json
import os
import re
import subprocess
import sys
import time

WORKFLOW = 'get-cloud-clock.yml'


def api(path, body=None):
    args = ['gh', 'api', path]
    if body is not None:
        args += ['--method', 'POST', '--input', '-']
    result = subprocess.run(args, input=None if body is None else json.dumps(body),
                            capture_output=True, text=True, timeout=25, check=True)
    return json.loads(result.stdout) if result.stdout.strip() else None


def arm(env, request=api, recover=False, pause=time.sleep):
    repo = env.get('GITHUB_REPOSITORY', '')
    run_id = env.get('GITHUB_RUN_ID', '')
    ticks = env.get('GET_CLOCK_VALIDATION_TICKS', '0')
    if (not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo)
            or not re.fullmatch(r'[1-9][0-9]*', run_id)
            or env.get('GITHUB_REF') != 'refs/heads/main'
            or ticks not in ('0', '1', '2')
            or env.get('GITHUB_EVENT_NAME') not in ('workflow_dispatch', 'workflow_run')):
        raise ValueError('clock_context_invalid')
    base = f'repos/{repo}'
    if not recover:
        name = 'get-clock-validation' if ticks != '0' else 'get-clock-hourly'
        policy = request(f'{base}/environments/{name}')
        timers = [p.get('wait_timer') for p in policy.get('protection_rules', [])
                  if p.get('type') == 'wait_timer']
        if timers != [1 if ticks != '0' else 60]:
            raise ValueError('clock_wait_timer_invalid')
    runs_path = f'{base}/actions/workflows/{WORKFLOW}/runs?branch=main&per_page=100'
    title = f'Get clock {run_id}'

    def runs():
        return request(runs_path)['workflow_runs']

    # One pending clock is sufficient; manual restarts and completed-run reruns must not fork it.
    if any(str(r['id']) != run_id and r['status'] != 'completed' for r in runs()):
        return 'already_armed'
    if any(r.get('display_title') == title for r in runs()):
        return 'successor_already_recorded'
    body = {'ref': 'main', 'inputs': {'validation_ticks': str(max(0, int(ticks) - 1)),
                                    'parent_run': run_id}}
    try:
        request(f'{base}/actions/workflows/{WORKFLOW}/dispatches', body)
    except Exception:
        # An uncertain POST is reconciled by its unique title, never repeated.
        pass
    for _ in range(6):
        if any(r.get('display_title') == title for r in runs()):
            return 'next_tick_verified'
        pause(2)
    raise RuntimeError('clock_dispatch_unverified')


if __name__ == '__main__':
    try:
        print(json.dumps({'cloud_clock': arm(os.environ, recover=sys.argv[1:] == ['--recover'])}))
    except Exception:
        print('Cloud clock rearm failed; inspect this run before restarting the clock.')
        raise SystemExit(1)
