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
    name = 'get-clock-validation' if not recover and ticks != '0' else 'get-clock-hourly'
    policy = request(f'{base}/environments/{name}')
    timers = [p.get('wait_timer') for p in policy.get('protection_rules', [])
              if p.get('type') == 'wait_timer']
    if timers != [1 if name == 'get-clock-validation' else 60]:
        raise ValueError('clock_wait_timer_invalid')
    runs_path = f'{base}/actions/workflows/{WORKFLOW}/runs?branch=main&per_page=100'
    title = f'Get clock {run_id}'

    def runs():
        return request(runs_path)['workflow_runs']

    # One pending clock is sufficient; manual restarts and completed-run reruns must not fork it.
    if any(str(r['id']) != run_id and r['status'] != 'completed' for r in runs()):
        return 'already_armed'
    return dispatch(request, base, WORKFLOW, title,
                    {'validation_ticks': '0' if recover else str(max(0, int(ticks) - 1)),
                     'parent_run': run_id}, pause)


def dispatch(request, base, workflow, title, inputs, pause=time.sleep):
    def runs():
        return request(f'{base}/actions/workflows/{workflow}/runs?branch=main&per_page=100')['workflow_runs']
    if any(r.get('display_title') == title for r in runs()):
        return 'successor_already_recorded'
    try:
        request(f'{base}/actions/workflows/{workflow}/dispatches', {'ref': 'main', 'inputs': inputs})
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
        recover = sys.argv[1:] == ['--recover']
        result = {'cloud_clock': arm(os.environ, recover=recover)}
        if not recover:
            run_id = os.environ['GITHUB_RUN_ID']
            result['sync_dispatch'] = dispatch(api, 'repos/' + os.environ['GITHUB_REPOSITORY'],
                'get-notion-incremental.yml', 'Direct Get clock-' + run_id,
                {'mode': 'sync', 'batch_size': '10', 'dispatch_key': 'clock-' + run_id})
        print(json.dumps(result))
    except Exception:
        print('Cloud clock rearm failed; inspect this run before restarting the clock.')
        raise SystemExit(1)
