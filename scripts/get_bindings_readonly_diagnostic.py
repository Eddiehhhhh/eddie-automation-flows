"""One bounded observation of the unchanged, pinned private binding loader.

Not a migration, provenance receipt, policy transition or permission to write.
Only the existing five GETs and temporary private local materialization run.
"""
import importlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

RUNTIME_SHA = 'aecf535c2f264d26fccea6526a615156e6d35537'
REPAIR_REF = 'refs/heads/fix/get-shadow-diagnostics-20261012'
PUBLIC_REPOSITORY = 'Eddiehhhhh/eddie-automation-flows'
PRIVATE_API = 'https://api.github.com/repos/Eddiehhhhh/eddie-llm-wiki'
CONTRACT = 'get-private-bindings-diagnostic-1'
MAX_REQUESTS = 5


class BoundaryError(ValueError):
    pass


def fail():
    raise BoundaryError('diagnostic_boundary_blocked') from None


def failure_diagnostic(error):
    """Fixed enums and bounded numbers; no error text, URLs or response bodies."""
    value = {'contract': CONTRACT, 'error_class': 'other', 'http_status': None,
             'rate_limit_reset_epoch': None}
    pending = [error]; seen = set(); rank = 0
    classes = ((ValueError, 'value_error', 1), (OSError, 'os_error', 2),
               (ConnectionError, 'connection_error', 3),
               (FileExistsError, 'file_exists_error', 3),
               (FileNotFoundError, 'file_not_found_error', 3),
               (PermissionError, 'permission_error', 3),
               (TimeoutError, 'timeout_error', 4),
               (urllib.error.URLError, 'url_error', 5))
    try:
        while pending and len(seen) < 8:
            current = pending.pop()
            if not isinstance(current, BaseException) or id(current) in seen:
                continue
            seen.add(id(current))
            if isinstance(current, urllib.error.HTTPError):
                value['error_class'] = 'http_error'
                status = current.code
                if type(status) is int and 100 <= status <= 599:
                    value['http_status'] = status
                try:
                    reset = current.headers.get('X-RateLimit-Reset')
                    if (type(reset) is str and re.fullmatch(r'[0-9]{1,10}', reset)
                            and int(reset) <= 4102444800):
                        value['rate_limit_reset_epoch'] = int(reset)
                except Exception:
                    pass
                return value
            for cls, label, priority in classes:
                if priority > rank and isinstance(current, cls):
                    value['error_class'] = label; rank = priority
            pending.extend((current.__context__, current.__cause__))
    except Exception:
        pass
    return value


class ReadOnlyOpener:
    """Deny before transport: fixed endpoint family, GET only, five calls total."""
    def __init__(self, opener):
        self.opener = opener
        self.failure = None
        self.calls = 0

    def open(self, request, *, timeout):
        if (not isinstance(request, urllib.request.Request)
                or request.get_method() != 'GET' or request.data is not None
                or timeout != 60 or self.calls >= MAX_REQUESTS):
            fail()
        url = request.full_url
        if not isinstance(url, str) or not url.startswith(PRIVATE_API):
            fail()
        suffix = url[len(PRIVATE_API):]
        if (suffix not in ('', '/git/ref/heads/main')
                and not re.fullmatch(r'/git/(commits|blobs)/[0-9a-f]{40}', suffix)
                and not re.fullmatch(r'/git/trees/[0-9a-f]{40}\?recursive=1', suffix)):
            fail()
        self.calls += 1
        self.failure = None
        try:
            return self.opener.open(request, timeout=timeout)
        except Exception as error:
            try:
                self.failure = failure_diagnostic(error)
            except Exception:
                self.failure = None
            raise


class ReadOnlyRemote:
    """Expose only the unchanged loader's five read methods, never create_only."""
    def __init__(self, remote):
        self.__remote = remote

    def ref(self, name):
        if name != 'refs/heads/main': fail()
        return self.__remote.ref(name)

    def metadata(self):
        return self.__remote.metadata()

    def commit(self, commit):
        return self.__remote.commit(commit)

    def tree_entries(self, tree, recursive=False):
        if recursive is not True: fail()
        return self.__remote.tree_entries(tree, recursive)

    def blob(self, blob, size):
        return self.__remote.blob(blob, size)


def load_runtime():
    """No private credential is looked up until the old runtime identity passes."""
    runtime = Path(__file__).resolve().parents[1] / 'runtime'
    if runtime.is_symlink() or not (runtime / '.git').is_dir(): fail()
    for args, expected in ((('rev-parse', 'HEAD'), (RUNTIME_SHA + '\n').encode()),
                           (('status', '--porcelain', '--untracked-files=all'), b'')):
        result = subprocess.run(['git', '-C', str(runtime), *args],
                                capture_output=True, timeout=30, check=False)
        if result.returncode != 0 or result.stdout != expected: fail()
    sys.path.insert(0, str(runtime / 'scripts'))
    modules = [importlib.import_module(name) for name in
               ('get_private_runtime_bindings', 'get_notion_route_selection',
                'get_private_preview_retention')]
    for module in modules:
        if Path(module.__file__).resolve().parent != runtime / 'scripts': fail()
    return modules


def main(argv=None):
    observed = None
    try:
        argv = sys.argv[1:] if argv is None else argv
        if (argv or os.environ.get('GITHUB_EVENT_NAME') != 'workflow_dispatch'
                or os.environ.get('GITHUB_REF') != REPAIR_REF
                or os.environ.get('GITHUB_REPOSITORY') != PUBLIC_REPOSITORY
                or os.environ.get('GET_BINDINGS_DIAGNOSTIC_ONLY') != 'true'
                or os.environ.get('APPLY') != 'false'
                or os.environ.get('NOTION_MODE') != 'canonical'
                or os.environ.get('NOTION_INPUT_SHA256', '') != ''):
            fail()
        bindings, route, transport = load_runtime()
        route.selection()  # Original credential-free selection guard unchanged.
        token = os.environ.get('PRIVATE_VAULT_TOKEN')
        if not token: fail()
        remote = transport.GitHubRemote(None, token)
        observed = ReadOnlyOpener(remote.opener)
        remote.opener = observed
        # Original bootstrap validates fixed repo/privacy, current ref, tree,
        # blob identity, original capsule digest and 0600 create-only local file.
        # The temporary parent and its private proof are never logged/uploaded.
        with tempfile.TemporaryDirectory(prefix='get-bindings-observe-') as root:
            bindings.bootstrap(ReadOnlyRemote(remote), Path(root) / 'private' / 'bindings.json')
        payload = {'contract': CONTRACT, 'error_class': 'none', 'http_status': None,
                   'rate_limit_reset_epoch': None}
        print(json.dumps(payload, sort_keys=True), file=sys.stderr, flush=True)
        return 0
    except Exception as error:
        try:
            diagnostic = observed.failure if observed is not None else None
            print(json.dumps(diagnostic or failure_diagnostic(error), sort_keys=True),
                  file=sys.stderr, flush=True)
        except Exception:
            pass
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
