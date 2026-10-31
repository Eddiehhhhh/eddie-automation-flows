#!/usr/bin/env python3
"""Checkout the existing private vault without logging repository metadata."""
import argparse
import base64
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import urllib.request


REPOSITORY = 'Eddiehhhhh/eddie-llm-wiki'


def verify_private_repository(token):
    request = urllib.request.Request('https://api.github.com/repos/' + REPOSITORY,
        headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json'})
    with urllib.request.urlopen(request, timeout=30) as response:
        metadata = json.load(response)
    if metadata.get('private') is not True:
        raise ValueError('Expected a private repository')
    return metadata


def verify_private_checkout(root, token):
    """Read-only identity and visibility gate for private state writers."""
    try:
        if not token:
            raise ValueError("Private checkout verification failed")
        expected = (r"(?:https://github\.com/|git@github\.com:|ssh://git@(?:github\.com|github-eddie-llm-wiki)/)"
                    + re.escape(REPOSITORY) + r"(?:\.git)?")
        for args in [("remote", "get-url", "--all", "origin"), ("remote", "get-url", "--push", "--all", "origin")]:
            origin = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=15)
            urls = origin.stdout.splitlines()
            if origin.returncode or len(urls) != 1 or not re.fullmatch(expected, urls[0]):
                raise ValueError("Private checkout verification failed")
        metadata = verify_private_repository(token)
        if metadata.get("full_name") != REPOSITORY or metadata.get("private") is not True:
            raise ValueError("Private checkout verification failed")
    except Exception:
        raise ValueError("Private checkout verification failed") from None


def checkout(destination):
    token = os.environ['PRIVATE_VAULT_TOKEN']
    if not token or destination.exists():
        raise ValueError('Invalid checkout configuration')
    verify_private_repository(token)
    auth = base64.b64encode(('x-access-token:' + token).encode()).decode()
    env = {key: value for key, value in os.environ.items()
           if key != 'PRIVATE_VAULT_TOKEN' and not key.startswith('GIT_CONFIG_')}
    env.update({'GIT_CONFIG_COUNT': '1',
                'GIT_CONFIG_KEY_0': 'http.https://github.com/.extraheader',
                'GIT_CONFIG_VALUE_0': 'AUTHORIZATION: basic ' + auth,
                'GIT_TERMINAL_PROMPT': '0'})
    # The header exists only in this subprocess environment, not argv or config.
    # Both streams are captured even on failure; never print CalledProcessError.
    subprocess.run(['git', '-c', 'credential.helper=', '-c', 'core.hooksPath=/dev/null',
                    'clone', '--depth', '1', '--single-branch', '--branch', 'main',
                    '--no-tags', 'https://github.com/' + REPOSITORY + '.git', str(destination)],
                   env=env, capture_output=True, timeout=600, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination', required=True, type=Path)
    args = parser.parse_args()
    try:
        checkout(args.destination)
    except Exception:
        print('Private checkout failed; details suppressed.', file=sys.stderr)
        return 1
    print('Private checkout completed.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
