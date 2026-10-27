"""Observe one unchanged source-proof discovery, never a migration or retry.

The separate runtime checkout and original private capsule remain byte-pinned.
No original validator, budget, retry policy, journal, or native writer is changed.
Only fixed error enums, status codes, phases and bounded counters may be emitted.
"""
import hashlib
import http.client
import importlib
import json
import math
import os
from pathlib import Path
import re
import ssl
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

RUNTIME_SHA = '29f0e44a9997c8c7028acf6dba33a3dfcc463f15'
REPAIR_REF = 'refs/heads/fix/get-shadow-diagnostics-20261012'
PUBLIC_REPOSITORY = 'Eddiehhhhh/eddie-automation-flows'
PUBLIC_API = 'https://api.github.com/repos/' + PUBLIC_REPOSITORY
PRIVATE_API = 'https://api.github.com/repos/Eddiehhhhh/eddie-llm-wiki'
POLICY_SHA = 'e17c18d8a6731554361a7df1d0c1b9785925d70da1dfc6f5f878650e0bb414bc'
POLICY = {'code_sha': '29f0e44a9997c8c7028acf6dba33a3dfcc463f15', 'contract': 'get-public-actions-provenance-policy-1', 'controller_template_sha256': '1d568531b4672b89ec80eaa08532e152d9c13a400602cfaf5438a3cd24f73dc5', 'get_policy_sha256': '8649ac4c88fe466bdcfa2e8faab25dd4f702bd5dc2fb31f273a91961489a45cc', 'head_branch': 'main', 'job_name': 'canonical-get', 'publication_step_name': 'Emit verified canonical publication anchor', 'repository': 'Eddiehhhhh/eddie-automation-flows', 'repository_id': 1270807355, 'rules_sha256': 'f81ce00ae8490c45295781cbbd4ed5fe2877da410fd15826660b4fe395d0a0f4', 'workflow_id': 387242896, 'workflow_path': '.github/workflows/get-canonical.yml'}
CONTRACT = 'get-source-readonly-diagnostic-1'
MAX_COUNTER = 2**53 - 1
PHASES = {'open', 'read', 'close', 'validation', 'boundary'}
STAGES = {'binding', 'metadata', 'ref', 'commit', 'tree', 'blob', 'receipt', 'ancestry', 'source'}


class BoundaryError(ValueError):
    pass


def fail():
    raise BoundaryError('source_diagnostic_boundary_blocked') from None


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode() + b'\n'


def classify(error):
    """Do not inspect exception text, URL, body, partial bytes or full headers."""
    result = {'error_class': 'other', 'http_status': None,
              'rate_limit_reset_epoch': None}
    pending = [error]; seen = set(); rank = 0
    classes = ((ValueError, 'value_error', 1), (OSError, 'os_error', 2),
               (ConnectionError, 'connection_error', 3),
               (urllib.error.URLError, 'url_error', 4),
               (TimeoutError, 'timeout_error', 5),
               (http.client.IncompleteRead, 'incomplete_read', 6),
               (ssl.SSLError, 'tls_error', 7),
               (json.JSONDecodeError, 'json_error', 8),
               (BoundaryError, 'boundary_error', 9))
    try:
        while pending and len(seen) < 8:
            current = pending.pop()
            if not isinstance(current, BaseException) or id(current) in seen:
                continue
            seen.add(id(current))
            if isinstance(current, urllib.error.HTTPError):
                result['error_class'] = 'http_error'
                if type(current.code) is int and 100 <= current.code <= 599:
                    result['http_status'] = current.code
                reset = current.headers.get('X-RateLimit-Reset') if current.headers else None
                if type(reset) is str and re.fullmatch(r'[0-9]{1,10}', reset) and int(reset) <= 4102444800:
                    result['rate_limit_reset_epoch'] = int(reset)
                return result
            for cls, name, priority in classes:
                if isinstance(current, cls) and priority > rank:
                    result['error_class'] = name; rank = priority
            pending.extend((current.__cause__, current.__context__))
            if isinstance(current, urllib.error.URLError) and isinstance(current.reason, BaseException):
                pending.append(current.reason)
    except Exception:
        pass
    return result


def endpoint(request, channel):
    if (not isinstance(request, urllib.request.Request) or request.get_method() != 'GET'
            or request.data is not None): fail()
    url = request.full_url
    if not isinstance(url, str) or any(c in url for c in ('\\', '\r', '\n', '#')): fail()
    if channel == 'private':
        if not url.startswith(PRIVATE_API): fail()
        suffix = url[len(PRIVATE_API):]
        if suffix == '': return 'metadata'
        if suffix == '/git/ref/heads/main': return 'ref'
        for kind in ('commits', 'trees', 'blobs'):
            pattern = '/git/' + kind + '/[0-9a-f]{40}'
            if kind == 'trees': pattern += r'(?:\?recursive=1)?'
            if re.fullmatch(pattern, suffix): return kind[:-1]
        fail()
    if channel != 'public': fail()
    if url.startswith(PUBLIC_API):
        suffix = url[len(PUBLIC_API):]
        patterns = (r'', r'/actions/workflows/387242896', r'/actions/workflows/387242896/runs\?per_page=30&page=[123]',
                    r'/actions/runs/[1-9][0-9]*(?:/attempts/[1-9][0-9]*)?',
                    r'/actions/runs/[1-9][0-9]*/attempts/[1-9][0-9]*/jobs\?per_page=100&page=1',
                    r'/actions/jobs/[1-9][0-9]*/logs',
                    r'/contents/\.github/workflows/get-canonical\.yml\?ref=[0-9a-f]{40}')
        if not any(re.fullmatch(p, suffix) for p in patterns): fail()
        return 'source'
    # The original public client alone authenticates the one log redirect.
    # This extra transport guard cannot forward any credential to a CDN.
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname or ''
    if (parsed.scheme != 'https' or parsed.username or parsed.password
            or parsed.port not in (None, 443) or parsed.fragment
            or not any(host.endswith(s) for s in ('.blob.core.windows.net', '.actions.githubusercontent.com', '.githubusercontent.com'))
            or any(k.lower() in ('authorization', 'proxy-authorization', 'cookie') for k, _ in request.header_items())):
        fail()
    return 'source'


class Observations:
    def __init__(self):
        self.failure = None
        self.last_stage = 'source'
        self.source_counts = {}
        self.counters = {k: 0 for k in ('public_attempts', 'private_attempts',
                                       'public_read_bytes', 'private_read_bytes',
                                       'public_failed_read_bound', 'private_failed_read_bound')}

    def add(self, key, amount):
        if type(amount) is not int or amount < 0 or self.counters[key] + amount > MAX_COUNTER: fail()
        self.counters[key] += amount

    def failed(self, error, phase, stage):
        value = classify(error)
        self.failure = {**value, 'phase': phase if phase in PHASES else 'boundary',
                        'stage': stage if stage in STAGES else 'source'}


class ReadResponse:
    def __init__(self, response, observations, channel, stage):
        self.response = response; self.observations = observations
        self.channel = channel; self.stage = stage

    @property
    def status(self): return self.response.status

    @property
    def headers(self): return self.response.headers

    def __enter__(self):
        try:
            if self.response.__enter__() is not self.response: fail()
            return self
        except Exception as error:
            self.observations.failed(error, 'open', self.stage); raise

    def __exit__(self, *args):
        try:
            value = self.response.__exit__(*args)
            if value: fail()  # Never suppress an original read failure.
            return value
        except Exception as error:
            self.observations.failed(error, 'close', self.stage); raise

    def read(self, maximum):
        if type(maximum) is not int or maximum < 0: fail()
        try:
            value = self.response.read(maximum)
            if isinstance(value, bytes): self.observations.add(self.channel + '_read_bytes', len(value))
            return value
        except Exception as error:
            self.observations.add(self.channel + '_failed_read_bound', maximum)
            self.observations.failed(error, 'read', self.stage); raise


class ReadOnlyOpener:
    """Observe, never retry. The original client owns its original retries."""
    def __init__(self, opener, observations, channel):
        self.opener = opener; self.observations = observations; self.channel = channel

    def open(self, request, *, timeout):
        stage = endpoint(request, self.channel)
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 60: fail()
        self.observations.failure = None
        self.observations.last_stage = stage
        self.observations.add(self.channel + '_attempts', 1)
        try:
            response = self.opener.open(request, timeout=timeout)
            if type(response.status) is int and response.status != 200:
                self.observations.failure = {'error_class': 'http_status', 'http_status': response.status if 100 <= response.status <= 599 else None,
                                            'rate_limit_reset_epoch': None, 'phase': 'open', 'stage': stage}
            return ReadResponse(response, self.observations, self.channel, stage)
        except Exception as error:
            self.observations.failed(error, 'open', stage)
            if isinstance(error, urllib.error.HTTPError):
                # Preserve the same HTTPError object/type for the original
                # client's explicit redirect/status handling. Its context
                # manager calls close(), so observe both read and close too.
                original_read, original_close = error.read, error.close
                def read(maximum=-1):
                    if type(maximum) is not int or maximum < 0: fail()
                    try:
                        value = original_read(maximum)
                        if isinstance(value, bytes): self.observations.add(self.channel + '_read_bytes', len(value))
                        return value
                    except Exception as nested:
                        self.observations.add(self.channel + '_failed_read_bound', maximum)
                        self.observations.failed(nested, 'read', stage); raise
                def close():
                    try: return original_close()
                    except Exception as nested:
                        self.observations.failed(nested, 'close', stage); raise
                error.read, error.close = read, close
            raise


class ReadOnlyRemote:
    """Only methods used by bootstrap and PrivateRecords; no mutation surface."""
    def __init__(self, remote): self.__remote = remote
    def metadata(self): return self.__remote.metadata()
    def ref(self, name):
        if name != 'refs/heads/main': fail()
        return self.__remote.ref(name)
    def commit(self, revision): return self.__remote.commit(revision)
    def tree_entries(self, revision, recursive=False): return self.__remote.tree_entries(revision, recursive)
    def blob(self, revision, size): return self.__remote.blob(revision, size)


def load_runtime():
    runtime = Path(__file__).resolve().parents[1] / 'runtime'
    if runtime.is_symlink() or not (runtime / '.git').is_dir(): fail()
    for args, expected in ((('rev-parse', 'HEAD'), (RUNTIME_SHA + '\n').encode()),
                           (('status', '--porcelain', '--untracked-files=all'), b'')):
        result = subprocess.run(['git', '-C', str(runtime), *args], capture_output=True, timeout=30, check=False)
        if result.returncode != 0 or result.stdout != expected: fail()
    if hashlib.sha256(encoded(POLICY)).hexdigest() != POLICY_SHA or POLICY.get('code_sha') != RUNTIME_SHA: fail()
    sys.path.insert(0, str(runtime / 'scripts'))
    return runtime


def import_original(runtime, name):
    module = importlib.import_module(name)
    if Path(module.__file__).resolve().parent != runtime / 'scripts': fail()
    return module


def source_once(provenance, private_proof, remote, public_token, observations):
    if (provenance.MAX_REQUESTS != 40 or private_proof.MAX_COMMITS != 1024
            or private_proof.MAX_PRIVATE_BYTES != 256 * 1024 * 1024): fail()
    policy = provenance.validate_policy(POLICY, POLICY_SHA)
    client = provenance.PublicGithub(public_token)
    client.opener = ReadOnlyOpener(client.opener, observations, 'public')
    resolver = private_proof.PrivateRecords(ReadOnlyRemote(remote))
    client.private_proof_source = resolver
    # Capacity only, not an invented execution identity or three actual reads.
    client.requests = 3
    try:
        result = provenance.discover(client, policy, reserve_next_run=True)
        if result.get('status') != 'public_provenance_capacity_verified': fail()
    finally:
        observations.source_counts = {'public_budget_used': client.requests, 'reserved_execution_reads': 3,
                                      'private_nodes': len(resolver.nodes), 'private_proof_bytes': resolver.bytes}
        if any(type(v) is not int or not 0 <= v <= MAX_COUNTER for v in observations.source_counts.values()): fail()
    return dict(observations.source_counts)


def main(argv=None):
    observations = Observations(); success = False; counts = {}
    previous = os.environ.get('GET_PRIVATE_BINDINGS_FILE')
    try:
        if ((sys.argv[1:] if argv is None else argv)
                or os.environ.get('GITHUB_EVENT_NAME') != 'workflow_dispatch'
                or os.environ.get('GITHUB_REF') != REPAIR_REF
                or os.environ.get('GITHUB_REPOSITORY') != PUBLIC_REPOSITORY
                or os.environ.get('GET_SOURCE_PROOF_DIAGNOSTIC_ONLY') != 'true'
                or os.environ.get('GET_BINDINGS_DIAGNOSTIC_ONLY') == 'true'
                or os.environ.get('APPLY') != 'false' or os.environ.get('NOTION_MODE') != 'canonical'
                or os.environ.get('NOTION_INPUT_SHA256', '') != '' or os.environ.get('NOTION_API_KEY')
                or previous): fail()
        runtime = load_runtime()
        bindings = import_original(runtime, 'get_private_runtime_bindings')
        transport = import_original(runtime, 'get_private_preview_retention')
        token = os.environ.get('PRIVATE_VAULT_TOKEN'); public_token = os.environ.get('GITHUB_TOKEN')
        if not token or not public_token: fail()
        remote = transport.GitHubRemote(None, token)
        remote.opener = ReadOnlyOpener(remote.opener, observations, 'private')
        with tempfile.TemporaryDirectory(prefix='get-source-observe-') as folder:
            destination = Path(folder) / 'private' / 'bindings.json'
            bindings.bootstrap(ReadOnlyRemote(remote), destination)
            os.environ[bindings.ENV_FILE] = str(destination)
            provenance = import_original(runtime, 'get_publication_provenance')
            private_proof = import_original(runtime, 'get_private_proof')
            counts = source_once(provenance, private_proof, remote, public_token, observations)
        success = True
        diagnostic = {'error_class': 'none', 'http_status': None, 'rate_limit_reset_epoch': None,
                      'phase': 'validation', 'stage': 'source'}
    except Exception as error:
        diagnostic = observations.failure or {**classify(error), 'phase': 'validation', 'stage': observations.last_stage}
    finally:
        if previous is None: os.environ.pop('GET_PRIVATE_BINDINGS_FILE', None)
        else: os.environ['GET_PRIVATE_BINDINGS_FILE'] = previous
    try:
        payload = {'contract': CONTRACT, 'source_discovery_pass': success, 'production_migration_claimed': False,
                   **diagnostic, **observations.counters, **observations.source_counts, **counts}
        print(json.dumps(payload, sort_keys=True), file=sys.stderr, flush=True)
    except Exception:
        return 1
    return 0 if success else 1


if __name__ == '__main__':
    raise SystemExit(main())
