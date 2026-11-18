"""Publish one large content-addressed asset using Git's binary transport."""
import base64
import os
import re
import subprocess
import tempfile
from urllib.parse import quote

from core import Hold


def publish_asset(http, token, repo, branch, target, data, expected):
    if not re.fullmatch(r'get-direct/[a-z0-9_-]+/assets/[0-9a-f]{64}\.bin', target):
        raise Hold('media_git_invalid_path')
    prefix = '/repos/' + repo
    ref = 'refs/heads/' + branch
    remote = 'https://github.com/' + repo + '.git'
    visible = http.request('GET', prefix)
    if not visible or visible.get('private') is not True:
        raise Hold('private_repository_required')
    auth = base64.b64encode(('x-access-token:' + token).encode()).decode()
    env = {key: os.environ[key] for key in ('PATH', 'SYSTEMROOT') if key in os.environ}
    env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
               GIT_TERMINAL_PROMPT='0', GIT_ASKPASS='/bin/false',
               GIT_CONFIG_COUNT='1', GIT_CONFIG_KEY_0='http.https://github.com/.extraheader',
               GIT_CONFIG_VALUE_0='AUTHORIZATION: basic ' + auth,
               GIT_AUTHOR_NAME='Get direct sync', GIT_AUTHOR_EMAIL='get-direct-sync@users.noreply.github.com',
               GIT_COMMITTER_NAME='Get direct sync', GIT_COMMITTER_EMAIL='get-direct-sync@users.noreply.github.com')
    ref_api = prefix + '/git/ref/heads/' + quote(branch, safe='/')

    def current_head():
        obj = (http.request('GET', ref_api) or {}).get('object', {})
        sha = obj.get('sha', '')
        if obj.get('type') != 'commit' or not re.fullmatch('[0-9a-f]{40}', sha):
            raise Hold('media_git_ref_invalid')
        return sha

    with tempfile.TemporaryDirectory(prefix='get-binary-') as work:
        def git(*args, payload=None):
            try:
                result = subprocess.run(
                    ['git', '-c', 'credential.helper=', '-c', 'http.followRedirects=false',
                     '-c', 'core.hooksPath=' + os.devnull, '-C', work, *args],
                    env=env, input=payload, capture_output=True, timeout=240)
            except (OSError, subprocess.TimeoutExpired):
                raise Hold('media_git_transport_failed') from None
            if result.returncode:
                raise Hold('media_git_transport_failed')
            return result.stdout

        parent = current_head()
        git('init', '--bare', '--quiet')
        git('check-ref-format', ref)
        git('fetch', '--quiet', '--no-tags', '--depth=1', '--filter=blob:none', remote, ref)
        if git('rev-parse', 'FETCH_HEAD').decode().strip() != parent:
            raise Hold('media_git_ref_changed')
        git('read-tree', parent)
        existing = git('ls-files', '--stage', '--', target)
        if existing:
            if existing != f'100644 {expected} 0\t{target}\n'.encode():
                raise Hold('media_github_content_conflict')
            return parent
        if git('hash-object', '-w', '--stdin', payload=data).decode().strip() != expected:
            raise Hold('media_github_readback_mismatch')
        git('update-index', '--add', '--cacheinfo', f'100644,{expected},{target}')
        tree = git('write-tree').decode().strip()
        commit = git('commit-tree', tree, '-p', parent, '-m', 'Archive Get binary asset').decode().strip()
        if current_head() != parent:
            raise Hold('media_git_ref_changed')
        try:
            git('push', '--quiet', remote, commit + ':' + ref)
        except Hold:
            if current_head() != commit:
                raise Hold('github_write_requires_reconcile') from None
        return commit
