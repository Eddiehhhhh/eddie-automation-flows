"""Accept trusted cloud wake events before loading private synchronization credentials."""
import json
import os
from pathlib import Path
import re

from core import Hold

SOURCES = {
    'Audit Raw filenames': 'schedule',
    'Link Notion workouts to diary': 'schedule',
    'Sync Notion flomo notes to Raw': 'schedule',
    'Get cloud sync wake': 'workflow_dispatch',
}


def verify(event_name, event, repository):
    if not isinstance(repository, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
        raise Hold('trigger_repository_invalid')
    if event_name in ('schedule', 'workflow_dispatch'):
        return {'trigger': event_name}
    if event_name != 'workflow_run' or not isinstance(event, dict):
        raise Hold('trigger_event_invalid')
    run = event.get('workflow_run')
    if not isinstance(run, dict):
        raise Hold('trigger_source_invalid')
    name = run.get('name')
    if (not isinstance(name, str) or name not in SOURCES or run.get('event') != SOURCES[name]
            or run.get('status') != 'completed' or run.get('head_branch') != 'main'
            or not isinstance(run.get('head_repository'), dict)
            or run['head_repository'].get('full_name') != repository
            or not isinstance(event.get('repository'), dict)
            or event['repository'].get('full_name') != repository
            or type(run.get('id')) is not int or run['id'] <= 0
            or type(run.get('run_attempt')) is not int or run['run_attempt'] <= 0):
        raise Hold('trigger_source_untrusted')
    return {'trigger': 'workflow_run', 'source_workflow': name,
            'source_event': run['event'], 'source_run_id': run['id'],
            'source_attempt': run['run_attempt']}


if __name__ == '__main__':
    try:
        with Path(os.environ['GITHUB_EVENT_PATH']).open('rb') as stream:
            raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise Hold('trigger_payload_size')
        result = verify(os.environ['GITHUB_EVENT_NAME'], json.loads(raw), os.environ['GITHUB_REPOSITORY'])
    except Exception:
        print('Cloud trigger rejected before private access.')
        raise SystemExit(1)
    print(json.dumps(result, sort_keys=True))
