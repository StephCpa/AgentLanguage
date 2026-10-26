"""Trusted Git publisher: one immutable operation ref, explicit reconciliation.

The host owns the repository, destination, credentials and ref namespace.
Plans supply only a full commit object ID. There are no automatic retries.
The budget unit counts dispatch rights, not GitHub billing or network traffic.
"""
from pathlib import Path
import os
import re
import subprocess

from .data import Intent, digest
from .errors import ContractError, EvidenceMismatch, InvalidData
from .execute import Response
from .flow import Label
from .ir import Catalog, Tool


PUBLISH = 'git.publish'
QUERY = 'git.query'
UNITS = {'git_dispatches': 1}
OID = re.compile(r'(?:[0-9a-f]{40}|[0-9a-f]{64})\Z')
OID_BYTES = re.compile(rb'(?:[0-9a-f]{40}|[0-9a-f]{64})\Z')


def valid_ref_bytes(ref):
    """Git refname restrictions are byte rules, not Unicode whitespace rules."""
    return (ref.startswith(b'refs/') and not ref.endswith((b'/', b'.'))
            and not any(c <= 32 or c == 127 or c in b'~^:?*[\\' for c in ref)
            and b'..' not in ref and b'@{' not in ref
            and all(part and not part.startswith(b'.') and not part.endswith(b'.lock') for part in ref.split(b'/')))


def parse_remote_refs(stdout):
    if not isinstance(stdout, bytes) or stdout and not stdout.endswith(b'\n'):
        raise EvidenceMismatch('Git reference query returned invalid framing')
    rows = [line.split(b'\t') for line in stdout.split(b'\n')[:-1]]
    if (any(len(row) != 2 or not OID_BYTES.fullmatch(row[0]) or set(row[0]) == {48}
            or not valid_ref_bytes(row[1]) for row in rows)
            or len({row[1] for row in rows}) != len(rows)):
        raise EvidenceMismatch('Git reference query returned malformed or duplicate records')
    return rows


class GitPublisher:
    """Publish pre-existing commits using an explicit empty ref lease.

    `remote` must be a concrete URL or absolute local path, never a mutable
    named remote. The registered namespace must be exclusively owned and kept
    immutable by the host; Git administrators remain part of the trusted base.
    """

    def __init__(self, repository, remote, *, recipients, commits, prefix='refs/heads/al0/publish/',
                 timeout=30, git='git', environment=None):
        self.repository = Path(repository).absolute()
        if not self.repository.is_dir():
            raise InvalidData('Git object repository must exist')
        if (type(remote) is not str or not remote or remote.startswith('-')
                or any(ord(c) < 32 for c in remote)
                or not (remote.startswith(('https://', 'ssh://')) or Path(remote).is_absolute())):
            raise InvalidData('use a concrete HTTPS/SSH URL or absolute local Git path')
        if (type(prefix) is not str or not prefix.startswith('refs/heads/')
                or not prefix.endswith('/') or len(prefix) > 150):
            raise InvalidData('publish prefix must be a bounded branch namespace')
        if type(timeout) not in (int, float) or not 0 < timeout <= 300:
            raise InvalidData('Git timeout must be positive and at most 300 seconds')
        self.remote, self.prefix, self.timeout, self.git = remote, prefix, timeout, git
        if (not isinstance(commits, (list, tuple, set, frozenset)) or not commits
                or any(type(c) is not str or not OID.fullmatch(c) or set(c) == {'0'} for c in commits)):
            raise InvalidData('host must explicitly approve full commit IDs for publication')
        # Approval covers all reachable objects/history, not merely the OID text.
        self.commits = frozenset(commits)
        self.environment = dict(os.environ if environment is None else environment)
        for key in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_COMMON_DIR', 'GIT_INDEX_FILE',
                    'GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES'):
            self.environment.pop(key, None)
        self.environment.update(GIT_TERMINAL_PROMPT='0', GCM_INTERACTIVE='never', LC_ALL='C')
        if self._command(['check-ref-format', prefix + 'probe']).returncode:
            raise InvalidData('invalid Git ref namespace')
        self.version = '1-' + digest({'remote': remote, 'prefix': prefix, 'protocol': 1,
                                     'commits': sorted(self.commits)})
        self.scope = 'git:' + digest({'remote': remote, 'prefix': prefix})
        self.catalog = Catalog()
        self.catalog.schema('GitPublishRequest', {'commit': 'Text'})
        # Both source declarations use one label so reconciliation does not add
        # a stronger source than the target request can ever acknowledge.
        output = Label(taints={'external:git'})
        for name, kind, source in ((PUBLISH, 'action', 'GitPublishRequest'), (QUERY, 'reconcile', 'Json')):
            self.catalog.register(Tool.define(name, kind, source, 'Json', self.scope, UNITS,
                version=self.version, recipients=recipients, output_label=output,
                reconciles=PUBLISH if name == QUERY else '', retry_window=0))

    def _command(self, arguments, *, raw=False):
        try:
            return subprocess.run([self.git, '-C', str(self.repository), *arguments],
                env=self.environment, capture_output=True,
                **({} if raw else {'text': True, 'encoding': 'utf-8', 'errors': 'replace'}),
                timeout=self.timeout, check=False)
        except (OSError, subprocess.TimeoutExpired):
            # Diagnostics can contain credential-bearing URLs. Do not export
            # stderr or the subprocess arguments into language-level results.
            raise ConnectionError('Git command did not produce a confirmed response') from None

    def _intent(self, intent):
        value = intent.value
        if (value['tool'], value['version'], value['scope']) != (PUBLISH, self.version, self.scope):
            raise EvidenceMismatch('Git publisher contract does not match the intent')
        request = value['args']
        if (type(request) is not dict or set(request) != {'commit'}
                or type(request['commit']) is not str or not OID.fullmatch(request['commit'])
                or set(request['commit']) == {'0'}):
            raise InvalidData('Git publication requires a full nonzero commit object ID')
        if request['commit'] not in self.commits:
            raise EvidenceMismatch('commit is outside the host publication approval')
        return request['commit']

    def reference(self, operation, intent_digest):
        # Raw operation names never become ref syntax or command arguments.
        return self.prefix + digest([operation, intent_digest])

    def publish(self, ticket):
        commit = self._intent(ticket.intent)
        ref = self.reference(ticket.operation, ticket.intent.digest)
        kind = self._command(['cat-file', '-t', commit])
        if kind.returncode or kind.stdout.strip() != 'commit':
            # A local configuration/input error is not remote business evidence.
            raise ContractError('requested commit object is unavailable locally')
        result = self._command(['-c', 'push.followTags=false', 'push', '--porcelain',
            '--no-verify', '--no-follow-tags', '--recurse-submodules=no',
            '--force-with-lease=' + ref + ':', '--', self.remote, commit + ':' + ref])
        rows = [line.split('\t') for line in result.stdout.splitlines()]
        confirmed = [row for row in rows if len(row) == 3 and row[1].endswith(':' + ref)]
        if result.returncode or len(confirmed) != 1 or confirmed[0][0] not in ('*', '='):
            raise ConnectionError('Git publication has no confirmed success receipt')
        return Response({'ref': ref, 'commit': commit}, dict(UNITS))

    def query(self, ticket, resolve_operation):
        """Resolve target metadata from the trusted Store, not the query body.

        `resolve_operation(id)` returns operation_info plus its snapshot attempts.
        Absence and transport failure never prove that a delayed push cannot apply.
        """
        descriptor = ticket.intent.value
        if (descriptor['tool'], descriptor['version'], descriptor['scope']) != (QUERY, self.version, self.scope):
            raise EvidenceMismatch('Git query contract does not match the intent')
        request = descriptor['args']
        if type(request) is not dict or set(request) != {'operation', 'intent_digest'}:
            raise InvalidData('invalid Git reconciliation request')
        info = resolve_operation(request['operation'])
        record = info['intent']
        target = Intent(record['tool'], record['version'], record['scope'], record['args'],
                        record['receipts'], label=Label.from_data(record.get('flow')))
        commit = self._intent(target)
        if target.digest != request['intent_digest']:
            raise EvidenceMismatch('Git query changed its target intent')
        ref = self.reference(request['operation'], target.digest)
        result = self._command(['ls-remote', '--refs', '--exit-code', '--', self.remote, ref], raw=True)
        if result.returncode not in (0, 2):
            raise ConnectionError('Git reference query has no confirmed response')
        rows = parse_remote_refs(result.stdout)
        if result.returncode == 2 and rows:
            raise EvidenceMismatch('Git query exit status contradicts its records')
        # ls-remote arguments are tail patterns, even for a full ref name.
        # A refs/original/<ref> backup is not evidence about the exact target.
        exact = [row for row in rows if row[1] == ref.encode('utf-8')]
        if not exact:
            business = None
        elif exact == [[commit.encode('ascii'), ref.encode('utf-8')]]:
            business = {'evidence_id': 'git-ref:' + digest([ref, commit]), 'outcome': 'Applied',
                        'details': {'output': {'ref': ref, 'commit': commit}}}
        else:
            raise EvidenceMismatch('Git operation reference violates its immutable binding')
        # This is a deterministic host quota: one unit per durable dispatch
        # right, including crash/timeout. It is not a remote billing observation.
        bills = [{'attempt': a['id'], 'evidence_id': 'git-dispatch:' + a['id'], 'actual': dict(UNITS)}
                 for a in info['attempts'] if a['phase'] == 'DispatchStarted']
        return Response({'operation': request['operation'], 'intent_digest': target.digest,
                         'business': business, 'bills': bills}, dict(UNITS))

    def callbacks(self, store, principal):
        def resolve(operation):
            info = store.operation_info(operation, principal)
            return {**info, 'attempts': store.snapshot(operation, principal)['attempts']}
        return {PUBLISH: self.publish, QUERY: lambda ticket: self.query(ticket, resolve)}
